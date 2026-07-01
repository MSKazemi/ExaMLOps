"""Package-level smoke tests — verify examlops is importable."""

import importlib


def test_examlops_importable():
    importlib.import_module("examlops")


def test_examlops_features_importable():
    importlib.import_module("examlops.features")


def test_examlops_schemas_importable():
    importlib.import_module("examlops.schemas")
