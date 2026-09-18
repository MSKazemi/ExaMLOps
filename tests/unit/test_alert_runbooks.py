"""Every alert links to a runbook section that exists, and every runbook section has its alert.

An alert that pages someone with only a summary line leaves the on-call engineer to rediscover
what it means, what it costs and what to do, at the worst possible moment. Each rule in
``alert_rules.yml`` therefore carries a ``runbook_url`` into ``docs/runbooks/``. This guard fails
when an alert has none, when the link points at a page or section that does not exist (an anchor
MkDocs cannot check, because it lives in a YAML file), or when a runbook section no longer has an
alert, which means the section describes a condition nobody is paged for.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "platform" / "infra" / "docker-compose" / "alert_rules.yml"
RUNBOOKS = ROOT / "docs" / "runbooks"
SITE = yaml.safe_load(
    "\n".join(
        line
        for line in (ROOT / "mkdocs.yml").read_text(encoding="utf-8").splitlines()
        if line.startswith("site_url:")
    )
)["site_url"]
PREFIX = SITE.rstrip("/") + "/runbooks/"


def _alerts() -> dict[str, dict]:
    out = {}
    for group in yaml.safe_load(RULES.read_text(encoding="utf-8"))["groups"]:
        for rule in group.get("rules", []):
            if "alert" in rule:
                out[rule["alert"]] = rule
    return out


def _anchors(page: Path) -> set[str]:
    return set(re.findall(r"\{#([a-z0-9-]+)\}", page.read_text(encoding="utf-8")))


def test_every_alert_has_a_runbook_url():
    missing = sorted(
        a for a, r in _alerts().items() if not r.get("annotations", {}).get("runbook_url")
    )
    assert not missing, f"alerts without a runbook_url: {missing}"
    assert len(_alerts()) >= 40  # guard the guard: the rules file was read


def test_every_runbook_url_points_at_an_existing_section():
    problems = []
    for alert, rule in _alerts().items():
        url = rule["annotations"]["runbook_url"]
        if not url.startswith(PREFIX):
            problems.append(f"{alert}: {url} is not under {PREFIX}")
            continue
        page_part, _, anchor = url[len(PREFIX) :].partition("#")
        page = RUNBOOKS / f"{page_part.strip('/')}.md"
        if not page.is_file():
            problems.append(f"{alert}: no page {page.relative_to(ROOT)}")
        elif anchor not in _anchors(page):
            problems.append(f"{alert}: {page.name} has no section {{#{anchor}}}")
    assert not problems, "\n".join(problems)


def test_every_runbook_section_has_an_alert():
    linked = {rule["annotations"]["runbook_url"] for rule in _alerts().values()}
    orphans = []
    for page in RUNBOOKS.glob("*.md"):
        for anchor in _anchors(page):
            if f"{PREFIX}{page.stem}/#{anchor}" not in linked:
                orphans.append(f"{page.name}#{anchor}")
    assert not orphans, f"runbook sections no alert links to: {sorted(orphans)}"


def test_the_runbooks_are_in_the_site_navigation():
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    missing = [p.name for p in RUNBOOKS.glob("*.md") if f"runbooks/{p.name}" not in nav]
    assert not missing, f"runbook pages missing from mkdocs.yml nav: {missing}"


# The commands *inside* a runbook are checked by `tests/unit/test_documented_commands_exist.py`,
# which scans all of `docs/` — runbooks included — against the live Click tree. The checks above
# are about structure (alert → url → section → alert); that one is about content.
