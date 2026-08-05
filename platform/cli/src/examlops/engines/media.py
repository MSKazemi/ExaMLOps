"""Track V (R-V5) — media validation for multimodal (VLM) requests.

A VLM endpoint that forwards arbitrary ``image_url`` values is an **SSRF primitive** (the
classic target being a cloud metadata address such as ``169.254.169.254``), and one that
accepts unbounded ``data:`` payloads is a memory-exhaustion primitive. This module is the
in-process half of a two-ended guard: it validates every media part *before* dispatch,
while :func:`examlops.engines.config.to_vllm_args` renders the equivalent
``--allowed-media-domains`` / ``--allowed-local-media-path`` / ``--limit-mm-per-prompt``
flags so the server enforces the same limits independently.

Deliberate non-goal: **this module never fetches remote media.** It validates and forwards;
vLLM performs the fetch. Adding an HTTP client here would put an SSRF-capable fetcher in
the control plane, double egress, and defeat vLLM's multimodal processor cache.

Pure stdlib, no engine imports — so it stays importable on any host and cannot create a
cycle back into the package ``__init__``.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

# Content-part type → modality bucket. ``limit_mm_per_prompt`` and the vLLM flags are
# keyed by the bucket, not the part type, so several part shapes share one budget.
_PART_KIND: dict[str, str] = {
    "image_url": "image",
    "image_pil": "image",
    "image_embeds": "image",
    "video_url": "video",
    "audio_url": "audio",
    "input_audio": "audio",
}

# Which modality settings permit which buckets. A ``vision`` model may take images; a
# ``video`` model may take video and the still images that compose it.
_MODALITY_ALLOWS: dict[str, set[str]] = {
    "text": set(),
    "vision": {"image"},
    "audio": {"audio"},
    "video": {"video", "image"},
}


class MediaRejected(ValueError):
    """A media part failed validation. Raised before any engine dispatch."""


@dataclass
class MediaStats:
    """What the request carried, after validation — fed to telemetry and cost."""

    counts: dict[str, int] = field(default_factory=dict)
    inline_bytes: int = 0
    remote_refs: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def images(self) -> int:
        return self.counts.get("image", 0)


def normalize_content(
    messages: list[dict[str, Any]], config: Any
) -> tuple[list[dict[str, Any]], MediaStats]:
    """Validate every media part in ``messages``; return the messages plus a media tally.

    ``config`` is duck-typed: anything exposing a ``multimodal`` attribute with the fields
    of :class:`~examlops.engines.config.MultimodalConfig` works, so this module has no
    import dependency on the engine package.

    Messages are returned **unchanged** on success — normalisation here means "checked and
    found acceptable", not "rewritten". Raises :class:`MediaRejected` on the first
    violation, so a rejected request never reaches an engine.
    """
    mm = getattr(config, "multimodal", None)
    stats = MediaStats()
    if not isinstance(messages, list):
        raise MediaRejected("messages must be a list")

    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue  # plain string content — nothing to validate
        for part in content:
            if not isinstance(part, dict):
                raise MediaRejected(f"content part must be a mapping, got {type(part).__name__}")
            ptype = str(part.get("type", ""))
            if ptype in ("text", ""):
                continue
            kind = _PART_KIND.get(ptype)
            if kind is None:
                raise MediaRejected(f"unsupported content part type '{ptype}'")
            _check_modality(kind, mm, ptype)
            _check_part(part, ptype, kind, mm, stats)
            stats.counts[kind] = stats.counts.get(kind, 0) + 1

    _check_limits(stats, mm)
    return messages, stats


def _check_modality(kind: str, mm: Any, ptype: str) -> None:
    modality = str(getattr(mm, "modality", "text") or "text").lower()
    allowed = _MODALITY_ALLOWS.get(modality, set())
    if kind not in allowed:
        raise MediaRejected(
            f"content part '{ptype}' ({kind}) not accepted by a model with "
            f"modality '{modality}' — set engine.multimodal.modality accordingly"
        )


def _check_part(part: dict[str, Any], ptype: str, kind: str, mm: Any, stats: MediaStats) -> None:
    if ptype == "image_embeds":
        if not bool(getattr(mm, "enable_mm_embeds", False)):
            raise MediaRejected(
                "pre-computed embeddings rejected: set engine.multimodal.enable_mm_embeds"
            )
        return
    if ptype == "image_pil":
        return  # an in-process PIL object; no URL/byte surface to validate

    url = _extract_url(part, ptype)
    if url is None:
        raise MediaRejected(f"content part '{ptype}' is missing a url")

    scheme = urlparse(url).scheme.lower()
    if scheme in ("http", "https"):
        _check_remote(url, mm)
        stats.remote_refs += 1
    elif url.startswith("data:"):
        stats.inline_bytes += _check_data_url(url, mm)
    elif scheme == "file":
        stats.inline_bytes += _check_file_url(url, mm)
    else:
        raise MediaRejected(
            f"unsupported media URL scheme '{scheme or url[:16]}' — "
            "expected http(s), data: or file:"
        )


def _extract_url(part: dict[str, Any], ptype: str) -> str | None:
    """Pull the URL out of either OpenAI shape: ``{"image_url": {"url": …}}`` or a bare str."""
    value = part.get(ptype)
    if isinstance(value, dict):
        url = value.get("url")
        return str(url) if url else None
    if isinstance(value, str):
        return value
    # `input_audio` carries {"data": base64, "format": "wav"} rather than a URL.
    if ptype == "input_audio" and isinstance(value, dict) and value.get("data"):
        return "data:audio/octet-stream;base64," + str(value["data"])
    return None


def _check_remote(url: str, mm: Any) -> None:
    """SSRF control — the host must be on the allow-list. An empty list denies all."""
    allowed = [str(d).lower() for d in (getattr(mm, "allowed_media_domains", None) or [])]
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise MediaRejected(f"media URL has no host: {url[:80]}")
    if not allowed:
        raise MediaRejected(
            "remote media URLs are rejected: engine.multimodal.allowed_media_domains is "
            "empty. Add the hosts you trust, or send the image as a data: URL."
        )
    if not any(host == d or host.endswith("." + d) for d in allowed):
        raise MediaRejected(
            f"media host '{host}' is not in allowed_media_domains {allowed}"
        )


def _check_data_url(url: str, mm: Any) -> int:
    """Bound an inline payload. Size is computed from the encoding, not by decoding it all."""
    max_bytes = int(getattr(mm, "max_image_bytes", 20 * 1024 * 1024))
    header, _, payload = url.partition(",")
    if not payload:
        raise MediaRejected("data: URL carries no payload")
    if ";base64" in header:
        # 4 base64 chars → 3 bytes; subtract padding. Cheaper and safer than decoding
        # a potentially huge payload just to measure it.
        padding = len(payload) - len(payload.rstrip("="))
        size = (len(payload) * 3) // 4 - padding
        if size > max_bytes:
            raise MediaRejected(
                f"inline media is {size} bytes, over max_image_bytes={max_bytes}"
            )
        try:  # validate the encoding itself, but only once the size is known to be sane
            base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MediaRejected(f"data: URL is not valid base64: {exc}") from exc
    else:
        size = len(unquote(payload).encode("utf-8", "ignore"))
        if size > max_bytes:
            raise MediaRejected(
                f"inline media is {size} bytes, over max_image_bytes={max_bytes}"
            )
    return size


def _check_file_url(url: str, mm: Any) -> int:
    """Local media must live under the configured root — no path traversal, no /etc/shadow."""
    root = getattr(mm, "allowed_local_media_path", None)
    if not root:
        raise MediaRejected(
            "file:// media is rejected: engine.multimodal.allowed_local_media_path is unset"
        )
    path = Path(unquote(urlparse(url).path)).resolve()
    root_path = Path(str(root)).resolve()
    if not path.is_relative_to(root_path):
        raise MediaRejected(f"file:// media '{path}' is outside allowed_local_media_path")
    if not path.is_file():
        raise MediaRejected(f"file:// media '{path}' does not exist")
    size = path.stat().st_size
    max_bytes = int(getattr(mm, "max_image_bytes", 20 * 1024 * 1024))
    if size > max_bytes:
        raise MediaRejected(f"local media is {size} bytes, over max_image_bytes={max_bytes}")
    return size


def _check_limits(stats: MediaStats, mm: Any) -> None:
    limits = dict(getattr(mm, "limit_mm_per_prompt", None) or {})
    for kind, count in stats.counts.items():
        limit = limits.get(kind)
        if limit is None:
            raise MediaRejected(
                f"no limit_mm_per_prompt.{kind} configured — refusing an unbounded "
                f"{kind} count (DoS surface)"
            )
        if count > int(limit):
            raise MediaRejected(
                f"{count} {kind} part(s) exceeds limit_mm_per_prompt.{kind}={limit}"
            )


# ── Text flattening for engines without a chat surface (R-V4) ─────────────────


def flatten_messages(messages: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    """Flatten chat messages to a prompt string, reporting what had to be dropped.

    Used only when the resolved engine has no ``chat`` surface. The dropped tally is what
    lets the caller warn instead of silently losing a user's images — silent media loss is
    the failure mode R-V4 exists to prevent.
    """
    parts: list[str] = []
    dropped: dict[str, int] = {}
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            parts.append(content)
            continue
        if not isinstance(content, list):
            if content is not None:
                parts.append(str(content))
            continue
        for part in content:
            if not isinstance(part, dict):
                parts.append(str(part))
                continue
            ptype = str(part.get("type", ""))
            if ptype == "text":
                parts.append(str(part.get("text", "")))
            elif ptype in _PART_KIND:
                kind = _PART_KIND[ptype]
                dropped[kind] = dropped.get(kind, 0) + 1
    return "\n".join(p for p in parts if p).strip(), dropped
