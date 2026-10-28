"""Validate all three address/approval workbooks and export map snapshots.

Network routing is explicit (--backend osrm); pytest stays offline.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd

from geofind.addressing import AddressChecker
from geofind.coverage import (
    analyze,
    load_boundary,
    make_grid,
    sla_summary,
    zone_features,
)
from geofind.data import read_workbook, safe_csv
from geofind.mapview import build_map
from geofind.routing import DemoRoadRouter, OSRMRouter

ROOT = Path(__file__).resolve().parents[1]
PATHS = [
    ROOT / "data/ural_inventory.xlsx",
    ROOT / "examples/02_ural_changed_addresses.xlsx",
    ROOT / "examples/03_ural_changed_addresses.xlsx",
]


def validate(backend, output, namespace):
    started = perf_counter()
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (16 * 1024**3, 16 * 1024**3))
    except ImportError:
        resource = None
    router = (
        OSRMRouter(
            os.environ.get("OSRM_URL", "https://router.project-osrm.org"),
            namespace,
            ROOT / ".cache/osrm.sqlite",
        )
        if backend == "osrm"
        else DemoRoadRouter(ROOT / "data/demo_roads.geojson")
    )
    boundary = load_boundary(ROOT / "data/sverdlovsk.geojson")
    grid = make_grid(boundary, 20)
    checker = AddressChecker(
        ROOT / ".cache/release-addresses.sqlite", ROOT / "data/address_reference.json"
    )
    output = Path(output)
    output.mkdir(exist_ok=True, parents=True)
    reports = []
    for i, path in enumerate(PATHS, 1):
        data = read_workbook(path)
        checks = [
            checker.check(row)
            for row in data.sites.to_dict("records") + data.services.to_dict("records")
        ]
        if any(c["status"] != "verified" for c in checks):
            raise AssertionError("Address not confirmed from prepared evidence")
        result, assignments = analyze(router, data.services, data.regional_sites, grid)
        summary = sla_summary(result)
        if result.drive_seconds.isna().any():
            raise AssertionError("Not all fixture sites have a routing result")
        assert summary["eligible"] + summary["approval_required"] == len(data.sites)
        assert all(
            row.status == "approval_required" and not row.sla_eligible
            for row in result[result.service_type == "approval_required"].itertuples()
        )
        service = data.services.iloc[0]
        target = result[result.site_id == "serov"].iloc[0]
        route = router.route(
            (service.latitude, service.longitude), (target.latitude, target.longitude)
        )
        if route is None or abs(route.seconds - target.drive_seconds) > max(
            5, route.seconds * 0.01
        ):
            raise AssertionError("Route/Table mismatch")
        result["equipment"] = (
            result.name.map(data.equipment["Site Name"].value_counts())
            .fillna(0)
            .astype(int)
        )
        result["calculation_mode"] = backend
        result["routing_observation"] = (
            namespace if backend == "osrm" else router.fingerprint
        )
        m = build_map(
            result,
            data.services,
            data.equipment,
            zone_features(grid, assignments),
            boundary,
            json.loads((ROOT / "data/demo_roads.geojson").read_text()),
            route,
            "serov",
        )
        m.save(str(output / f"{i:02}_map.html"))
        (output / f"{i:02}_coverage.csv").write_bytes(safe_csv(result))
        (output / f"{i:02}_addresses.csv").write_bytes(safe_csv(pd.DataFrame(checks)))
        reports.append(
            {
                "file": path.name,
                "equipment": len(data.equipment),
                "address_checks": len(checks),
                "all_address_checks": "verified_from_saved_OSM_responses",
                "sla": summary,
                "route_table_difference_seconds": abs(
                    route.seconds - target.drive_seconds
                ),
            }
        )
        print(json.dumps(reports[-1], ensure_ascii=False), flush=True)
    report = {
        "backend": backend,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "namespace": namespace,
        "grid_cells": len(grid.cells),
        "books": reports,
        "elapsed_seconds": round(perf_counter() - started, 3),
        "peak_client_rss_mib": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1
        )
        if resource
        else None,
        "osrm_server_memory_measured": False,
        "coordinates": "Real OSM address points selected for fictional equipment locations; not official GAR/FIAS validation",
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--backend", choices=["demo", "osrm"], default="demo")
    p.add_argument("--output", default="artifacts/15_sites_validation")
    p.add_argument(
        "--namespace",
        default="public-osrm-addresses-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
    )
    a = p.parse_args()
    validate(a.backend, a.output, a.namespace)
