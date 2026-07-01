def test_tools_assembled():
    from skipper.tools import TOOLS

    names = {t.name for t in TOOLS}
    for expected in [
        "list_models",
        "predict",
        "get_metrics",
        "trigger_retrain",
        "list_pending_approvals",
        "modelzoo_status",
        "list_services",
        "list_deployments",
        "search_docs",
    ]:
        assert expected in names, f"missing {expected}"
    assert len(TOOLS) >= 30


def test_write_tools_registered():
    from skipper import (
        confirm,
        tools,  # noqa: F401  (import triggers tool module loading)
    )

    for w in [
        "trigger_retrain",
        "approve_model",
        "reject_model",
        "reload_models",
        "scaffold_create",
    ]:
        assert w in confirm.WRITE_TOOLS


def test_memory_persists(tmp_path):
    from skipper.memory import build_checkpointer

    db = str(tmp_path / "mem.db")
    saver = build_checkpointer(db)
    assert saver is not None
    import os

    assert os.path.exists(db)
