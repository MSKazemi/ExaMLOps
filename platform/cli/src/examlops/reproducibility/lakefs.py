"""ADR 0038 clause 2 — verify and **restore** a lakeFS-pinned dataset revision.

An A1 revision of kind ``lakefs`` is a lakeFS commit id (``lakefs://<repo>/<commit>``). Before
this module a bundle pinned to one only checked that the ``dataset_revisions`` row still
existed — nothing asked lakeFS whether the commit did, and nothing could put the data back.

* :func:`verify_commit` — ``GET /api/v1/repositories/{repo}/commits/{commit}``: the commit
  must exist. An unset endpoint, an unreachable server or a non-200 answer is a *reason*, never a
  pass.
* :func:`restore` — lists every object at the commit (paged ``objects/ls``) and downloads each
  into a destination directory, checking its size and, when lakeFS reports a plain MD5 ETag, its
  MD5. Bounded by ``max_objects`` / ``max_bytes``; object paths that would escape the
  destination are refused; the destination must be empty so a restore never mixes with other
  data. A restore reads a commit, which is immutable, so it is idempotent.

Stdlib only (``urllib``): no lakeFS SDK in any manifest, and none needed for three GET calls.
Credentials come from ``EXAMLOPS_LAKEFS_ACCESS_KEY_ID`` / ``EXAMLOPS_LAKEFS_SECRET_ACCESS_KEY``
(HTTP basic, as lakeFS's API accepts); they are never logged or put in an error message.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

ENV_ENDPOINT = "EXAMLOPS_LAKEFS_ENDPOINT"
ENV_ACCESS_KEY = "EXAMLOPS_LAKEFS_ACCESS_KEY_ID"
ENV_SECRET_KEY = "EXAMLOPS_LAKEFS_SECRET_ACCESS_KEY"

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_OBJECTS = 100_000
DEFAULT_MAX_BYTES = 50 * 1024**3
_PAGE = 1000
_CHUNK = 1024 * 1024
_MD5 = re.compile(r"^[0-9a-f]{32}$")
_URI = re.compile(r"^lakefs://(?P<repo>[^/]+)/(?P<ref>[^/]+)/?$")


class LakeFSError(RuntimeError):
    """A lakeFS call that did not produce a verified answer."""


@dataclass
class RestoreResult:
    ok: bool
    objects: int = 0
    bytes: int = 0
    detail: str = ""
    checked_md5: int = 0
    files: list[str] = field(default_factory=list)


def parse_uri(uri: str | None) -> tuple[str, str] | None:
    """``(repository, commit)`` from a ``lakefs://`` URI, else ``None``."""
    m = _URI.match((uri or "").strip())
    return (m.group("repo"), m.group("ref")) if m else None


def endpoint() -> str | None:
    raw = (os.getenv(ENV_ENDPOINT) or "").strip()
    return raw.rstrip("/") or None


def _headers() -> dict[str, str]:
    key, secret = os.getenv(ENV_ACCESS_KEY), os.getenv(ENV_SECRET_KEY)
    if key and secret:
        token = base64.b64encode(f"{key}:{secret}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {}


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower())
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


class _SameOriginAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect, but never carry the lakeFS credentials to another origin.

    urllib's default handler copies every header — ``Authorization`` included — onto the
    redirected request, whatever host (or plain-http downgrade) it points at. A lakeFS object
    download may legitimately redirect to a pre-signed object-store URL; that URL carries its
    own authority and must not receive the lakeFS key pair.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            new.remove_header("Authorization")
        return new


_OPENER = urllib.request.build_opener(_SameOriginAuthRedirect)


def _open(url: str, timeout: float) -> Any:
    if not url.startswith(("http://", "https://")):
        raise LakeFSError("lakeFS endpoint must be an http(s) URL")
    req = urllib.request.Request(url, headers=_headers())
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise LakeFSError(f"lakeFS answered HTTP {exc.code} for {_redact(url)}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise LakeFSError(f"lakeFS unreachable ({type(exc).__name__})") from None


def _redact(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _json(url: str, timeout: float) -> dict[str, Any]:
    with _open(url, timeout) as resp:
        try:
            data = json.loads(resp.read(8 * 1024 * 1024).decode())
        except ValueError as exc:
            raise LakeFSError(f"lakeFS returned non-JSON: {exc}") from None
    if not isinstance(data, dict):
        raise LakeFSError("lakeFS returned an unexpected document")
    return data


def verify_commit(repo: str, commit: str, *, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """``None`` when the commit exists in lakeFS, else why it could not be confirmed."""
    base = endpoint()
    if not base:
        return f"lakeFS revision {commit[:16]} cannot be verified: {ENV_ENDPOINT} is not set"
    try:
        data = _json(f"{base}/api/v1/repositories/{_q(repo)}/commits/{_q(commit)}", timeout)
    except LakeFSError as exc:
        return f"lakeFS commit {repo}@{commit[:16]} not confirmed: {exc}"
    if str(data.get("id") or "") != commit:
        return f"lakeFS returned commit {str(data.get('id'))[:16]} for {commit[:16]}"
    return None


def _safe_target(dest: Path, obj_path: str) -> Path:
    rel = PurePosixPath(obj_path)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise LakeFSError(f"refusing object path that escapes the destination: {obj_path!r}")
    target = (dest / Path(*rel.parts)).resolve()
    if dest.resolve() not in target.parents:
        raise LakeFSError(f"refusing object path that escapes the destination: {obj_path!r}")
    return target


def list_objects(
    repo: str, commit: str, *, prefix: str = "", max_objects: int, timeout: float
) -> list[dict[str, Any]]:
    base = endpoint()
    if not base:
        raise LakeFSError(f"{ENV_ENDPOINT} is not set")
    out: list[dict[str, Any]] = []
    after = ""
    while True:
        qs = urllib.parse.urlencode({"amount": _PAGE, "after": after, "prefix": prefix})
        page = _json(
            f"{base}/api/v1/repositories/{_q(repo)}/refs/{_q(commit)}/objects/ls?{qs}", timeout
        )
        for obj in page.get("results") or []:
            if obj.get("path_type", "object") != "object":
                continue
            out.append(obj)
            if len(out) > max_objects:
                raise LakeFSError(f"commit holds more than {max_objects} objects (cap)")
        pag = page.get("pagination") or {}
        if not pag.get("has_more"):
            return out
        nxt = str(pag.get("next_offset") or "")
        if not nxt or nxt == after:
            raise LakeFSError("lakeFS pagination did not advance")
        after = nxt


def restore(
    repo: str,
    commit: str,
    dest: str | Path,
    *,
    prefix: str = "",
    max_objects: int = DEFAULT_MAX_OBJECTS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT,
) -> RestoreResult:
    """Download every object of ``repo@commit`` into ``dest`` (which must be empty)."""
    dest_p = Path(dest)
    if dest_p.exists() and (not dest_p.is_dir() or any(dest_p.iterdir())):
        return RestoreResult(False, detail=f"{dest_p} is not an empty directory — refusing")
    why = verify_commit(repo, commit, timeout=timeout)
    if why:
        return RestoreResult(False, detail=why)
    base = endpoint()
    assert base is not None  # verify_commit checked it
    res = RestoreResult(True)
    try:
        objs = list_objects(repo, commit, prefix=prefix, max_objects=max_objects, timeout=timeout)
        declared = sum(int(o.get("size_bytes") or 0) for o in objs)
        if declared > max_bytes:
            raise LakeFSError(f"commit holds {declared} bytes, over the {max_bytes}-byte cap")
        dest_p.mkdir(parents=True, exist_ok=True)
        for obj in objs:
            path = str(obj.get("path") or "")
            target = _safe_target(dest_p, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            url = (
                f"{base}/api/v1/repositories/{_q(repo)}/refs/{_q(commit)}/objects?"
                + urllib.parse.urlencode({"path": path})
            )
            md5 = hashlib.md5(usedforsecurity=False)
            n = 0
            with _open(url, timeout) as resp, target.open("wb") as fh:
                while chunk := resp.read(_CHUNK):
                    n += len(chunk)
                    if res.bytes + n > max_bytes:
                        raise LakeFSError(f"download exceeded the {max_bytes}-byte cap")
                    md5.update(chunk)
                    fh.write(chunk)
                # http.client's read(amt) returns b"" at a premature EOF instead of raising
                # IncompleteRead, leaving the unread part of Content-Length in `length`. Without
                # this check a dropped connection is a silently short file whenever lakeFS did
                # not list a size and the checksum is not a plain MD5.
                remaining = getattr(resp, "length", None)
                if isinstance(remaining, int) and remaining > 0:
                    raise LakeFSError(f"{path}: connection closed with {remaining} byte(s) unread")
            want = obj.get("size_bytes")
            if want is not None and int(want) != n:
                raise LakeFSError(f"{path}: size {n} != recorded {want}")
            etag = str(obj.get("checksum") or "").strip('"').lower()
            if _MD5.match(etag):
                if md5.hexdigest() != etag:
                    raise LakeFSError(f"{path}: MD5 does not match lakeFS checksum")
                res.checked_md5 += 1
            res.objects += 1
            res.bytes += n
            res.files.append(path)
    except (LakeFSError, OSError, ValueError, http.client.HTTPException) as exc:
        # ValueError: a listing whose size_bytes is not a number.
        # OSError / HTTPException: a connection reset, a read timeout or a truncated body in the
        # middle of a download, a full disk, or an object path colliding with a directory. Each
        # must fail the restore and clean up exactly like a checksum mismatch — never escape as a
        # traceback that leaves a half-written dataset behind.
        # The destination was empty when we started: a half-restored dataset must not be left
        # behind looking like the pinned revision.
        if dest_p.is_dir():
            for child in dest_p.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        res.ok = False
        res.detail = (
            str(exc)
            if isinstance(exc, LakeFSError)
            else f"restore failed ({type(exc).__name__}: {str(exc)[:200]})"
        )
        res.files = []
        res.objects = res.bytes = res.checked_md5 = 0
        return res
    res.detail = (
        f"restored {res.objects} object(s), {res.bytes} bytes from lakefs://{repo}/{commit[:16]}"
        f" ({res.checked_md5} MD5-checked, all size-checked)"
    )
    return res


__all__ = [
    "ENV_ACCESS_KEY",
    "ENV_ENDPOINT",
    "ENV_SECRET_KEY",
    "LakeFSError",
    "RestoreResult",
    "endpoint",
    "list_objects",
    "parse_uri",
    "restore",
    "verify_commit",
]
