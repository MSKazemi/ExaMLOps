"""Rich markup handed to an escaping helper is printed at the operator, verbatim.

``_output.ok/error/warning/info/hint/detail`` all run their message through Rich's
``escape()``, and for a good reason recorded in that module: an unescaped ``examlops[mcp]``
renders as ``examlops``, so a hint would silently instruct someone to install the wrong
thing. The escape is correct. What was wrong is that 59 call sites across 29 files still
passed ``[bold]…[/bold]`` and friends, so ``exa secrets set`` greeted a clean install with

    ✓ Stored [bold]demo/key[/bold] (tenant default, v1)

Nothing failed, nothing logged; the emphasis simply became noise in front of the operator.
These guards keep the two halves consistent: the helpers escape, so the call sites must not
pretend otherwise.
"""

from __future__ import annotations

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "platform" / "cli" / "src" / "examlops"

HELPERS = ("ok", "error", "warning", "info", "hint", "detail")
CALL = re.compile(r"_output\.(" + "|".join(HELPERS) + r")\(")
STYLE_TAG = re.compile(
    r"\[/?(?:bold|italic|dim|green|red|yellow|cyan|magenta|blue|white)(?: [a-z]+)*\]"
)


def _argument_text(src: str, start: int) -> str:
    """The text between a call's parentheses, balanced."""
    depth, i = 1, start
    while i < len(src) and depth:
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
        i += 1
    return src[start : i - 1]


def _offenders(files) -> list[str]:
    found = []
    for path in files:
        src = path.read_text()
        for m in CALL.finditer(src):
            arg = _argument_text(src, m.end())
            for tag in STYLE_TAG.findall(arg):
                line = src[: m.start()].count("\n") + 1
                found.append(f"{path.name}:{line} _output.{m.group(1)}(… {tag} …)")
    return found


def test_no_message_helper_is_handed_markup_it_will_escape():
    offenders = _offenders(sorted(PKG.rglob("*.py")))
    assert not offenders, (
        "These call sites pass Rich style tags into a helper that escapes them, so the "
        "operator sees the tags as literal text:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_catches_a_planted_offender(tmp_path):
    """The opposite arm — a guard only ever seen passing proves nothing."""
    bad = tmp_path / "bad_cmd.py"
    bad.write_text('_output.ok(f"Stored [bold]{name}[/bold] now")\n')
    assert _offenders([bad]), "a planted [bold] tag was not detected"


def test_plain_brackets_are_not_mistaken_for_markup(tmp_path):
    """`examlops[mcp]` and `[project.scripts]` are exactly what the escaping protects."""
    good = tmp_path / "good_cmd.py"
    good.write_text(
        "_output.hint(\"Install it with: uv pip install 'examlops[mcp]'\")\n"
        '_output.error("Add a [project.entry-points] table to pyproject.toml")\n'
    )
    assert _offenders([good]) == []


# --- packaging: the install page has to name the extras that exist -------------


def test_every_declared_extra_is_documented():
    """A capability behind an undocumented extra is a capability nobody installs.

    `pip install examlops` deliberately ships a small CLI, so most of the platform's heavier
    features arrive as extras. That only works if the install page lists them — otherwise the
    operator meets the feature as an error message instead of as an option.
    """
    import tomllib

    root = Path(__file__).resolve().parents[2]
    manifest = tomllib.load(open(root / "platform" / "cli" / "pyproject.toml", "rb"))
    declared = set(manifest["project"]["optional-dependencies"])
    doc = (root / "docs" / "guides" / "quickstart.md").read_text()
    documented = set(re.findall(r"`examlops\[([a-z-]+)\]`", doc))
    assert declared == documented, (
        f"extras declared but not on the install page: {sorted(declared - documented)}; "
        f"documented but not declared: {sorted(documented - declared)}"
    )


def test_the_cli_declares_what_it_imports_at_module_load():
    """A transitive dependency is not a declared one.

    `click` reached the CLI through typer for two years. typer 0.27 dropped it, and a clean
    `pip install examlops` produced an `exa` that died on `import click` before printing
    anything. Three modules import click directly; the package now says so.
    """
    import tomllib

    root = Path(__file__).resolve().parents[2]
    base = tomllib.load(open(root / "platform" / "cli" / "pyproject.toml", "rb"))["project"][
        "dependencies"
    ]
    names = {re.split(r"[<>=!\[ ;]", r)[0].lower() for r in base}
    for direct in ("click", "rich", "typer", "httpx"):
        assert direct in names, (
            f"{direct} is imported at module load by the CLI but is not a declared dependency"
        )
