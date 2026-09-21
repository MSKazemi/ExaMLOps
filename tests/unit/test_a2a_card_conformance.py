"""Conformance checks for the platform and per-agent A2A cards (ADR 0141 decisions 5 and 6).

Scope, stated plainly: this asserts what THIS repo can verify offline - required fields, types,
the protocol version string, that no capability is advertised without code behind it, that the
per-agent card is content-addressed and round-trips. It does NOT validate against the published
A2A 1.0 JSON schema (not available offline), so it is not a protocol-conformance claim.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import agent_versions as av  # noqa: E402
from examlops.cli.main import app  # noqa: E402
from examlops.mcp import agent_card as ac  # noqa: E402
from examlops.mcp.resources import RESOURCES, agent_card  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402

runner = CliRunner()
REQUIRED = {
    "protocolVersion": str,
    "name": str,
    "description": str,
    "version": str,
    "capabilities": dict,
    "defaultInputModes": list,
    "defaultOutputModes": list,
    "skills": list,
}
IMAGE = "ghcr.io/example/jobdoc@sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    for k in ("EXAMLOPS_SIGNING_KEY", "EXAMLOPS_SIGNING_PRIVATE_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)

    def _no_secret(*_a, **_k):
        raise LookupError("no secret store in this test")

    monkeypatch.setattr("examlops.secrets.get_secret", _no_secret)
    init_db()


def _doc(prompt_version: int = 7) -> dict:
    return {
        "schema_version": 1,
        "agent": "jobdoc",
        "code": {"image": IMAGE, "entrypoint": "app.graph:build", "framework": "langgraph"},
        "prompts": [{"name": "jobdoc-system", "version": prompt_version}],
        "models": [
            {"role": "planner", "servable": "gen://q", "binding": "pin", "version": 1},
        ],
        "tools": {"tools": av.mcp_tool_manifest(["platform_status", "list_models"])},
        "policy": {"autonomy": "L1"},
    }


def _registered(**kw):
    out = av.register(_doc(**kw))
    return av.get(out["version_id"]), out["version_id"]


def _card_for(row) -> dict:
    return ac.build_agent_version_card(
        av.AgentVersion(row["version_id"], row["agent"], row["manifest"])
    )


def _check_shape(card: dict) -> None:
    for key, typ in REQUIRED.items():
        assert key in card, key
        assert isinstance(card[key], typ), key
    assert card["protocolVersion"] == "1.0"
    assert card["name"] and card["version"]
    for s in card["skills"]:
        assert {"id", "name", "description"} <= set(s)
    caps = card["capabilities"]
    for flag in ("streaming", "pushNotifications", "stateTransitionHistory"):
        assert isinstance(caps[flag], bool)
    assert isinstance(caps["extensions"], list)


def test_platform_card_shape_and_version():
    card = ac.build_agent_card(base_url="https://x", include_writes=False)
    _check_shape(card)
    assert card["protocolVersion"] != "0.2.0"


def test_agent_version_card_shape():
    row, vid = _registered()
    card = _card_for(row)
    _check_shape(card)
    assert card["version"] == vid  # content-addressed
    assert {s["id"] for s in card["skills"]} == {"platform_status", "list_models"}
    assert all(s["description"] for s in card["skills"])


def test_card_claims_no_capability_without_code():
    """The registry decides; a truthy card flag must have a resolvable implementation."""
    for card in (ac.build_agent_card(include_writes=False), _card_for(_registered()[0])):
        for flag, claimed in card["capabilities"].items():
            if flag == "extensions":
                assert set(claimed) <= set(ac.IMPLEMENTED_EXTENSIONS)
            elif claimed:
                assert ac._backed(ac.IMPLEMENTED_CAPABILITIES.get(flag)), flag
    # today nothing is implemented, so nothing may be advertised
    assert not any(v for k, v in ac.derive_capabilities().items() if k != "extensions")
    assert ac.derive_capabilities()["extensions"] == []


def test_capability_claim_without_code_is_caught(monkeypatch):
    """Mutation: registering a path that does not resolve must NOT flip the flag."""
    monkeypatch.setitem(ac.IMPLEMENTED_CAPABILITIES, "streaming", "examlops.nope:sse")
    assert ac.derive_capabilities()["streaming"] is False
    monkeypatch.setitem(
        ac.IMPLEMENTED_CAPABILITIES, "streaming", "examlops.mcp.agent_card:card_digest"
    )
    assert ac.derive_capabilities()["streaming"] is True  # backed -> allowed


def test_agent_card_declares_no_unserved_interface_or_security():
    card = _card_for(_registered()[0])
    assert card["interfaces"] == {} and "url" not in card
    assert card["securitySchemes"] == {}


def test_card_hash_stable_and_changes_with_version():
    row1, v1 = _registered()
    assert ac.card_digest(_card_for(row1)) == ac.card_digest(_card_for(av.get(v1)))
    row2, v2 = _registered(prompt_version=8)
    assert v1 != v2
    assert ac.card_digest(_card_for(row1)) != ac.card_digest(_card_for(row2))


def test_card_json_round_trips():
    card = _card_for(_registered()[0])
    assert json.loads(json.dumps(card)) == card
    assert ac.card_digest(json.loads(json.dumps(card))) == ac.card_digest(card)
    plat = ac.build_agent_card(include_writes=False)
    assert json.loads(json.dumps(plat)) == plat


def test_digest_detects_tampering():
    card = _card_for(_registered()[0])
    forged = copy.deepcopy(card)
    forged["capabilities"]["streaming"] = True
    assert ac.card_digest(forged) != ac.card_digest(card)


def test_cli_card_by_alias_and_out(tmp_path):
    row, vid = _registered()
    av.set_alias("jobdoc", "Staging", vid)
    r = runner.invoke(app, ["--json", "agent", "version", "card", "jobdoc@Staging"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["version"] == vid
    r = runner.invoke(app, ["--json", "agent", "version", "card", vid])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["version"] == vid
    out = tmp_path / "c.json"
    r = runner.invoke(app, ["agent", "version", "card", vid, "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert json.loads(out.read_text())["version"] == vid
    r = runner.invoke(app, ["--json", "agent", "version", "card", "nope@Production"])
    assert r.exit_code == 1


def test_mcp_resource_is_registered_and_read_only():
    res = [r for r in RESOURCES if r.uri == "examlops://agent/{name}/card"]
    assert len(res) == 1 and res[0].templated
    row, vid = _registered()
    assert agent_card(vid)["version"] == vid
    av.set_alias("jobdoc", "Staging", vid)
    assert agent_card("jobdoc@Staging")["version"] == vid
    assert "error" in agent_card("ghost")
