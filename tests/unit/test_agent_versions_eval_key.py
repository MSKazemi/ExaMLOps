"""ADR 0146 d1: ``eval_suite_results`` carries the ``agent_version_id`` it measures.

Recorded automatically for results under an agent's ``agent-<name>`` key; looked up per version
in SQL before the LIMIT; legacy rows (written before the column existed) still match; and the
evidence pack is built from the keyed lookup.
"""

from __future__ import annotations

from examlops.data.evaluation import (
    get_eval_results_for_agent_version,
    record_eval_result,
)
from examlops.platform_db import get_db, init_db

V1 = "av-sha256:" + "1" * 64
V2 = "av-sha256:" + "2" * 64


def _col(run_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT agent_version_id FROM eval_suite_results WHERE run_id=?", (run_id,)
        ).fetchone()
    return row[0]


def test_agent_results_are_keyed_to_their_version_and_models_are_not():
    init_db()
    record_eval_result("traj", "agent-jobdoc", {"task_success": 0.8}, run_id="r1", model_version=V1)
    record_eval_result("traj", "jpcp", {"rmse": 3.1}, run_id="r2", model_version="17")
    record_eval_result(  # an explicit key wins over the derivation
        "traj", "custom", {"task_success": 0.7}, run_id="r3", model_version="x", agent_version_id=V2
    )
    assert _col("r1") == V1
    assert _col("r2") is None
    assert _col("r3") == V2


def test_the_lookup_filters_by_version_before_the_limit():
    init_db()
    # A busy sibling version fills the newest rows...
    for i in range(30):
        record_eval_result(
            "traj", "agent-jobdoc", {"task_success": 0.5}, run_id=f"v2-{i}", model_version=V2
        )
    record_eval_result("traj", "agent-jobdoc", {"task_success": 0.9}, run_id="v1", model_version=V1)
    for i in range(30, 40):
        record_eval_result(
            "traj", "agent-jobdoc", {"task_success": 0.5}, run_id=f"v2-{i}", model_version=V2
        )
    # ...and still cannot crowd V1's one row out of a small window.
    rows = get_eval_results_for_agent_version("jobdoc", V1, limit=5)
    assert [r["run_id"] for r in rows] == ["v1"]
    assert len(get_eval_results_for_agent_version("jobdoc", V2, limit=5)) == 5
    assert get_eval_results_for_agent_version("other", V1) == []  # another agent's key
    assert get_eval_results_for_agent_version("jobdoc", V1, suite="safety") == []


def test_rows_written_before_the_column_existed_still_match():
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO eval_suite_results (suite, model, model_version, metric, score, run_id) "
            "VALUES ('traj', 'agent-jobdoc', ?, 'task_success', 0.6, 'legacy')",
            (V1,),
        )
    rows = get_eval_results_for_agent_version("jobdoc", V1)
    assert [r["run_id"] for r in rows] == ["legacy"] and rows[0]["agent_version_id"] is None
