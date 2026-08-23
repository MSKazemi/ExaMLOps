"""`exa chat`'s remedy must be one that works in the environment printing it.

The hint said `uv pip install 'examlops[chat]'`. On a deploy whose source was synced
without re-running the install, the recorded metadata is older than the source and has
never heard of that extra — and uv answers an unknown extra with "Checked 1 package":
no warning, no error, nothing installed. Measured on lxp-cpu01 on 2026-08-23 (source
v0.48.0, recorded metadata v0.46.0), where the operator ran the remedy four times.

So the hint has to consult this install's own metadata before naming the extra.
"""

from __future__ import annotations

import importlib.metadata

import pytest
from typer.testing import CliRunner

from examlops.cli.main import app


class _Meta:
    def __init__(self, extras):
        self._extras = extras

    def get_all(self, key):
        return self._extras if key == "Provides-Extra" else None


class _Dist:
    def __init__(self, extras):
        self.metadata = _Meta(extras)


@pytest.fixture
def no_kq(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)


def _run():
    """The rendered hint, unwrapped — Rich breaks it across terminal lines."""
    return " ".join(CliRunner().invoke(app, ["chat"]).output.split())


def test_the_extra_is_named_when_this_install_declares_it(no_kq, monkeypatch):
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: _Dist(["chat", "synth"]))
    out = _run()
    assert "examlops[chat]" in out


def test_a_stale_install_is_told_the_form_that_works(no_kq, monkeypatch):
    """The whole point: never print a remedy that this interpreter cannot carry out."""
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: _Dist(["synth"]))
    out = _run()
    # the *imperative* must be the form that works here...
    assert "Install it with: uv pip install kube-q" in out
    # ...and the extra may only appear inside the explanation of why it would not
    assert "does not declare the 'chat' extra" in out
    assert out.index("uv pip install kube-q") < out.index("examlops[chat]")


def test_no_metadata_at_all_still_gives_a_usable_answer(no_kq, monkeypatch):
    def boom(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", boom)
    assert "Install it with: uv pip install kube-q" in _run()
