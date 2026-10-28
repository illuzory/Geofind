"""Keep legacy fixture coverage, isolate persistent runtime state and network config."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("GEOFIND_WORKBOOK", str(root / "data/demo_inventory.xlsx"))
    monkeypatch.setenv("GEOFIND_STATE_DIR", str(tmp_path / "runtime"))
    for key in ["OSRM_URL", "OSRM_DATA_VERSION", "OSRM_CACHE_NAMESPACE"]:
        monkeypatch.delenv(key, raising=False)
