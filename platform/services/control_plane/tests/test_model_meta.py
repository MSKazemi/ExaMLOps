import pytest
from model_meta import ModelMeta, get_model_meta, list_model_names, read_readme


def test_list_model_names_returns_jpcp_mack_mcbound():
    names = list_model_names()
    assert "JPCP" in names
    assert "MACK" in names
    assert "MCBound" in names


def test_get_model_meta_jpcp():
    meta = get_model_meta("JPCP")
    assert isinstance(meta, ModelMeta)
    assert meta.task_type in ("regression", "classification")
    assert "PM100Dataset" in meta.supported_datasets
    assert meta.estimator_class != ""
    assert meta.promotion["metric"]
    assert meta.promotion["direction"] in ("higher_is_better", "lower_is_better")
    assert meta.path_in_repo.endswith("/jpcp/")


def test_get_model_meta_unknown_raises_lookup():
    with pytest.raises(LookupError):
        get_model_meta("DoesNotExist")


def test_read_readme_jpcp_returns_text_and_sha():
    text, sha = read_readme("JPCP")
    assert sha and len(sha) == 64
    assert text  # JPCP has README.md today


def test_read_readme_unknown_returns_empty_string_and_empty_sha():
    text, sha = read_readme("DoesNotExist")
    assert text == ""
    assert sha == ""
