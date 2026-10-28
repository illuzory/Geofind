from itertools import pairwise
from pathlib import Path

import pytest

from geofind.coverage import Grid, analyze, sla_summary
from geofind.data import DataError, read_workbook, write_workbook
from geofind.imports import ImportStore, compare
from geofind.routing import Travel

ROOT = Path(__file__).resolve().parents[1]
PATHS = [
    ROOT / "data/ural_inventory.xlsx",
    ROOT / "examples/02_ural_changed_addresses.xlsx",
    ROOT / "examples/03_ural_changed_addresses.xlsx",
]


class FixedRouter:
    def table(self, origin, destinations):
        return [Travel(8 * 3600 if i == 0 else 100) for i in range(len(destinations))]


def test_approval_does_not_change_sla_numerator_denominator_or_maximum():
    data = read_workbook(PATHS[0])
    sites = data.sites.iloc[:2].copy()
    sites.loc[sites.index[0], "service_type"] = "approval_required"
    result, _ = analyze(FixedRouter(), data.services, sites, Grid([], [], 20))
    summary = sla_summary(result)
    assert (
        summary["eligible"] == 1
        and summary["within_sla"] == 1
        and summary["percentage"] == 1
    )
    assert summary["maximum_seconds"] == 100
    assert (
        result.iloc[0].drive_seconds == 28800
        and result.iloc[0].status == "approval_required"
    )
    assert result.iloc[0].routing_status == "critical"
    sites["service_type"] = "approval_required"
    result, _ = analyze(FixedRouter(), data.services, sites, Grid([], [], 20))
    summary = sla_summary(result)
    assert (
        summary["eligible"] == 0
        and summary["percentage"] is None
        and summary["maximum_seconds"] is None
    )


def test_three_examples_preserve_ids_but_update_addresses_types_and_devices():
    datasets = [read_workbook(path) for path in PATHS]
    assert [len(d.equipment) for d in datasets] == [120, 121, 122]
    assert [
        int((d.sites.service_type == "approval_required").sum()) for d in datasets
    ] == [4, 5, 4]
    assert all(set(d.sites.site_id) == set(datasets[0].sites.site_id) for d in datasets)
    for left, right in pairwise(datasets):
        diff = compare(left, right)
        assert diff["sites_changed"] == 15 and diff["services_changed"]
        assert diff["equipment_delta"] == 1
        assert all(a != b for a, b in zip(left.sites.address, right.sites.address))


def test_transactional_import_survives_restart_rejects_invalid_and_rolls_back(tmp_path):
    store = ImportStore(tmp_path / "imports.sqlite")
    first = store.apply(PATHS[0].read_bytes(), "base.xlsx")
    store.apply(PATHS[1].read_bytes(), "update.xlsx", expected_current=first)
    assert (
        read_workbook(ImportStore(tmp_path / "imports.sqlite").active()).fingerprint
        == read_workbook(PATHS[1]).fingerprint
    )
    with pytest.raises(DataError):
        store.apply(b"not xlsx", "bad.xlsx")
    assert (
        read_workbook(store.active()).fingerprint == read_workbook(PATHS[1]).fingerprint
    )
    with pytest.raises(ValueError, match="другой сессии"):
        store.apply(PATHS[2].read_bytes(), "stale.xlsx", expected_current=first)
    assert len(store.history()) == 2
    store.activate(first)
    assert store.active() == PATHS[0].read_bytes()


@pytest.mark.parametrize(
    "field,value", [("service_type", "typo"), ("site_id", "service-ekb")]
)
def test_invalid_role_or_id_does_not_import(field, value):
    data = read_workbook(PATHS[0])
    sites = data.sites.copy()
    sites.loc[0, field] = value
    with pytest.raises(DataError):
        read_workbook(write_workbook(data.equipment, sites, data.services))


def test_workbook_status_is_not_an_approval_decision():
    data = read_workbook(PATHS[0])
    sites = data.sites.copy()
    sites["approval_granted"] = True
    result, _ = analyze(FixedRouter(), data.services, sites, Grid([], [], 20))
    assert int((result.status == "approval_required").sum()) == 4


def test_derived_sla_columns_cannot_be_forged_by_workbook():
    data = read_workbook(PATHS[0])
    sites = data.sites.copy()
    sites["status"] = "ok"
    sites["sla_eligible"] = True
    sites["total_seconds"] = 0
    loaded = read_workbook(write_workbook(data.equipment, sites, data.services))
    assert not {"status", "sla_eligible", "total_seconds"} & set(loaded.sites.columns)
    result, _ = analyze(FixedRouter(), loaded.services, loaded.sites, Grid([], [], 20))
    assert int((result.status == "approval_required").sum()) == 4


def test_example_regeneration_preserves_equipment_without_duplicates(tmp_path):
    from scripts.prepare_address_examples import build

    target = tmp_path / "regenerated.xlsx"
    references = ROOT / "data/address_reference.json"
    build(references, PATHS[0], target, 0)
    original = read_workbook(PATHS[0])
    regenerated = read_workbook(target)
    assert len(regenerated.sites) == 15 and len(regenerated.equipment) == 120
    assert (
        regenerated.equipment["Serial Number"].tolist()
        == original.equipment["Serial Number"].tolist()
    )
    assert regenerated.sites.address.tolist() == original.sites.address.tolist()
