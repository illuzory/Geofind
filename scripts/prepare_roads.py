"""Clip public-domain Natural Earth roads; do not infer real traffic rules."""

import argparse
import hashlib
import json
from pathlib import Path

from shapely.geometry import mapping, shape
from shapely.ops import unary_union


def prepare(source, boundary_path, output):
    raw = Path(source).read_bytes()
    boundary = unary_union(
        [
            shape(f["geometry"])
            for f in json.loads(Path(boundary_path).read_text())["features"]
        ]
    )
    # Keep neighbouring roads: regional analysis does not mean regional-only routes.
    extent = boundary.buffer(0.8)
    features = []
    for f in json.loads(raw)["features"]:
        geom = shape(f["geometry"])
        if not geom.intersects(extent):
            continue
        clipped = geom.intersection(extent)
        if clipped.is_empty:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "source": "Natural Earth 1:10m",
                    "speed_kmh": 65,
                    "direction": "bidirectional_demo",
                    "original_id": f["properties"].get("orig_fid"),
                },
                "geometry": mapping(clipped),
            }
        )
    payload = {
        "type": "FeatureCollection",
        "metadata": {
            "source": "https://github.com/nvkelso/natural-earth-vector/blob/master/geojson/ne_10m_roads.geojson",
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "license": "Public domain",
            "method": "Generalized road geometry. Uniform speed 65 km/h; both directions. Not an OSRM result.",
            "limitations": "Intersections are planar; no turn restrictions, traffic, street-level accuracy or verified travel times.",
        },
        "features": features,
    }
    Path(output).write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    print(len(features), "road features")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("source")
    p.add_argument("boundary")
    p.add_argument("output")
    a = p.parse_args()
    prepare(a.source, a.boundary, a.output)
