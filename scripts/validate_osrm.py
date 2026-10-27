"""Explicit live integration check; not run by pytest or CI.

The public demo is throttled to <=1 request/s. Its graph version is not
exposed, so use an observation namespace, never claim an immutable graph.
"""

import argparse
import json
import os
import resource
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

resource.setrlimit(resource.RLIMIT_AS, (16 * 1024**3, 16 * 1024**3))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from shapely.geometry import shape

from geofind.coverage import analyze, load_boundary, make_grid, zone_features
from geofind.data import read_workbook, safe_csv
from geofind.mapview import build_map
from geofind.routing import OSRMRouter

ROOT = Path(__file__).resolve().parents[1]


def validate(workbook, output, url, namespace, step):
    started = perf_counter()
    data = read_workbook(workbook)
    router = OSRMRouter(url, namespace, ROOT / ".cache/osrm.sqlite")
    boundary = load_boundary(ROOT / "data/sverdlovsk.geojson")
    grid = make_grid(boundary, step)
    if router.public_demo and (len(grid.cells) > 1000 or len(data.services) > 2):
        raise ValueError(
            "Public demo integration check: <=1000 cells, <=2 base services"
        )
    sites = data.regional_sites
    baseline, base_assignments = analyze(router, data.services, sites, grid)
    print(
        "Base scenario:",
        len(grid.cells),
        "cells",
        Counter(x["status"] for x in base_assignments),
        flush=True,
    )
    routes = {}
    checks = []
    for row in baseline.to_dict("records"):
        service = data.services[data.services.site_id == row["service_id"]].iloc[0]
        route = router.route(
            (service.latitude, service.longitude), (row["latitude"], row["longitude"])
        )
        if route is None or row["drive_seconds"] is None:
            raise AssertionError(f"No route to control site {row['name']}")
        difference = abs(route.seconds - row["drive_seconds"])
        if difference > max(5, 0.01 * route.seconds):
            raise AssertionError("Table/Route mismatch")
        routes[row["site_id"]] = route
        checks.append(
            {
                "name": row["name"],
                "table_seconds": row["drive_seconds"],
                "route_seconds": route.seconds,
                "difference_seconds": difference,
            }
        )
        print(checks[-1], flush=True)
    extra = sites[sites.site_id == "tagil"].copy()
    if len(extra) != 1:
        raise ValueError("This integration fixture requires the tagil site")
    extra["site_id"] = "scenario-tagil"
    extra["name"] = "Инженеры · Нижний Тагил"
    services = pd.concat([data.services, extra], ignore_index=True)
    scenario, assignments = analyze(router, services, sites, grid)
    destinations = (
        list(sites[["latitude", "longitude"]].itertuples(index=False, name=None))
        + grid.coordinates
    )
    matrix = {
        r["site_id"]: router.table((r["latitude"], r["longitude"]), destinations)
        for r in services.to_dict("records")
    }
    for i, assignment in enumerate(scenario.to_dict("records") + assignments):
        candidates = [
            (values[i].seconds, sid)
            for sid, values in matrix.items()
            if values[i].seconds is not None
        ]
        expected = min(candidates) if candidates else (None, None)
        if (assignment["drive_seconds"], assignment["service_id"]) != expected:
            raise AssertionError(
                "Assignment does not match minimum of full OSRM matrix"
            )
    zones = zone_features(grid, assignments)
    polys = [shape(f["geometry"]) for f in zones["features"]]
    if not all(p.is_valid and p.difference(boundary).area < 1e-9 for p in polys):
        raise AssertionError("Invalid/out-of-region zone")
    if any(
        p.intersection(q).area > 1e-9
        for i, p in enumerate(polys)
        for q in polys[i + 1 :]
    ):
        raise AssertionError("Overlapping assignments")
    counts = data.equipment["Site Name"].value_counts()
    output = Path(output)
    output.mkdir(exist_ok=True, parents=True)
    for label, result, service_set, cell_assignments in [
        ("base", baseline, data.services, base_assignments),
        ("tagil", scenario, services, assignments),
    ]:
        result["equipment"] = result.name.map(counts).fillna(0).astype(int)
        result["calculation_mode"] = "osrm"
        result["routing_observation"] = namespace
        result["workbook_version"] = data.fingerprint
        selected = result[result.site_id == "serov"].iloc[0]
        source = service_set[service_set.site_id == selected.service_id].iloc[0]
        route = router.route(
            (source.latitude, source.longitude), (selected.latitude, selected.longitude)
        )
        m = build_map(
            result,
            service_set,
            data.equipment,
            zone_features(grid, cell_assignments),
            boundary,
            json.loads(ROOT.joinpath("data/demo_roads.geojson").read_text()),
            route,
            "serov",
        )
        m.save(str(output / f"osrm-{label}.html"))
        (output / f"osrm-{label}.csv").write_bytes(safe_csv(result))
    report = {
        "observed_at_utc": datetime.now(UTC).isoformat(),
        "endpoint": url,
        "cache_namespace": namespace,
        "server_graph_version_exposed": False,
        "coordinates": "illustrative",
        "grid_km": step,
        "grid_cells": len(grid.cells),
        "base_cell_statuses": dict(Counter(x["status"] for x in base_assignments)),
        "scenario_cell_statuses": dict(Counter(x["status"] for x in assignments)),
        "table_route_checks": checks,
        "base_sites_in_sla": int((baseline.status == "ok").sum()),
        "tagil_sites_in_sla": int((scenario.status == "ok").sum()),
        "min_time_matrix_verified": True,
        "nonoverlapping_zones_verified": True,
        "elapsed_seconds": round(perf_counter() - started, 3),
        "peak_client_rss_mib": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1
        ),
        "client_address_space_limit_gib": 16,
        "remote_osrm_memory_measured": False,
    }
    (output / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--workbook", default="data/demo_inventory.xlsx")
    p.add_argument("--output", default="artifacts/osrm")
    p.add_argument(
        "--url", default=os.environ.get("OSRM_URL", "https://router.project-osrm.org")
    )
    p.add_argument(
        "--namespace",
        default=os.environ.get("OSRM_CACHE_NAMESPACE")
        or "public-demo-observed-" + datetime.now(UTC).isoformat(),
    )
    p.add_argument("--step", type=int, default=20)
    a = p.parse_args()
    validate(a.workbook, a.output, a.url, a.namespace, a.step)
