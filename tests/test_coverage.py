from pathlib import Path

import pandas as pd
import pytest
from shapely.geometry import shape
from shapely.ops import unary_union

from geofind.coverage import (
    Grid,
    analyze,
    classify,
    load_boundary,
    make_grid,
    zone_features,
)
from geofind.data import read_workbook
from geofind.routing import DemoRoadRouter, RoutingError, Travel

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "seconds,status",
    [
        (0, "ok"),
        (14400, "ok"),
        (14401, "risk"),
        (21600, "risk"),
        (21601, "critical"),
        (28800, "critical"),
        (28801, "outside"),
    ],
)
def test_threshold_boundaries(seconds, status):
    assert classify(seconds) == status


class DirectedRouter:
    def __init__(self):
        self.calls = []

    def table(self, origin, destinations):
        self.calls.append((origin, destinations))
        # A is further geographically but faster to the exact SAME destinations.
        seconds = [10, 40] if origin == (56.0, 60.0) else [20, 20]
        return [Travel(v) for v in seconds]


def test_shared_destinations_and_minimum_not_geometry():
    sites = pd.DataFrame(
        [
            {"site_id": "c1", "latitude": 56.1, "longitude": 61.1},
            {"site_id": "c2", "latitude": 57.1, "longitude": 62.1},
        ]
    )
    services = pd.DataFrame(
        [
            {"site_id": "b", "name": "B", "latitude": 57.0, "longitude": 61.0},
            {"site_id": "a", "name": "A", "latitude": 56.0, "longitude": 60.0},
        ]
    )
    router = DirectedRouter()
    result, _ = analyze(router, services, sites, Grid([], [], 20))
    assert list(result.service_id) == ["a", "b"]
    assert router.calls[0][1] == router.calls[1][1]
    assert router.calls[0][0] == (56.0, 60.0)


def test_tie_zero_and_failed_candidate():
    services = pd.DataFrame(
        [
            {"site_id": "b", "name": "B", "latitude": 57.0, "longitude": 61.0},
            {"site_id": "a", "name": "A", "latitude": 56.0, "longitude": 60.0},
        ]
    )
    sites = pd.DataFrame([{"site_id": "c", "latitude": 57.0, "longitude": 61.0}])

    class EqualRouter:
        def table(self, *args):
            return [Travel(0)]

    result, _ = analyze(
        EqualRouter(), services, sites, Grid([], [], 20), dispatch_minutes=60
    )
    assert result.service_id.iloc[0] == "a"
    assert result.drive_seconds.iloc[0] == 0 and result.total_seconds.iloc[0] == 3600

    class FailedRouter:
        def table(self, *args):
            raise RoutingError("outage")

    with pytest.raises(RoutingError):
        analyze(FailedRouter(), services, sites, Grid([], [], 20))


def test_grid_and_zones_cover_region_without_positive_area_overlaps():
    boundary = load_boundary(ROOT / "data/sverdlovsk.geojson")
    grid = make_grid(boundary, 20)
    data = read_workbook(ROOT / "data/demo_inventory.xlsx")
    extras = data.regional_sites[data.regional_sites.site_id == "tagil"].copy()
    extras["site_id"] = "scenario-tagil"
    services = pd.concat([data.services, extras])
    result, assignment = analyze(
        DemoRoadRouter(ROOT / "data/demo_roads.geojson"),
        services,
        data.regional_sites,
        grid,
    )
    assert len(assignment) == len(grid.cells)
    assert set(result.service_id) == {"service-ekb", "scenario-tagil"}
    features = zone_features(grid, assignment)["features"]
    polygons = [shape(f["geometry"]) for f in features]
    assert all(p.is_valid for p in polygons)
    assert all(p.difference(boundary).area < 1e-9 for p in polygons)
    for i, p in enumerate(polygons):
        assert all(p.intersection(q).area < 1e-9 for q in polygons[i + 1 :])
    assert unary_union(polygons).area / boundary.area > 0.999
    assert any(p.geom_type == "MultiPolygon" for p in polygons)
    with pytest.raises(ValueError, match="Слишком мелкая|Лимит"):
        make_grid(boundary, 0.1)


def test_additional_engineers_improve_serov():
    data = read_workbook(ROOT / "data/demo_inventory.xlsx")
    grid = Grid([], [], 20)
    router = DemoRoadRouter(ROOT / "data/demo_roads.geojson")
    base, _ = analyze(router, data.services, data.regional_sites, grid)
    extra = data.regional_sites[data.regional_sites.site_id == "serov"].copy()
    extra["site_id"] = "scenario-serov"
    expanded, _ = analyze(
        router, pd.concat([data.services, extra]), data.regional_sites, grid
    )
    assert base.set_index("site_id").loc["serov", "status"] == "risk"
    assert expanded.set_index("site_id").loc["serov", "status"] == "ok"
    assert expanded.set_index("site_id").loc["serov", "total_seconds"] == 0
