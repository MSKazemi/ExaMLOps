"""D8 — Guardrails / safety / PII defense (ADR 0026).

A declarative input/output guardrail layer for the B2 gateway and the Skipper agent:
prompt-injection/jailbreak detection, PII detection + redaction, secret-leak defense, a
topic/tool allow-list, per-tenant policies, and `off | monitor | enforce` modes with
**fail-closed** enforcement. Every block/redaction is audited (D4) and counted for metrics.

The production PII engine is Presidio and moderation is a hosted/LLM classifier; the
**fallback** is a set of regex detectors + the D7 secret scanner, so guardrails work with no
external service. Presidio is an **additive, opt-in supplement** to the regex detectors, not a
replacement for them (:func:`_ner_engine`): it adds the one class of PII a regex structurally
cannot find — a person's name, a place — while the regex patterns keep owning email/phone/SSN/
credit-card/IBAN/IP, which they already detect correctly and which Presidio's own bundled
recognizers do not reliably improve on (verified 2026-09-19: Presidio's `UsSsnRecognizer` missed
a plain hyphenated SSN it should match, a defect in Presidio itself, not this fallback).
"""

from __future__ import annotations

import functools
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

log = logging.getLogger("examlops.guardrails")

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


# ── Presidio NER supplement (ADR 0026 clause 1; additive, opt-in) ─────────────────────────────
# The entity types NER adds *on top of* the regex detectors above — deliberately only the ones a
# regex cannot find at all. Presidio also ships pattern-based recognizers for email/phone/SSN/
# credit-card/IBAN/IP; those are not surfaced here because the regex detectors above already own
# them, tested and validated against known edge cases (a MAC address, a timecode, a documentation
# slug). Duplicating that ground through Presidio's own pattern recognizers would trade a checked
# detector for an unchecked one — verified 2026-09-19 that this is not hypothetical: Presidio's
# bundled `UsSsnRecognizer` failed to match a plain `123-45-6789` even at `score_threshold=0.0`.
_NER_ENTITY_TYPES = ("PERSON", "LOCATION", "NRP")

_ner_cache: dict[str, Any] = {}


def _ner_engine() -> Any | None:
    """The Presidio analyzer, built once and cached; ``None`` when NER is off or unavailable.

    Off by default (`EXAMLOPS_GUARDRAIL_PII_NER`): Presidio + a spaCy model are a real, if
    modest, dependency (`examlops[guardrails-presidio]`, plus one manual spaCy model install —
    see `docs/guides/guardrails.md`), so this never becomes a surprise startup cost for a
    deployment that has not opted in. Any failure to construct the engine (package missing, no
    model installed) degrades to ``None`` — regex-only detection, exactly today's behaviour —
    logged once rather than raised, the same shape as :mod:`examlops.semantic_cache`'s embedder
    fallback.
    """
    if os.getenv("EXAMLOPS_GUARDRAIL_PII_NER", "").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return None
    if "engine" in _ner_cache:
        return _ner_cache["engine"]
    engine: Any | None = None
    try:
        from presidio_analyzer import AnalyzerEngine  # noqa: PLC0415
        from presidio_analyzer.nlp_engine import NlpEngineProvider  # noqa: PLC0415

        model = os.getenv("EXAMLOPS_GUARDRAIL_PRESIDIO_MODEL", "en_core_web_lg").strip()
        nlp_engine = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": model}],
            }
        ).create_engine()
        engine = AnalyzerEngine(nlp_engine=nlp_engine)
    except Exception as exc:  # noqa: BLE001 - unavailable NER must never break a guardrail check
        log.warning(
            "EXAMLOPS_GUARDRAIL_PII_NER is set but Presidio is unavailable (%s) — falling back "
            "to regex-only PII detection. Install examlops[guardrails-presidio] and a spaCy "
            "model; see docs/guides/guardrails.md.",
            exc,
        )
        engine = None
    _ner_cache["engine"] = engine
    return engine


def _ner_findings(text: str) -> list[tuple[str, int, int]]:
    """``(lowercase entity type, start, end)`` for every NER-only match, or ``[]`` if NER is off."""
    engine = _ner_engine()
    if engine is None:
        return []
    try:
        results = engine.analyze(text=text, language="en", entities=list(_NER_ENTITY_TYPES))
    except Exception as exc:  # noqa: BLE001 - a bad call must degrade, never break the guardrail
        log.warning("Presidio NER analysis failed (%s) — this call falls back to regex-only", exc)
        return []
    return [(r.entity_type.lower(), r.start, r.end) for r in results]


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
    for name, start, end in _ner_findings(text):
        out.setdefault(name, []).append(text[start:end])
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
    # NER runs last, over the already-regex-redacted text: offsets below are computed against
    # (and applied to) that same string, so the two passes never disagree about positions.
    ner = _ner_findings(redacted)
    for name, start, end in sorted(ner, key=lambda hit: hit[1], reverse=True):
        redacted = redacted[:start] + f"[redacted-{name}]" + redacted[end:]
        if name not in findings:
            findings.append(name)
    return redacted, findings


_CheckFn = TypeVar("_CheckFn", bound=Callable[..., Any])


def _traced_check(stage: str) -> Callable[[_CheckFn], _CheckFn]:
    """Run a guardrail check inside a GUARDRAIL span (ADR 0021 decision 1).

    The span records the verdict — action, whether it blocked, and the finding *categories*
    (``email``, ``injection`` …) — never the checked text. Tracing is fail-open: a telemetry
    failure runs the check untraced, and the check's own result and exceptions pass through
    unchanged, so a guardrail can never be weakened by its instrumentation.
    """

    def deco(fn: _CheckFn) -> _CheckFn:
        @functools.wraps(fn)
        def wrapper(self: Any, subject: str, ctx: dict | None = None) -> Any:
            try:
                from examlops.telemetry import genai

                if not genai.tracing_enabled():
                    return fn(self, subject, ctx)
                cm = genai.guardrail_span(stage, tenant=self.tenant, mode=self.mode)
                span = cm.__enter__()
            except Exception:  # noqa: BLE001 - untraced is better than unchecked
                return fn(self, subject, ctx)
            try:
                result = fn(self, subject, ctx)
            except BaseException as exc:
                try:
                    cm.__exit__(type(exc), exc, exc.__traceback__)
                except BaseException as tel_exc:  # noqa: BLE001
                    # contextmanager re-raises the check's own exception; anything else is the
                    # span failing to close, which must not replace the check's exception.
                    if tel_exc is not exc:
                        log.debug("guardrail span close failed: %s", type(tel_exc).__name__)
                raise
            try:
                if isinstance(result, GuardResult):
                    span.set_attribute("examlops.guardrail.action", result.action)
                    span.set_attribute("examlops.guardrail.blocked", result.blocked)
                    span.set_attribute("examlops.guardrail.findings", list(result.findings))
                else:  # check_tool_call: a bool verdict about a named tool
                    span.set_attribute("examlops.guardrail.tool", str(subject))
                    span.set_attribute("examlops.guardrail.action", "allow" if result else "block")
                    span.set_attribute("examlops.guardrail.blocked", not result)
            except Exception:  # noqa: BLE001
                pass
            try:
                cm.__exit__(None, None, None)
            except Exception as tel_exc:  # noqa: BLE001 - the verdict is already decided
                log.debug("guardrail span close failed: %s", type(tel_exc).__name__)
            return result

        return wrapper  # type: ignore[return-value]

    return deco


@dataclass
class DefaultGuardrail:
    """Regex/D7-backed guardrail with modes and a per-tenant tool allow-list."""

    mode: str = "enforce"  # off | monitor | enforce
    tenant: str = "default"
    allowed_tools: set[str] | None = None  # None = allow all
    block_injection: bool = True
    redact_pii: bool = True

    # ── input ────────────────────────────────────────────────────────────────
    @_traced_check("input")
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
    @_traced_check("output")
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
    @_traced_check("tool")
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
            from examlops.data import get_db, init_db

            # A guardrail check can be the very first thing to touch platform.db in a request
            # (e.g. a streaming output scan with no virtual key and no cache) -- without this,
            # the INSERT below silently no-ops on an uninitialized schema and the swallow-all
            # except makes that failure indistinguishable from "nothing to record" (found by
            # BL-115's streaming guardrail tests). Near-free after the first call per DB path.
            init_db()
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
                # The tenant whose traffic was blocked owns the record (ADR 0026 cl. 3, per-tenant).
                tenant=self.tenant,
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


_TELEMETRY_MODES = ("off", "monitor", "enforce")


def telemetry_redaction_mode() -> str:
    """``EXAMLOPS_TELEMETRY_REDACTION``: off | monitor | enforce (default ``enforce``).

    Unlike the request boundary, the default is enforce: content capture is already opt-in, so
    redacting what an operator asked to export cannot break traffic, and an unrecognised value
    falls back to enforce rather than off so a typo cannot leak prompts (ADR 0148 d2).
    """
    mode = os.getenv("EXAMLOPS_TELEMETRY_REDACTION", "enforce").strip().lower()
    return mode if mode in _TELEMETRY_MODES else "enforce"


def telemetry_redactor(tenant: str = "default", mode: str | None = None):
    """Text -> text redactor for prompts/completions leaving as telemetry (ADR 0148 d2).

    ``enforce`` replaces PII and secrets; ``monitor`` returns the text unchanged but records what
    would have been redacted to ``guardrail_events`` (direction ``telemetry``); ``off`` is the
    identity. Any failure inside raises, so the caller (``genai.maybe_capture_content``) drops the
    capture and counts it instead of exporting unredacted text.
    """
    mode = mode or telemetry_redaction_mode()
    if mode == "off":
        return lambda text: text
    recorder = DefaultGuardrail(mode=mode, tenant=tenant)

    def _redact(text: str) -> str:
        out, findings = redact_pii(text)
        if _secret_hit(out):
            findings = [*findings, "secret"]
            out = _redact_secret(out)
        if mode == "monitor":
            for rule in findings:
                recorder._record("telemetry", "monitor", rule)
            return text
        for rule in findings:
            recorder._record("telemetry", "redact", rule)
        return out

    return _redact


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
