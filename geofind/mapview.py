import html
import math
from pathlib import Path

import folium
from branca.element import Element

from .coverage import COLORS, LABELS

ROOT = Path(__file__).resolve().parents[1]


def _text(value):
    return html.escape(str(value)).replace("`", "&#96;").replace("$", "&#36;")


def fmt_time(seconds):
    if seconds is None or not math.isfinite(float(seconds)):
        return "Нет расчёта"
    minutes = round(float(seconds) / 60)
    return f"{minutes // 60} ч {minutes % 60:02} мин"


def build_map(
    sites,
    services,
    equipment,
    zones,
    boundary,
    roads,
    route=None,
    selected=None,
    online_tiles=False,
):
    m = folium.Map(
        location=[58.0, 60.7],
        zoom_start=7,
        tiles=None,
        control_scale=True,
        prefer_canvas=True,
    )
    # Embed the exact vendored assets: map works without external CDN access.
    m.default_js = []
    m.default_css = []
    m.get_root().header.add_child(
        Element(
            "<style>"
            + ROOT.joinpath("assets/leaflet.css").read_text()
            + '\n.leaflet-container{background:#e9f0f4;font-family:Arial,sans-serif}.leaflet-control-layers-toggle{background-image:none!important}.leaflet-control-layers-toggle:after{content:"☷";font-size:26px;margin-left:8px}.service-dot{background:#173e63;color:white;border:3px solid white;border-radius:50%;box-shadow:0 1px 5px #40556c;text-align:center;font:bold 14px Arial;line-height:27px}.leaflet-popup-content{line-height:1.6}</style>'
        )
    )
    for name in ["leaflet.js", "jquery.js"]:
        m.get_root().header.add_child(
            Element(
                "<script>" + ROOT.joinpath("assets", name).read_text() + "</script>"
            )
        )
    if online_tiles:
        folium.TileLayer("CartoDB positron", name="Подложка CartoDB").add_to(m)
    folium.GeoJson(
        boundary.__geo_interface__,
        name="Граница региона",
        style_function=lambda _: {
            "color": "#637e98",
            "weight": 1.5,
            "fillColor": "#f8fafb",
            "fillOpacity": 0.8,
        },
    ).add_to(m)
    display_zones = {
        **zones,
        "features": [
            {
                **f,
                "properties": {
                    **f["properties"],
                    "service": _text(f["properties"]["service"]),
                },
            }
            for f in zones["features"]
        ],
    }
    folium.GeoJson(
        display_zones,
        name="Зоны доезда · общая сетка",
        style_function=lambda f: {
            "color": f["properties"]["color"],
            "weight": 0.4,
            "fillColor": f["properties"]["color"],
            "fillOpacity": 0.23,
        },
        tooltip=folium.GeoJsonTooltip(
            fields=["service", "label", "step_km"],
            aliases=["Сервис", "Категория", "Шаг, км"],
            sticky=False,
        ),
    ).add_to(m)
    folium.GeoJson(
        roads,
        name="Обобщённые дороги Natural Earth",
        style_function=lambda _: {"color": "#748fa0", "weight": 1.2, "opacity": 0.6},
    ).add_to(m)
    service_layer = folium.FeatureGroup(name="Инженеры", show=True)
    for i, row in enumerate(services.to_dict("records"), 1):
        popup = f"<b>{_text(row['name'])}</b><br>{_text(row['address'])}<br><small>Условная точка выезда</small>"
        folium.Marker(
            [row["latitude"], row["longitude"]],
            icon=folium.DivIcon(
                html=f'<div class="service-dot">{i}</div>',
                icon_size=(32, 32),
                icon_anchor=(16, 16),
            ),
            popup=folium.Popup(popup, max_width=320),
            tooltip=_text(row["name"]),
            z_index_offset=1000,
        ).add_to(service_layer)
    service_layer.add_to(m)
    clients = folium.FeatureGroup(name="Площадки", show=True)
    for row in sites.to_dict("records"):
        records = equipment[equipment["Site Name"] == row["name"]]
        types = records["Device Type"].value_counts()
        details = ", ".join(f"{_text(str(k))}: {v}" for k, v in types.items())
        service = row.get("service") or "Не назначен"
        popup = f"<b>{_text(row['name'])}</b><br>{_text(LABELS[row['status']])} · <b>{fmt_time(row.get('total_seconds'))}</b><br>Сервис: {_text(service)}<br>Оборудование: {len(records)}<br><small>{details}</small><br><small>Координаты: условная площадка MVP</small>"
        folium.CircleMarker(
            [row["latitude"], row["longitude"]],
            radius=10 if row["site_id"] == selected else 7,
            color="white",
            weight=2,
            fill=True,
            fill_color=COLORS[row["status"]],
            fill_opacity=1,
            tooltip=f"{_text(row['name'])} · {fmt_time(row.get('total_seconds'))}",
            popup=folium.Popup(popup, max_width=340),
        ).add_to(clients)
    clients.add_to(m)
    if route:
        folium.PolyLine(
            [[lat, lon] for lon, lat in route.coordinates],
            color="#224f9c",
            weight=4,
            opacity=0.9,
            tooltip=f"Маршрут выезда · {fmt_time(route.seconds)}",
        ).add_to(m)
    points = list(
        sites[["latitude", "longitude"]].itertuples(index=False, name=None)
    ) + list(services[["latitude", "longitude"]].itertuples(index=False, name=None))
    if points:
        m.fit_bounds(
            [
                [min(p[0] for p in points) - 0.2, min(p[1] for p in points) - 0.25],
                [max(p[0] for p in points) + 0.2, max(p[1] for p in points) + 0.25],
            ]
        )
    folium.LayerControl(collapsed=True).add_to(m)
    m.get_root().html.add_child(
        Element(
            '<div style="position:fixed;bottom:8px;left:8px;background:white;padding:5px 8px;border-radius:4px;z-index:999;font:11px Arial">Условные площадки · Natural Earth, public domain · граница: Natural Earth</div>'
        )
    )
    return m
