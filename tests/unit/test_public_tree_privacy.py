"""Regression guards for personal data and private assistant material in the public tree."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from tests.unit._guard_deps import require_binary

REPO = Path(__file__).resolve().parents[2]
PRIVATE_PARTS = {".claude", ".codex"}
PRIVATE_ROOTS = {"design", "memory", "memories", "paper", "plans", "reviews"}
PRIVATE_FILES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md"}
PERSONAL_HOME = re.compile(
    r"(?:/home/(?!agent(?:/|\b)|jovyan(?:/|\b)|me(?:/|\b))[^/\s]+(?:/|\b)|"
    # `(?!\{)`: a macOS account name cannot start with `{`, so `/Users/{id}` is a URL template
    # (SCIM's standard resource path, RFC 7644), never a home directory. Nothing else is exempt.
    r"/Users/(?!\{)[^/\s]+(?:/|\b)|[A-Za-z]:\\Users\\[^\\\s]+(?:\\|\b))"
)
EMAIL = re.compile(r"[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})")
PERSONAL_EMAIL_DOMAINS = {"gmail.com", "hotmail.com", "icloud.com", "outlook.com", "yahoo.com"}
PRIVATE_REFERENCE = re.compile(r"(?:^|[/\\])\.(?:claude|codex)(?:[/\\]|\b)|\bCLAUDE\.md\b")


def _public_paths() -> list[Path]:
    """Return tracked files plus untracked files eligible for the public repository."""
    require_binary(
        "git",
        "no private assistant material, personal home path or consumer email address is tracked in the public tree",
    )
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [Path(line) for line in output.splitlines()]


def test_home_path_pattern_exempts_url_templates_and_nothing_else():
    """SCIM's `/Users/{id}` is a route template; `/Users/alice/` is still a leaked home path."""
    for home in ("/Users/alice/Library", "see /Users/alice", "C:\\Users\\alice\\x"):
        assert PERSONAL_HOME.search(home), home
    for template in ('"/Users/{account_id}"', "/api/scim/v2/Users/{id}", "{base}/Users/{acc.id}"):
        assert not PERSONAL_HOME.search(template), template


def test_private_assistant_material_is_not_publicly_tracked():
    leaked = [
        str(path)
        for path in _public_paths()
        if path.name in PRIVATE_FILES
        or PRIVATE_PARTS.intersection(path.parts)
        or (path.parts and path.parts[0] in PRIVATE_ROOTS)
    ]
    assert not leaked, f"private assistant material is tracked publicly: {leaked}"


def test_public_text_has_no_personal_home_paths_or_consumer_email_addresses():
    findings: list[str] = []
    for relative in _public_paths():
        if relative == Path(__file__).relative_to(REPO):
            continue  # This guard necessarily contains the patterns it detects.
        path = REPO / relative
        # Git stores a symlink as the path it points to, so that is what the public tree holds;
        # the target is scanned on its own. Reading through a link to a directory would raise.
        raw = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        if b"\0" in raw:
            continue
        text = raw.decode(errors="replace")
        if PERSONAL_HOME.search(text):
            findings.append(f"{relative}: personal home path")
        domains = {match.group(1).lower() for match in EMAIL.finditer(text)}
        if domains & PERSONAL_EMAIL_DOMAINS:
            findings.append(f"{relative}: personal email provider")
    assert not findings, "personal data in public files:\n  " + "\n  ".join(findings)


def test_public_docs_and_runtime_code_do_not_link_private_assistant_material():
    findings: list[str] = []
    for relative in _public_paths():
        if "tests" in relative.parts or relative.suffix not in {
            ".md",
            ".py",
            ".sh",
            ".toml",
            ".yaml",
            ".yml",
        }:
            continue
        path = REPO / relative
        # Git stores a symlink as the path it points to, so that is what the public tree holds;
        # the target is scanned on its own. Reading through a link to a directory would raise.
        raw = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        if b"\0" in raw:
            continue
        if PRIVATE_REFERENCE.search(raw.decode(errors="replace")):
            findings.append(str(relative))
    assert not findings, "public files refer to private assistant material: " + ", ".join(findings)
