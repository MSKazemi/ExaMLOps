"""``exa instance`` — this install's three layers: core · deployment · instance data (ADR 0128)."""

from __future__ import annotations

import os
import platform as _py_platform
from pathlib import Path
from typing import Any

import typer

from examlops.cli import _output

app = typer.Typer(
    no_args_is_help=True,
    help="This install's layers — core, deployment and instance data (ADR 0128)",
)

_EX_INFO = (
    "Examples:\n\n"
    "  [dim]# Where the code, the deployment and every piece of user data are[/dim]\n"
    "  exa instance info\n\n"
    "  exa --json instance info"
)
_EX_CHECK = (
    "Examples:\n\n"
    "  [dim]# Pre-flight before and after installing a new release (exit 1 on a problem)[/dim]\n"
    "  exa instance check\n\n"
    "  exa --json instance check"
)
_EX_INIT = (
    "Examples:\n\n"
    "  [dim]# Create a data root, seed it with a use-case pack, pick a site preset[/dim]\n"
    "  exa instance init --data-dir /srv/examlops-data --pack usecases/reference "
    "--preset standard\n\n"
    "  [dim]# Then make every process use it[/dim]\n"
    "  export EXAMLOPS_DATA_DIR=/srv/examlops-data"
)


def _install_mode() -> str:
    """How the core is installed — asked of the package metadata, never of the directory tree.

    PEP 610's ``direct_url.json`` says whether the distribution is an editable (source) install;
    anything else is a built package. No path walking, so a wheel with no checkout around it
    answers the same way as a developer's tree.
    """
    import json
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        dist = distribution("examlops")
    except PackageNotFoundError:
        return "not installed (running from a source tree on sys.path)"
    raw = dist.read_text("direct_url.json")
    info = json.loads(raw) if raw else {}
    if info.get("dir_info", {}).get("editable"):
        return f"editable source install ({info.get('url', '').removeprefix('file://')})"
    return f"installed package {dist.version}"


def _core() -> dict[str, Any]:
    from examlops.lifecycle import dataformat

    return {
        "version": dataformat.code_version(),
        "data_format": dataformat.CODE_DATA_FORMAT,
        "install": _install_mode(),
        "python": _py_platform.python_version(),
    }


def _deployment() -> dict[str, Any]:
    from examlops.lifecycle.datadir import deployment_kind

    out: dict[str, Any] = {"kind": deployment_kind()}
    for key, var in (
        ("image_tag", "EXAMLOPS_IMAGE_TAG"),
        ("compose_project", "COMPOSE_PROJECT_NAME"),
        ("kubernetes_namespace", "POD_NAMESPACE"),
        ("datastore_engine", "EXAMLOPS_DB_BACKEND"),
    ):
        if value := os.getenv(var):
            out[key] = value
    out.setdefault("datastore_engine", "sqlite")
    return out


def _stamp_and_compat() -> tuple[dict[str, Any] | None, dict[str, Any]]:
    from examlops.lifecycle import dataformat
    from examlops.lifecycle.upgrade import _read_stamp

    stamp = _read_stamp()
    return (stamp.to_dict() if stamp else None, dataformat.evaluate_stamp(stamp).to_dict())


def _info() -> dict[str, Any]:
    from examlops.lifecycle import datadir, modules

    stamp, compat = _stamp_and_compat()
    profile = modules.resolve()
    return {
        "core": _core(),
        "deployment": _deployment(),
        "data": {
            "root": str(datadir.data_root()) if datadir.data_root() else None,
            "stamp": stamp,
            "compatibility": compat,
            "locations": [loc.to_dict() for loc in datadir.inventory()],
        },
        "modules": {
            "preset": profile.preset,
            "enabled": profile.enabled_ids(),
            "disabled": profile.disabled_ids(),
            "sources": profile.sources,
            "warnings": profile.warnings,
        },
    }


def _human_size(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@app.command("info", epilog=_EX_INFO)
def info() -> None:
    """Show the three layers of this install: core, deployment, and where all user data lives."""
    data = _info()
    if _output.json_mode:
        _output.print_json(data)
        return
    _output.print_record({"layer": "core", **data["core"]})
    _output.print_record({"layer": "deployment", **data["deployment"]})
    d = data["data"]
    compat = d["compatibility"]
    stamp = d["stamp"] or {}
    _output.print_record(
        {
            "layer": "instance data",
            "data root": d["root"]
            or "(not set — legacy per-store locations; see EXAMLOPS_DATA_DIR)",
            "instance id": stamp.get("instance_id", "—"),
            "data format": stamp.get("data_format", "—"),
            "created with": stamp.get("created_with", "—"),
            "compatibility": f"{compat['status']} — {compat['message']}",
        }
    )
    rows = [
        [
            loc["name"],
            loc["where"],
            loc["source"],
            "✓" if loc["exists"] else "·",
            _human_size(loc["size_bytes"]),
            loc["backed_up_by"],
        ]
        for loc in d["locations"]
    ]
    _output.print_table(
        "Instance data — every place user data lives",
        ["What", "Where", "Set by", "Exists", "Size", "Backed up by"],
        rows,
    )
    m = data["modules"]
    _output.print_record(
        {
            "site profile": f"preset {m['preset']} ({', '.join(m['sources'])})",
            "modules on": ", ".join(m["enabled"]),
            "modules off": ", ".join(m["disabled"]) or "—",
        }
    )
    for w in m["warnings"]:
        _output.warning(w)


def _checks() -> list[dict[str, Any]]:
    from examlops.lifecycle import datadir, modules

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    _stamp, compat = _stamp_and_compat()
    add(
        "data compatible with this release",
        bool(compat["ok"]) and compat["status"] != "upgrade_required",
        f"{compat['status']}: {compat['message']}"
        + (f" → {compat['action']}" if compat.get("action") else ""),
    )

    root = datadir.data_root()
    if root is None:
        add("data root", True, "not set — legacy per-store locations (EXAMLOPS_DATA_DIR)")
    elif not root.is_dir():
        add("data root", False, f"{root} does not exist → exa instance init --data-dir {root}")
    else:
        writable = os.access(root, os.W_OK)
        add("data root", writable, f"{root} ({'writable' if writable else 'NOT writable'})")

    profile = modules.resolve()
    add(
        "site profile",
        not profile.warnings,
        "; ".join(profile.warnings) or f"preset {profile.preset}, {len(profile.enabled_ids())} on",
    )

    pack, source = datadir.usecase_pack_dir()
    if pack is None:
        add("use-case pack", False, "no pack could be resolved")
    else:
        add("use-case pack", pack.is_dir(), f"{pack} ({source})")
        verdict = _pack_requirement(pack)
        if verdict is not None:
            add("use-case pack supports this release", verdict[0], verdict[1])
    return checks


def _pack_requirement(pack: Path) -> tuple[bool, str] | None:
    """Check a pack's optional ``[pack] requires_examlops = "<specifier>"`` against this release."""
    import tomllib

    toml = pack / "pack.toml"
    if not toml.is_file():
        return None
    try:
        spec = tomllib.loads(toml.read_text()).get("pack", {}).get("requires_examlops")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return False, f"unreadable pack.toml: {exc}"
    if not spec:
        return None
    from examlops.lifecycle.dataformat import code_version

    version = code_version()
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        ok = Version(version) in SpecifierSet(str(spec))
    except Exception as exc:  # noqa: BLE001 — a dev build or an odd specifier is not a failure
        return True, f"requires {spec}; could not compare with {version} ({exc})"
    return ok, f"pack requires examlops {spec}; this is {version}"


@app.command("check", epilog=_EX_CHECK)
def check() -> None:
    """Pre-flight the install: data compatibility, data root, site profile, use-case pack."""
    checks = _checks()
    failed = [c for c in checks if not c["ok"]]
    if _output.json_mode:
        _output.print_json({"ok": not failed, "checks": checks})
    else:
        _output.print_table(
            "Instance check",
            ["Check", "Result", "Detail"],
            [[c["check"], "✓" if c["ok"] else "✗", c["detail"]] for c in checks],
        )
    if failed:
        raise typer.Exit(1)


@app.command("init", epilog=_EX_INIT)
def init(
    data_dir: str = typer.Option(
        "",
        "--data-dir",
        help="Data root to create (default: $EXAMLOPS_DATA_DIR)",
    ),
    pack: str = typer.Option("", "--pack", help="Use-case pack to copy into <data root>/usecase"),
    preset: str = typer.Option("", "--preset", help="Site preset to record in site.toml"),
    site_name: str = typer.Option("", "--site-name", help="A label for this centre"),
    overwrite_pack: bool = typer.Option(
        False, "--overwrite-pack", help="Replace an existing <data root>/usecase"
    ),
) -> None:
    """Create an instance-data root: layout, site profile, use-case pack, stamped datastore."""
    from examlops.lifecycle import datadir, modules

    root_raw = data_dir or os.getenv(datadir.DATA_DIR_ENV, "")
    if not root_raw:
        _output.error(
            "No data root given.",
            hint="exa instance init --data-dir /srv/examlops-data   (or set EXAMLOPS_DATA_DIR)",
        )
    root = Path(root_raw).expanduser().resolve()
    # The rest of this process (site profile path, datastore path) must see the new root.
    os.environ[datadir.DATA_DIR_ENV] = str(root)
    try:
        result = datadir.init_layout(
            root, pack=Path(pack) if pack else None, overwrite_pack=overwrite_pack
        )
        if preset or site_name or not modules.site_profile_path()[0].exists():
            modules.set_preset(preset or modules.DEFAULT_PRESET, site_name=site_name or None)
    except (ValueError, KeyError) as exc:
        _output.error(str(exc))
    from examlops.data import init_db

    init_db(force=True)
    from examlops.lifecycle.upgrade import _read_stamp

    stamp = _read_stamp()
    result.update(
        {
            "site_profile": str(modules.site_profile_path()[0]),
            "stamp": stamp.to_dict() if stamp else None,
            "env": {datadir.DATA_DIR_ENV: str(root)},
        }
    )
    if _output.json_mode:
        _output.print_json(result)
        return
    _output.ok(f"Instance data root ready at {root}")
    for rel in result["created"]:
        _output.detail(f"created {rel}")
    if result["pack_seeded_from"]:
        _output.ok(f"Use-case pack copied from {result['pack_seeded_from']}")
    if stamp:
        _output.ok(f"Datastore stamped: format {stamp.data_format}, instance {stamp.instance_id}")
    _output.hint(f"export {datadir.DATA_DIR_ENV}={root}   (and set it on every service)")
    _output.hint("exa instance info   ·   exa modules list")
