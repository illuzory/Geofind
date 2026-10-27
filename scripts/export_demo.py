"""Generate a standalone, offline map snapshot and machine-readable results."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from geofind.coverage import analyze, load_boundary, make_grid, zone_features
from geofind.data import read_workbook, safe_csv
from geofind.mapview import build_map
from geofind.routing import DemoRoadRouter


def export(workbook, output, step=20):
    root = Path(__file__).resolve().parents[1]
    data = read_workbook(workbook)
    router = DemoRoadRouter(root / "data/demo_roads.geojson")
    boundary = load_boundary(root / "data/sverdlovsk.geojson")
    grid = make_grid(boundary, step)
    results, assignment = analyze(router, data.services, data.regional_sites, grid)
    counts = data.equipment["Site Name"].value_counts()
    results["equipment"] = results.name.map(counts).fillna(0).astype(int)
    results["calculation_mode"] = "demo_natural_earth"
    results["data_version"] = data.fingerprint
    serov = (
        results[results.site_id == "serov"].iloc[0]
        if "serov" in set(results.site_id)
        else results.iloc[0]
    )
    route = None
    if serov.service_id:
        service = data.services[data.services.site_id == serov.service_id].iloc[0]
        route = router.route(
            (service.latitude, service.longitude), (serov.latitude, serov.longitude)
        )
    m = build_map(
        results,
        data.services,
        data.equipment,
        zone_features(grid, assignment),
        boundary,
        json.loads(root.joinpath("data/demo_roads.geojson").read_text()),
        route,
        serov.site_id,
    )
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    m.save(str(output / "ural_coverage.html"))
    (output / "ural_coverage.csv").write_bytes(safe_csv(results))
    print(
        results[["name", "drive_seconds", "status", "equipment"]].to_string(index=False)
    )
    print("grid cells", len(grid.cells), "output", output)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--workbook", default="data/demo_inventory.xlsx")
    p.add_argument("--output", default="artifacts")
    p.add_argument("--step", type=int, default=20)
    a = p.parse_args()
    export(a.workbook, a.output, a.step)
