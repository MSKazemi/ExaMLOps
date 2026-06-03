"""Unit tests for SeanergysModelMetadata."""




def test_metadata_instantiation():
    """SeanergysModelMetadata requires name, others optional."""
    from seanergys_modelzoo.models.common.seanergys_model_metadata import (
        SeanergysModelMetadata,
    )

    meta = SeanergysModelMetadata(name="test-model")
    assert meta.name == "test-model"
    assert meta.version == "1.0"
    assert meta.is_active is True


def test_metadata_serialize_deserialize():
    """SeanergysModelMetadata round-trips via model_dump and from_dict."""
    from seanergys_modelzoo.models.common.seanergys_model_metadata import (
        SeanergysModelMetadata,
    )

    meta = SeanergysModelMetadata(
        name="fraud-detector",
        version="2.1.0",
        description="Test model",
        framework="PyTorch",
        metrics={"accuracy": 0.97},
    )
    d = meta.model_dump()
    meta2 = SeanergysModelMetadata.from_dict(d)
    assert meta2.name == meta.name
    assert meta2.version == meta.version
    assert meta2.metrics == meta.metrics


def test_metadata_set_field():
    """SeanergysModelMetadata.set_field updates a field."""
    from seanergys_modelzoo.models.common.seanergys_model_metadata import (
        SeanergysModelMetadata,
    )

    meta = SeanergysModelMetadata(name="test-model")
    meta.set_field("description", "Updated description")
    assert meta.description == "Updated description"
