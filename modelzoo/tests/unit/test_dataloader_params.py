"""Unit tests for SeanergysDataloaderParams validation."""


def test_dataloader_params_to_dict():
    """SeanergysDataloaderParams serializes to dict."""
    from seanergys_modelzoo.models.common.seanergys_configurator import (
        SeanergysDataloaderParams,
    )

    params = SeanergysDataloaderParams(batch_size=8, drop_last=True)
    d = params.to_dict()
    assert isinstance(d, dict)
    assert d.get("batch_size") == 8
    assert d.get("drop_last") is True


def test_dataloader_params_from_dict():
    """SeanergysDataloaderParams instantiates from dict."""
    from seanergys_modelzoo.models.common.seanergys_configurator import (
        SeanergysDataloaderParams,
    )

    config = {"batch_size": 16, "shuffle": True}
    params = SeanergysDataloaderParams.from_dict(config)
    assert params.batch_size == 16
    assert params.shuffle is True





def test_dataloader_params_roundtrip():
    """SeanergysDataloaderParams round-trips via to_dict and from_dict."""
    from seanergys_modelzoo.models.common.seanergys_configurator import (
        SeanergysDataloaderParams,
    )

    original = SeanergysDataloaderParams(
        batch_size=32,
        shuffle=True,
        num_workers=2,
    )
    d = original.to_dict()
    restored = SeanergysDataloaderParams.from_dict(d)
    assert restored.batch_size == original.batch_size
    assert restored.shuffle == original.shuffle
    assert restored.num_workers == original.num_workers
