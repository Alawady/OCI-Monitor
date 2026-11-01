"""HTTP API, static UI, token and host protection."""

import pytest
from fastapi.testclient import TestClient

from ocimon.app import create_app
from ocimon.demo import DemoSource
from ocimon.refresher import Refresher
from ocimon.settings import Settings, normalize_base_path, parse_args

LOCAL = "http://127.0.0.1:8080"


def make_client(refresh=True, **settings):
    refresher = Refresher(DemoSource(delay=0), 120, mode="demo")
    if refresh:
        refresher.refresh_once()
    app = create_app(Settings(**settings), refresher, {"kind": "demo"})
    return TestClient(app, base_url=LOCAL), refresher


@pytest.fixture(scope="module")
def client():
    c, _ = make_client()
    return c


def test_summary(client):
    d = client.get("/api/summary").json()
    assert d["totals"]["problems"] == 5
    assert {c["label"] for c in d["cards"]} >= {"Instances", "Databases", "Load Balancers", "Volumes", "OKE"}
    assert d["last_refresh"]


def test_resources_filters(client):
    all_items = client.get("/api/resources").json()
    assert all_items["count"] == len(all_items["items"]) > 50
    item = all_items["items"][0]
    for key in ("id", "name", "type", "state", "state_class", "status", "problem", "compartment_path",
                "console_url", "details", "related", "region"):
        assert key in item

    d = client.get("/api/resources", params={"type": "load_balancer", "problems": "true"}).json()
    assert {i["name"] for i in d["items"]} == {"prod-public-lb", "stg-lb"}
    d = client.get("/api/resources", params={"region": "us-phoenix-1", "state": "RUNNING"}).json()
    assert d["count"] and all(i["region"] == "us-phoenix-1" and i["state"] == "RUNNING" for i in d["items"])
    d = client.get("/api/resources", params={"q": "orders-mysql"}).json()
    assert {i["name"] for i in d["items"]} == {"orders-mysql", "orders-mysql-replica"}


def test_resources_related_resolved(client):
    d = client.get("/api/resources", params={"type": "boot_volume", "q": "prod-web-01"}).json()
    (bv,) = d["items"]
    assert bv["related"][0]["rel"] == "instance" and bv["related"][0]["name"] == "prod-web-01"


def test_compartments_tree(client):
    d = client.get("/api/compartments").json()
    tree = d["tree"]
    assert tree["id"] == d["root_id"] and tree["total_problems"] == 5
    names = {c["name"] for c in tree["children"]}
    assert {"Production", "Staging", "Development", "SharedServices", "Security"} <= names
    # Counts follow the region filter
    ruh = client.get("/api/compartments", params={"region": "me-riyadh-1"}).json()["tree"]
    assert ruh["total"] < tree["total"]


def test_health(client):
    d = client.get("/api/health").json()
    assert d["status"] == "degraded" and d["mode"] == "demo"
    assert d["refresh_interval"] == 120 and d["resource_count"] > 50
    assert len(d["errors"]) == 4
    e = next(e for e in d["errors"] if e["operation"] == "ListInstances")
    assert e["compartment_name"] == "Security" and e["kind"] == "permission"


def test_health_before_first_refresh():
    c, _ = make_client(refresh=False)
    d = c.get("/api/health").json()
    assert d["status"] == "loading" and d["last_refresh"] is None
    assert c.get("/api/resources").json()["items"] == []


def test_refresh_requires_custom_header():
    c, refresher = make_client()
    assert c.post("/api/refresh").status_code == 403
    r = c.post("/api/refresh", headers={"X-OCIMON": "1"})
    assert r.status_code == 202 and r.json()["accepted"] is True


def test_static_ui_and_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "OCI Monitor" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/api/health").headers["cache-control"] == "no-store"
    # The page references only local assets
    assert "http" not in r.text.replace("http-equiv", "")


def test_rejects_foreign_host_without_token(client):
    assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "localhost:8080"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "[::1]:8080"}).status_code == 200


def test_token_required():
    c, _ = make_client(host="0.0.0.0", token="s3cret")
    assert c.get("/api/health").status_code == 401
    assert c.get("/").status_code == 401
    assert c.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/health", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    # Token in the URL becomes an HttpOnly cookie and is stripped from the URL
    r = c.get("/?token=s3cret", follow_redirects=False)
    assert r.status_code == 303 and "token" not in r.headers["location"]
    assert "httponly" in r.headers["set-cookie"].lower()
    assert c.get("/api/health").status_code == 200  # cookie now set on the client
    # With a token, any Host is fine (reverse proxies)
    assert c.get("/api/health", headers={"Host": "dash.example.com"}).status_code == 200


def test_base_path():
    c, _ = make_client(base_path="/ocimon")
    r = c.get("/ocimon", follow_redirects=False)
    assert r.status_code in (301, 302, 307) and r.headers["location"].endswith("/ocimon/")
    assert c.get("/ocimon/").status_code == 200
    assert c.get("/ocimon/api/summary").status_code == 200
    assert c.get("/ocimon/static/app.js").status_code == 200
    assert c.get("/api/summary").status_code == 404


def test_cli_parsing(monkeypatch):
    monkeypatch.setenv("OCIMON_TOKEN", "envtok")
    monkeypatch.setenv("OCI_CLI_PROFILE", "WORK")
    s = parse_args(["--regions", "all", "--base-path", "dash/", "--refresh", "3", "--no-browser"])
    assert s.regions == "all" and s.base_path == "/dash" and s.refresh == 10
    assert s.token == "envtok" and s.profile == "WORK" and s.open_browser is False and s.is_local
    assert normalize_base_path("/") == "" and normalize_base_path("/a/b/") == "/a/b"
    assert not parse_args(["--host", "0.0.0.0"]).is_local
