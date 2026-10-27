from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

from geofind.data import (
    EQUIPMENT_COLUMNS,
    DataError,
    read_workbook,
    validate,
    write_workbook,
)
from scripts.prepare_data import build

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def data():
    return read_workbook(ROOT / "data/demo_inventory.xlsx")


def test_demo_workbook_has_new_regional_cities_and_traceable_rows(data):
    assert len(data.equipment) == 272
    assert len(data.sites) == 16
    assert set(data.regional_sites.name) == {
        "Екатеринбург",
        "Нижний Тагил",
        "Каменск-Уральский",
        "Серов",
    }
    assert (
        data.equipment[
            data.equipment["Site Name"].isin(data.regional_sites.name)
        ].shape[0]
        == 54
    )
    assert data.sites.is_demo.all() and data.services.is_demo.all()
    assert data.equipment["Serial Number"].str.startswith("DEMO-").all()


def test_adaptation_keeps_original_values_and_adds_only_eighteen(data, tmp_path):
    original = data.equipment.iloc[:254][EQUIPMENT_COLUMNS].copy()
    original["Vendor"] = "Original test vendor"
    path = tmp_path / "original.xlsx"
    original.to_excel(path, index=False)
    adapted = read_workbook(build(path))
    pd.testing.assert_frame_equal(
        adapted.equipment.iloc[:254][EQUIPMENT_COLUMNS].reset_index(drop=True),
        original.reset_index(drop=True),
    )
    assert len(adapted.equipment) == len(original) + 18
    assert adapted.equipment["Data Source"].value_counts().to_dict() == {
        "Исходная таблица": 254,
        "Демо": 18,
    }
    pd.testing.assert_frame_equal(pd.read_excel(path), original)


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("serial", "серийный"),
        ("coordinates", "координаты"),
        ("unknown", "Нет координат"),
        ("id", "site_id"),
        ("bool", "true/false"),
        ("outside", "вне границы"),
        ("duplicate_name", "повторяющийся"),
    ],
)
def test_invalid_import_rejected(data, mutation, match):
    eq = data.equipment.copy()
    sites = data.sites.copy()
    services = data.services.copy()
    if mutation == "serial":
        eq.loc[1, "Serial Number"] = eq.loc[0, "Serial Number"]
    if mutation == "coordinates":
        sites.loc[0, "latitude"] = 999
    if mutation == "unknown":
        eq.loc[0, "Site Name"] = "Unknown site"
    if mutation == "id":
        sites.loc[1, "site_id"] = sites.loc[0, "site_id"]
    if mutation == "bool":
        sites["is_demo"] = sites["is_demo"].astype(object)
        sites.loc[0, "is_demo"] = "maybe"
    if mutation == "outside":
        sites.loc[sites.site_id == "serov", "latitude"] = 40
    if mutation == "duplicate_name":
        sites.loc[1, "name"] = sites.loc[0, "name"]
    with pytest.raises(DataError, match=match):
        validate(eq, sites, services)


def test_formula_like_strings_remain_strings(data):
    eq = data.equipment.copy()
    eq.loc[0, "Device Name"] = '=HYPERLINK("https://example.com")'
    output = write_workbook(eq, data.sites, data.services)
    workbook = load_workbook(BytesIO(output), data_only=False)
    assert workbook["Equipment"]["B2"].data_type == "s"
    assert read_workbook(output).equipment.loc[0, "Device Name"].startswith("=")


def test_missing_sheets_rejected():
    b = BytesIO()
    pd.DataFrame({"test": [1]}).to_excel(b, index=False)
    with pytest.raises(DataError, match="Equipment"):
        read_workbook(b.getvalue())


def test_csv_export_does_not_execute_formulas():
    from geofind.data import safe_csv

    raw = safe_csv(
        pd.DataFrame({"name": ["=1+1", "@formula", "normal"], "time": [0, 1, 2]})
    )
    table = pd.read_csv(BytesIO(raw))
    assert table.name.tolist() == ["'=1+1", "'@formula", "normal"]
    assert table.time.tolist() == [0, 1, 2]
