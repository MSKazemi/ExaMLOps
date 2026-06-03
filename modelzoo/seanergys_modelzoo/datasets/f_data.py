# Changed by Mohsen: added 'from __future__ import annotations' for Python 3.12 compatibility.
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple, TypeVar, Union, Iterable
from pathlib import Path
import time

import pandas as pd
import torch
from pydantic import Field, model_validator

from seanergys_modelzoo.datasets.common.seanergys_parquet_dataset import SeanergysParquetDataset
from seanergys_modelzoo.logger.seanergys_logger import SeanergysLogger


class FDataDataset(SeanergysParquetDataset):
    """
    Dataset class for F-DATA: A Fugaku Workload Dataset from Supercomputer Fugaku.

    F-DATA contains ~24 million jobs from Fugaku supercomputer with extensive features
    including exit codes, duration, power consumption, performance metrics (#flops,
    memory bandwidth, operational intensity, memory/compute bound labels).

    Dataset: https://zenodo.org/records/11467483
    Paper: https://doi.org/10.1038/s41597-025-05633-1

    The dataset is split across 38 monthly parquet files (YY_MM.parquet) covering
    March 2021 to April 2024.

    Key Features:
    - job_id: Unique job identifier
    - user_encoded: Anonymized user ID
    - account_encoded: Anonymized account ID
    - partition: Partition where job was executed
    - exit_code: Job exit code
    - n_nodes: Number of nodes allocated
    - duration: Job duration (seconds)
    - power_consumption: Average power consumption (watts)
    - flops: Floating point operations per second
    - memory_bandwidth: Memory bandwidth (GB/s)
    - operational_intensity: Ratio of computation to memory access
    - mem_compute_bound: Memory/compute bound classification
    - And more...
    """

    # Base URL for F-DATA files on Zenodo
    ZENODO_BASE_URL: str = r"https://zenodo.org/records/11467483/files"

    # Available monthly files
    AVAILABLE_FILES: List[str] = [
        # 2021
        "21_03", "21_04", "21_05", "21_06", "21_07", "21_08",
        "21_09", "21_10", "21_11", "21_12",
        # 2022
        "22_01", "22_02", "22_03", "22_04", "22_05", "22_06",
        "22_07", "22_08", "22_09", "22_10", "22_11", "22_12",
        # 2023
        "23_01", "23_02", "23_03", "23_04", "23_05", "23_06",
        "23_07", "23_08", "23_09", "23_10", "23_11", "23_12",
        # 2024
        "24_01", "24_02", "24_03", "24_04"
    ]

    # Pydantic fields specific to FDataDataset
    files: Optional[Union[str, List[str]]] = Field(
        default=None,
        description=(
            "Monthly file(s) to load. Can be: "
            "single file ('21_03'), list (['21_03', '21_04']), "
            "range ('21_03:21_06'), or 'all' / None for all files."
        )
    )
    use_zenodo_url: bool = Field(
        default=False,
        description="If True and data_path is None, load files from Zenodo."
    )
    download_path: Optional[Union[str, Path]] = Field(
        default=None,
        description="If provided, save the merged dataset to this directory after loading."
    )

    # Private — not a Pydantic field
    _file_list: Optional[List[str]] = None

    @model_validator(mode='after')
    def validate_model(self) -> 'FDataDataset':
        """Validate parameters, resolve file list, and trigger data loading."""
        
        if self.columns is None:
            self.columns = self.input_features + self.output_features

        # Enrich metadata with F-DATA specific info
        self.metadata.update({
            'dataset_name': 'F-DATA',
            'dataset_version': '1.0',
            'source': 'Supercomputer Fugaku (RIKEN, Japan)',
            'time_period': 'March 2021 - April 2024',
            'n_jobs': '~24 million',
            'doi': '10.5281/zenodo.11467483',
            'paper_doi': '10.1038/s41597-025-05633-1',
            'files_loaded': self._file_list if self._file_list else 'local'
        })

        # If dummy swap parameters
        if self.is_dummy:
            self.files = "21_04"
            self.filters = [("adt", "<", "2021-04-02")]
        
        # Resolve file list — per-file caches are handled in load_data, not here.
        # Resolution order: explicit data_path → backend (Phase 1) → legacy use_zenodo_url.
        if self.data_path is None and (self.backend is not None or self.use_zenodo_url):
            self._file_list = self._parse_files_parameter(self.files)
        elif self.data_path is None:
            raise ValueError(
                "data_path must be provided, backend must be set, or use_zenodo_url must be True."
            )
        else:
            self._file_list = None

        self.load_data()
        return self

    def _parse_files_parameter(self, files: Optional[Union[str, List[str]]]) -> List[str]:
        """
        Parse the files parameter to get list of file names to load.

        Args:
            files: File specification (None, "all", single file, list, or range)

        Returns:
            List of file names (without .parquet extension)
        """
        if files is None or files == "all":
            return self.AVAILABLE_FILES.copy()

        if isinstance(files, str):
            if ":" in files:
                start, end = files.split(":")
                start = start.replace(".parquet", "")
                end = end.replace(".parquet", "")
                try:
                    start_idx = self.AVAILABLE_FILES.index(start)
                    end_idx = self.AVAILABLE_FILES.index(end)
                    return self.AVAILABLE_FILES[start_idx:end_idx + 1]
                except ValueError as e:
                    raise ValueError(
                        f"Invalid file range: {files}. "
                        f"Both {start} and {end} must be in available files."
                    ) from e
            else:
                file_name = files.replace(".parquet", "")
                if file_name not in self.AVAILABLE_FILES:
                    raise ValueError(
                        f"File {file_name} not found in available files. "
                        f"Available: {self.AVAILABLE_FILES}"
                    )
                return [file_name]

        elif isinstance(files, list):
            file_list = [f.replace(".parquet", "") for f in files]
            invalid_files = [f for f in file_list if f not in self.AVAILABLE_FILES]
            if invalid_files:
                raise ValueError(
                    f"Invalid files: {invalid_files}. "
                    f"Available: {self.AVAILABLE_FILES}"
                )
            return file_list

        else:
            raise TypeError(
                f"files must be str, list, or None, got {type(files)}"
            )

    def load_data(self) -> None:
        """
        Load data from Parquet file(s) (local or remote) and merge them.
        """
        start_time = time.time()

        try:
            if (self.use_zenodo_url or self.backend is not None) and self._file_list:
                via = self.backend.name if self.backend else "zenodo-url"
                self.logger.info(
                    f"Loading {len(self._file_list)} F-DATA file(s) via {via}: "
                    f"{self._file_list[0]} to {self._file_list[-1]}"
                )

                cache_dir = Path(self.download_path) if self.download_path else Path(".data_cache/fdata")
                dfs = []
                for file_name in self._file_list:
                    # Backend path (Phase 1): delegate fetching/caching to the backend,
                    # which guarantees a local parquet-readable file.
                    if self.backend is not None:
                        source = str(self.backend.fetch(f"FData/{file_name}.parquet", cache_dir))
                        self.logger.info(f"  {self.backend.name}: {file_name}.parquet -> {source}")
                    else:
                        # Legacy Zenodo path with full-file caching.
                        cached_file = (
                            Path(self.download_path) / f"{file_name}_full.parquet"
                            if self.download_path else None
                        )
                        if cached_file and cached_file.exists():
                            self.logger.info(f"  Using cached {file_name}.parquet")
                            source = str(cached_file)
                        else:
                            file_url = f"{self.ZENODO_BASE_URL}/{file_name}.parquet"
                            self.logger.info(f"  Downloading {file_name}.parquet from Zenodo...")
                            try:
                                raw_chunk = pd.read_parquet(file_url)
                            except Exception as e:
                                self.logger.error(f"  Failed to download {file_name}.parquet: {e}")
                                raise
                            if cached_file:
                                cached_file.parent.mkdir(parents=True, exist_ok=True)
                                raw_chunk.to_parquet(cached_file, index=False)
                                self.logger.info(f"  Cached {file_name}.parquet to: {cached_file}")
                            source = str(cached_file) if cached_file else file_url

                    try:
                        df_chunk = pd.read_parquet(
                            source,
                            columns=self.columns,
                            filters=self.filters,
                        )
                    except Exception as e:
                        self.logger.error(f"  Failed to load {file_name}.parquet: {e}")
                        raise
                    dfs.append(df_chunk)
                    self.logger.info(f"  Loaded {file_name}.parquet: {len(df_chunk)} rows")

                self.logger.info("Merging all dataframes...")
                self._df = pd.concat(dfs, ignore_index=True)
                self.logger.info(f"Merged F-DATA with shape: {self._df.shape}")

            else:
                data_path = Path(self.data_path)

                if data_path.is_dir():
                    self.logger.info(f"Loading F-DATA from directory: {data_path}")
                    parquet_files = sorted(data_path.glob("*.parquet"))
                    if self._file_list is not None:
                        parquet_files = [f for f in parquet_files if f.stem in self._file_list]
                    if not parquet_files:
                        raise ValueError(f"No parquet files found in {data_path}")

                    self.logger.info(f"Found {len(parquet_files)} parquet file(s)")
                    dfs = []
                    for file_path in parquet_files:
                        self.logger.info(f"Loading {file_path.name}...")
                        df_chunk = pd.read_parquet(
                            file_path,
                            columns=self.columns,
                            filters=self.filters
                        )
                        dfs.append(df_chunk)
                        self.logger.info(f"  Loaded: {len(df_chunk)} rows")

                    self._df = pd.concat(dfs, ignore_index=True)
                    self.logger.info(f"Merged F-DATA with shape: {self._df.shape}")

                else:
                    self.logger.info(f"Loading F-DATA from local file: {data_path}")
                    self._df = pd.read_parquet(
                        data_path,
                        columns=self.columns,
                        filters=self.filters
                    )
                    self.logger.info(f"Loaded F-DATA with shape: {self._df.shape}")

            # Apply preprocessing: each entry is (columns, function, output_feature_name)
            for fn in self.preprocessing_functions:
                self._df = fn(self._df)

            # Update statistics
            self.stats.update({
                'n_samples': len(self._df),
                'n_input_features': len(self.input_features),
                'n_output_features': len(self.output_features),
                'n_features': len(self.input_features),
                'load_time': time.time() - start_time,
            })

            self.logger.info(
                f"{self.__class__.__name__} ready: {self.stats['n_samples']} samples, "
                f"{self.stats['n_input_features']} input features, "
                f"{self.stats['n_output_features']} output features "
                f"(loaded in {self.stats['load_time']:.2f}s)"
            )

        except Exception as e:
            self.logger.error(f"Failed to load {self.__class__.__name__} file(s): {e}")
            raise

    def __getitem__(self, idx: int, return_tensor: bool = False, dtype: Union[str, TypeVar] = None) -> Tuple:
        """
        Retrieve a single sample from the dataset.

        Args:
            idx: Index of the sample to retrieve
            return_tensor: If True, return torch tensors instead of numpy arrays
            dtype: Tensor dtype (only used when return_tensor=True)

        Returns:
            Tuple of (input_data, output_data)
        """
        if idx >= len(self):
            raise IndexError(
                f"Index {idx} out of range for dataset of size {len(self)}"
            )

        input_data = self._df[self.input_features].iloc[idx].values
        output_data = self._df[self.output_features].iloc[idx].values

        if self.transform is not None:
            input_data = self.transform(input_data)

        if self.target_transform is not None:
            output_data = self.target_transform(output_data)

        if return_tensor:
            input_data = torch.tensor(input_data, dtype=dtype)
            output_data = torch.tensor(output_data, dtype=dtype)

        return input_data, output_data

    def get_statistics_by_partition(self) -> Dict[str, Dict[str, float]]:
        """
        Compute job statistics grouped by partition.

        Returns:
            Dictionary mapping partition name to job statistics.
        """
        if self._df is None:
            raise RuntimeError("Data not loaded. Call load_data() first.")
        if 'partition' not in self._df.columns:
            raise ValueError("'partition' column not available in dataset.")

        partition_stats = {}
        for partition in self._df['partition'].unique():
            partition_jobs = self._df[self._df['partition'] == partition]
            partition_stats[partition] = {'n_jobs': len(partition_jobs)}

            for col, key in [('duration', 'avg_duration'), ('n_nodes', 'avg_nodes')]:
                if col in partition_jobs.columns:
                    partition_stats[partition][key] = float(partition_jobs[col].mean())

            if 'power_consumption' in partition_jobs.columns:
                partition_stats[partition]['avg_power'] = float(
                    partition_jobs['power_consumption'].mean()
                )

        return partition_stats

    @classmethod
    def get_available_files(cls) -> List[str]:
        """Return a copy of all available monthly file identifiers."""
        return cls.AVAILABLE_FILES.copy()

    @classmethod
    def get_date_range(cls) -> Tuple[str, str]:
        """
        Return the date range covered by the dataset.

        Returns:
            Tuple of (start_date, end_date) in YY_MM format.
        """
        return (cls.AVAILABLE_FILES[0], cls.AVAILABLE_FILES[-1])
    
