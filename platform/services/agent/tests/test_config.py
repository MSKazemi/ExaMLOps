def test_defaults_present():
    from skipper import config

    assert config.MLFLOW_URL.startswith("http")
    assert config.RAY_SERVE_URL.startswith("http")
    assert config.CONTROL_PLANE_URL.startswith("http")
    assert config.DASHBOARD_URL.startswith("http")
    assert config.AGENT_DB.endswith(".db")


def test_docs_root_points_at_repo_docs():
    from skipper import config

    assert config.AGENT_DOCS_ROOT.endswith("docs")
    assert config.CLAUDE_MD.endswith("CLAUDE.md")
