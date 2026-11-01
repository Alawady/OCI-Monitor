"""Build OCI SDK credentials from the same config the OCI CLI uses.

Nothing in this module logs or returns key material, fingerprints or tokens;
`describe()` exposes only non-secret facts for /api/health.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import oci

AUTH_KINDS = ("auto", "api_key", "security_token", "instance_principal")


class AuthError(Exception):
    """A configuration problem the user can fix; message is safe to print."""


def _token_expiry(token: str) -> float | None:
    """Read the `exp` claim of a session token (JWT) without verifying it."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


@dataclass
class Auth:
    kind: str
    config: dict[str, Any]
    signer: Any
    tenancy_id: str
    region: str
    profile: str | None = None
    config_file: str | None = None
    token_expires_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _token_mtime: float | None = field(default=None, repr=False)

    def make_client(self, client_cls: type, region: str, **kwargs: Any) -> Any:
        config = dict(self.config, region=region)
        return client_cls(config, signer=self.signer, **kwargs)

    def reload(self) -> None:
        """Pick up a refreshed session token (`oci session refresh`) between runs."""
        if self.kind != "security_token":
            return
        token_file = os.path.expanduser(self.config["security_token_file"])
        try:
            mtime = os.path.getmtime(token_file)
        except OSError:
            return
        with self._lock:
            if mtime == self._token_mtime:
                return
            self.signer, self.token_expires_at = _security_token_signer(self.config)
            self._token_mtime = mtime

    def token_expired(self) -> bool:
        return self.token_expires_at is not None and self.token_expires_at < time.time()

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "profile": self.profile,
            "region": self.region,
            "token_expires_at": self.token_expires_at,
            "token_expired": self.token_expired(),
        }


def _security_token_signer(config: dict[str, Any]) -> tuple[Any, float | None]:
    token_file = os.path.expanduser(config["security_token_file"])
    try:
        with open(token_file, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as exc:
        raise AuthError(f"Cannot read security_token_file {token_file}: {exc.strerror}") from None
    try:
        private_key = oci.signer.load_private_key_from_file(
            os.path.expanduser(config["key_file"]), config.get("pass_phrase")
        )
    except Exception as exc:  # message never includes key content
        raise AuthError(f"Cannot load the profile's key_file: {type(exc).__name__}") from None
    return oci.auth.signers.SecurityTokenSigner(token, private_key), _token_expiry(token)


def load_auth(config_file: str | None, profile: str | None, kind: str = "auto") -> Auth:
    if kind not in AUTH_KINDS:
        raise AuthError(f"Unknown auth type {kind!r}; choose one of {', '.join(AUTH_KINDS)}")

    if kind == "instance_principal":
        try:
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        except Exception as exc:
            raise AuthError(f"Instance principal auth failed (is this an OCI instance?): {exc}") from None
        config = {"region": signer.region, "tenancy": signer.tenancy_id}
        return Auth("instance_principal", config, signer, signer.tenancy_id, signer.region)

    config_file = os.path.expanduser(config_file or oci.config.DEFAULT_LOCATION)
    profile = profile or oci.config.DEFAULT_PROFILE
    try:
        config = oci.config.from_file(config_file, profile)
    except oci.exceptions.ConfigFileNotFound:
        raise AuthError(f"OCI config file not found: {config_file} (run `oci setup config`)") from None
    except oci.exceptions.ProfileNotFound:
        raise AuthError(f"Profile {profile!r} not found in {config_file}") from None
    except Exception as exc:
        raise AuthError(f"Cannot parse {config_file}: {type(exc).__name__}") from None

    for key in ("tenancy", "region"):
        if not config.get(key):
            raise AuthError(f"Profile {profile!r} has no {key!r} entry")

    if kind == "auto":
        kind = "security_token" if config.get("security_token_file") else "api_key"

    if kind == "security_token":
        if not config.get("security_token_file"):
            raise AuthError(f"Profile {profile!r} has no security_token_file (run `oci session authenticate`)")
        signer, expires = _security_token_signer(config)
        auth = Auth(kind, config, signer, config["tenancy"], config["region"], profile, config_file, expires)
        auth._token_mtime = os.path.getmtime(os.path.expanduser(config["security_token_file"]))
        return auth

    try:
        oci.config.validate_config(config)
    except oci.exceptions.InvalidConfig as exc:
        # InvalidConfig carries a dict of field -> problem; field names only.
        fields = ", ".join(sorted(exc.args[0])) if exc.args and isinstance(exc.args[0], dict) else "config"
        raise AuthError(f"Profile {profile!r} is invalid ({fields})") from None
    key_file = config.get("key_file")
    if key_file and not os.path.exists(os.path.expanduser(key_file)):
        raise AuthError(f"Profile {profile!r}: key_file does not exist")
    signer = oci.signer.Signer(
        tenancy=config["tenancy"],
        user=config["user"],
        fingerprint=config["fingerprint"],
        private_key_file_location=key_file,
        pass_phrase=config.get("pass_phrase"),
        private_key_content=config.get("key_content"),
    )
    return Auth("api_key", config, signer, config["tenancy"], config["region"], profile, config_file)
