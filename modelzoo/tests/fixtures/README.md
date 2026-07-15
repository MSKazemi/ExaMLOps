# Test Fixtures

## Sample Data (for local and CI tests)

Small parquet files for dataset load and integration tests. Created by
`make sample-data` — auto-discovers all models via `get_train_config()` and
downloads a raw subset from Zenodo for each unique dataset found.

No dataset names are hardcoded. Adding a new model with `get_train_config()`
automatically causes its dataset fixture to be created on the next run.

### Create sample data

```bash
# From project root
make sample-data

# Limit rows per fixture (default: 100)
SAMPLE_MAX_ROWS=50 make sample-data

# Force re-download (e.g. after schema change)
make clean-fixtures && make sample-data
```

### Output

```
tests/fixtures/sample_data/
├── sample_fdata.parquet    # F-DATA subset (~100 rows, all columns)
└── sample_pm100.parquet    # PM100 subset (~100 rows, all columns)
```

New datasets appear here automatically once a model references them in
`get_train_config()`.

### Usage in tests

- `tests/integration/test_model_train.py` — train each model on its fixture
- `tests/integration/test_model_save_load.py` — save/load round-trip per model
- `tests/smoke/test_datasets_load.py` — dataset load smoke tests

Tests skip with a clear message if the fixture for their model is missing.

### Manual script

```bash
python scripts/create_sample_data.py
python scripts/create_sample_data.py --max-rows 50
```

## See also

- `docs/TESTING_AND_CI_GUIDE.md` — full test strategy
- `Makefile` — `make help` for all targets
