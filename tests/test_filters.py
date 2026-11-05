"""Filtering, summaries and tree counts on the demo snapshot."""

import pytest

from ocimon.demo import build_demo
from ocimon.filters import ResourceQuery, compartment_tree, filter_resources, summarize


@pytest.fixture(scope="module")
def snap():
    return build_demo(tick=1)


def comp_id(snap, name):
    return next(c.id for c in snap.compartments.values() if c.name == name)


def q(**kw):
    return ResourceQuery.from_params(**kw)


def test_terminated_hidden_by_default(snap):
    names = {r.name for r in filter_resources(snap, q())}
    assert "old-load-test" not in names
    assert "old-load-test" in {r.name for r in filter_resources(snap, q(include_terminated=True))}
    # Explicitly asking for TERMINATED shows them even without the toggle
    assert {r.name for r in filter_resources(snap, q(state="TERMINATED"))} == {"old-load-test"}


def test_compartment_includes_children(snap):
    prod = comp_id(snap, "Production")
    prod_app = comp_id(snap, "prod-app")
    with_children = filter_resources(snap, q(compartment=prod))
    only_here = filter_resources(snap, q(compartment=prod, subtree=False))
    assert {r.compartment_id for r in with_children} >= {prod, prod_app, comp_id(snap, "prod-data")}
    assert {r.compartment_id for r in only_here} == {prod}
    assert all(r.compartment_id == prod_app for r in filter_resources(snap, q(compartment=prod_app)))


def test_type_state_region_filters(snap):
    inst = filter_resources(snap, q(type="instance"))
    assert inst and all(r.type == "instance" for r in inst)
    both = filter_resources(snap, q(type="instance,volume"))
    assert {r.type for r in both} == {"instance", "volume"}
    stopped = filter_resources(snap, q(state="stopped"))  # case-insensitive
    assert stopped and all(r.state == "STOPPED" for r in stopped)
    ruh = filter_resources(snap, q(region="me-riyadh-1"))
    assert ruh and all(r.region == "me-riyadh-1" for r in ruh)
    # Health values work as a state filter too
    assert {r.name for r in filter_resources(snap, q(state="CRITICAL"))} == {"stg-lb"}
    assert all(r.type == "volume" or r.type == "boot_volume"
               for r in filter_resources(snap, q(category="storage")))


def test_search_matches_name_ip_and_compartment(snap):
    assert {r.name for r in filter_resources(snap, q(q="prod-web"))} == {
        "prod-web-01", "prod-web-02", "prod-web-03",
        "prod-web-01 (Boot Volume)", "prod-web-02 (Boot Volume)", "prod-web-03 (Boot Volume)"}
    web01 = next(r for r in snap.resources if r.name == "prod-web-01")
    ip = web01.details["private_ips"][0]
    assert web01 in filter_resources(snap, q(q=ip))
    assert web01 in filter_resources(snap, q(q=web01.id))
    # Multiple tokens must all match; compartment path is searchable
    assert {r.name for r in filter_resources(snap, q(q="sandbox bucket"))} == {"sandbox-tmp"}


def test_problems_only(snap):
    problems = filter_resources(snap, q(problems=True))
    assert {r.name for r in problems} == {"prod-public-lb", "stg-lb", "prodatp", "orders-mysql-replica", "dev-oke",
                                          "prod-app-02", "orders-mysql",  # memory / storage metrics
                                          "orders-replication", "prod-switchover"}  # broken replication, stale DR plan


def test_summary_counts(snap):
    s = summarize(filter_resources(snap, q()))
    assert s["totals"]["problems"] == 9
    assert s["totals"]["critical"] == 3 and s["totals"]["warning"] == 6
    cards = {c["category"]: c for c in s["cards"]}
    inst = s["types"]["instance"]
    assert cards["compute"]["total"] == inst["total"]
    assert inst["by_state"]["RUNNING"] == inst["by_status"]["ok"] + 1  # prod-app-02 runs but has a memory warning
    assert sum(cards["compute"]["by_status"].values()) == cards["compute"]["total"]
    assert cards["load_balancing"]["problems"] == 2


def test_tree_counts_roll_up(snap):
    tree = compartment_tree(snap, filter_resources(snap, q()))
    assert tree["name"] == "acme-corp"
    assert tree["total"] == len(filter_resources(snap, q()))
    prod = next(c for c in tree["children"] if c["name"] == "Production")
    assert prod["total"] == sum(ch["total"] for ch in prod["children"]) + prod["count"]
    # LB warning, ATP attention, failed MySQL replica, prod-app-02 memory, orders-mysql storage,
    # broken replication channel, DR plan needing refresh
    assert prod["total_problems"] == 7
    staging = next(c for c in tree["children"] if c["name"] == "Staging")
    assert staging["problems"] == 1
