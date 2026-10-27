"""Bounded cold-run benchmark. JSON output includes the actual imposed limit."""

import resource

LIMIT = 16 * 1024**3
resource.setrlimit(resource.RLIMIT_AS, (LIMIT, LIMIT))
import json
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd

from geofind.coverage import analyze, load_boundary, make_grid, zone_features
from geofind.data import read_workbook
from geofind.mapview import build_map
from geofind.routing import DemoRoadRouter

root = Path(__file__).resolve().parents[1]
started = perf_counter()
data = read_workbook(root / "data/demo_inventory.xlsx")
services = pd.concat(
    [
        data.services,
        data.regional_sites.assign(site_id=lambda x: "scenario-" + x.site_id),
    ],
    ignore_index=True,
)
router = DemoRoadRouter(root / "data/demo_roads.geojson")
boundary = load_boundary(root / "data/sverdlovsk.geojson")
grid = make_grid(boundary, 10)
results, assignments = analyze(router, services, data.regional_sites, grid)
zones = zone_features(grid, assignments)
m = build_map(
    results,
    services,
    data.equipment,
    zones,
    boundary,
    json.loads(root.joinpath("data/demo_roads.geojson").read_text()),
)
html = m.get_root().render()
print(
    json.dumps(
        {
            "backend": "demo_natural_earth",
            "services": len(services),
            "sites": len(results),
            "grid_cells": len(grid.cells),
            "grid_km": 10,
            "graph_nodes": len(router.graph),
            "html_bytes": len(html.encode()),
            "elapsed_seconds": round(perf_counter() - started, 3),
            "peak_rss_mib": round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1
            ),
            "address_space_limit_gib": 16,
            "osrm_benchmarked": False,
        },
        ensure_ascii=False,
    )
)
