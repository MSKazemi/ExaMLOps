"""Static guards for the embedded chat UI's untrusted rendering boundaries."""

from skipper.chat_html import CHAT_HTML


def test_thread_ids_are_rendered_as_text_and_url_encoded():
    assert "label.textContent = t" in CHAT_HTML
    assert "encodeURIComponent(threadId)" in CHAT_HTML
    assert "/ws/chat/${encodeURIComponent(threadId)}" in CHAT_HTML
    assert '`<div class="thread-id">${t}</div>`' not in CHAT_HTML


def test_markdown_is_sanitized_before_entering_the_live_dom():
    assert "sanitizeHtml(marked.parse" in CHAT_HTML
    assert "script,iframe,object,embed" in CHAT_HTML
    assert "name.startsWith('on')" in CHAT_HTML


def test_hitl_uses_typed_opaque_action_instead_of_plain_text_resume():
    assert "pendingActionId = actionId" in CHAT_HTML
    assert "type: 'action', action_id: actionId, decision" in CHAT_HTML
    assert "type: 'resume'" not in CHAT_HTML
