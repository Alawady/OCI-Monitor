from ocimon.collectors import resource_types
from ocimon.console import console_url
from ocimon.models import Resource, normalize_state, state_class


def res(state="RUNNING", health=None, **kw):
    return Resource(id="ocid1.instance.oc1.me-jeddah-1.abc", name="n", type="instance",
                    region="me-jeddah-1", compartment_id="c", state=state, health=health, **kw)


def test_normalize_state_handles_search_casing():
    assert normalize_state("Active") == "ACTIVE"
    assert normalize_state("active") == "ACTIVE"
    assert normalize_state(None) == "UNKNOWN"
    assert normalize_state("Needs Attention") == "NEEDS_ATTENTION"


def test_state_classes_match_color_rules():
    for s in ("RUNNING", "AVAILABLE", "ACTIVE", "OK"):
        assert state_class(s) == "ok"
    for s in ("STOPPED", "INACTIVE"):
        assert state_class(s) == "stopped"
    for s in ("PROVISIONING", "STARTING", "STOPPING", "UPDATING", "WARNING", "SOMETHING_PENDING_ING"):
        assert state_class(s) == "warn"
    for s in ("FAILED", "TERMINATED", "CRITICAL"):
        assert state_class(s) == "bad"
    assert state_class("WEIRD") == "unknown"


def test_problem_rules():
    assert res("RUNNING").problem is None
    assert res("STOPPED").problem is None
    assert res("PROVISIONING").problem is None
    assert res("FAILED").problem == "critical"
    assert res("TERMINATED").problem is None  # intentional, not a problem
    assert res("ACTIVE", health="CRITICAL").problem == "critical"
    assert res("ACTIVE", health="WARNING").problem == "warning"
    assert res("AVAILABLE_NEEDS_ATTENTION").problem == "warning"
    assert res("ACTIVE", health="UNKNOWN").problem is None


def test_status_is_worst_of_state_and_health():
    assert res("ACTIVE", health="OK").status == "ok"
    assert res("ACTIVE", health="WARNING").status == "warn"
    assert res("ACTIVE", health="CRITICAL").status == "bad"
    assert res("STOPPED").status == "stopped"


def test_console_urls():
    types = resource_types()
    url = console_url(res(), types["instance"])
    assert url == ("https://cloud.oracle.com/compute/instances/ocid1.instance.oc1.me-jeddah-1.abc"
                   "?region=me-jeddah-1")
    subnet = Resource(id="ocid1.subnet.oc1.x.s", name="s", type="subnet", region="r", compartment_id="c",
                      refs={"vcn_id": "ocid1.vcn.oc1.x.v"})
    assert "networking/vcns/ocid1.vcn.oc1.x.v/subnets/ocid1.subnet.oc1.x.s" in console_url(subnet, types["subnet"])
    # A template ref that's missing -> no link rather than a broken one.
    orphan = Resource(id="ocid1.subnet.oc1.x.s", name="s", type="subnet", region="r", compartment_id="c")
    assert console_url(orphan, types["subnet"]) is None
    # Non-commercial realm -> no guess.
    gov = Resource(id="ocid1.instance.oc2.us-langley-1.abc", name="g", type="instance", region="us-langley-1",
                   compartment_id="c")
    assert console_url(gov, types["instance"]) is None
