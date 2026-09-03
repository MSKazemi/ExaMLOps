"""The root `.env.example` is published; keep it free of anyone's addressing.

Until 2026-08-23 this file was excluded from the public tree because it named an internal
GitLab host, its numeric project id and one site's node and tunnel machines. Both published
install guides nevertheless open with `cp .env.example .env`, so the first instruction could
not be followed from a public clone. It was scrubbed and carved out instead.

That trade has a cost worth guarding: `.gitignore` now carries `!/.env.example`, so the file
is staged by a plain `git add -A` in the *public* git. A site value pasted back into it would
be published silently, and no reviewer reads a config template closely. These tests are the
reviewer — they fail on a hostname, an IP or a numeric project id, and on any secret that
ships with a value.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit._guard_deps import require_binary

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"

# Hosts a template may legitimately name: they belong to nobody, or to the whole world.
_GENERIC_HOSTS = {
    "localhost",
    "gitlab.com",
    "example.com",
    "gitlab.internal.example.com",  # the documented placeholder for a site's own GitLab
    "host.docker.internal",
    "services.ai.azure.com",  # Microsoft's own domain; the resource name is <your-resource>
    "your.server.ip.or.hostname",  # a literal placeholder, not a host
    "keepachangelog.com",
    "semver.org",
}

_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# A dotted name with a real TLD. `.env.example`, `docker-compose.lxp.yml` and `llama3.1:8b`
# are not hostnames; a trailing 2+ letter component after two dots usually is.
_FQDN = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.){2,}[a-z]{2,}\b")

# Every variable that carries a credential. None may ship with a value: a published default
# is a working password, which is exactly how `change-me-admin` came to be live on a node.
_SECRET_VARS = {
    "DASHBOARD_VIEWER_PASSWORD",
    "DASHBOARD_ADMIN_PASSWORD",
    "DASHBOARD_JWT_SECRET",
    "DASHBOARD_SECRET_KEY",
    "CONTROL_PLANE_TOKEN",
    "GITLAB_TOKEN",
    "MODELZOO_WEBHOOK_SECRET",
    "AZURE_OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
}


def _text() -> str:
    assert ENV_EXAMPLE.exists(), "the published install guides tell readers to copy this file"
    return ENV_EXAMPLE.read_text()


def _assignments(text: str) -> dict[str, str]:
    """Active assignments only — a commented example is documentation, not configuration."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.split("#", 1)[0].strip()
    return out


def test_no_ip_address_anywhere():
    """Including in comments: a bridge gateway from one run is noise to every other reader."""
    hits = [
        line
        for line in _text().splitlines()
        # version numbers and bind-all are not addressing, and a documented
        # placeholder pair (`gitlab.internal.example.com:10.0.0.5`) names nobody
        if _IPV4.search(line) and "0.0.0.0" not in line and "example.com" not in line
    ]
    assert not hits, f"IP address in a published template: {hits}"


def test_no_site_hostname_anywhere():
    found = {h for h in _FQDN.findall(_text().lower())} - _GENERIC_HOSTS
    # Filenames and module paths trip the FQDN shape without being hosts.
    found = {h for h in found if not h.endswith((".yml", ".yaml", ".md", ".py", ".json"))}
    assert not found, f"hostname in a published template: {sorted(found)}"


def test_no_numeric_project_id():
    """`GITLAB_PROJECT_ID=87` identifies one project on one private GitLab."""
    assignments = _assignments(_text())
    for key in ("GITLAB_PROJECT_ID", "AI_PROD_GITLAB_PROJECT_ID"):
        assert not assignments.get(key), f"{key} ships with a value"


def test_every_secret_ships_empty():
    assignments = _assignments(_text())
    filled = {k: v for k, v in assignments.items() if k in _SECRET_VARS and v}
    assert not filled, f"published credential value: {sorted(filled)}"


def test_the_guard_catches_what_it_is_for():
    """Proves the three detectors red, so a clean run means checked and not merely quiet."""
    with pytest.raises(AssertionError):
        assert not _IPV4.search("SEANERBUS_HOST=172.19.0.1")
    leaked = _FQDN.findall("GITLAB_URL=https://gitlab.seanergys.fz-juelich.de")
    assert leaked and set(leaked) - _GENERIC_HOSTS, "an internal FQDN must not look generic"
    assert _assignments("GITLAB_PROJECT_ID=87\n")["GITLAB_PROJECT_ID"] == "87"


def test_the_nested_compose_template_is_not_published():
    """It still names a site; only the root template was scrubbed and carved out."""
    import subprocess

    repo = ENV_EXAMPLE.parent
    require_binary("git", "the published .env.example matches what the code actually reads")
    tracked = subprocess.run(
        ["git", "ls-files", "platform/infra/docker-compose/.env.example"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert tracked.stdout.strip() == "", "the compose template must stay out of the public git"
