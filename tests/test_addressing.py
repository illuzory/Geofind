import json
from pathlib import Path

import pytest
import requests

from geofind.addressing import (
    AddressChecker,
    evaluate,
    local_issues,
    query_key,
    street_key,
)
from geofind.data import read_workbook

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def row():
    return read_workbook(ROOT / "data/ural_inventory.xlsx").sites.iloc[0].to_dict()


@pytest.fixture
def candidates(row):
    return json.loads((ROOT / "data/address_reference.json").read_text())["responses"][
        query_key(row)
    ]["candidates"]


def test_all_48_prepared_addresses_are_verified_offline(tmp_path):
    checker = AddressChecker(
        tmp_path / "addresses.sqlite", ROOT / "data/address_reference.json"
    )
    paths = [
        ROOT / "data/ural_inventory.xlsx",
        ROOT / "examples/02_ural_changed_addresses.xlsx",
        ROOT / "examples/03_ural_changed_addresses.xlsx",
    ]
    for path in paths:
        data = read_workbook(path)
        assert all(
            checker.check(r)["status"] == "verified"
            for r in data.sites.to_dict("records") + data.services.to_dict("records")
        )


def test_coordinate_mismatch_is_not_silently_fixed(row, tmp_path):
    original = dict(row)
    row["latitude"] += 0.02
    row["address_status"] = "verified"  # arbitrary workbook assertions are ignored
    result = AddressChecker(
        tmp_path / "a.sqlite", ROOT / "data/address_reference.json"
    ).check(row)
    assert result["status"] == "coordinate_mismatch" and result["distance_m"] > 2000
    assert result["suggested_latitude"] == original["latitude"]
    assert row["latitude"] != original["latitude"]


def test_street_types_are_significant_but_abbreviations_are_supported():
    assert street_key("пр-т Ленина") == street_key("проспект Ленина")
    assert street_key("ул. Ленина") == street_key("улица Ленина")
    assert street_key("улица Буторина") != street_key("переулок Буторина")


def test_tampered_or_missing_address_fields_are_detected(row):
    row["house"] = "999999"
    assert local_issues(row)
    row["house"] = ""
    assert any("house" in issue for issue in local_issues(row))


def test_wrong_details_and_separated_duplicates_require_review(row, candidates):
    bad = dict(candidates[0])
    bad["address"] = {**bad["address"], "house_number": "999999"}
    assert evaluate(row, [bad], "test", "now")["status"] == "details_mismatch"
    duplicate = {**candidates[0], "lat": float(candidates[0]["lat"]) + 0.02}
    assert (
        evaluate(row, [candidates[0], duplicate], "test", "now")["status"]
        == "ambiguous"
    )


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if isinstance(self.payload, Exception):
            raise self.payload
        return Response(self.payload)


def test_online_success_is_cached_and_coordinate_changes_are_rechecked(
    row, candidates, tmp_path
):
    session = Session(candidates)
    checker = AddressChecker(
        tmp_path / "a.sqlite", url="https://owned-geocoder.example", session=session
    )
    assert checker.check(row, allow_network=True)["status"] == "verified"
    row["longitude"] += 0.03
    assert checker.check(row, allow_network=True)["status"] == "coordinate_mismatch"
    assert session.calls == 1
    assert "q" not in session.last_kwargs["params"]
    assert session.last_kwargs["params"]["street"].startswith(row["house"])


def test_outage_not_found_and_unchecked_are_distinct(row, tmp_path):
    checker = AddressChecker(
        tmp_path / "a.sqlite",
        url="https://owned-geocoder.example",
        session=Session(requests.Timeout()),
    )
    assert checker.check(row)["status"] == "not_checked"
    assert checker.check(row, allow_network=True)["status"] == "service_error"
    checker.session = Session([])
    assert checker.check(row, allow_network=True)["status"] == "not_found"
