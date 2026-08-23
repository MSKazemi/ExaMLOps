"""The A2A agent card must not advertise an authentication check the platform does not perform.

`examlops.oidc` implements RS256/JWKS validation, and `examlops.config_validate` warns when
`EXAMLOPS_OIDC_ISSUER` is set without a JWKS — so the *configuration* surface is real. But as of
2026-08-23 nothing calls `oidc.verify_bearer` or `oidc.verify_token` outside this test suite: no
request path on any server checks an IdP token. The card said the token was "verified against the
issuer JWKS", which is a claim made to peers about a check that never runs.

The second test is the one that matters: it is wired to the actual caller count, so the day someone
enforces OIDC it fails and asks for the stronger wording back, rather than leaving the card
permanently understated.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "platform" / "cli" / "src"

sys.path.insert(0, str(SRC))

from examlops.mcp.agent_card import _security_schemes, build_agent_card  # noqa: E402


def _oidc_description(monkeypatch_env: str) -> str:
    old = os.environ.get("EXAMLOPS_OIDC_ISSUER")
    os.environ["EXAMLOPS_OIDC_ISSUER"] = monkeypatch_env
    try:
        return _security_schemes()["default"]["description"]
    finally:
        if old is None:
            os.environ.pop("EXAMLOPS_OIDC_ISSUER", None)
        else:
            os.environ["EXAMLOPS_OIDC_ISSUER"] = old


def _verifier_callers() -> list[str]:
    """Non-test, non-self references to the OIDC verifier, one per line."""
    out = subprocess.run(
        [
            "grep",
            "-rn",
            "--include=*.py",
            "-e",
            "verify_bearer",
            "-e",
            "oidc.verify_token",
            str(REPO / "platform"),
            str(REPO / "pipelines"),
            str(REPO / "serving"),
        ],
        capture_output=True,
        text=True,
    ).stdout
    keep = []
    for ln in out.splitlines():
        if "/tests/" in ln or re.search(r"src/examlops/oidc\.py", ln):
            continue
        # `grep -n` yields "<path>:<lineno>:<code>". Prose mentioning the verifier — this
        # module's own explanatory comments among it — is not a caller.
        code = ln.split(":", 2)[2] if ln.count(":") >= 2 else ln
        if code.strip().startswith(("#", '"', "'")):
            continue
        if re.search(r"(verify_bearer|verify_token)\s*\(|import[^\n]*verify_bearer", code):
            keep.append(ln)
    return keep


def test_card_declares_openid_scheme_when_an_issuer_is_configured():
    scheme = _security_schemes()
    assert scheme["default"]["type"] in {"openIdConnect", "http"}
    with_issuer = os.environ.get("EXAMLOPS_OIDC_ISSUER")
    if with_issuer:
        assert scheme["default"]["type"] == "openIdConnect"


def test_card_claim_matches_whether_anything_actually_verifies():
    """Card wording and enforcement must agree — in whichever direction reality moves."""
    description = _oidc_description("https://idp.example.com")
    callers = _verifier_callers()
    understated = "not yet enforced" in description.lower()

    if callers:
        assert not understated, (
            "The OIDC verifier is now called from:\n  "
            + "\n  ".join(callers)
            + "\nSSO is enforced, so the agent card should stop saying it is not. "
            "Restore the stronger wording in `_security_schemes`."
        )
    else:
        assert understated, (
            "Nothing calls oidc.verify_bearer / oidc.verify_token outside the tests, so no server "
            "checks an IdP token — the agent card must not tell peers the token is verified."
        )


# ── Protocol capability flags ────────────────────────────────────────────────
# Each A2A `capabilities` flag is a promise a peer may act on. `stateTransitionHistory`
# means a peer can ask for a task's status-transition history, which needs a task concept
# this surface does not have — no task id, no task store, no `tasks/get`. It was published
# as `True`. `audit_events` is not a substitute: it records what the platform did, not the
# lifecycle of an A2A task.


def _task_machinery_exists() -> bool:
    """True once the MCP surface grows something a task history could be read from."""
    mcp_dir = SRC / "examlops" / "mcp"
    for path in mcp_dir.rglob("*.py"):
        if path.name == "agent_card.py":
            continue
        text = path.read_text()
        if re.search(r"\btasks/get\b|\bTaskStore\b|\btask_history\b", text):
            return True
    return False


def test_state_transition_history_is_not_claimed_without_a_task_store():
    claimed = build_agent_card()["capabilities"]["stateTransitionHistory"]
    if _task_machinery_exists():
        assert claimed, (
            "The MCP surface now has task machinery, so a peer can be told the agent keeps "
            "state-transition history — set `stateTransitionHistory` back to True."
        )
    else:
        assert not claimed, (
            "There is no task id, task store or `tasks/get` anywhere in examlops/mcp, so the "
            "agent card must not promise peers a task status-transition history."
        )


def test_streaming_and_push_flags_stay_conservative():
    """Under-claiming is safe; over-claiming is not. These two have no implementation."""
    caps = build_agent_card()["capabilities"]
    assert caps["streaming"] is False
    assert caps["pushNotifications"] is False
