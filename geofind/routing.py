"""Explicit routing backends: illustrative road graph or a configured OSRM."""

import hashlib
import heapq
import json
import math
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlparse

import requests
from pyproj import Transformer
from requests.adapters import HTTPAdapter
from shapely import transform
from shapely.geometry import Point, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree
from urllib3.util.retry import Retry

TO_METRIC = Transformer.from_crs(4326, 32641, always_xy=True).transform
TO_WGS = Transformer.from_crs(32641, 4326, always_xy=True).transform


class RoutingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Travel:
    seconds: float | None
    state: str = "ok"


@dataclass
class Route:
    coordinates: list
    seconds: float
    note: str


class DiskCache:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS travel (key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )

    def get(self, key):
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT payload FROM travel WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put_many(self, rows):
        with sqlite3.connect(self.path) as db:
            db.executemany(
                "INSERT OR REPLACE INTO travel VALUES (?,?)",
                [(key, json.dumps(value)) for key, value in rows],
            )


class OSRMRouter:
    def __init__(self, url, version, cache_path, max_snap_m=3000, session=None):
        parsed = urlparse(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise RoutingError("Некорректный OSRM_URL")
        if parsed.scheme == "http" and parsed.hostname not in (
            "localhost",
            "127.0.0.1",
            "::1",
        ):
            raise RoutingError("Для удалённого OSRM используйте HTTPS")
        if not version.strip():
            raise RoutingError(
                "Задайте OSRM_DATA_VERSION — идентификатор графа и профиля"
            )
        self.url = url.rstrip("/")
        self.version = version
        self.max_snap_m = max_snap_m
        self.cache = DiskCache(cache_path)
        self.session = session or requests.Session()
        if session is None:
            retry = Retry(
                total=2,
                backoff_factor=0.3,
                status_forcelist=[429, 502, 503, 504],
                allowed_methods=["GET"],
            )
            self.session.mount("https://", HTTPAdapter(max_retries=retry))
            self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.fingerprint = hashlib.sha256(
            f"{self.url}|{version}|driving|{max_snap_m}".encode()
        ).hexdigest()

    def _get(self, kind, points, params):
        coords = ";".join(f"{lon:.7f},{lat:.7f}" for lat, lon in points)
        try:
            response = self.session.get(
                f"{self.url}/{kind}/v1/driving/{coords}", params=params, timeout=(3, 30)
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError("Invalid OSRM response")
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise RoutingError(
                "OSRM недоступен или вернул ошибку. Расчёт не выполнен; это не отсутствие дороги."
            ) from exc
        if payload.get("code") not in ("Ok", "NoRoute"):
            raise RoutingError(
                f"OSRM: {payload.get('code', 'invalid response')}. Проверьте покрытие графа и привязку координат."
            )
        return payload

    def _key(self, origin, destination):
        return hashlib.sha256(
            json.dumps(
                [self.fingerprint, list(origin), list(destination)],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def table(self, origin, destinations):
        keys = [self._key(origin, dest) for dest in destinations]
        values = [self.cache.get(key) for key in keys]
        pending = [i for i, value in enumerate(values) if value is None]
        for offset in range(0, len(pending), 50):
            indices = pending[offset : offset + 50]
            points = [origin] + [destinations[i] for i in indices]
            payload = self._get(
                "table",
                points,
                {
                    "sources": "0",
                    "destinations": ";".join(str(i) for i in range(1, len(points))),
                    "annotations": "duration",
                    "radiuses": ";".join([str(self.max_snap_m)] * len(points)),
                },
            )
            matrix = payload.get("durations")
            if (
                payload.get("code") != "Ok"
                or not isinstance(matrix, list)
                or len(matrix) != 1
                or not isinstance(matrix[0], list)
                or len(matrix[0]) != len(indices)
            ):
                raise RoutingError(
                    "OSRM вернул неполную матрицу. Итоговое назначение не сформировано."
                )
            rows = []
            for index, seconds in zip(indices, matrix[0]):
                if seconds is not None and (
                    isinstance(seconds, bool)
                    or not isinstance(seconds, (int, float))
                    or not math.isfinite(seconds)
                    or seconds < 0
                ):
                    raise RoutingError("OSRM вернул некорректное время")
                value = {
                    "seconds": seconds,
                    "state": "ok" if seconds is not None else "no_route",
                }
                values[index] = value
                rows.append((keys[index], value))
            # Only completed, valid responses are persisted. Never cache an outage as no_route.
            self.cache.put_many(rows)
        return [Travel(**value) for value in values]

    def route(self, origin, destination):
        key = "route:" + self._key(origin, destination)
        cached = self.cache.get(key)
        if cached is not None:
            return None if cached.get("no_route") else Route(**cached)
        payload = self._get(
            "route",
            [origin, destination],
            {
                "overview": "full",
                "geometries": "geojson",
                "radiuses": f"{self.max_snap_m};{self.max_snap_m}",
            },
        )
        if payload.get("code") == "NoRoute":
            self.cache.put_many([(key, {"no_route": True})])
            return None
        try:
            route = payload["routes"][0]
            seconds = float(route["duration"])
            coords = route["geometry"]["coordinates"]
            if not math.isfinite(seconds) or seconds < 0 or len(coords) < 2:
                raise ValueError("Invalid route")
            if any(
                len(c) < 2
                or not all(math.isfinite(float(v)) for v in c[:2])
                or not (-180 <= c[0] <= 180 and -90 <= c[1] <= 90)
                for c in coords
            ):
                raise ValueError("Invalid route geometry")
            route = Route(
                coords, seconds, "OSRM · автомобильный профиль; без текущих пробок"
            )
            self.cache.put_many(
                [(key, {"coordinates": coords, "seconds": seconds, "note": route.note})]
            )
            return route
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RoutingError("OSRM вернул некорректную геометрию маршрута") from exc


def _lines(geom):
    if geom.geom_type == "LineString":
        yield geom
    elif hasattr(geom, "geoms"):
        for child in geom.geoms:
            yield from _lines(child)


class DemoRoadRouter:
    """Generalized Natural Earth graph: explicit model, not real driving directions.

    Planar intersections, bidirectional edges, configurable uniform speed.
    Straight connectors to the generalized roads use 20 km/h, max 25 km.
    """

    def __init__(self, path, speed_kmh=65, max_snap_m=25000):
        if not 10 <= speed_kmh <= 120:
            raise ValueError("Некорректная скорость")
        raw = Path(path).read_bytes()
        self.fingerprint = hashlib.sha256(
            raw + f"|planar-v1|{speed_kmh}|{max_snap_m}".encode()
        ).hexdigest()
        self.speed = speed_kmh / 3.6
        self.max_snap_m = max_snap_m
        data = json.loads(raw)
        geometries = [
            transform(shape(f["geometry"]), TO_METRIC, interleaved=False)
            for f in data["features"]
        ]
        noded = unary_union(geometries)
        self.graph = {}
        self.edges = []
        self.lines = []
        for line in _lines(noded):
            coords = list(line.coords)
            for a, b in pairwise(coords):
                a = tuple(round(v, 2) for v in a)
                b = tuple(round(v, 2) for v in b)
                length = math.dist(a, b)
                if length < 0.01:
                    continue
                cost = length / self.speed
                self.graph.setdefault(a, []).append((b, cost))
                self.graph.setdefault(b, []).append((a, cost))
                from shapely.geometry import LineString

                self.lines.append(LineString([a, b]))
                self.edges.append((a, b, length))
        if not self.edges:
            raise RoutingError("Демонстрационный граф пуст")
        self.tree = STRtree(self.lines)
        # Bound cached shortest-path trees; no per-request geometry cache in RAM.
        self._tree_from = lru_cache(maxsize=10)(self._shortest_tree)

    def _snap(self, coordinates):
        lat, lon = coordinates
        point = Point(TO_METRIC(lon, lat))
        index = int(self.tree.nearest(point))
        line = self.lines[index]
        at = line.project(point)
        projected = line.interpolate(at)
        gap = point.distance(projected)
        return index, at, projected, gap

    def _shortest_tree(self, origin):
        index, at, projected, gap = self._snap(origin)
        if gap > self.max_snap_m:
            return {}, {}, (index, at, projected, gap)
        a, b, length = self.edges[index]
        distance = {a: at / self.speed, b: (length - at) / self.speed}
        previous = {a: None, b: None}
        heap = [(cost, node) for node, cost in distance.items()]
        heapq.heapify(heap)
        while heap:
            cost, node = heapq.heappop(heap)
            if cost > distance[node]:
                continue
            for target, weight in self.graph[node]:
                candidate = cost + weight
                if candidate < distance.get(target, math.inf):
                    distance[target] = candidate
                    previous[target] = node
                    heapq.heappush(heap, (candidate, target))
        return distance, previous, (index, at, projected, gap)

    def _destination(self, origin, destination):
        distances, previous, start = self._tree_from(tuple(origin))
        index, at, projected, gap = self._snap(destination)
        if start[3] > self.max_snap_m or gap > self.max_snap_m:
            return Travel(None, "off_network"), None, None
        a, b, length = self.edges[index]
        candidates = [
            (distances.get(a, math.inf) + at / self.speed, a),
            (distances.get(b, math.inf) + (length - at) / self.speed, b),
        ]
        cost, node = min(candidates)
        direct = index == start[0] and abs(at - start[1]) / self.speed <= cost
        if direct:
            cost = abs(at - start[1]) / self.speed
        if not math.isfinite(cost):
            return Travel(None, "no_route"), None, None
        if tuple(origin) == tuple(destination):
            return (
                Travel(0.0),
                [list(reversed(origin)), list(reversed(destination))],
                None,
            )
        # Connectors are deliberately modelled, never described as measured road segments.
        total = cost + (gap + start[3]) / (20 / 3.6)
        return Travel(total), (node, previous, start[2], projected, direct), None

    def table(self, origin, destinations):
        return [self._destination(origin, dest)[0] for dest in destinations]

    def route(self, origin, destination):
        travel, details, _ = self._destination(origin, destination)
        if travel.seconds is None:
            return None
        if tuple(origin) == tuple(destination):
            return Route(details, 0.0, "Совпадающие условные координаты")
        node, previous, start, end, direct = details
        chain = []
        if not direct:
            while node is not None:
                chain.append(node)
                node = previous[node]
            chain.reverse()
        xy = [(start.x, start.y)] + chain + [(end.x, end.y)]
        coords = (
            [list(reversed(origin))]
            + [list(TO_WGS(*p)) for p in xy]
            + [list(reversed(destination))]
        )
        return Route(
            coords,
            travel.seconds,
            "Модель Natural Earth: двусторонние дороги, единая скорость; подъезды по прямой до 25 км при 20 км/ч",
        )
