"""Unit tests for dataset contract (__len__, __getitem__, supported_features)."""

import pytest

pytest.importorskip("torch")


def test_dataset_len_and_getitem():
    """Minimal dataset implements __len__ and __getitem__."""
    from seanergys_modelzoo.datasets.common.seanergys_dataset import SeanergysDataset

    # Create a minimal concrete dataset for testing the contract
    class SyntheticDataset(SeanergysDataset):
        """Minimal dataset with 3 synthetic samples."""

        def _load_data_impl(self) -> None:
            self._samples = [([i * 1.0, i * 2.0], i) for i in range(3)]

        def __getitem__(self, idx: int):
            return self._samples[idx]

        def __len__(self) -> int:
            return len(self._samples)

    ds = SyntheticDataset()
    ds._load_data_impl()
    assert len(ds) == 3
    x, y = ds[0]
    assert x == [0.0, 0.0]
    assert y == 0
    x, y = ds[2]
    assert x == [2.0, 4.0]
    assert y == 2


def test_dataset_get_stats():
    """Dataset get_stats returns dict."""
    from seanergys_modelzoo.datasets.common.seanergys_dataset import SeanergysDataset

    class MinimalDataset(SeanergysDataset):
        def _load_data_impl(self) -> None:
            self._samples = []

        def __getitem__(self, idx: int):
            return self._samples[idx]

        def __len__(self) -> int:
            return len(self._samples)

    ds = MinimalDataset()
    ds._load_data_impl()
    stats = ds.get_stats()
    assert isinstance(stats, dict)
    assert "n_samples" in stats or "load_time" in stats or "n_features" in stats
