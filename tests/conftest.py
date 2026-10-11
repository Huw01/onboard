from pathlib import Path

import pytest

from onboard.corrupt import corrupt
from onboard.generate import generate, read_tables, write_tables

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


@pytest.fixture(scope="session")
def clean_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("clean")
    write_tables(generate(42), d)
    return d


@pytest.fixture(scope="session")
def clean(clean_dir):
    return read_tables(clean_dir)


@pytest.fixture(scope="session")
def corrupted(clean_dir, tmp_path_factory):
    base = tmp_path_factory.mktemp("out")
    c = corrupt(clean_dir, base / "legacy", base / "ground_truth", seed=42)
    return c, base
