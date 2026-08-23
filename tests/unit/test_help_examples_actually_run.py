"""Every example the CLI prints in its own help must resolve against the CLI.

The epilogs are the copy-paste surface: a first-time operator types what the help
shows. Three of them could not be typed at all — `exa autopilot run --model JPCP`
(the model is a positional, so click answers "No such option: --model") and three
`exa models card JPCP` lines missing the `generate` leaf. Nothing checked them,
because a string in an epilog is just a string.

This walks all 451 nodes of the live command tree, extracts every ``exa …`` line
from every epilog and docstring, and resolves it the way click would: command path
first, then options against that command plus the root's global ones.

Two exemptions are deliberate, not laziness:

* a line containing ``<placeholder>`` or ``[optional]`` is a *shape*, not a command;
* a command declaring ``allow_extra_args``/``ignore_unknown_options`` parses its own
  flags at runtime, so an option this checker has never heard of may still be real —
  ``exa pipeline promote --if-rmse-lt 5.0`` is the case that proves it.
"""

from __future__ import annotations

import pathlib
import re
import shlex

import typer.main

from examlops.cli.main import app

EXAMPLE = re.compile(r"^\s*(?:\$\s*)?(exa\s+[^\n]+)$", re.M)


def _subs(cmd):
    # NB: ``isinstance(cmd, click.Group)`` is False for Typer's group class under
    # typer 0.27 / click 8.4 — the same trap ``docs_cmd.py`` documents for options.
    return dict(getattr(cmd, "commands", {}) or {})


def _walk(cmd, path):
    yield path, cmd
    for name, sub in _subs(cmd).items():
        yield from _walk(sub, [*path, name])


def _options(cmd) -> dict[str, bool]:
    """Every option string this command accepts → whether it consumes a value."""
    out: dict[str, bool] = {}
    for param in cmd.params:
        if getattr(param, "param_type_name", None) != "option":
            continue
        takes_value = not getattr(param, "is_flag", False) and getattr(param, "nargs", 1) != 0
        for opt in list(param.opts) + list(param.secondary_opts):
            out[opt] = takes_value
    return out


def _freeform(cmd) -> bool:
    settings = getattr(cmd, "context_settings", None) or {}
    return bool(settings.get("allow_extra_args") or settings.get("ignore_unknown_options"))


def _broken() -> list[str]:
    root = typer.main.get_command(app)
    global_opts = _options(root) | {"--help": False, "-h": False}
    failures: list[str] = []
    seen: set[tuple[str, str]] = set()

    for path, cmd in sorted(_walk(root, [])):
        text = (cmd.epilog or "") + "\n" + (cmd.help or "")
        for match in EXAMPLE.finditer(text):
            line = re.sub(r"\[/?[a-z ]+\]", "", match.group(1)).strip().rstrip("\\")
            line = re.split(r"\s+#|\s+—|\||&&|>", line)[0].strip()
            where = " ".join(path) or "exa"
            if (where, line) in seen:
                continue
            seen.add((where, line))
            try:
                tokens = shlex.split(line)[1:]
            except ValueError:
                continue
            if "--" in tokens:  # everything past `--` goes to another program
                tokens = tokens[: tokens.index("--")]
            if any("<" in t or "[" in t for t in tokens):
                continue  # a shape, not a command
            problem = _resolve(root, tokens, global_opts)
            if problem:
                failures.append(f"[{where}] {line}\n    → {problem}")
    return failures


def _resolve(root, tokens: list[str], global_opts: dict[str, bool]) -> str | None:
    current, resolved, i = root, [], 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("-"):
            known = _options(current) | global_opts
            name = token.split("=")[0]
            if name not in known and not _freeform(current):
                return f"unknown option {name} on 'exa {' '.join(resolved)}'"
            if known.get(name) and "=" not in token:
                i += 1  # this option eats the next token
            i += 1
            continue
        nxt = _subs(current).get(token)
        if nxt is None:
            break
        current, resolved = nxt, [*resolved, token]
        i += 1
    if i < len(tokens) and _subs(current):
        return f"no such command: exa {' '.join([*resolved, tokens[i]])}"
    return None


def test_every_help_example_resolves():
    failures = _broken()
    assert not failures, "the CLI's own help prints commands it rejects:\n" + "\n".join(failures)


def test_the_checker_would_catch_a_broken_example():
    """Red arm — otherwise a checker that silently resolves nothing looks green."""
    root = typer.main.get_command(app)
    globals_ = _options(root) | {"--help": False, "-h": False}
    assert _resolve(root, ["autopilot", "run", "--model", "JPCP"], globals_) is not None
    assert _resolve(root, ["models", "card", "JPCP"], globals_) is not None
    assert _resolve(root, ["autopilot", "run", "JPCP"], globals_) is None


def test_a_command_that_parses_its_own_flags_is_not_flagged():
    """`exa pipeline promote --if-rmse-lt 5.0` is real: promote reads ctx.args itself."""
    root = typer.main.get_command(app)
    globals_ = _options(root) | {"--help": False, "-h": False}
    assert _resolve(root, ["pipeline", "promote", "jpcp", "--if-rmse-lt", "5.0"], globals_) is None


def test_every_exa_example_in_the_docs_resolves():
    """The published docs are the same copy-paste surface, so hold them to the same rule.

    656 examples across ``docs/`` at the time this was written, of which exactly one was
    wrong: ``exa genai cost --input …/--output …`` for a command whose options are
    ``--in``/``--out``. Same exemptions as the help sweep — a line carrying a placeholder
    is a shape, not a command.
    """
    root = typer.main.get_command(app)
    global_opts = _options(root) | {"--help": False, "-h": False}
    docs = pathlib.Path(__file__).resolve().parents[2] / "docs"
    failures, checked = [], 0
    for md in sorted(docs.rglob("*.md")):
        for raw in md.read_text(errors="ignore").splitlines():
            match = re.match(r"^`?(exa\s+[^`|]+)`?$", raw.strip().lstrip("$ ").strip())
            if not match:
                continue
            line = re.split(r"\s+#|\s+—", match.group(1))[0].strip().rstrip("\\`")
            try:
                tokens = shlex.split(line)[1:]
            except ValueError:
                continue
            if "--" in tokens:
                tokens = tokens[: tokens.index("--")]
            if not tokens or any(c in t for t in tokens for c in "<[{…"):
                continue
            checked += 1
            problem = _resolve(root, tokens, global_opts)
            if problem:
                failures.append(f"{md.relative_to(docs.parent)}: {line}\n    → {problem}")
    assert checked > 500, f"only {checked} doc examples found — the scan is broken, not clean"
    assert not failures, "the docs publish commands the CLI rejects:\n" + "\n".join(failures)


def test_it_is_actually_looking_at_the_whole_tree():
    root = typer.main.get_command(app)
    assert len(list(_walk(root, []))) > 400
