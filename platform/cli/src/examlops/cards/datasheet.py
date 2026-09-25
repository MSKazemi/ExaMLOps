"""Datasheets for Datasets as a lint-able, gate-able governance artifact (ADR 0079 decision 6).

Gebru et al., *Datasheets for Datasets* (arXiv:1803.09010; CACM 64(12), 2021) organise a
dataset's documentation as a questionnaire in seven sections — motivation, composition,
collection process, preprocessing/cleaning/labeling, uses, distribution, maintenance. The
Croissant dataset card (``examlops.cards.croissant_record``) carries only what the platform can
derive from data; the questionnaire is what a *person* has to answer, so it lives in a file an
operator authors and reviews like code:

* ``$EXAMLOPS_DATASHEETS_DIR/<dataset>.yaml`` when set, else
* ``<active use-case pack>/datasheets/<dataset>.yaml`` (datasets are use-case content, ADR 0094), else
* ``<config dir>/datasheets/<dataset>.yaml``.

``.yml`` and ``.json`` are accepted too. :func:`lint_questionnaire` reports every required
question left unanswered (absent, empty, or a placeholder such as ``TODO``/``not provided``);
:func:`questionnaire_completeness` is the answered fraction. The ``datasheet`` promotion gate
(:mod:`examlops.policy_engine.gates`, off unless armed) refuses to promote a model whose training
datasets — the ``datasets:`` of its per-model YAML in the active pack — have no datasheet or one
below the configured floor. The platform names no concrete dataset: everything is read through
the pack.

The question set is a faithful *subset* of the paper's (the principal question of each theme,
not all ~57); the keys are stable, so a site may answer more questions than are required.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_BYTES = 256 * 1024  # a datasheet is prose, not data; refuse anything larger
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_PLACEHOLDERS = {"", "todo", "tbd", "tbc", "?", "not provided", "fixme", "xxx", "-"}
_SUFFIXES = (".yaml", ".yml", ".json")


@dataclass(frozen=True)
class Question:
    key: str
    text: str


# Gebru et al. (2021), §3 "Questions and Workflow" — section → required questions.
SECTIONS: dict[str, tuple[Question, ...]] = {
    "motivation": (
        Question("purpose", "For what purpose was the dataset created?"),
        Question("creators", "Who created the dataset and on behalf of which entity?"),
        Question("funding", "Who funded the creation of the dataset?"),
    ),
    "composition": (
        Question("instances", "What do the instances that comprise the dataset represent?"),
        Question("count", "How many instances are there in total?"),
        Question("sampling", "Is it a sample of a larger set, and how representative is it?"),
        Question("missing_data", "Is any information missing from individual instances?"),
        Question(
            "confidential_data", "Does it contain data that might be considered confidential?"
        ),
        Question("personal_data", "Does it identify individuals, directly or indirectly?"),
    ),
    "collection_process": (
        Question("acquisition", "How was the data associated with each instance acquired?"),
        Question("timeframe", "Over what timeframe was the data collected?"),
        Question("ethical_review", "Were any ethical review processes conducted?"),
        Question("consent", "Were the individuals concerned notified and did they consent?"),
    ),
    "preprocessing": (
        Question("preprocessing", "Was any preprocessing/cleaning/labeling of the data done?"),
        Question("raw_data", "Was the raw data saved in addition to the preprocessed data?"),
    ),
    "uses": (
        Question("prior_uses", "Has the dataset been used for any tasks already?"),
        Question("intended_uses", "What (other) tasks could the dataset be used for?"),
        Question("prohibited_uses", "Are there tasks for which the dataset should not be used?"),
    ),
    "distribution": (
        Question("distribution", "How will the dataset be distributed?"),
        Question("license", "Under which copyright / IP license is it distributed?"),
        Question("restrictions", "Do any export controls or regulatory restrictions apply?"),
    ),
    "maintenance": (
        Question("maintainer", "Who will be supporting / hosting / maintaining the dataset?"),
        Question("contact", "How can the owner / curator / manager be contacted?"),
        Question("updates", "Will the dataset be updated, how often and by whom?"),
    ),
}

REQUIRED: tuple[tuple[str, str], ...] = tuple(
    (section, q.key) for section, qs in SECTIONS.items() for q in qs
)


class DatasheetError(ValueError):
    """A datasheet file exists but cannot be read as a questionnaire."""


def _config_dir() -> Path:
    from examlops.lifecycle.datadir import config_dir

    return config_dir()


def datasheet_dirs() -> list[Path]:
    """Where datasheets are looked up, highest precedence first."""
    env = os.getenv("EXAMLOPS_DATASHEETS_DIR", "").strip()
    if env:
        return [Path(env).expanduser()]
    dirs: list[Path] = []
    try:
        from examlops.usecase import _pack_dir

        pack = _pack_dir()
        if pack is not None:
            dirs.append(pack / "datasheets")
    except Exception:  # noqa: BLE001 — no pack is a legitimate state, not an error
        pass
    dirs.append(_config_dir() / "datasheets")
    return dirs


def _check_name(dataset: str) -> str:
    if not _NAME.match(dataset or ""):
        raise DatasheetError(f"invalid dataset name {dataset!r} (letters, digits, _ . - only)")
    return dataset


def find_datasheet(dataset: str) -> Path | None:
    """The datasheet file for ``dataset``, or ``None``. The name is validated (no traversal)."""
    name = _check_name(dataset)
    for d in datasheet_dirs():
        for suffix in _SUFFIXES:
            p = d / f"{name}{suffix}"
            if p.is_file():
                return p
    return None


def load_datasheet(dataset: str) -> dict[str, Any] | None:
    """The parsed datasheet mapping, ``None`` when absent; :class:`DatasheetError` when broken."""
    path = find_datasheet(dataset)
    if path is None:
        return None
    # An unreadable file (permissions, a non-UTF-8 byte, a race with a deletion) is a broken
    # datasheet, not a crash: residency treats DatasheetError as "allowed nowhere" (fail closed),
    # and a raw OSError/UnicodeDecodeError escaping here would instead crash every caller —
    # including a `monitor`-mode gate, which must never block.
    try:
        if path.stat().st_size > _MAX_BYTES:
            raise DatasheetError(f"{path} is larger than {_MAX_BYTES} bytes")
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DatasheetError(f"{path} could not be read: {exc}") from exc
    try:
        if path.suffix == ".json":
            doc = json.loads(text)
        else:
            import yaml

            doc = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 — surfaced as a finding, never a crash
        raise DatasheetError(f"{path} could not be parsed: {exc}") from exc
    if not isinstance(doc, dict):
        raise DatasheetError(f"{path} is not a mapping of sections")
    return doc


def _answered(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool | int | float):
        return True
    if isinstance(value, list | tuple):
        return any(_answered(v) for v in value)
    if isinstance(value, dict):
        return any(_answered(v) for v in value.values())
    return str(value).strip().lower() not in _PLACEHOLDERS


def lint_questionnaire(doc: dict[str, Any]) -> list[str]:
    """Every required question the datasheet leaves unanswered, as ``section.key: question``."""
    findings: list[str] = []
    for section, questions in SECTIONS.items():
        block = doc.get(section)
        if not isinstance(block, dict):
            block = {}
        for q in questions:
            if not _answered(block.get(q.key)):
                findings.append(f"{section}.{q.key} unanswered: {q.text}")
    return findings


def questionnaire_completeness(doc: dict[str, Any]) -> float:
    """Answered fraction of the required questions, 0..1."""
    missing = len(lint_questionnaire(doc))
    return round((len(REQUIRED) - missing) / len(REQUIRED), 4)


def template(dataset: str) -> str:
    """A YAML skeleton with every required question, each answer left as ``TODO``."""
    _check_name(dataset)
    lines = [
        f"# Datasheet for {dataset} — Gebru et al., Datasheets for Datasets (arXiv:1803.09010).",
        "# Replace every TODO with an answer; `exa cards lint <dataset> --datasheet` checks it.",
        f"dataset: {dataset}",
    ]
    for section, questions in SECTIONS.items():
        lines.append(f"{section}:")
        for q in questions:
            lines.append(f"  # {q.text}")
            lines.append(f"  {q.key}: TODO")
    return "\n".join(lines) + "\n"


def lint_dataset(dataset: str) -> tuple[list[str], float, Path | None]:
    """``(findings, completeness, path)`` for one dataset — a missing file is one finding."""
    try:
        doc = load_datasheet(dataset)
    except DatasheetError as exc:
        return [str(exc)], 0.0, find_datasheet(dataset) if _NAME.match(dataset) else None
    if doc is None:
        where = ", ".join(str(d) for d in datasheet_dirs())
        return [f"no datasheet for {dataset} (looked in: {where})"], 0.0, None
    return lint_questionnaire(doc), questionnaire_completeness(doc), find_datasheet(dataset)


def _datasets_in(path: Path) -> list[str]:
    """The ``datasets[].name`` of one per-model YAML; :class:`DatasheetError` when unreadable."""
    try:
        import yaml

        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — surfaced as a typed error, never swallowed here
        raise DatasheetError(f"model YAML {path} could not be read: {exc}") from exc
    if not isinstance(doc, dict):
        raise DatasheetError(f"model YAML {path} is not a mapping")
    names: list[str] = []
    for entry in doc.get("datasets") or []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def model_datasets(model: str, *, strict: bool = False) -> list[str]:
    """The dataset names declared in ``model``'s per-model YAML in the active pack.

    Lenient by default (a missing or unreadable YAML declares no dataset — the datasheet gate
    then refuses, because "no dataset" is itself a reason). ``strict=True`` raises
    :class:`DatasheetError` instead, for a caller where "no dataset" would *lift* a constraint
    (residency): a run whose data cannot be named cannot be shown to stay in-region.
    """
    from examlops.usecase import models_dir

    path = models_dir() / f"{model.lower()}.yaml"
    if not path.is_file():
        if strict:
            raise DatasheetError(f"no per-model YAML for {model!r} in {path.parent}")
        return []
    try:
        return _datasets_in(path)
    except DatasheetError:
        if strict:
            raise
        return []


def pack_datasets() -> list[str]:
    """Every dataset any per-model YAML of the active pack declares (strict: raises when one
    cannot be read) — what a ``exa pipeline run`` with no ``--model`` may train on."""
    from examlops.usecase import models_dir

    names: list[str] = []
    root = models_dir()
    for path in sorted([*root.glob("*.yaml"), *root.glob("*.yml")]):
        for name in _datasets_in(path):
            if name not in names:
                names.append(name)
    return names


def promotion_reasons(model: str, *, floor: float = 1.0) -> list[str]:
    """Why ``model`` may not be promoted under the datasheet gate — empty means it may.

    A model whose YAML declares no dataset cannot show its data is documented, so that is a
    reason too (fail closed: the gate is only consulted when a site armed it).
    """
    datasets = model_datasets(model)
    if not datasets:
        return [f"{model} declares no training dataset in its model YAML — no datasheet to check"]
    reasons: list[str] = []
    for ds in datasets:
        findings, score, _ = lint_dataset(ds)
        if score < floor:
            head = findings[0] if findings else "incomplete"
            reasons.append(
                f"datasheet for {ds}: completeness {score:.2f} < floor {floor:.2f} ({head}"
                + (f"; +{len(findings) - 1} more)" if len(findings) > 1 else ")")
            )
    return reasons


__all__ = [
    "REQUIRED",
    "SECTIONS",
    "DatasheetError",
    "Question",
    "datasheet_dirs",
    "find_datasheet",
    "lint_dataset",
    "lint_questionnaire",
    "load_datasheet",
    "model_datasets",
    "pack_datasets",
    "promotion_reasons",
    "questionnaire_completeness",
    "template",
]
