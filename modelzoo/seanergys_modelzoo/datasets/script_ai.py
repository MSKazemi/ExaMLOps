import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from seanergys_modelzoo.datasets.common.seanergys_dataset import SeanergysDataset
from seanergys_modelzoo.logger.seanergys_logger import SeanergysLogger


class SCRIPTAIDataset(SeanergysDataset):
    """
    Dataset class for SCRIPT-AI: A collection of HPC job scripts.

    This dataset contains job submission scripts for different HPC schedulers
    (SLURM, pjsub, PBS, etc.) along with extracted features describing each script.

    Repository: https://github.com/francescoantici/SCRIPT-AI
    """

    # Raw link to the data of the repo of SCRIPT-AI
    RAW_GITHUB_URL: str = (
        "https://raw.githubusercontent.com/francescoantici/SCRIPT-AI/refs/heads/main"
    )

    # Pydantic fields
    output_features: List[str] = Field(
        ..., description="List of column names to use as output/target features."
    )
    use_github_url: bool = Field(
        default=False,
        description="If True and data_path is None, load the dataset from GitHub."
    )
    download_path: Optional[Union[str, Path]] = Field(
        default=None,
        description="If provided, persist loaded data to this directory."
    )

    # Private — not Pydantic fields
    _df: Optional[pd.DataFrame] = None

    @model_validator(mode='after')
    def validate_model(self) -> 'SCRIPTAIDataset':
        """Resolve data_path, enrich metadata, and trigger data loading."""
        if self.data_path is None and self.use_github_url:
            self.logger.info(
                f"Using GitHub URL for SCRIPT-AI dataset: {self.RAW_GITHUB_URL}"
            )
            self.data_path = self.RAW_GITHUB_URL
        elif self.data_path is None:
            raise ValueError(
                "data_path must be provided or use_github_url must be True."
            )

        self.metadata.update({
            'dataset_name': 'SCRIPT-AI',
            'dataset_version': '1.0',
            'source': 'GitHub',
            'n_jobs': '651'
        })

        self.load_data()
        return self

    def load_data(self) -> None:
        """
        Load the SCRIPT-AI dataset.

        Loads:
        1. features.csv — metadata/features for each job script
        2. Job script contents — fetched from disk or remote URL via the
           'jid' column in features.csv
        """
        start_time = time.time()

        self.logger.info(f"Loading SCRIPT-AI dataset from {self.data_path}")

        self._load_features()
        self._load_job_scripts()

        self.stats.update({
            'load_time': time.time() - start_time,
            'n_samples': len(self._df),
            'n_output_features': len(self.output_features),
            'n_features': len(self._df.columns),
        })

        self.logger.info(
            f"Loaded {self.stats['n_samples']} scripts with "
            f"{self.stats['n_features']} features in {self.stats['load_time']:.2f}s"
        )

    def _load_features(self) -> None:
        """Load features.csv (local or remote) into self._df."""
        data_path_str = str(self.data_path)
        features_path = f"{data_path_str}/features.csv"

        if data_path_str.startswith(('http://', 'https://', 's3://', 'gs://')):
            self.logger.info(
                f"Loading {self.__class__.__name__} features from remote: {features_path}"
            )
        else:
            self.logger.info(
                f"Loading {self.__class__.__name__} features from local path: {features_path}"
            )

        try:
            # Always load at minimum the columns we need; include 'jid' for script lookup
            usecols = list(dict.fromkeys(['jid'] + list(self.output_features)))
            self._df = pd.read_csv(features_path, usecols=usecols)
            self.logger.info(
                f"Loaded {self.__class__.__name__} features with shape: {self._df.shape}"
            )

            if self.download_path:
                download_path = Path(self.download_path)
                download_path.mkdir(parents=True, exist_ok=True)
                out_file = download_path / "features.csv"
                self._df.to_csv(out_file, index=False)
                self.logger.info(
                    f"Saved {self.__class__.__name__} features.csv to: {out_file}"
                )

        except Exception as e:
            self.logger.error(
                f"Failed to load {self.__class__.__name__} features.csv: {e}"
            )
            raise

    def _load_job_scripts(self) -> None:
        """Load job script contents into self._df['script'] via 'jid' column."""
        if self._df is None or 'jid' not in self._df.columns:
            raise RuntimeError(
                "'jid' column is required in features.csv to load job scripts."
            )

        data_path_str = str(self.data_path)

        if data_path_str.startswith(('http://', 'https://', 's3://', 'gs://')):
            self.logger.info(
                f"Loading {self.__class__.__name__} scripts from remote: {data_path_str}"
            )

            def loading_function(job_id: str) -> Optional[str]:
                url = f"{data_path_str}/{job_id}"
                try:
                    with urllib.request.urlopen(url) as response:
                        return response.read().decode('utf-8')
                except Exception as e:
                    self.logger.error(f"Failed to fetch script {job_id}: {e}")
                    return None
        else:
            self.logger.info(
                f"Loading {self.__class__.__name__} scripts from local path: {data_path_str}"
            )

            def loading_function(job_id: str) -> Optional[str]:
                path = os.path.join(data_path_str, job_id)
                try:
                    with open(path) as f:
                        return f.read()
                except Exception as e:
                    self.logger.error(f"Failed to read script {job_id}: {e}")
                    return None

        self._df['script'] = self._df['jid'].apply(loading_function)
        valid_scripts = int(self._df['script'].notna().sum())

        self.logger.info(f"Found {valid_scripts} valid script files.")
        self.metadata['valid_scripts'] = valid_scripts

    def __len__(self) -> int:
        """Return the number of samples in the dataset."""
        if self._df is None:
            return 0
        return len(self._df)

    def __getitem__(self, idx: int) -> Tuple:
        """
        Retrieve a single sample from the dataset.

        Args:
            idx: Index of the sample to retrieve

        Returns:
            Tuple of (script_text, output_values)
        """
        if self._df is None:
            raise RuntimeError("Data not loaded. Call load_data() first.")
        if idx >= len(self):
            raise IndexError(
                f"Index {idx} out of range for dataset of size {len(self)}"
            )

        input_data = self._df['script'].iloc[idx]
        output_data = self._df[self.output_features].iloc[idx].values

        if self.transform is not None:
            input_data = self.transform(input_data)

        if self.target_transform is not None:
            output_data = self.target_transform(output_data)

        return input_data, output_data

    def get_stats(self) -> Dict[str, Any]:
        """
        Return dataset statistics, extended with SCRIPT-AI-specific info.
        """
        stats = super().get_stats()

        if self._df is not None:
            numeric_cols = self._df.select_dtypes(include=[np.number])
            stats['feature_stats'] = {
                'mean': numeric_cols.mean().to_dict(),
                'std': numeric_cols.std().to_dict(),
            }

        stats['valid_scripts'] = self.metadata.get('valid_scripts', 0)
        return stats

    def summary(self) -> str:
        """Return a human-readable summary of the dataset."""
        n_samples = len(self._df) if self._df is not None else 0
        lines = [
            "SCRIPT-AI Dataset Summary",
            "=" * 50,
            f"Dataset Path:   {self.data_path}",
            f"Total Samples:  {n_samples}",
            f"Output Features:{self.output_features}",
            f"Load Time:      {self.stats.get('load_time', 0):.2f}s",
            f"Valid Scripts:  {self.metadata.get('valid_scripts', 'N/A')}",
        ]
        return "\n".join(lines)