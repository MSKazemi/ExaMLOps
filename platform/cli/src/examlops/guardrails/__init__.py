"""D8 — Guardrails / safety / PII defense (ADR 0026).

A declarative input/output guardrail layer for the B2 gateway and the Skipper agent:
prompt-injection/jailbreak detection, PII detection + redaction, secret-leak defense, a
topic/tool allow-list, per-tenant policies, and `off | monitor | enforce` modes with
**fail-closed** enforcement. Every block/redaction is audited (D4) and counted for metrics.

The production PII engine is Presidio and moderation is a hosted/LLM classifier; the
**fallback** is a set of regex detectors + the D7 secret scanner, so guardrails work with no
external service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ── detectors (regex fallback; Presidio in production) ────────────────────────

_INJECTION = re.compile(
    r"(ignore (all |the )?previous instructions|disregard .* above|"
    r"system prompt|you are now|jailbreak|do anything now)",
    re.IGNORECASE,
)
_PII_PATTERNS = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "phone": re.compile(r"\b(?:\+?\d{1,3}[ -]?)?(?:\(?\d{3}\)?[ -]?)\d{3}[ -]?\d{4}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # Before `credit_card`, whose digit run would otherwise eat the account number out of an IBAN.
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    # Candidates only — confirmed by `_VALIDATORS` below. A pattern loose enough to catch every
    # IPv6 form also catches MAC addresses (`00:1b:44:11:3a:b7`) and timecodes (`01:02:03:04`),
    # and redacting those as "ipv6" would be a wrong label on data that was not an address.
    "ipv6": re.compile(
        r"\b[0-9A-Fa-f:]*::?[0-9A-Fa-f:]*[0-9A-Fa-f]\b|\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b"
    ),  # noqa: E501
}


def _is_ipv6(candidate: str) -> bool:
    """Whether a candidate really is an IPv6 address, rather than a MAC or a timecode."""
    import ipaddress

    try:
        ipaddress.IPv6Address(candidate)
    except ValueError:
        return False
    return True


#: Detectors whose matches are confirmed before being treated as a finding. A regex decides where
#: to *look*; the validator decides whether it found anything — which is what keeps "ipv6" from
#: meaning "any run of hex separated by colons".
_VALIDATORS = {"ipv6": _is_ipv6}


def _matches(name: str, pat: re.Pattern[str], text: str) -> list[str]:
    """Every confirmed match of one detector, in order."""
    check = _VALIDATORS.get(name)
    out = []
    for m in pat.finditer(text):
        value = m.group(0)
        if check is None or check(value):
            out.append(value)
    return out


# A tiny toxicity wordlist stub (a hosted classifier replaces this in production).
_TOXIC = re.compile(r"\b(kill yourself|i hate you|slur1|slur2)\b", re.IGNORECASE)


@dataclass
class GuardResult:
    action: str  # allow | redact | block
    text: str
    findings: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.action == "block"


@runtime_checkable
class Guardrail(Protocol):
    def check_input(self, text: str, ctx: dict) -> GuardResult: ...
    def check_output(self, text: str, ctx: dict) -> GuardResult: ...
    def check_tool_call(self, tool: str, ctx: dict) -> bool: ...


def detect_pii(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, pat in _PII_PATTERNS.items():
        found = _matches(name, pat, text)
        if found:
            out[name] = found
    return out


def redact_pii(text: str) -> tuple[str, list[str]]:
    findings: list[str] = []
    redacted = text
    for name, pat in _PII_PATTERNS.items():
        check = _VALIDATORS.get(name)
        if check is None:
            if pat.search(redacted):
                findings.append(name)
                redacted = pat.sub(f"[redacted-{name}]", redacted)
            continue
        # A validated detector replaces only the matches its validator confirms, so a MAC address
        # that looks like an address to the regex is left exactly as it was found.
        hit = False

        def _sub(m: re.Match[str], _check=check, _name=name) -> str:
            nonlocal hit
            if not _check(m.group(0)):
                return m.group(0)
            hit = True
            return f"[redacted-{_name}]"

        redacted = pat.sub(_sub, redacted)
        if hit:
            findings.append(name)
    return redacted, findings


@dataclass
class DefaultGuardrail:
    """Regex/D7-backed guardrail with modes and a per-tenant tool allow-list."""

    mode: str = "enforce"  # off | monitor | enforce
    tenant: str = "default"
    allowed_tools: set[str] | None = None  # None = allow all
    block_injection: bool = True
    redact_pii: bool = True

    # ── input ────────────────────────────────────────────────────────────────
    def check_input(self, text: str, ctx: dict | None = None) -> GuardResult:
        if self.mode == "off":
            return GuardResult("allow", text)
        findings: list[str] = []
        try:
            if _INJECTION.search(text):
                findings.append("injection")
            pii = detect_pii(text)
            findings.extend(pii.keys())
            # secret-leak reuse (D7)
            if _secret_hit(text):
                findings.append("secret")
        except Exception:
            # fail-closed in enforce (R6)
            if self.mode == "enforce":
                self._record("input", "block", "scanner-error")
                return GuardResult("block", "", ["scanner-error"], "fail-closed")
            return GuardResult("allow", text, ["scanner-error"], "monitor: scanner error")

        if not findings:
            return GuardResult("allow", text)
        if self.mode == "monitor":
            self._record("input", "allow", ",".join(findings))
            return GuardResult("allow", text, findings, "monitor: not blocked")
        # enforce
        if "injection" in findings and self.block_injection:
            self._record("input", "block", "injection")
            return GuardResult("block", "", findings, "prompt injection blocked")
        redacted, _ = redact_pii(text)
        if "secret" in findings:
            # Detected and, until now, sent on: the input path redacted only personal data, so a
            # credential pasted into a prompt reached the model in enforce mode. The output path
            # already replaced secrets; the request must not be the weaker side.
            redacted = _redact_secret(redacted)
        self._record("input", "redact", ",".join(findings))
        return GuardResult("redact", redacted, findings, "PII/secret redacted")

    # ── output ───────────────────────────────────────────────────────────────
    def check_output(self, text: str, ctx: dict | None = None) -> GuardResult:
        if self.mode == "off":
            return GuardResult("allow", text)
        findings: list[str] = []
        redacted = text
        try:
            if _TOXIC.search(text):
                findings.append("toxicity")
            redacted, pii = redact_pii(text)
            findings.extend(pii)
            if _secret_hit(text):
                findings.append("secret")
                redacted = "[redacted-secret]"
        except Exception:
            if self.mode == "enforce":
                self._record("output", "block", "scanner-error")
                return GuardResult("block", "", ["scanner-error"], "fail-closed")
            return GuardResult("allow", text, ["scanner-error"])

        if not findings:
            return GuardResult("allow", text)
        if self.mode == "monitor":
            self._record("output", "allow", ",".join(findings))
            return GuardResult("allow", text, findings, "monitor: not blocked")
        if "toxicity" in findings:
            self._record("output", "block", "toxicity")
            return GuardResult("block", "", findings, "toxic output blocked")
        self._record("output", "redact", ",".join(findings))
        return GuardResult("redact", redacted, findings, "PII/secret redacted")

    # ── tool call ────────────────────────────────────────────────────────────
    def check_tool_call(self, tool: str, ctx: dict | None = None) -> bool:
        if self.mode == "off" or self.allowed_tools is None:
            return True
        allowed = tool in self.allowed_tools
        if not allowed:
            self._record("tool", "block", tool)
        return allowed if self.mode == "enforce" else True

    def _record(self, direction: str, action: str, rule: str) -> None:
        # The telemetry row and the audit record are recorded independently. They shared one
        # `try` before, so a failed insert also skipped the audit write — the governance record
        # of a block was lost because a counters table was unavailable, which is a different
        # system having a different problem.
        try:
            from examlops.data import get_db

            with get_db() as conn:
                conn.execute(
                    """INSERT INTO guardrail_events (tenant, direction, action, rule, mode)
                       VALUES (?,?,?,?,?)""",
                    (self.tenant, direction, action, rule, self.mode),
                )
        except Exception:  # noqa: BLE001 - guardrail telemetry must never block the request
            pass
        if action in ("block", "redact"):
            # Counted rather than swallowed: a block that happened and was not recorded reads,
            # afterwards, exactly like a block that never happened.
            from examlops.data.audit import audit_best_effort

            audit_best_effort(
                "exa-guardrails",
                None,
                f"guardrail_{action}",
                f"{self.tenant}/{direction}",
                {"rule": rule, "mode": self.mode},
            )


def _redact_secret(text: str) -> str:
    try:
        from examlops.secrets import redact_secrets

        return redact_secrets(text)[0]
    except Exception:
        # The scanner said there is a secret and we cannot locate it: fail closed on the text.
        return "[redacted-secret]"


def _secret_hit(text: str) -> bool:
    try:
        from examlops.secrets import scan_text

        return bool(scan_text(text))
    except Exception:
        return False


def rag_guardrail_adapter(guard: DefaultGuardrail):
    """Return a B4-compatible guardrail fn: text -> (flagged, neutralized_text)."""

    def _fn(text: str) -> tuple[bool, str]:
        res = guard.check_input(text, {"source": "rag"})
        if res.action == "allow" and not res.findings:
            return False, text
        return True, (res.text if res.action != "block" else "[blocked-content]")

    return _fn


def guardrail_stats(tenant: str | None = None) -> dict[str, Any]:
    """Aggregate guardrail actions for the dashboard/Prometheus (R8)."""
    from examlops.data import get_db, init_db

    init_db()
    where = "WHERE tenant=?" if tenant else ""
    params = (tenant,) if tenant else ()
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT action, COUNT(*) AS n FROM guardrail_events {where} GROUP BY action", params
        ).fetchall()
    counts = {r["action"]: int(r["n"]) for r in rows}
    return {
        "allow": counts.get("allow", 0),
        "redact": counts.get("redact", 0),
        "block": counts.get("block", 0),
        "total": sum(counts.values()),
    }
