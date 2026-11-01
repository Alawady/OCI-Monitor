"""Deep links into the OCI Console (commercial realm only)."""

from __future__ import annotations

from urllib.parse import quote

from .models import Resource, ResourceType

CONSOLE_BASE = "https://cloud.oracle.com"


class _Missing(dict):
    def __missing__(self, key: str) -> str:
        raise KeyError(key)


def console_url(resource: Resource, rtype: ResourceType | None) -> str | None:
    if rtype is None or not rtype.console:
        return None
    # Other realms (gov, sovereign) use different console hosts; don't guess.
    if resource.id.startswith("ocid1.") and ".oc1." not in resource.id:
        return None
    values = _Missing({k: quote(str(v), safe="") for k, v in resource.refs.items() if v})
    values["id"] = quote(resource.id, safe="")
    values["region"] = quote(resource.region, safe="")
    try:
        path = rtype.console.format_map(values)
    except KeyError:
        return None
    return f"{CONSOLE_BASE}/{path}?region={values['region']}"
