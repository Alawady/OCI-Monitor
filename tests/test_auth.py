"""Loading credentials from an OCI CLI style config without leaking secrets."""

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ocimon.auth import AuthError, load_auth

FINGERPRINT = "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"


@pytest.fixture
def key_file(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "key.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return path


def write_config(tmp_path, body):
    cfg = tmp_path / "config"
    cfg.write_text(body)
    return str(cfg)


def jwt(exp):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


def test_api_key_profile(tmp_path, key_file):
    cfg = write_config(tmp_path, f"""[DEFAULT]
user=ocid1.user.oc1..u
fingerprint={FINGERPRINT}
tenancy=ocid1.tenancy.oc1..t
region=me-jeddah-1
key_file={key_file}

[OTHER]
region=us-phoenix-1
""")
    auth = load_auth(cfg, None)
    assert auth.kind == "api_key" and auth.tenancy_id == "ocid1.tenancy.oc1..t" and auth.region == "me-jeddah-1"
    other = load_auth(cfg, "OTHER")  # inherits DEFAULT keys, like the CLI
    assert other.region == "us-phoenix-1" and other.profile == "OTHER"
    described = json.dumps(auth.describe())
    assert FINGERPRINT not in described and str(key_file) not in described


def test_session_token_profile(tmp_path, key_file):
    token = tmp_path / "token"
    token.write_text(jwt(time.time() + 3600))
    cfg = write_config(tmp_path, f"""[SESSION]
tenancy=ocid1.tenancy.oc1..t
region=me-riyadh-1
key_file={key_file}
security_token_file={token}
""")
    auth = load_auth(cfg, "SESSION")
    assert auth.kind == "security_token" and not auth.token_expired()
    assert auth.token_expires_at and auth.token_expires_at > time.time()

    # Refreshed token on disk is picked up on the next reload()
    token.write_text(jwt(time.time() - 10))
    import os
    os.utime(token, (time.time() + 5, time.time() + 5))
    auth.reload()
    assert auth.token_expired()


def test_errors_are_friendly_and_secret_free(tmp_path, key_file):
    with pytest.raises(AuthError, match="not found"):
        load_auth(str(tmp_path / "missing"), None)
    cfg = write_config(tmp_path, f"[DEFAULT]\ntenancy=ocid1.tenancy.oc1..t\nregion=r\nfingerprint={FINGERPRINT}\n")
    with pytest.raises(AuthError) as err:
        load_auth(cfg, "NOPE")
    assert "NOPE" in str(err.value)
    with pytest.raises(AuthError) as err:
        load_auth(cfg, None)  # no user / key_file
    assert FINGERPRINT not in str(err.value)
    with pytest.raises(AuthError, match="security_token_file"):
        load_auth(cfg, None, "security_token")
