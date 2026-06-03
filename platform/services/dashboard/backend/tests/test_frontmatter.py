from frontmatter import Frontmatter, parse_readme

VALID = """---
display_name: "JPCP"
summary: "predicts power"
status: stable
last_reviewed: 2026-04-15
paper:
  title: "JPCP paper"
  url: "https://arxiv.org/abs/1"
  citation: "Smith 2024"
use_cases:
  - "budgeting"
maintainers:
  - "team@x"
tags: ["regression"]
---
# Body

Hello.
"""


def test_parse_valid_frontmatter():
    fm, body, warnings = parse_readme(VALID)
    assert fm.display_name == "JPCP"
    assert fm.status == "stable"
    assert fm.paper["title"] == "JPCP paper"
    assert fm.use_cases == ["budgeting"]
    assert body.startswith("# Body")
    assert warnings == []


def test_no_frontmatter_returns_empty_fm_full_body():
    fm, body, warnings = parse_readme("# Just body\n")
    assert fm == Frontmatter()
    assert body == "# Just body\n"
    assert warnings == []


def test_invalid_yaml_emits_warning_keeps_body():
    src = "---\n  not: : valid\n---\nBody here."
    fm, body, warnings = parse_readme(src)
    assert fm == Frontmatter()
    assert body == "Body here."
    assert any("yaml" in w.lower() for w in warnings)


def test_invalid_status_warns_and_drops():
    src = "---\nstatus: bogus\n---\n"
    fm, _body, warnings = parse_readme(src)
    assert fm.status is None
    assert any("status" in w.lower() for w in warnings)


def test_invalid_date_warns_and_drops():
    src = "---\nlast_reviewed: not-a-date\n---\n"
    fm, _body, warnings = parse_readme(src)
    assert fm.last_reviewed is None
    assert any("last_reviewed" in w.lower() for w in warnings)


def test_unknown_keys_ignored():
    src = "---\ndisplay_name: X\nfuture_field: Y\n---\n"
    fm, _body, warnings = parse_readme(src)
    assert fm.display_name == "X"
    assert warnings == []
