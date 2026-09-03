import pytest

# Import the static method directly — no Ray runtime needed.
# This import will fail until Task 2 creates the file.
from serving.inference_pipeline.app import FeatureTransformer, ModelRouter


class TestTransformOne:
    def test_valid_input_produces_feature_dict(self):
        req = {
            "job_id": "j1",
            "model_name": "JPCP",
            "alias": "Production",
            "embedding": [0.1] * 384,
            "num_nodes": 4,
            "user_id": "u001",
        }
        result = FeatureTransformer._transform_one(req)
        # Models are FData-trained: only the embedding goes to the model.
        assert result["features"] == {"embedding": [0.1] * 384}
        assert result["num_nodes"] == 4
        assert result["user_id"] == "u001"
        assert result["model_name"] == "JPCP"
        assert result["alias"] == "Production"
        assert result["job_id"] == "j1"

    def test_wrong_embedding_size_raises(self):
        req = {"embedding": [0.1] * 383, "num_nodes": 4}
        with pytest.raises(ValueError, match="expected 384"):
            FeatureTransformer._transform_one(req)

    def test_missing_embedding_raises(self):
        req = {"num_nodes": 4}
        with pytest.raises(ValueError, match="embedding is required"):
            FeatureTransformer._transform_one(req)

    def test_missing_num_nodes_raises(self):
        req = {"embedding": [0.1] * 384}
        with pytest.raises(ValueError, match="num_nodes is required"):
            FeatureTransformer._transform_one(req)

    def test_num_nodes_coerced_to_int(self):
        req = {"embedding": [0.0] * 384, "num_nodes": "8"}
        result = FeatureTransformer._transform_one(req)
        assert result["num_nodes"] == 8
        assert isinstance(result["num_nodes"], int)

    def test_user_id_coerced_to_str(self):
        req = {"embedding": [0.0] * 384, "num_nodes": 2, "user_id": 42}
        result = FeatureTransformer._transform_one(req)
        assert result["user_id"] == "42"

    def test_missing_optional_fields_default_to_none_or_empty(self):
        req = {"embedding": [0.0] * 384, "num_nodes": 1}
        result = FeatureTransformer._transform_one(req)
        assert result["model_name"] is None
        assert result["alias"] is None
        assert result["job_id"] is None


class TestResolve:
    def test_explicit_model_and_alias_returned_as_is(self):
        payload = {"model_name": "MACK", "alias": "Canary", "features": {}}
        model, alias = ModelRouter._resolve(payload)
        assert model == "mack"
        assert alias == "Canary"

    def test_missing_model_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SEANERBUS_DEFAULT_MODEL", "JPCP")
        import importlib

        import serving.inference_pipeline.app as m

        importlib.reload(m)
        payload = {"alias": "Production", "features": {}}
        model, _ = m.ModelRouter._resolve(payload)
        assert model == "jpcp"

    def test_empty_string_model_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SEANERBUS_DEFAULT_MODEL", "JPCP")
        import importlib

        import serving.inference_pipeline.app as m

        importlib.reload(m)
        payload = {"model_name": "", "alias": "Production", "features": {}}
        model, _ = m.ModelRouter._resolve(payload)
        assert model == "jpcp"

    def test_missing_alias_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SEANERBUS_DEFAULT_ALIAS", "Production")
        import importlib

        import serving.inference_pipeline.app as m

        importlib.reload(m)
        payload = {"model_name": "JPCP", "features": {}}
        _, alias = m.ModelRouter._resolve(payload)
        assert alias == "Production"


class TestTrafficSplitCache:
    """S3/S8 regression guards: the split cache must expire, and must cache negatives."""

    def test_negative_result_cached_within_ttl(self, monkeypatch):
        import serving.inference_pipeline.app as m

        m._traffic_rules.clear()
        calls = []
        monkeypatch.setattr(m, "_db_get_traffic", lambda name: calls.append(name) or None)
        assert m._get_split("jpcp") is None
        assert m._get_split("jpcp") is None
        assert len(calls) == 1, "a split-less model must not pay a DB read per request"

    def test_split_change_applies_after_ttl(self, monkeypatch):
        import serving.inference_pipeline.app as m

        m._traffic_rules.clear()
        monkeypatch.setattr(m, "_TRAFFIC_TTL_SECONDS", 0.0)
        vals = iter([{"Production": 90, "Canary": 10}, {"Production": 100}])
        monkeypatch.setattr(m, "_db_get_traffic", lambda name: next(vals))
        assert m._get_split("jpcp") == {"Production": 90, "Canary": 10}
        # TTL expired → the rolled-back split must be re-read, not served stale forever.
        assert m._get_split("jpcp") == {"Production": 100}

    def test_db_error_degrades_to_no_split(self, monkeypatch):
        import serving.inference_pipeline.app as m

        m._traffic_rules.clear()

        def boom(name):
            raise RuntimeError("db down")

        monkeypatch.setattr(m, "_db_get_traffic", boom)
        assert m._get_split("jpcp") is None
