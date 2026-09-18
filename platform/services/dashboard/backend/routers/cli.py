"""CLI Console (ADR 0119) — every `exa` command, runnable from the dashboard.

The bespoke consoles cover the hot paths with purpose-built UI; this router covers *everything*,
so the dashboard can do whatever the CLI can and cannot fall behind it. It never re-implements a
command: it runs the real CLI (``cli_runner``) with arguments validated by the platform's own
surface table (``examlops.cli.surface``), which is the single place that decides, per command,
who may run it.

Reads (``cli.run``, every signed-in user): the catalog, and ``read``-tier commands.
Writes (``cli.write``, admin): ``admin``-tier commands — and a ``read`` command that the supplied
arguments turn into a write (``--record``, a file path, a network target) — plus
``destructive``-tier commands, which additionally need the command path typed back as
confirmation. ``cli_only`` commands are listed with the reason they cannot run from a browser.

Every accepted run is audited twice in the platform's hash chain: ``cli_run`` (who ran what, with
secrets masked) and ``cli_run_finished`` (the outcome).

The whole surface sits behind the ``cliConsole`` feature flag (F25), enforced here: switched off,
every endpoint but cancel answers 403.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import audit_write
import cli_runner
import feature_flags
from auth import require_role
from capabilities import CLI_RUN, CLI_WRITE, can, deny_reason
from dbconn import platform_db_path
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, Response

logger = logging.getLogger("dashboard.cli")

router = APIRouter(prefix="/v1/cli", tags=["cli"])
_viewer = require_role("viewer")
FLAG = "cliConsole"


async def _console(principal: dict = Depends(_viewer)) -> dict:
    """The signed-in caller, provided the ``cliConsole`` kill switch is on for them.

    Enforced here rather than only by hiding the page: a deployment that switches the console off
    must not still accept runs from anyone who knows the URL. Cancelling a run deliberately skips
    this gate — turning the switch off must never stop an operator from stopping a run.
    """
    enabled = await asyncio.to_thread(
        feature_flags.is_enabled,
        platform_db_path(),
        FLAG,
        role=principal.get("role", ""),
        tenant=principal.get("tenant", "default"),
        subject=principal.get("sub", principal.get("role", "anonymous")),
    )
    if not enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"the CLI Console is switched off in this deployment (feature flag {FLAG!r})",
        )
    return principal


_FORMATS = {"json": "json", "text": "table"}
_CATALOG_LOCK = asyncio.Lock()
_CATALOG: dict[str, Any] = {"at": 0.0, "data": None, "raw": b"", "gz": b"", "by_path": {}}


def _surface():
    """Lazy, guarded import of the shared surface table (503 if the platform package is absent)."""
    try:
        from examlops.cli import surface  # type: ignore

        return surface
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the CLI Console requires the examlops package (not available in this deployment)",
        ) from exc


def _actor(principal: dict) -> str:
    return f"dashboard:{principal.get('sub', principal.get('role', '?'))}"


def _catalog_ttl() -> float:
    try:
        return max(0.0, float(os.getenv("EXAMLOPS_DASHBOARD_CLI_CATALOG_TTL", "300")))
    except ValueError:
        return 300.0


async def _load_catalog() -> dict[str, Any]:
    """The command catalog, built in a subprocess from the live CLI and cached for a TTL.

    A subprocess (``python -m examlops.cli.surface``) rather than an import: the dashboard never
    loads the ~80 command modules into its own process, and after a ``git pull`` on the
    bind-mounted repo the catalog describes the same code the next run will execute.
    """
    now = time.monotonic()
    if _CATALOG["data"] is not None and now - _CATALOG["at"] < _catalog_ttl():
        return _CATALOG
    async with _CATALOG_LOCK:
        if _CATALOG["data"] is not None and time.monotonic() - _CATALOG["at"] < _catalog_ttl():
            return _CATALOG
        _surface()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "examlops.cli.surface",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=cli_runner.child_env("dashboard:catalog"),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except TimeoutError as exc:
            proc.kill()
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "catalog build timed out"
            ) from exc
        if proc.returncode != 0:
            logger.error("CLI catalog build failed: %s", err.decode(errors="replace")[-2000:])
            if _CATALOG["data"] is not None:
                return _CATALOG  # serve the last good catalog rather than nothing
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "could not build the CLI command catalog"
            )
        data = json.loads(out)
        raw = json.dumps(data, separators=(",", ":")).encode()
        _CATALOG.update(
            at=time.monotonic(),
            data=data,
            raw=raw,
            gz=gzip.compress(raw, compresslevel=6),
            by_path={c["path"]: c for c in data["commands"]},
        )
        return _CATALOG


def _audit(actor: str, action: str, target: str, details: dict[str, Any]) -> None:
    """Append to the audit chain on its own locked transaction.

    Deliberately *no* ``conn``: passing one is for a router that has just written the thing it
    audits and already holds the write lock. This router writes nothing itself, so a fresh
    connection would be idle — and concurrent runs would each read the same chain head and fork
    the chain (seen: five ``cli_run`` rows sharing one ``prev_hash``). The standalone path takes
    an IMMEDIATE lock across head-read + append and retries on contention.
    """
    audit_write.audit(actor, action, target, details)


async def _audit_async(actor: str, action: str, target: str, details: dict[str, Any]) -> None:
    await asyncio.to_thread(_audit, actor, action, target, details)


def _visible(run: cli_runner.Run, principal: dict) -> bool:
    return principal.get("role") == "admin" or run.actor == _actor(principal)


def _require(
    principal: dict, capability: str, why: str = "", request: Request | None = None
) -> None:
    """Refuse the role that lacks `capability`, then run the enforcing path for it.

    Every other router names its capability in a route-level
    `Depends(require_capability(...))`. This one cannot: the capability is chosen **per request**
    from the tier of the command being run — `cli.run` for a read, `cli.write` for one that
    changes platform state — and that is only known after the argv has been built. So the
    enforcement `require_capability` would have done happens here instead, once the answer exists.
    Without it a federated caller's centre saw only the coarse `api.write` for `/api/v1/cli/run`,
    whatever `exa` command was inside it.
    """
    role = principal.get("role", "")
    if not can(role, capability):
        detail = deny_reason(role, capability)
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{why} {detail}".strip())
    from iam_gate import enforce  # noqa: PLC0415 — keeps this module import-light

    enforce(capability, principal, request)


# ── catalog ───────────────────────────────────────────────────────────────────────────────


@router.get("/catalog")
async def get_catalog(request: Request, principal: dict = Depends(_console)) -> Response:
    """Every `exa` leaf command with its params, examples, panel and tier (gzip when accepted)."""
    _require(principal, CLI_RUN, request=request)
    cat = await _load_catalog()
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(
            cat["gz"],
            media_type="application/json",
            headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
        )
    return Response(cat["raw"], media_type="application/json", headers={"Vary": "Accept-Encoding"})


# ── runs ──────────────────────────────────────────────────────────────────────────────────


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    request: Request, payload: dict = Body(...), principal: dict = Depends(_console)
) -> dict:
    """Run one `exa` command. Body: ``{command, args?, format?: json|text, context?, confirm?}``.

    Returns the run record at once (status ``running``); poll ``GET /runs/{id}`` for the output.
    """
    surface = _surface()
    command = str(payload.get("command") or "").strip()
    args = payload.get("args") or {}
    fmt = str(payload.get("format") or "json")
    context = str(payload.get("context") or "").strip()
    if not isinstance(args, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "args must be an object")
    if fmt not in _FORMATS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "format must be 'json' or 'text'")

    cat = await _load_catalog()
    descriptor = cat["by_path"].get(command)
    if descriptor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such command: exa {command}")

    workspace = cli_runner.workspace_root()
    try:
        if context:
            surface.valid_context(context)
        invocation = surface.build_argv(descriptor, args, workspace=workspace)
    except surface.SurfaceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    tier = invocation.tier
    if tier == surface.READ:
        _require(principal, CLI_RUN, request=request)
    else:
        escalated = tier != descriptor["tier"]
        why = (
            f"`exa {command}` with these arguments changes platform state or reaches outside "
            "the dashboard."
            if escalated
            else f"`exa {command}` is an {tier} command."
        )
        _require(principal, CLI_WRITE, why, request=request)
    if tier == surface.DESTRUCTIVE and str(payload.get("confirm") or "").strip() != command:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"`exa {command}` is destructive — send confirm='{command}' to run it",
        )

    # A terminal user would `mkdir` an output's directory first; the console has no mkdir, so make
    # the parents here. Safe: every path was contained in the workspace by `build_argv`.
    for rel in invocation.paths:
        (workspace / rel).parent.mkdir(parents=True, exist_ok=True)

    actor = _actor(principal)
    display = cli_runner.display_command(invocation.display, fmt, context)
    await _audit_async(
        actor,
        "cli_run",
        f"exa {command}",
        {"command": display, "tier": tier, "role": principal.get("role")},
    )

    async def finished(run: cli_runner.Run) -> None:
        await _audit_async(
            actor,
            "cli_run_finished",
            f"exa {command}",
            {
                "run_id": run.id,
                "status": run.status,
                "exit_code": run.exit_code,
                "duration_ms": run.summary()["duration_ms"],
                "files": run.files[:20],
            },
        )

    try:
        run = cli_runner.RUNNER.submit(
            command=command,
            argv=invocation.argv,
            display=display,
            tier=tier,
            fmt=_FORMATS[fmt],
            context=context,
            actor=actor,
            role=str(principal.get("role", "")),
            args=invocation.redacted,
            workspace=workspace,
            on_finish=finished,
        )
    except cli_runner.Busy as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    return run.summary()


@router.get("/runs")
async def list_runs(limit: int = 50, principal: dict = Depends(_console)) -> dict:
    """Recent runs — your own, or everyone's for an admin (from the shared store, newest first)."""
    actor = None if principal.get("role") == "admin" else _actor(principal)
    runs = await cli_runner.RUNNER.alist_runs(actor=actor, limit=max(1, min(limit, 200)))
    return {"runs": [r.summary() for r in runs]}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, principal: dict = Depends(_console)) -> dict:
    run = await cli_runner.RUNNER.aget(run_id)
    if run is None or not _visible(run, principal):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
    return run.detail()


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, principal: dict = Depends(_viewer)) -> dict:
    run = await cli_runner.RUNNER.aget(run_id)
    if run is None or not _visible(run, principal):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
    cancelled = await cli_runner.RUNNER.cancel(run_id)
    return {"id": run_id, "cancelled": cancelled, "status": run.status}


# ── workspace (the only place path arguments may point) ──────────────────────────────────


def _max_upload() -> int:
    try:
        return int(os.getenv("EXAMLOPS_DASHBOARD_CLI_MAX_UPLOAD", str(25 * 1024 * 1024)))
    except ValueError:
        return 25 * 1024 * 1024


#: Most files one listing will return. A workspace with more is reported as truncated.
_LIST_LIMIT = 2000


def _workspace_path(path: str) -> tuple[Path, str]:
    surface = _surface()
    root = cli_runner.workspace_root()
    try:
        rel = surface.contain_path(path, root)
    except surface.SurfaceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return root / rel, rel


@router.get("/workspace")
async def list_workspace(request: Request, principal: dict = Depends(_console)) -> dict:
    """Files in the CLI workspace (inputs uploaded for commands, outputs they wrote)."""
    _require(principal, CLI_WRITE, "The CLI workspace is admin-only.", request=request)
    root = cli_runner.workspace_root()
    files: list[dict[str, Any]] = []
    truncated = False
    # The cap is right — this must not stream an unbounded tree — but a truncated answer has to
    # admit it is one. A full-looking list of 2000 let an operator conclude that the output a
    # command had just written was never produced.
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        if len(files) >= _LIST_LIMIT:
            truncated = True
            break
        st = p.stat()
        files.append(
            {"path": str(p.relative_to(root)), "size": st.st_size, "modified": st.st_mtime}
        )
    return {"root": "cli-workspace", "files": files, "truncated": truncated, "limit": _LIST_LIMIT}


@router.post("/workspace", status_code=status.HTTP_201_CREATED)
async def upload_workspace_file(
    request: Request,
    file: UploadFile = File(...),
    path: str = Form(""),
    overwrite: bool = Form(False),
    principal: dict = Depends(_console),
) -> dict:
    """Upload an input file (e.g. the JSONL for `exa rag ingest --docs`) into the workspace."""
    _require(principal, CLI_WRITE, "Uploading to the CLI workspace is admin-only.", request=request)
    target, rel = _workspace_path(path or (file.filename or ""))
    if target.exists() and not overwrite:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{rel} exists — set overwrite to replace")
    limit = _max_upload()
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"file exceeds {limit} bytes")
    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(target.write_bytes, data)
    await _audit_async(
        _actor(principal), "cli_workspace_upload", rel, {"size": len(data), "overwrite": overwrite}
    )
    return {"path": rel, "size": len(data)}


@router.get("/workspace/file")
async def download_workspace_file(
    request: Request, path: str, principal: dict = Depends(_console)
) -> FileResponse:
    _require(
        principal, CLI_WRITE, "Downloading from the CLI workspace is admin-only.", request=request
    )
    target, rel = _workspace_path(path)
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such file: {rel}")
    return FileResponse(str(target), filename=target.name, media_type="application/octet-stream")


@router.delete("/workspace/file")
async def delete_workspace_file(
    request: Request, path: str, principal: dict = Depends(_console)
) -> dict:
    _require(
        principal, CLI_WRITE, "Deleting from the CLI workspace is admin-only.", request=request
    )
    target, rel = _workspace_path(path)
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such file: {rel}")
    target.unlink()
    await _audit_async(_actor(principal), "cli_workspace_delete", rel, {})
    return {"path": rel, "deleted": True}
