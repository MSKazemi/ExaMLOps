"""Instance-data format stamp, compatibility rule, migrations and upgrade (ADR 0128)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from examlops import platform_db as pdb
from examlops.lifecycle import dataformat as fmt
from examlops.lifecycle import migrations as mig
from examlops.lifecycle import upgrade


def _stamp():
    with pdb.get_db() as conn:
        return fmt.read_stamp(conn)


def _fresh_init():
    pdb._INITIALIZED_PATHS.clear()
    pdb.init_db()


def _registry(*extra: mig.Migration) -> tuple[mig.Migration, ...]:
    return (*mig.MIGRATIONS, *extra)


def _step(version: int, name: str, *, online: bool = True, breaking: bool = False, calls=None):
    def apply(conn):  # writes through a real table, so no fixture-only schema is invented
        if calls is not None:
            calls.append(name)
        conn.execute(
            "INSERT INTO platform_meta (key, value, updated_at) VALUES (?, 'ran', 'now') "
            "ON CONFLICT(key) DO NOTHING",
            (f"test-probe:{name}",),
        )

    return mig.Migration(
        version, name, f"test step {name}", apply, online=online, breaking=breaking
    )


# ── the registry ──────────────────────────────────────────────────────────────────────────


def test_shipped_registry_is_valid_and_starts_at_the_baseline():
    mig.validate()
    assert mig.MIGRATIONS[0].version == 1 and mig.MIGRATIONS[0].name == "baseline"
    assert fmt.CODE_DATA_FORMAT == mig.code_format() >= 1


@pytest.mark.parametrize(
    "bad",
    [
        (mig.Migration(2, "a", "gap"),),
        (mig.Migration(1, "a", "x"), mig.Migration(2, "a", "dup name")),
        (mig.Migration(1, "a", "x"), mig.Migration(2, "b", "y", online=True, breaking=True)),
    ],
)
def test_malformed_registries_are_refused(bad):
    with pytest.raises(ValueError):
        mig.validate(bad)


# ── stamping ──────────────────────────────────────────────────────────────────────────────


def test_fresh_datastore_is_stamped_at_the_code_format():
    _fresh_init()
    stamp = _stamp()
    assert stamp is not None
    assert stamp.data_format == fmt.CODE_DATA_FORMAT
    assert stamp.adopted_from == "fresh"
    assert stamp.instance_id and stamp.created_with == fmt.code_version()
    kinds = [h["kind"] for h in upgrade.history()]
    assert kinds == ["create"]


def test_existing_unstamped_data_is_adopted_at_the_baseline(monkeypatch):
    _fresh_init()
    with pdb.get_db() as conn:  # simulate pre-0128 data: records exist, no stamp
        conn.execute("DROP TABLE platform_meta")
        conn.execute("DROP TABLE platform_upgrades")
        conn.execute("INSERT INTO audit_events (source, actor, action) VALUES ('t', 'u', 'legacy')")
    _fresh_init()
    stamp = _stamp()
    assert stamp.adopted_from == "legacy" and stamp.data_format == fmt.BASELINE_FORMAT
    assert upgrade.history()[-1]["kind"] == "adopt"


def test_restamping_is_idempotent_and_keeps_the_instance_id():
    _fresh_init()
    first = _stamp()
    _fresh_init()
    _fresh_init()
    assert _stamp().instance_id == first.instance_id
    assert len(upgrade.history()) == 1


def test_last_opened_with_follows_the_running_release(monkeypatch):
    _fresh_init()
    monkeypatch.setattr(fmt, "code_version", lambda: "9.9.9")
    _fresh_init()
    assert _stamp().last_opened_with == "9.9.9"


# ── the compatibility rule ────────────────────────────────────────────────────────────────


def test_compatibility_matrix():
    reg = _registry(_step(2, "two"), _step(3, "three", online=False))
    assert fmt.evaluate(None, None, registry=reg).status == fmt.UNSTAMPED
    assert fmt.evaluate(3, 1, registry=reg).status == fmt.CURRENT
    avail = fmt.evaluate(1, 1, registry=reg[:2])
    assert avail.status == fmt.UPGRADE_AVAILABLE and avail.ok
    req = fmt.evaluate(1, 1, registry=reg)
    assert req.status == fmt.UPGRADE_REQUIRED and req.ok
    assert [p["name"] for p in req.pending] == ["two", "three"]
    newer = fmt.evaluate(5, 2, registry=reg)  # older code, additive newer data: rollback window
    assert newer.status == fmt.NEWER_COMPATIBLE and newer.ok
    too_new = fmt.evaluate(5, 4, registry=reg)  # a breaking change this code predates
    assert too_new.status == fmt.TOO_NEW and not too_new.ok


def test_init_db_refuses_data_a_newer_release_made_unreadable(monkeypatch):
    _fresh_init()
    with pdb.get_db() as conn:
        conn.execute("UPDATE platform_meta SET value='99' WHERE key IN "
                     "('data_format','min_reader_format')")  # fmt: skip
    pdb._INITIALIZED_PATHS.clear()
    with pytest.raises(fmt.IncompatibleDataError, match="newer ExaMLOps"):
        pdb.init_db()
    # stays refused: the path is not cached as ready
    with pytest.raises(fmt.IncompatibleDataError):
        pdb.init_db()
    monkeypatch.setenv(fmt.ALLOW_INCOMPATIBLE_ENV, "1")
    pdb.init_db()  # explicit override opens it


def test_newer_but_compatible_data_opens_for_a_rollback():
    _fresh_init()
    with pdb.get_db() as conn:
        conn.execute("UPDATE platform_meta SET value='7' WHERE key='data_format'")
    _fresh_init()  # no raise: min_reader_format is still 1
    assert fmt.evaluate_stamp(_stamp()).status == fmt.NEWER_COMPATIBLE


# ── migrations ────────────────────────────────────────────────────────────────────────────


def test_online_migrations_apply_on_open_exactly_once(monkeypatch):
    _fresh_init()
    calls: list[str] = []
    monkeypatch.setattr(mig, "MIGRATIONS", _registry(_step(2, "two", calls=calls)))
    _fresh_init()
    _fresh_init()
    assert calls == ["two"]
    assert _stamp().data_format == 2
    assert upgrade.history()[0]["migration"] == "two"
    assert upgrade.history()[0]["kind"] == "online"


def test_offline_migration_waits_for_upgrade_apply_which_backs_up_first(monkeypatch, tmp_path):
    _fresh_init()
    calls: list[str] = []
    monkeypatch.setattr(
        mig,
        "MIGRATIONS",
        _registry(
            _step(2, "two", calls=calls),
            _step(3, "breaking-three", online=False, breaking=True, calls=calls),
        ),
    )
    _fresh_init()
    assert calls == ["two"]  # the online step ran, the offline one waits
    assert fmt.evaluate_stamp(_stamp()).status == fmt.UPGRADE_REQUIRED
    plan = upgrade.plan()
    assert plan["ready"] and [p["name"] for p in plan["pending"]] == ["breaking-three"]

    assert upgrade.apply(dry_run=True)["applied"] == []
    assert calls == ["two"]

    res = upgrade.apply(backup_dir=str(tmp_path / "bk"))
    assert res["ok"] and res["applied"] == ["breaking-three"]
    assert res["backup"] and Path(res["backup"]["bundle"]).is_dir()
    stamp = _stamp()
    assert stamp.data_format == 3 and stamp.min_reader_format == 3  # breaking raised the floor
    last = upgrade.history()[0]
    assert last["kind"] == "upgrade" and last["backup_id"] == Path(res["backup"]["bundle"]).name


def test_upgrade_refuses_data_it_must_not_read(monkeypatch, tmp_path):
    _fresh_init()
    with pdb.get_db() as conn:
        conn.execute("UPDATE platform_meta SET value='50' WHERE key IN "
                     "('data_format','min_reader_format')")  # fmt: skip
    p = upgrade.plan()
    assert not p["ready"] and p["compatibility"]["status"] == fmt.TOO_NEW
    res = upgrade.apply(backup_dir=str(tmp_path / "bk"))
    assert not res["ok"] and res["applied"] == []
    assert not (tmp_path / "bk").exists()  # refused before touching anything


# ── backup / restore compatibility ────────────────────────────────────────────────────────


def test_bundle_manifest_carries_the_stamp_and_restore_checks_it(tmp_path):
    from examlops.backup import create_bundle, restore_bundle

    _fresh_init()
    res = create_bundle(str(tmp_path / "bk"), tiers=["sqlite"])
    manifest = res.manifest
    stamp = _stamp()
    assert manifest["data_format"] == stamp.data_format
    assert manifest["instance_id"] == stamp.instance_id

    out = restore_bundle(res.bundle_dir, tiers=["sqlite"], force=True)
    assert out["compatibility"]["ok"] is True

    # A bundle from a release this code cannot read is refused before anything is restored.
    mp = Path(res.bundle_dir) / "bundle.manifest.json"
    data = json.loads(mp.read_text())
    data["data_format"] = data["min_reader_format"] = 999
    mp.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="incompatible bundle"):
        restore_bundle(res.bundle_dir, tiers=["sqlite"], force=True)


def test_pre_stamp_bundles_are_the_baseline_and_restorable():
    assert fmt.evaluate_manifest({}).ok
    assert fmt.evaluate_manifest({"examlops_version": "0.40.0"}).status in (
        fmt.CURRENT,
        fmt.UPGRADE_AVAILABLE,
        fmt.UPGRADE_REQUIRED,
    )
