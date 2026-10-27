import sqlite3
from pathlib import Path

import pytest
import requests

from geofind.routing import DemoRoadRouter, OSRMRouter, RoutingError

ROOT = Path(__file__).resolve().parents[1]


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
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.payload, Exception):
            raise self.payload
        return Response(self.payload)


def test_osrm_direction_null_zero_and_persistent_cache(tmp_path):
    session = Session(
        {
            "code": "Ok",
            "durations": [[0, 1800, None]],
            "sources": [{"distance": 1}],
            "destinations": [{"distance": 1}] * 3,
        }
    )
    path = tmp_path / "cache.sqlite"
    router = OSRMRouter("http://localhost:5000", "graph-a", path, session=session)
    origin = (56.8, 60.6)
    destinations = [origin, (57.0, 61.0), (58.0, 60.0)]
    values = router.table(origin, destinations)
    assert [v.seconds for v in values] == [0, 1800, None]
    assert values[-1].state == "no_route"
    url, kwargs = session.calls[0]
    assert url.endswith(
        "60.6000000,56.8000000;60.6000000,56.8000000;61.0000000,57.0000000;60.0000000,58.0000000"
    )
    assert kwargs["params"]["sources"] == "0"
    assert kwargs["params"]["destinations"] == "1;2;3"
    offline = Session(requests.ConnectionError("offline"))
    restored = OSRMRouter("http://localhost:5000", "graph-a", path, session=offline)
    assert restored.table(origin, destinations) == values
    assert not offline.calls
    changed = OSRMRouter("http://localhost:5000", "graph-b", path, session=offline)
    with pytest.raises(RoutingError):
        changed.table(origin, destinations)
    assert len(offline.calls) == 1


def test_osrm_errors_are_not_cached(tmp_path):
    path = tmp_path / "cache.sqlite"
    router = OSRMRouter(
        "http://localhost:5000", "a", path, session=Session(requests.Timeout())
    )
    with pytest.raises(RoutingError, match="не отсутствие дороги"):
        router.table((56, 60), [(57, 61)])
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM travel").fetchone()[0] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "Ok", "durations": [[1]]},
        {"code": "Ok", "durations": [None]},
        {"code": "Ok", "durations": [[True, None]]},
        {"code": "Ok", "durations": [[1, 2]]},
        {"code": "NoSegment"},
        {"code": "Ok", "durations": [[-1, None]]},
        {"code": "Ok", "durations": [[float("nan"), 1]]},
    ],
)
def test_incomplete_or_invalid_matrix_fails(tmp_path, payload):
    router = OSRMRouter(
        "http://localhost:5000", "a", tmp_path / "c.db", session=Session(payload)
    )
    with pytest.raises(RoutingError):
        router.table((56, 60), [(57, 61), (58, 62)])


def test_table_route_consistency_and_geometry_direction(tmp_path):
    response = Session(
        {
            "code": "Ok",
            "routes": [
                {"duration": 10, "geometry": {"coordinates": [[60, 56], [61, 57]]}}
            ],
        }
    )
    router = OSRMRouter(
        "http://localhost:5000", "a", tmp_path / "c.db", session=response
    )
    route = router.route((56, 60), (57, 61))
    assert route.seconds == 10
    assert route.coordinates[0] == [60, 56]
    assert router.route((56, 60), (57, 61)).seconds == 10
    assert len(response.calls) == 1
    assert response.calls[0][0].endswith("60.0000000,56.0000000;61.0000000,57.0000000")


def test_demo_path_uses_roads_and_matches_table():
    router = DemoRoadRouter(ROOT / "data/demo_roads.geojson")
    origin = (56.8176, 60.6395)
    serov = (59.6049, 60.5756)
    value = router.table(origin, [serov])[0]
    route = router.route(origin, serov)
    assert 4 * 3600 < value.seconds < 8 * 3600
    assert route.seconds == pytest.approx(value.seconds)
    assert route.coordinates[0] == list(reversed(origin))
    assert route.coordinates[-1] == list(reversed(serov))
    assert len(route.coordinates) > 20
    assert router.table(origin, [origin])[0].seconds == 0
    assert router.table(origin, [(0, 0)])[0].state == "off_network"


def test_osrm_filters_far_snaps_without_losing_valid_grid_nodes(tmp_path):
    response = Session(
        {
            "code": "Ok",
            "durations": [[100, 200, None]],
            "sources": [{"distance": 2}],
            "destinations": [{"distance": 1}, {"distance": 3001}, {"distance": 3000}],
        }
    )
    router = OSRMRouter(
        "http://localhost:5000", "a", tmp_path / "c.db", session=response
    )
    values = router.table((56, 60), [(57, 61), (58, 62), (59, 63)])
    assert [(v.seconds, v.state) for v in values] == [
        (100, "ok"),
        (None, "off_network"),
        (None, "no_route"),
    ]
    assert "radiuses" not in response.calls[0][1]["params"]
    assert response.calls[0][1]["params"]["skip_waypoints"] == "false"


def test_osrm_far_origin_is_not_allowed_to_assign_territory(tmp_path):
    response = Session(
        {
            "code": "Ok",
            "durations": [[100]],
            "sources": [{"distance": 3001}],
            "destinations": [{"distance": 1}],
        }
    )
    router = OSRMRouter(
        "http://localhost:5000", "a", tmp_path / "c.db", session=response
    )
    values = router.table((56, 60), [(57, 61)])
    assert values[0].seconds is None and values[0].state == "off_network"


@pytest.mark.parametrize("distance", [None, -1, float("nan"), True, "10"])
def test_osrm_rejects_invalid_snap_metadata(tmp_path, distance):
    response = Session(
        {
            "code": "Ok",
            "durations": [[100]],
            "sources": [{"distance": 1}],
            "destinations": [{"distance": distance}],
        }
    )
    router = OSRMRouter(
        "http://localhost:5000", "a", tmp_path / "c.db", session=response
    )
    with pytest.raises(RoutingError, match="привязку"):
        router.table((56, 60), [(57, 61)])


def test_public_demo_throttles_requests(monkeypatch, tmp_path):
    from geofind import routing

    response = Session(
        {
            "code": "Ok",
            "routes": [
                {"duration": 10, "geometry": {"coordinates": [[60, 56], [61, 57]]}}
            ],
        }
    )
    monkeypatch.setattr(OSRMRouter, "_public_last_request", 10.0)
    monkeypatch.setattr(routing.time, "monotonic", lambda: 10.2)
    waits = []
    monkeypatch.setattr(routing.time, "sleep", waits.append)
    router = OSRMRouter(
        "https://router.project-osrm.org", "a", tmp_path / "c.db", session=response
    )
    router.route((56, 60), (57, 61))
    assert waits == pytest.approx([0.85])
