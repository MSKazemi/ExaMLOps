# tests/unit/test_scheduler_jobs.py
"""`examlops.scheduler_jobs` — the rules every generated scheduler job follows, held directly.

Asset builds (ADR 0036) and reindex jobs (ADR 0043) are tested end-to-end in their own files; this
holds the shared rules on their own, so a third caller inherits a guarded contract.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))

import examlops  # noqa: E402
from examlops import scheduler_jobs as jobs  # noqa: E402


def test_nothing_in_a_title_can_leave_its_comment(tmp_path):
    marker = tmp_path / "pwned"
    text = jobs.script_text(["/bin/true"], title=f"x\ntouch {marker}\r\n# and\n\ntouch {marker}")
    script = tmp_path / "run.sh"
    script.write_text(text)

    subprocess.run(["bash", str(script)], check=True)

    assert not marker.exists()
    assert sum(line.startswith("#") for line in text.splitlines()) == 2  # shebang + title


def test_every_argument_reaches_the_program_intact(tmp_path):
    argv_file = tmp_path / "argv"
    hostile = ["$(id)", "`id`", "a b", "'q'", '"dq"', "; exit 7", "\n", ""]
    text = jobs.script_text(
        [
            sys.executable,
            "-c",
            f"import sys; open({str(argv_file)!r},'w').write(repr(sys.argv[1:]))",
        ]
        + hostile,
        title="argv",
    )
    script = tmp_path / "run.sh"
    script.write_text(text)

    subprocess.run(["bash", str(script)], check=True)

    assert argv_file.read_text() == repr(hostile)


def test_the_submitters_examlops_comes_first(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_HPC_REMOTE_REPO", raising=False)
    roots = jobs.pythonpath_roots(sys.executable, [Path("/shared/code")])

    assert roots[0] == str(Path(examlops.__file__).resolve().parent.parent)
    assert roots[1] == "/shared/code"


def test_paths_in_the_checkout_are_re_rooted_on_the_cluster(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_HPC_REMOTE_REPO", "/cluster/examlops")
    roots = jobs.pythonpath_roots("/cluster/examlops/.venv/bin/python")

    src = Path(examlops.__file__).resolve().parent.parent  # platform/cli/src in a checkout
    assert roots == [str(Path("/cluster/examlops") / src.relative_to(jobs.repo_root()))]


def test_scripts_are_private_and_outside_the_repository(monkeypatch, tmp_path):
    monkeypatch.delenv("EXAMLOPS_JOB_SCRIPT_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    script, key = jobs.write_script("unit", "#!/bin/sh\n")

    assert script.parent == tmp_path / "cache" / "examlops" / "jobs" / key
    assert stat.S_IMODE(script.stat().st_mode) == 0o700
    assert not script.resolve().is_relative_to(ROOT)


def test_the_v0_53_name_for_the_script_dir_still_works(monkeypatch, tmp_path):
    """`EXAMLOPS_ASSET_JOB_DIR` shipped in v0.53.0; renaming it must not move a deployment's
    scripts. The new name wins when both are set."""
    monkeypatch.delenv("EXAMLOPS_JOB_SCRIPT_DIR", raising=False)
    monkeypatch.setenv("EXAMLOPS_ASSET_JOB_DIR", str(tmp_path / "old"))
    assert jobs.job_dir() == tmp_path / "old"

    monkeypatch.setenv("EXAMLOPS_JOB_SCRIPT_DIR", str(tmp_path / "new"))
    assert jobs.job_dir() == tmp_path / "new"
