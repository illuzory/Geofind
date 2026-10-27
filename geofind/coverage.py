import json
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from shapely import transform
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

from .routing import TO_METRIC, TO_WGS

COLORS = {
    "ok": "#199c78",
    "risk": "#e9ad35",
    "critical": "#dc675a",
    "outside": "#98627d",
    "no_route": "#8995a5",
    "off_network": "#8995a5",
}
LABELS = {
    "ok": "В SLA",
    "risk": "Риск",
    "critical": "Критично",
    "outside": "За пределами SLA",
    "no_route": "Нет пути в графе",
    "off_network": "Нет допустимой привязки к дороге",
}


@dataclass
class Grid:
    cells: list
    coordinates: list
    step_km: int
    boundary: object = None


def load_boundary(path):
    data = json.loads(Path(path).read_text())
    region = unary_union([shape(f["geometry"]) for f in data["features"]])
    if region.is_empty or not region.is_valid:
        raise ValueError("Некорректная граница региона")
    return region


def make_grid(region, step_km=20, max_cells=10000):
    if not math.isfinite(step_km) or step_km <= 0:
        raise ValueError("Шаг должен быть положительным")
    metric = transform(region, TO_METRIC, interleaved=False)
    step = step_km * 1000
    left, bottom, right, top = metric.bounds
    x0 = math.floor(left / step) * step
    y0 = math.floor(bottom / step) * step
    nx = math.ceil((right - x0) / step)
    ny = math.ceil((top - y0) / step)
    if nx * ny > max_cells * 4:
        raise ValueError("Слишком мелкая сетка: увеличьте шаг")
    cells = []
    coordinates = []
    for row in range(ny):
        for col in range(nx):
            tile = box(
                x0 + col * step,
                y0 + row * step,
                x0 + (col + 1) * step,
                y0 + (row + 1) * step,
            )
            if not metric.intersects(tile):
                continue
            clipped = metric.intersection(tile)
            if clipped.area < 1:
                continue
            p = clipped.representative_point()
            lon, lat = TO_WGS(p.x, p.y)
            cells.append(clipped)
            coordinates.append((lat, lon))
            if len(cells) > max_cells:
                raise ValueError("Лимит 10000 ячеек: увеличьте шаг")
    return Grid(cells, coordinates, step_km, region)


def classify(seconds, thresholds=(4, 6, 8)):
    if len(thresholds) != 3 or not 0 < thresholds[0] < thresholds[1] < thresholds[2]:
        raise ValueError("Пороги SLA должны возрастать")
    hours = seconds / 3600
    for limit, label in zip(thresholds, ["ok", "risk", "critical"]):
        if hours <= limit:
            return label
    return "outside"


def analyze(router, services, sites, grid, thresholds=(4, 6, 8), dispatch_minutes=0):
    if services.empty:
        raise ValueError("Выберите хотя бы один сервис")
    if not 0 <= dispatch_minutes <= 240:
        raise ValueError("Задержка выезда должна быть от 0 до 240 минут")
    destinations = (
        list(sites[["latitude", "longitude"]].itertuples(index=False, name=None))
        + grid.coordinates
    )
    winners = [None] * len(destinations)
    states = [[] for _ in destinations]
    names = {}
    for service in services.sort_values("site_id").to_dict("records"):
        sid = service["site_id"]
        names[sid] = service["name"]
        values = router.table((service["latitude"], service["longitude"]), destinations)
        if len(values) != len(destinations):
            raise ValueError("Неполный результат маршрутизации")
        for i, value in enumerate(values):
            states[i].append(value.state)
            if value.seconds is not None:
                candidate = (float(value.seconds), sid)
                if winners[i] is None or candidate < winners[i]:
                    winners[i] = candidate
    assignments = []
    for winner, state in zip(winners, states):
        if winner is None:
            status = "off_network" if "off_network" in state else "no_route"
            assignments.append(
                {
                    "service_id": None,
                    "service": None,
                    "drive_seconds": None,
                    "total_seconds": None,
                    "status": status,
                }
            )
        else:
            seconds, sid = winner
            total = seconds + dispatch_minutes * 60
            assignments.append(
                {
                    "service_id": sid,
                    "service": names[sid],
                    "drive_seconds": seconds,
                    "total_seconds": total,
                    "status": classify(total, thresholds),
                }
            )
    sites_result = sites.reset_index(drop=True).join(
        pd.DataFrame(assignments[: len(sites)])
    )
    return sites_result, assignments[len(sites) :]


def zone_features(grid, assignments):
    groups = {}
    for cell, assignment in zip(grid.cells, assignments, strict=True):
        key = (assignment["service_id"], assignment["service"], assignment["status"])
        groups.setdefault(key, []).append(cell)
    features = []
    for (sid, name, status), cells in groups.items():
        merged = unary_union(cells)
        geographic = transform(merged, TO_WGS, interleaved=False)
        if grid.boundary is not None:
            geographic = geographic.intersection(grid.boundary)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "service_id": sid,
                    "service": name or "Не назначено",
                    "status": status,
                    "label": LABELS[status],
                    "color": COLORS[status],
                    "step_km": grid.step_km,
                },
                "geometry": mapping(geographic),
            }
        )
    return {"type": "FeatureCollection", "features": features}
