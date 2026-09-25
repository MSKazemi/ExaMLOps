"""OSS guardrail framework adapters (ADR 0026 clause 3).

**LLM-Guard** (Protect AI, MIT) is the framework adapter shipped in core: its scanners are
self-hosted classifiers (prompt injection, toxicity, banned topics, gibberish, invisible text,
malicious URLs, relevance, …) that slot straight into a policy's ``input:``/``output:`` list::

    input:
      - injection
      - check: llm_guard
        timeout_s: 5
        scanners:
          - {name: PromptInjection, threshold: 0.9}
          - {name: BanTopics, topics: [violence], threshold: 0.6}

It is an **optional dependency** (``pip install llm-guard`` in a separate guardrail image — no extra, see platform/cli/pyproject.toml) imported lazily on first use: the
package pulls in transformers/torch and downloads models, which no deployment should get as a
surprise. When it is absent a policy naming ``llm_guard`` still loads; the check is reported
unavailable (``exa guardrails checks`` / ``policy validate``) and, per the ADR's fail-closed rule,
blocks in ``enforce`` and is recorded in ``monitor``.

NeMo Guardrails and Guardrails AI are conversation/validator frameworks rather than text
scanners; they plug in through the ``exa.guardrails.checks`` entry-point group
(:func:`examlops.guardrails.policy.register_check`) instead of a core dependency.
"""

from __future__ import annotations

import importlib
import re
import threading
from typing import Any

from examlops.guardrails.policy import CheckOutcome

#: Scanners that need a shared ``Vault`` object (Anonymize ↔ Deanonymize) cannot be built from a
#: policy file; the built-in ``pii`` check covers that ground.
_UNSUPPORTED = frozenset({"Anonymize", "Deanonymize"})
_NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9]{1,63}$")
MAX_SCANNERS = 16

_MODULES = {"input": "llm_guard.input_scanners", "output": "llm_guard.output_scanners"}


def llm_guard_unavailable_reason() -> str | None:
    """``None`` when ``llm_guard`` imports, else why not (for ``exa guardrails checks``)."""
    try:
        importlib.import_module("llm_guard")
    except Exception as exc:  # noqa: BLE001 - ImportError or a broken transitive dependency
        return (
            f"llm-guard is not importable ({type(exc).__name__}: {exc}); "
            "install llm-guard (separate guardrail image; Python <3.13)"
        )
    return None


def _plain(value: Any, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return len(value) <= 256 and all(_plain(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _plain(v, depth + 1) for k, v in value.items())
    return False


def validate_llm_guard_scanners(scanners: Any) -> list[str]:
    """Structural validation of an ``llm_guard`` check's ``scanners:`` list (no import needed)."""
    if not isinstance(scanners, list) or not scanners:
        return ["llm_guard needs a non-empty `scanners` list"]
    if len(scanners) > MAX_SCANNERS:
        return [f"llm_guard: at most {MAX_SCANNERS} scanners"]
    errors: list[str] = []
    for i, sc in enumerate(scanners):
        if not isinstance(sc, dict) or not isinstance(sc.get("name"), str):
            errors.append(f"scanners[{i}]: must be a mapping with a `name`")
            continue
        name = sc["name"]
        if not _NAME_RE.match(name):
            errors.append(f"scanners[{i}]: {name!r} is not a scanner class name")
        elif name in _UNSUPPORTED:
            errors.append(f"scanners[{i}]: {name} needs a shared Vault; use the `pii` check")
        kwargs = {k: v for k, v in sc.items() if k != "name"}
        if not all(k.isidentifier() and not k.startswith("_") for k in kwargs):
            errors.append(f"scanners[{i}]: argument names must be plain identifiers")
        elif not _plain(kwargs):
            errors.append(f"scanners[{i}]: arguments must be plain data (str/number/bool/list/map)")
    return errors


class LLMGuardCheck:
    """One ``llm_guard`` policy entry: a list of LLM-Guard scanners run as one check.

    Scanners are built lazily per direction and cached on the instance (LLM-Guard loads a model
    per scanner, so this happens once per policy configuration, not per request). Findings are
    ``llm_guard:<ScannerName>`` for each scanner that returned *invalid*; the sanitised text
    LLM-Guard returns is offered as the redaction when the check's action is ``redact``.
    """

    name = "llm_guard"

    def __init__(self, scanners: list[dict[str, Any]], fail_fast: bool = True):
        errs = validate_llm_guard_scanners(scanners)
        if errs:
            raise ValueError("; ".join(errs))
        self._specs = scanners
        self._fail_fast = fail_fast
        self._built: dict[str, list[Any]] = {}
        self._lock = threading.Lock()

    def _scanners(self, direction: str) -> list[Any]:
        with self._lock:
            if direction in self._built:
                return self._built[direction]
            module = importlib.import_module(_MODULES[direction])
            exported = set(getattr(module, "__all__", ()) or ())
            built = []
            for spec in self._specs:
                name = spec["name"]
                # Only names the module exports: a policy file never reaches an arbitrary attr.
                if name not in exported:
                    raise ValueError(f"llm_guard has no {direction} scanner {name!r}")
                kwargs = {k: v for k, v in spec.items() if k != "name"}
                built.append(getattr(module, name)(**kwargs))
            self._built[direction] = built
            return built

    def run(self, text: str, direction: str, ctx: dict[str, Any]) -> CheckOutcome:
        scanners = self._scanners(direction)
        llm_guard = importlib.import_module("llm_guard")
        if direction == "input":
            sanitized, valid, _scores = llm_guard.scan_prompt(
                scanners, text, fail_fast=self._fail_fast
            )
        else:
            sanitized, valid, _scores = llm_guard.scan_output(
                scanners, str(ctx.get("prompt") or ""), text, fail_fast=self._fail_fast
            )
        failed = tuple(f"llm_guard:{name}" for name, ok in valid.items() if not ok)
        if not failed:
            return CheckOutcome()
        return CheckOutcome(failed, sanitized if isinstance(sanitized, str) else None)
