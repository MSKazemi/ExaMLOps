"""Track V — multimodal request path + media guard (ADR 0107, spec §4.5).

GWT-V4 content parts survive to a chat-capable engine / are reported when dropped ·
GWT-V5 SSRF, size, count and path guards reject **before** dispatch.

The negative cases are the point of this file: a VLM endpoint that forwards arbitrary
``image_url`` values is an SSRF primitive, and one that accepts unbounded base64 is a
memory-exhaustion primitive.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import engines  # noqa: E402
from examlops import gateway as gw  # noqa: E402
from examlops.engines.media import MediaRejected, flatten_messages, normalize_content  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    init_db()


def _vision_cfg(**mm):
    base = {
        "modality": "vision",
        "limit_mm_per_prompt": {"image": 2},
        "allowed_media_domains": ["example.com"],
    }
    base.update(mm)
    return engines.EngineConfig.from_dict({"engine": "vllm", "multimodal": base})


def _img(url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


def _msg(*parts) -> list[dict]:
    return [{"role": "user", "content": list(parts)}]


def _data_url(nbytes: int) -> str:
    return "data:image/png;base64," + base64.b64encode(b"\x00" * nbytes).decode()


# ── GWT-V5: SSRF guard ────────────────────────────────────────────────────────


def test_gwtv5_allowed_domain_passes():
    msgs, stats = normalize_content(
        _msg({"type": "text", "text": "hi"}, _img("https://example.com/a.png")), _vision_cfg()
    )
    assert stats.images == 1 and stats.remote_refs == 1
    assert msgs[0]["content"][1]["image_url"]["url"].endswith("a.png")  # untouched


def test_gwtv5_subdomain_of_an_allowed_domain_passes():
    _, stats = normalize_content(_msg(_img("https://cdn.example.com/a.png")), _vision_cfg())
    assert stats.images == 1


def test_gwtv5_other_host_is_rejected():
    with pytest.raises(MediaRejected, match="not in allowed_media_domains"):
        normalize_content(_msg(_img("https://evil.test/a.png")), _vision_cfg())


def test_gwtv5_cloud_metadata_address_is_rejected():
    """The canonical SSRF target must not slip through on an IP literal."""
    with pytest.raises(MediaRejected):
        normalize_content(_msg(_img("http://169.254.169.254/latest/meta-data/")), _vision_cfg())


def test_gwtv5_empty_allow_list_denies_all_remote_media():
    # Empty means "no remote URLs", not "anything goes" — fail closed.
    cfg = _vision_cfg(allowed_media_domains=[])
    with pytest.raises(MediaRejected, match="allowed_media_domains is"):
        normalize_content(_msg(_img("https://example.com/a.png")), cfg)


def test_gwtv5_unknown_scheme_is_rejected():
    with pytest.raises(MediaRejected, match="unsupported media URL scheme"):
        normalize_content(_msg(_img("gopher://example.com/a.png")), _vision_cfg())


# ── GWT-V5: size guard ────────────────────────────────────────────────────────


def test_gwtv5_inline_payload_within_budget_passes():
    cfg = _vision_cfg(max_image_bytes=1024)
    _, stats = normalize_content(_msg(_img(_data_url(512))), cfg)
    assert 500 <= stats.inline_bytes <= 520


def test_gwtv5_oversized_inline_payload_is_rejected():
    cfg = _vision_cfg(max_image_bytes=1024)
    with pytest.raises(MediaRejected, match="over max_image_bytes"):
        normalize_content(_msg(_img(_data_url(4096))), cfg)


def test_gwtv5_invalid_base64_is_rejected():
    cfg = _vision_cfg(max_image_bytes=1024)
    with pytest.raises(MediaRejected, match="not valid base64"):
        normalize_content(_msg(_img("data:image/png;base64,!!!!not-base64!!!!")), cfg)


# ── GWT-V5: count guard ───────────────────────────────────────────────────────


def test_gwtv5_image_count_over_the_limit_is_rejected():
    cfg = _vision_cfg(limit_mm_per_prompt={"image": 2})
    three = _msg(*(_img(f"https://example.com/{i}.png") for i in range(3)))
    with pytest.raises(MediaRejected, match="exceeds limit_mm_per_prompt.image=2"):
        normalize_content(three, cfg)


def test_gwtv5_counts_accumulate_across_messages():
    cfg = _vision_cfg(limit_mm_per_prompt={"image": 1})
    two_turns = [
        {"role": "user", "content": [_img("https://example.com/a.png")]},
        {"role": "user", "content": [_img("https://example.com/b.png")]},
    ]
    with pytest.raises(MediaRejected, match="exceeds limit_mm_per_prompt"):
        normalize_content(two_turns, cfg)


def test_gwtv5_media_on_a_text_only_model_is_rejected():
    cfg = engines.EngineConfig.from_dict({"engine": "vllm"})  # modality defaults to text
    with pytest.raises(MediaRejected, match="modality 'text'"):
        normalize_content(_msg(_img("https://example.com/a.png")), cfg)


# ── GWT-V5: local path guard ──────────────────────────────────────────────────


def test_gwtv5_local_file_inside_the_root_passes(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    img = media / "a.png"
    img.write_bytes(b"\x89PNG" + b"\x00" * 32)
    cfg = _vision_cfg(allowed_local_media_path=str(media))
    _, stats = normalize_content(_msg(_img(f"file://{img}")), cfg)
    assert stats.inline_bytes == img.stat().st_size


def test_gwtv5_path_traversal_outside_the_root_is_rejected(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"x")
    cfg = _vision_cfg(allowed_local_media_path=str(media))
    with pytest.raises(MediaRejected, match="outside allowed_local_media_path"):
        normalize_content(_msg(_img(f"file://{media}/../secret.png")), cfg)


def test_gwtv5_file_url_rejected_when_no_root_configured():
    with pytest.raises(MediaRejected, match="allowed_local_media_path is unset"):
        normalize_content(_msg(_img("file:///etc/passwd")), _vision_cfg())


def test_gwtv5_embeddings_require_an_explicit_opt_in():
    with pytest.raises(MediaRejected, match="enable_mm_embeds"):
        normalize_content(_msg({"type": "image_embeds", "image_embeds": "b64"}), _vision_cfg())
    ok = _vision_cfg(enable_mm_embeds=True)
    _, stats = normalize_content(_msg({"type": "image_embeds", "image_embeds": "b64"}), ok)
    assert stats.images == 1


# ── GWT-V4: pass-through vs flatten ───────────────────────────────────────────


def test_gwtv4_flatten_keeps_text_and_reports_dropped_media():
    prompt, dropped = flatten_messages(
        _msg({"type": "text", "text": "what is this?"}, _img("https://example.com/a.png"))
    )
    assert prompt == "what is this?"
    assert dropped == {"image": 1}


def test_gwtv4_messages_to_prompt_no_longer_stringifies_a_part_list():
    """The old implementation produced the Python repr of the content list."""
    prompt = gw._messages_to_prompt(
        _msg({"type": "text", "text": "hello"}, _img("https://example.com/a.png"))
    )
    assert prompt == "hello"
    assert "image_url" not in prompt and "[{" not in prompt


def test_gwtv4_text_only_engine_warns_about_dropped_images():
    router = gw.build_engine_router("m", engines.EngineConfig(engine="echo"))
    client = gw.GatewayClient(router)
    with pytest.warns(RuntimeWarning, match="dropped 1 image"):
        comp = client.chat(
            "m", _msg({"type": "text", "text": "describe"}, _img("https://example.com/a.png"))
        )
    assert comp.text  # the text half still served


def test_gwtv4_plain_string_content_is_unaffected():
    router = gw.build_engine_router("m", engines.EngineConfig(engine="echo"))
    comp = gw.GatewayClient(router).chat("m", [{"role": "user", "content": "hello world"}])
    assert comp.text == "hello world"


# ── rejection is a hard error, not a failover ─────────────────────────────────


def test_media_rejection_surfaces_as_medianotallowed_not_allbackendsfailed():
    """Retrying a policy denial on another backend would hide the real reason.

    Exercises the real edge: a server-mode engine (pointed at an unreachable port, which is
    never dialled because validation runs first) behind a router that also has a fallback
    backend. The denial must escape as MediaNotAllowed rather than being converted into
    "all backends failed" — or worse, silently answered by the fallback.
    """
    cfg = engines.EngineConfig.from_dict(
        {
            "engine": "vllm",
            "base_url": "http://127.0.0.1:1",  # never dialled — validation runs first
            "multimodal": {
                "modality": "vision",
                "limit_mm_per_prompt": {"image": 2},
                "allowed_media_domains": ["example.com"],
            },
        }
    )
    backend = gw.engine_backend("m", cfg)
    assert backend.engine.name == "vllm-server"  # the real chat-capable edge

    fallback_calls = []

    def _fallback(model, messages, **kw):
        fallback_calls.append(model)
        return "fallback answered"

    router = gw.Router()
    router.add_route("m", [("engine:m", backend), ("fallback", _fallback)])
    with pytest.raises(gw.MediaNotAllowed, match="allowed_media_domains"):
        gw.GatewayClient(router).chat("m", _msg(_img("https://evil.test/a.png")))
    assert fallback_calls == []  # the denial did not leak to another backend


def test_server_engine_rejects_media_before_any_http_call():
    # Unreachable host: if validation ran first, we get MediaRejected, not a connection error.
    eng = engines.VLLMServerEngine("http://127.0.0.1:1", "m", _vision_cfg())
    with pytest.raises(MediaRejected):
        eng.chat(_msg(_img("https://evil.test/a.png")))
