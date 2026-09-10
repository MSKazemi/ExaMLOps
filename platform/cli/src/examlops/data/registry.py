"""examlops.data.registry — Model registry — signatures/BOM/cards.

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
from typing import Any  # noqa: F401

from examlops.platform_db import get_db, init_db, install_write_retry  # noqa: F401

__all__ = [
    "get_dataset_card",
    "get_model_bom",
    "get_model_card",
    "get_model_signature",
    "register_device_pool",
    "save_dataset_card",
    "save_model_card",
    "store_model_bom",
    "store_model_signature",
]


def get_dataset_card(dataset: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM dataset_cards WHERE dataset=? ORDER BY version DESC LIMIT 1",
            (dataset,),
        ).fetchone()
    return dict(row) if row else None


def get_model_bom(model: str, version: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT bom_json FROM model_boms WHERE model=? AND version=?", (model, version)
        ).fetchone()
    return json.loads(row["bom_json"]) if row else None


def get_model_card(model: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM model_card_records WHERE model=? ORDER BY version DESC LIMIT 1",
            (model,),
        ).fetchone()
    return dict(row) if row else None


def get_model_signature(model: str, version: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        # Case-insensitive on the model: operators sign the registry name (`exa models sign JPCP`)
        # and serving verifies the MLflow name (`jpcp`); an exact match reported every signed
        # model as unsigned the moment serving asked (plan P0.6).
        row = conn.execute(
            "SELECT * FROM model_signatures WHERE lower(model)=lower(?) AND version=? "
            "ORDER BY signed_at DESC LIMIT 1",
            (model, version),
        ).fetchone()
    return dict(row) if row else None


def register_device_pool(
    name: str,
    *,
    target: str = "hpc",
    accelerator: str = "nvidia",
    capabilities: list[str] | None = None,
    count: int = 0,
    region: str | None = None,
    cost_per_hour: float = 0.0,
    carbon_factor: float = 0.0,
    supports_fractions: bool = False,
    status: str = "active",
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO device_pools
                   (name, target, accelerator, capabilities, count, region, cost_per_hour,
                    carbon_factor, supports_fractions, status, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (
                name,
                target,
                accelerator,
                json.dumps(capabilities or []),
                count,
                region,
                cost_per_hour,
                carbon_factor,
                1 if supports_fractions else 0,
                status,
            ),
        )


def save_dataset_card(dataset: str, croissant_json: str, *, revision: str | None = None) -> int:
    init_db()
    with get_db() as conn:
        prev = conn.execute(
            "SELECT MAX(version) AS v FROM dataset_cards WHERE dataset=?", (dataset,)
        ).fetchone()
        version = (prev["v"] or 0) + 1
        conn.execute(
            "INSERT INTO dataset_cards (dataset, revision, version, croissant_json) "
            "VALUES (?,?,?,?)",
            (dataset, revision, version, croissant_json),
        )
    return version


def save_model_card(
    model: str,
    card_json: str,
    completeness: float,
    *,
    tenant: str = "default",
    created_by: str | None = None,
) -> int:
    init_db()
    with get_db() as conn:
        prev = conn.execute(
            "SELECT MAX(version) AS v FROM model_card_records WHERE model=?", (model,)
        ).fetchone()
        version = (prev["v"] or 0) + 1
        conn.execute(
            "INSERT INTO model_card_records (model, tenant, version, completeness, card_json, "
            "created_by) VALUES (?,?,?,?,?,?)",
            (model, tenant, version, completeness, card_json, created_by),
        )
    return version


def store_model_bom(model: str, version: str, bom: dict[str, Any]) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO model_boms (model, version, bom_json, created_at)
               VALUES (?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(model, version) DO UPDATE SET
                   bom_json=excluded.bom_json, created_at=CURRENT_TIMESTAMP""",
            (model, version, json.dumps(bom)),
        )


def store_model_signature(
    model: str,
    version: str,
    digest: str,
    signature: str,
    *,
    algo: str = "hmac-sha256",
    cert: str | None = None,
    signed_by: str | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO model_signatures
                   (model, version, digest, algo, signature, cert, signed_by, signed_at)
               VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(model, version) DO UPDATE SET
                   digest=excluded.digest, algo=excluded.algo, signature=excluded.signature,
                   cert=excluded.cert, signed_by=excluded.signed_by, signed_at=CURRENT_TIMESTAMP""",
            (model, version, digest, algo, signature, cert, signed_by),
        )


install_write_retry(__name__)
