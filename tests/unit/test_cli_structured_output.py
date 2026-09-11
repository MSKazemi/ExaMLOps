"""One document per command in every structured mode — enforced by `_output`, not per command.

147 command functions call both `ok()` and a data printer; most guard the order by hand, and some
did not — `exa --json retrain` printed three documents (the ok message, the record, the raw
result). In a structured mode `print_json` now buffers, and the command emits exactly one
document when it closes: the only one, or the status and data documents merged.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from examlops.cli import _output


class _Ctx:
    def __init__(self):
        self.closers = []

    def call_on_close(self, fn):
        self.closers.append(fn)

    def close(self):
        for fn in self.closers:
            fn()


@pytest.mark.parametrize(
    ("docs", "merged"),
    [
        ([{"ok": True, "message": "m"}], {"ok": True, "message": "m"}),
        ([[1, 2]], [1, 2]),
        ([{"ok": True, "message": "m"}, {"a": 1}], {"ok": True, "message": "m", "a": 1}),
        (
            [{"ok": True, "message": "m"}, {"flow_run_id": "f"}, {"state": "RUNNING"}],
            {"ok": True, "message": "m", "flow_run_id": "f", "state": "RUNNING"},
        ),
        ([[], {"ok": True, "message": "valid"}], {"ok": True, "message": "valid", "items": []}),
        (
            [{"ok": True, "message": "m"}, {"error": "boom", "exit_code": 1}],
            {"message": "m", "error": "boom", "exit_code": 1},
        ),
        ([[1], [2]], {"items": [[1], [2]]}),
    ],
)
def test_merge_documents(docs, merged):
    assert _output.merge_documents(docs) == merged


def _guarded(monkeypatch, capsys, fmt="json"):
    monkeypatch.setattr(_output, "json_mode", True)
    monkeypatch.setattr(_output, "output_format", fmt)
    ctx = _Ctx()
    _output.install_structured_guard(ctx)
    return ctx


def test_ok_then_record_then_result_is_one_document(monkeypatch, capsys):
    ctx = _guarded(monkeypatch, capsys)
    _output.ok("Retrain scheduled for JPCP")
    _output.print_record({"flow_run_id": "f-1", "model": "JPCP"})
    _output.print_json({"flow_run_id": "f-1", "state": "SCHEDULED"})
    ctx.close()
    out = capsys.readouterr().out
    assert json.loads(out) == {
        "ok": True,
        "message": "Retrain scheduled for JPCP",
        "flow_run_id": "f-1",
        "model": "JPCP",
        "state": "SCHEDULED",
    }


def test_nothing_printed_still_yields_one_document(monkeypatch, capsys):
    ctx = _guarded(monkeypatch, capsys)
    _output.info("No budgets configured.")
    ctx.close()
    assert json.loads(capsys.readouterr().out) == {"ok": True, "message": "No budgets configured."}


def test_the_merged_document_honours_the_output_format(monkeypatch, capsys):
    ctx = _guarded(monkeypatch, capsys, fmt="yaml")
    _output.ok("done")
    _output.print_record({"a": 1})
    ctx.close()
    out = capsys.readouterr().out
    assert "a: 1" in out and "{" not in out


def test_an_error_after_output_is_still_one_document_and_still_fails(monkeypatch, capsys):
    import typer

    ctx = _guarded(monkeypatch, capsys)
    _output.print_record({"step": 1})
    with pytest.raises(typer.Exit) as exc:
        _output.error("second step failed", hint="retry")
    ctx.close()
    assert exc.value.exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "step": 1,
        "error": "second step failed",
        "exit_code": 1,
        "hint": "retry",
    }


def test_table_mode_is_untouched(monkeypatch, capsys):
    monkeypatch.setattr(_output, "json_mode", False)
    _output.ok("done")
    assert "done" in capsys.readouterr().out


def test_retrain_prints_one_document_end_to_end(monkeypatch, tmp_path):
    # Through the real root app: dry-run path, which needs no control plane.
    from examlops.cli.main import app

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "c.toml"))
    result = CliRunner().invoke(
        app, ["--json", "retrain", "JPCP", "--dataset", "PM100Dataset", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    json.loads(result.output)  # exactly one JSON value
