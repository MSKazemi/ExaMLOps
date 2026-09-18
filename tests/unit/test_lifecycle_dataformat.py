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


def _probe_rows() -> list[str]:
    with pdb.get_db() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT key FROM platform_meta WHERE key LIKE 'test-probe:%' ORDER BY key"
            ).fetchall()
        ]


def test_a_migration_that_dies_partway_leaves_nothing_behind(monkeypatch, tmp_path):
    """The question an operator asks before upgrading production data, answered on both engines.

    `exa upgrade apply` rewrites data in place. If a migration dies on row 10,000 — a bad row, a
    lost connection, a killed pod — the operator needs to know whether the datastore is now half
    old and half new, because that state is not something a restore-less site can reason its way
    out of. The answer must be *nothing was left behind*, and it must be a fact rather than an
    accident: nothing pinned it before this test, so the day someone gave the migration loop its
    own autocommitting connection, the guarantee would have gone away in silence.

    Paired against a **succeeding** migration on purpose. Asserting only that the failed one wrote
    nothing would pass just as well if migrations could never write anything at all — the failure
    mode every scan and drill in this repository is written against.
    """
    _fresh_init()

    def half(conn):
        conn.execute(
            "INSERT INTO platform_meta (key, value, updated_at) "
            "VALUES ('test-probe:half', 'written', 'now') ON CONFLICT(key) DO NOTHING"
        )
        raise RuntimeError("the migration died on row 10000")

    # 1. The control: a migration that finishes leaves its row behind.
    monkeypatch.setattr(mig, "MIGRATIONS", _registry(_step(2, "whole")))
    _fresh_init()
    assert _probe_rows() == ["test-probe:whole"], (
        "a succeeding migration wrote nothing, so the failure half of this test would pass over a "
        "migration mechanism that cannot write at all"
    )
    assert _stamp().data_format == 2

    # 2. The subject: one that raises leaves neither its row nor a moved stamp.
    monkeypatch.setattr(
        mig,
        "MIGRATIONS",
        _registry(
            _step(2, "whole"),
            mig.Migration(3, "half", "dies partway", half, online=False, breaking=True),
        ),
    )
    _fresh_init()
    with pytest.raises(RuntimeError, match="row 10000"):
        upgrade.apply(backup=False)

    assert _probe_rows() == ["test-probe:whole"], (
        "the dead migration's partial write survived: the datastore is now half migrated, which "
        "is the one outcome an operator cannot recover from without the backup"
    )
    stamp = _stamp()
    assert stamp.data_format == 2, "the stamp advanced past a migration that did not finish"
    assert stamp.min_reader_format == 1, (
        "the dead step was the breaking one, and it raised the reader floor anyway — every older "
        "release would now refuse data that was never actually migrated"
    )
    assert [p["name"] for p in upgrade.plan()["pending"]] == ["half"], (
        "the migration must still be pending, so running the upgrade again retries it"
    )


def test_an_upgrade_that_moved_nothing_does_not_report_success(monkeypatch, tmp_path):
    """`ok` is measured, not asserted.

    It was the literal `True`, so "every migration ran" and "the compare-and-set lost, so none of
    them did" were the same answer — and the CLI prints an empty `applied` list as *none pending*,
    making an upgrade that moved nothing read exactly like an instance that had nothing to move.
    """
    _fresh_init()

    def already_done(conn):
        """Stands in for another process finishing first: the stamp has moved on before our
        compare-and-set runs, so this process applies nothing and `applied` comes back empty."""
        conn.execute("UPDATE platform_meta SET value='2' WHERE key='data_format'")

    monkeypatch.setattr(
        mig,
        "MIGRATIONS",
        _registry(mig.Migration(2, "offline-two", "needs apply", already_done, online=False)),
    )
    _fresh_init()

    res = upgrade.apply(backup=False)
    assert res["applied"] == [], "the compare-and-set should have found the stamp already moved"
    assert res["pending_after"] == [], "nothing is pending: the data really is at format 2"
    assert res["ok"] is True, (
        "`ok` must come from the data, not from `applied` — this process applied nothing and the "
        "instance is nonetheless fully upgraded"
    )
    assert _stamp().data_format == 2


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


# ── the pre-upgrade backup has to be a rollback point, not just a directory ───


def _backup_returning(status: str, monkeypatch, tmp_path):
    """Make `create_bundle` answer with `status` and record that it was called."""
    from examlops import backup as _backup

    made: list[str] = []

    class _Res:
        def __init__(self) -> None:
            self.bundle_dir = str(tmp_path / "bk" / "bundle")
            self.overall_status = status
            # A manifest consistent with the status: an `ok` bundle really did capture the
            # platform tier, and a `skipped` one captured nothing. The first version of this
            # stub claimed `ok` with an empty manifest, which no real bundle does — and the
            # inconsistency only surfaced once the code started asking what the bundle holds.
            captured = status in ("ok", "partial")
            self.manifest = {
                "tiers": {
                    "sqlite": {
                        "status": status,
                        "items": [{"name": "platform", "status": "ok"}] if captured else [],
                    }
                }
            }

    def _fake(out_dir, **kw):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        made.append(status)
        return _Res()

    monkeypatch.setattr(_backup, "create_bundle", _fake)
    return made


def _one_breaking_migration(monkeypatch, calls):
    monkeypatch.setattr(
        mig,
        "MIGRATIONS",
        _registry(_step(2, "breaking-two", online=False, breaking=True, calls=calls)),
    )


def test_an_upgrade_refuses_a_backup_that_captured_nothing(monkeypatch, tmp_path):
    """`apply` refused only `overall_status == "failed"`.

    A bundle whose every requested tier was skipped reports **`skipped`**, not `failed` — it is a
    directory with a manifest and no data in it. Proceeding on that migrates the instance's data
    with no rollback point at all, which is the one thing the backup step exists to prevent. The
    same distinction iteration P8.21 drew for restore: a tier that captured nothing is not a tier
    you can restore.
    """
    _fresh_init()
    calls: list[str] = []
    _one_breaking_migration(monkeypatch, calls)
    _fresh_init()
    _backup_returning("skipped", monkeypatch, tmp_path)

    res = upgrade.apply(backup_dir=str(tmp_path / "bk"))
    assert res["ok"] is False, "the upgrade proceeded on a backup that captured nothing"
    assert res["applied"] == []
    assert calls == [], "the migration ran despite having no rollback point"


def test_an_upgrade_proceeds_on_a_backup_that_captured_something(monkeypatch, tmp_path):
    """Anti-vacuity: an `ok` bundle must still let the upgrade run."""
    _fresh_init()
    calls: list[str] = []
    _one_breaking_migration(monkeypatch, calls)
    _fresh_init()
    _backup_returning("ok", monkeypatch, tmp_path)

    res = upgrade.apply(backup_dir=str(tmp_path / "bk"))
    assert res["ok"] and res["applied"] == ["breaking-two"]
    assert calls == ["breaking-two"]


def test_the_refusal_says_why(monkeypatch, tmp_path):
    """An operator blocked here must be told what to fix, not just that it stopped."""
    _fresh_init()
    calls: list[str] = []
    _one_breaking_migration(monkeypatch, calls)
    _fresh_init()
    _backup_returning("skipped", monkeypatch, tmp_path)

    res = upgrade.apply(backup_dir=str(tmp_path / "bk"))
    assert res.get("reason"), f"the refusal carries no explanation: {res}"
    assert "backup" in res["reason"].lower()


def test_the_operator_is_told_why_the_upgrade_refused(monkeypatch, tmp_path):
    """The reason must reach the person, not only the payload.

    The CLI's failure branch is keyed on `pending_after`, which a pre-flight refusal does not set —
    so the run printed a green `Pre-upgrade backup: … (skipped)` and exited 1 with nothing said.
    """
    from typer.testing import CliRunner

    from examlops.cli.main import app

    _fresh_init()
    calls: list[str] = []
    _one_breaking_migration(monkeypatch, calls)
    _fresh_init()
    _backup_returning("skipped", monkeypatch, tmp_path)

    result = CliRunner().invoke(
        app, ["--yes", "upgrade", "apply", "--backup-dir", str(tmp_path / "bk")]
    )
    assert result.exit_code == 1, result.output
    assert "rollback point" in result.output or "captured nothing" in result.output, (
        f"the operator was stopped with no explanation:\n{result.output}"
    )
    assert calls == []


def test_the_pre_upgrade_backup_covers_the_tier_that_holds_platform_state(monkeypatch):
    """On Postgres the default bundle captured none of the data it was about to migrate.

    `apply()` defaulted to `tiers=["sqlite", "config"]`. Under `EXAMLOPS_DB_BACKEND=postgres` the
    sqlite tier skips the platform DB **on purpose** — its state is in Postgres, dumped by the
    `postgres` tier, which that default never requested. The bundle then reported `partial` (config
    captured), which the previous fix deliberately allowed through, so the breaking migration ran
    against Postgres with a rollback point holding none of the migrated data.

    The sqlite tier's own comment states the principle this violated: "Backing up the leftover file
    here would produce a bundle that looks complete and restores nothing."

    Checked on the pure selector, so it needs no reachable Postgres.
    """
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "postgres")
    monkeypatch.setenv("EXAMLOPS_POSTGRES_DSN", "postgresql://u@h/db")
    tiers = upgrade.default_backup_tiers()
    assert "postgres" in tiers, f"the pre-upgrade bundle would ask for {tiers}"
    assert "config" in tiers


def test_the_default_tiers_are_sqlite_on_the_sqlite_engine(monkeypatch):
    """Anti-vacuity: the tier list follows the engine rather than always holding everything."""
    monkeypatch.delenv("EXAMLOPS_DB_BACKEND", raising=False)
    tiers = upgrade.default_backup_tiers()
    assert "sqlite" in tiers and "config" in tiers
    assert "postgres" not in tiers, "asking for a tier this engine keeps no state in"


def test_an_explicit_tier_list_is_still_honoured(monkeypatch, tmp_path):
    """`--tier` is the operator's override and must not be silently widened."""
    requested: list[list[str]] = []

    from examlops import backup as _backup

    class _Res:
        bundle_dir = str(tmp_path / "bk" / "b")
        overall_status = "ok"
        manifest = {"tiers": {}}

    def _fake(out_dir, **kw):
        requested.append(list(kw.get("tiers") or []))
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        return _Res()

    monkeypatch.setattr(_backup, "create_bundle", _fake)
    _fresh_init()
    calls: list[str] = []
    _one_breaking_migration(monkeypatch, calls)
    _fresh_init()
    upgrade.apply(backup_dir=str(tmp_path / "bk"), tiers=["config"])
    assert requested[0] == ["config"]


def _bundle_with_manifest(manifest, status, monkeypatch, tmp_path):
    from examlops import backup as _backup

    class _Res:
        bundle_dir = str(tmp_path / "bk" / "b")
        overall_status = status

        def __init__(self) -> None:
            self.manifest = manifest

    def _fake(out_dir, **kw):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        return _Res()

    monkeypatch.setattr(_backup, "create_bundle", _fake)


def test_a_partial_bundle_missing_the_platform_tier_is_refused(monkeypatch, tmp_path):
    """Asking for the right tier is not the same as getting it.

    A `postgres` tier requested but skipped — `pg_dump` absent, the server unreachable — still
    leaves `config` captured, so the bundle is `partial` and the previous fix let it through. The
    guarantee has to be about what the bundle *holds*, not what was asked for.
    """
    _fresh_init()
    calls: list[str] = []
    _one_breaking_migration(monkeypatch, calls)
    _fresh_init()
    _bundle_with_manifest(
        {
            "tiers": {
                "sqlite": {
                    "status": "skipped",
                    "items": [{"name": "platform", "status": "skipped"}],
                },
                "config": {"status": "ok", "items": [{"name": "config.toml", "status": "ok"}]},
            }
        },
        "partial",
        monkeypatch,
        tmp_path,
    )

    res = upgrade.apply(backup_dir=str(tmp_path / "bk"))
    assert res["ok"] is False, "migrated with a bundle holding none of the platform state"
    assert calls == []
    assert "platform state" in (res.get("reason") or "").lower()


def test_a_partial_bundle_that_captured_the_platform_tier_proceeds(monkeypatch, tmp_path):
    """Anti-vacuity, and the case that must not be blocked: config empty, platform captured."""
    _fresh_init()
    calls: list[str] = []
    _one_breaking_migration(monkeypatch, calls)
    _fresh_init()
    _bundle_with_manifest(
        {
            "tiers": {
                "sqlite": {"status": "ok", "items": [{"name": "platform", "status": "ok"}]},
                "config": {"status": "skipped", "items": []},
            }
        },
        "partial",
        monkeypatch,
        tmp_path,
    )

    res = upgrade.apply(backup_dir=str(tmp_path / "bk"))
    assert res["ok"] and res["applied"] == ["breaking-two"]


def test_the_capture_check_reads_a_real_manifest_not_just_a_stub(tmp_path, monkeypatch):
    """Every other test here hands `_captured` a manifest I wrote. This one hands it a real one.

    A predicate that agrees with my own fixtures and disagrees with the shape `create_bundle`
    actually produces would refuse every upgrade — a false refusal is worse than the bug it
    replaced, because it blocks the operator entirely. So the shape is checked against a bundle the
    real code built.
    """
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    _fresh_init()

    from examlops.backup import create_bundle

    res = create_bundle(str(tmp_path / "bk"), tiers=["sqlite", "config"])
    assert upgrade._captured(res.manifest, "sqlite") is True, (
        f"the predicate does not recognise a real bundle's sqlite tier: "
        f"{list((res.manifest.get('tiers') or {}).get('sqlite', {}).keys())}"
    )
    # And it must say no for a tier the bundle genuinely holds nothing for.
    assert upgrade._captured(res.manifest, "objects") is False
    assert upgrade._captured(res.manifest, "not-a-tier") is False
