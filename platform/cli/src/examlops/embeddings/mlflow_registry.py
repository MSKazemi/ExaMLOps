"""The encoder registry in MLflow (ADR 0043 clause 1).

Selected with ``EXAMLOPS_ENCODER_REGISTRY=mlflow``. Each encoder is one MLflow run in the
experiment ``examlops-encoders`` (``EXAMLOPS_ENCODER_EXPERIMENT``), named by its ``encoder_id``,
whose **artifact** ``encoder.json`` is the encoder's card — name, version, dim, metric,
normalization — with the same fields as run params and the id as a tag, so encoders sit in the
same registry as the models that use them, with MLflow's UI and access control.

**MLflow is written first.** Registration publishes here and only then records the encoder in
``platform.db``, which becomes an index: the compatibility guard and reindex read it locally and
fast, and an encoder published by another instance sharing this MLflow is pulled into it on first
use. A failed publish registers nothing, so the two cannot disagree.

**Integrity.** An ``encoder_id`` is content-addressed. A record whose fields do not hash to its
own id — edited by hand, or corrupted — is refused on read rather than served: an encoder that
claims one space and describes another would defeat the guard it exists for.

**Duplicates are harmless.** Two processes registering the same encoder at once can each create a
run; they carry identical cards, readers take the earliest finished one, and a run that did not
finish (a publish that failed half-way) is never read.
"""

from __future__ import annotations

import json
import os
from typing import Any

SCHEMA = "examlops.encoder/v1"
_TAG_ID = "examlops.encoder_id"
_FIELDS = ("name", "version", "dim", "metric", "normalization")


class EncoderRegistryError(RuntimeError):
    """The MLflow encoder registry is not usable, or refused a record."""


def experiment_name() -> str:
    return os.getenv("EXAMLOPS_ENCODER_EXPERIMENT") or "examlops-encoders"


def _client() -> Any:
    uri = os.getenv("EXAMLOPS_ENCODER_MLFLOW_URI") or os.getenv("MLFLOW_TRACKING_URI")
    if not uri:
        raise EncoderRegistryError(
            "EXAMLOPS_ENCODER_REGISTRY=mlflow needs MLFLOW_TRACKING_URI (or "
            "EXAMLOPS_ENCODER_MLFLOW_URI) — the registry the encoders live in"
        )
    try:
        from mlflow import MlflowClient
    except ImportError as exc:  # pragma: no cover - mlflow is a platform dependency
        raise EncoderRegistryError("the MLflow encoder registry needs the mlflow package") from exc
    return MlflowClient(tracking_uri=uri)


def _artifact_location(uri: str) -> str | None:
    """Where a **local** tracking store should keep this experiment's artifacts, or ``None``.

    ``None`` for a tracking *server* (``http(s)``, ``databricks``): the server owns its artifact
    store — MinIO in the platform's Compose and Helm deployments — and a client that overrode it
    would scatter artifacts outside it.

    For a local store (``sqlite:``, ``file:``, a bare path) MLflow's default artifact root is
    ``./mlruns``, i.e. relative to whatever directory the process happens to run in: a repository
    checkout, a user's home, an HPC job's scratch. Artifacts are instance data, so they belong
    under the data root (ADR 0128). With no data root configured the default is left alone, which
    is the rule that layer follows everywhere: unset means unchanged.
    """
    from urllib.parse import urlsplit  # noqa: PLC0415

    if urlsplit(uri).scheme in ("http", "https", "databricks"):
        return None
    from examlops.lifecycle.datadir import data_path  # noqa: PLC0415

    root = data_path("mlflow-artifacts", experiment_name())
    return root.as_uri() if root else None


def _experiment_id(client: Any, *, create: bool) -> str | None:
    exp = client.get_experiment_by_name(experiment_name())
    if exp is not None:
        return str(exp.experiment_id)
    if not create:
        return None
    location = _artifact_location(str(getattr(client, "tracking_uri", "") or ""))
    return str(client.create_experiment(experiment_name(), artifact_location=location))


def _card_from_run(run: Any) -> dict[str, Any] | None:
    """The card a finished run records, or None if it is not a valid encoder record."""
    from examlops.embeddings import encoder_id

    if getattr(run.info, "status", "") != "FINISHED":
        return None
    params, tags = dict(run.data.params or {}), dict(run.data.tags or {})
    try:
        card = {
            "encoder_id": tags[_TAG_ID],
            "name": params["name"],
            "version": params["version"],
            "dim": int(params["dim"]),
            "metric": params["metric"],
            "normalization": params["normalization"],
        }
    except (KeyError, ValueError):
        return None
    expected = encoder_id(
        card["name"], card["version"], card["dim"], card["metric"], card["normalization"]
    )
    if expected != card["encoder_id"]:
        return None  # the fields do not hash to the id they claim — refused, never served
    card["mlflow_run_id"] = run.info.run_id
    card["created_at"] = run.info.start_time
    return card


def _runs(client: Any, exp_id: str, filter_string: str = "") -> list[Any]:
    out: list[Any] = []
    token = None
    while True:
        page = client.search_runs(
            [exp_id],
            filter_string=filter_string,
            max_results=500,
            order_by=["attributes.start_time ASC"],
            page_token=token,
        )
        out.extend(page)
        token = getattr(page, "token", None)
        if not token:
            return out


def fetch(encoder_id: str) -> dict[str, Any] | None:
    """The earliest valid finished record of ``encoder_id``, or None."""
    client = _client()
    exp_id = _experiment_id(client, create=False)
    if exp_id is None:
        return None
    for run in _runs(client, exp_id, f"tags.`{_TAG_ID}` = '{_quote(encoder_id)}'"):
        card = _card_from_run(run)
        if card is not None:
            return card
    return None


def fetch_all() -> list[dict[str, Any]]:
    """Every valid encoder record, one per ``encoder_id`` (earliest finished run wins)."""
    client = _client()
    exp_id = _experiment_id(client, create=False)
    if exp_id is None:
        return []
    cards: dict[str, dict[str, Any]] = {}
    for run in _runs(client, exp_id):
        card = _card_from_run(run)
        if card is not None:
            cards.setdefault(card["encoder_id"], card)
    return list(cards.values())


def publish(card: dict[str, Any], *, actor: str | None = None) -> str:
    """Record ``card`` in MLflow (idempotent); returns the run id that holds it."""
    existing = fetch(card["encoder_id"])
    if existing is not None:
        return str(existing["mlflow_run_id"])
    client = _client()
    exp_id = _experiment_id(client, create=True)
    tags = {_TAG_ID: card["encoder_id"], "examlops.schema": SCHEMA}
    if actor:
        tags["examlops.actor"] = actor
    run = client.create_run(exp_id, run_name=card["encoder_id"], tags=tags)
    run_id = run.info.run_id
    try:
        for field in _FIELDS:
            client.log_param(run_id, field, str(card[field]))
        document = {"schema": SCHEMA, "encoder_id": card["encoder_id"]}
        document.update({f: card[f] for f in _FIELDS})
        client.log_text(run_id, json.dumps(document, indent=2, sort_keys=True), "encoder.json")
    except Exception as exc:
        client.set_terminated(run_id, status="FAILED")  # never read: only FINISHED runs count
        raise EncoderRegistryError(
            f"could not publish encoder {card['encoder_id']}: {exc}"
        ) from exc
    client.set_terminated(run_id, status="FINISHED")
    return str(run_id)


def _quote(value: str) -> str:
    """Escape a value for an MLflow filter string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")
