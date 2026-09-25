"""ADR 0116 decision 6 + verification 2 — a typed resource graph feeds admission.

Verification 2: *a job with ``scale_up_domain="required"`` and insufficient free capacity in any
single domain is QUEUEd, not placed spanning.* Before the graph, ``largest_free_domain_gpus`` was
never set from live state, so such a job was always *rejected* as "topology unknown" and the
verification could only be exercised through a hand-written what-if. These tests drive it from a
real ``hpc_nodes`` snapshot plus a declared topology file.
"""

from __future__ import annotations

import json

import pytest

from examlops.admission_seam import JobRequest, Resources, topology
from examlops.admission_seam.policy import Admit, Queue, Quotas, Reject
from examlops.admission_seam.service import current_state, decide


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv(topology.TOPOLOGY_ENV, raising=False)
    monkeypatch.delenv(topology.MAX_AGE_ENV, raising=False)
    monkeypatch.delenv("EXAMLOPS_ADMISSION_GATES", raising=False)
    import examlops.platform_db as pdb

    pdb.init_db()
    return pdb


def _inventory():
    from examlops.data.hpc import record_node_snapshot

    record_node_snapshot(
        "c1",
        "slurm",
        [
            {"name": "a1", "gpus": 4, "state": "idle"},
            {"name": "a2", "gpus": 4, "state": "allocated"},
            {"name": "b1", "gpus": 4, "state": "idle"},
            {"name": "b2", "gpus": 4, "state": "idle"},
            {"name": "c1n", "gpus": 8, "state": "mixed"},
        ],
    )


def _topology(tmp_path, monkeypatch, doc, name="topology.json"):
    path = tmp_path / name
    path.write_text(json.dumps(doc) if name.endswith(".json") else doc)
    monkeypatch.setenv(topology.TOPOLOGY_ENV, str(path))
    return path


DOC = {
    "scale_up_domains": {
        "nvl-a": {"nodes": ["a1", "a2"]},
        "nvl-b": {"nodes": ["b1", "b2"]},
        "nvl-c": {"nodes": ["c1n"]},
    },
    "power": {"pdu-1": {"cap_kw": 40, "nodes": ["a1", "a2", "b1"]}},
    "fabrics": {"ib0": {"kind": "infiniband", "nodes": ["a1", "b1"]}},
    "storage": {"lustre": {"nodes": ["a1"]}},
}


def _required(gpus):
    return JobRequest(
        project="p",
        resources=Resources(gpus=gpus),
        network_tier="scale_up",
        scale_up_domain="required",
    )


def test_graph_counts_free_gpus_per_domain_conservatively(db, tmp_path, monkeypatch):
    _inventory()
    _topology(tmp_path, monkeypatch, DOC)
    g = topology.current_graph()
    assert g.domain_free_gpus() == {
        "scale_up_domain:nvl-a": 4,  # a2 allocated
        "scale_up_domain:nvl-b": 8,
        "scale_up_domain:nvl-c": 0,  # mixed: how much is free is unknown, so none is promised
    }
    assert g.largest_free_domain_gpus() == 8
    s = g.summary()
    assert s["vertices"]["accelerator"] == 24 and s["vertices"]["power"] == 1
    assert s["edges"] == {"connects": 3, "contains": 5 + 24 + 5 + 3}
    assert s["problems"] == []


def test_verification_2_required_domain_without_room_is_queued_not_spanned(
    db, tmp_path, monkeypatch
):
    _inventory()
    _topology(tmp_path, monkeypatch, DOC)
    st = current_state()
    assert st.largest_free_domain_gpus == 8
    q = Quotas(max_running=10, per_tenant_cap=10)
    # 12 GPUs are free cluster-wide (4 + 8) but no single domain has 10.
    decision, _ = decide(_required(10), state=st, quotas=q, policy="baseline-over-quota")
    assert isinstance(decision, Queue) and "not placed spanning" in decision.reason
    decision, _ = decide(_required(8), state=st, quotas=q, policy="baseline-over-quota")
    assert isinstance(decision, Admit)


def test_no_declared_topology_stays_unknown_and_never_promises(db):
    _inventory()
    st = current_state()
    assert st.largest_free_domain_gpus is None
    decision, _ = decide(
        _required(1), state=st, quotas=Quotas(10, 10), policy="baseline-over-quota"
    )
    assert isinstance(decision, Reject) and "topology is unknown" in decision.reason


def test_yaml_topology_in_the_config_dir_is_found(db, tmp_path, monkeypatch):
    _inventory()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "topology.yaml").write_text("scale_up_domains:\n  d:\n    nodes: [b1, b2]\n")
    assert topology.topology_path() == cfg / "topology.yaml"
    assert topology.live_largest_free_domain_gpus() == 8


def test_a_broken_topology_file_degrades_to_unknown_not_to_fits(db, tmp_path, monkeypatch, caplog):
    _inventory()
    _topology(tmp_path, monkeypatch, "scale_up_domains: [not, a, mapping]\n", "t.yaml")
    assert topology.live_largest_free_domain_gpus() is None
    assert "treated as unknown" in caplog.text


def test_a_missing_named_topology_file_is_unknown(db, tmp_path, monkeypatch):
    monkeypatch.setenv(topology.TOPOLOGY_ENV, str(tmp_path / "gone.yaml"))
    assert topology.live_largest_free_domain_gpus() is None
    with pytest.raises(OSError):
        topology.load_topology()


def test_declared_facts_that_do_not_fit_are_reported(db):
    inv = [
        {"cluster": "x", "node": "n1", "gpus": 2, "state": "idle"},
        {"cluster": "y", "node": "n1", "gpus": 2, "state": "idle"},
        {"cluster": "x", "node": "n2", "gpus": 2, "state": "idle"},
    ]
    doc = {
        "scale_up_domains": {
            "d1": {"nodes": ["n1", "x/n2", "ghost"]},
            "d2": {"nodes": ["x/n2"]},
        }
    }
    g = topology.build(inv, doc)
    joined = " | ".join(g.problems)
    assert "'n1' is ambiguous" in joined
    assert "'ghost' is not in the inventory" in joined
    assert "two scale-up domains" in joined
    # x/n2 stays in d1 only, so d2 is empty
    assert g.domain_free_gpus() == {"scale_up_domain:d1": 2, "scale_up_domain:d2": 0}


@pytest.mark.parametrize(
    "doc,match",
    [
        ({"racks": {}}, "unknown topology section"),
        ({"scale_up_domains": ["a"]}, "must map a name"),
        ({"scale_up_domains": {"d": {"nodes": "a1"}}}, "must be a list"),
    ],
)
def test_malformed_documents_raise(doc, match):
    with pytest.raises(topology.TopologyError, match=match):
        topology.build([], doc)


def test_edges_are_typed():
    g = topology.ResourceGraph()
    g.add_vertex("f", "fabric")
    g.add_vertex("s", "storage")
    g.add_vertex("n", "node")
    g.add_edge("connects", "f", "n")
    with pytest.raises(topology.TopologyError, match="cannot contains"):
        g.add_edge("contains", "f", "n")
    with pytest.raises(topology.TopologyError, match="cannot connects"):
        g.add_edge("connects", "f", "s")
    with pytest.raises(topology.TopologyError, match="unknown vertex kind"):
        g.add_vertex("q", "quantum")
    with pytest.raises(topology.TopologyError, match="is a node"):
        g.add_vertex("n", "fabric")


def test_cli_topology_reports_the_graph(db, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    _inventory()
    _topology(tmp_path, monkeypatch, DOC)
    res = CliRunner().invoke(app, ["--json", "admission", "topology"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    assert out["largest_free_domain_gpus"] == 8
    assert out["topology_file"].endswith("topology.json")


def test_a_stale_inventory_row_is_not_evidence_of_free_gpus(db, tmp_path, monkeypatch):
    """An `idle` read long ago must not let admission promise a single-domain fit now."""
    _inventory()
    _topology(tmp_path, monkeypatch, DOC)
    import examlops.platform_db as pdb

    with pdb.get_db() as conn:
        conn.execute("UPDATE hpc_nodes SET captured_at = '2020-01-01 00:00:00' WHERE node='b2'")
    g = topology.current_graph()
    assert g.domain_free_gpus()["scale_up_domain:nvl-b"] == 4, "b2's stale idle counts 0"
    assert any("older than" in p for p in g.summary()["problems"])

    # and the policy, fed live, now queues the 8-GPU required job instead of admitting it
    decision, _ = decide(
        _required(8), state=current_state(), quotas=Quotas(10, 10), policy="baseline-over-quota"
    )
    assert isinstance(decision, Queue), decision


def test_the_staleness_limit_can_be_disabled(db, tmp_path, monkeypatch):
    _inventory()
    _topology(tmp_path, monkeypatch, DOC)
    import examlops.platform_db as pdb

    with pdb.get_db() as conn:
        conn.execute("UPDATE hpc_nodes SET captured_at = '2020-01-01 00:00:00'")
    monkeypatch.setenv(topology.MAX_AGE_ENV, "0")
    assert topology.current_graph().largest_free_domain_gpus() == 8
