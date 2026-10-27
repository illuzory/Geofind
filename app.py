import hashlib
import json
import os
from pathlib import Path
from time import perf_counter

import altair as alt
import pandas as pd
import streamlit as st

from geofind.coverage import (
    COLORS,
    LABELS,
    analyze,
    load_boundary,
    make_grid,
    zone_features,
)
from geofind.data import DataError, read_workbook, safe_csv
from geofind.mapview import build_map, fmt_time
from geofind.routing import DemoRoadRouter, OSRMRouter, RoutingError

ROOT = Path(__file__).resolve().parent
st.set_page_config(page_title="Geofind · Урал", page_icon="🧭", layout="wide")
st.markdown(
    """<style>
.block-container{padding-top:3.5rem;max-width:1550px}h1{letter-spacing:-1.5px}div[data-testid="stMetric"]{background:white;border:1px solid #dfe6ee;border-radius:12px;padding:14px 18px}div[data-testid="stMetricLabel"]{color:#64758a}div[data-testid="stMetricValue"]{font-size:28px}section[data-testid="stSidebar"]{border-right:1px solid #dfe6ee}.eyebrow{font-size:12px;letter-spacing:2px;color:#197c70;font-weight:700;margin-bottom:2px}.subtle{color:#6a7b8c;font-size:14px}.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:#485d72;margin:10px 0}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}
</style>""",
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner=False, max_entries=10)
def parse(content):
    return read_workbook(content)


@st.cache_resource(max_entries=5)
def demo_router(speed):
    return DemoRoadRouter(ROOT / "data/demo_roads.geojson", speed)


@st.cache_resource(max_entries=3)
def osrm_router(url, version):
    return OSRMRouter(url, version, ROOT / ".cache/osrm.sqlite")


@st.cache_data(show_spinner=False, max_entries=3)
def grid_for(step):
    return make_grid(load_boundary(ROOT / "data/sverdlovsk.geojson"), step)


@st.cache_data(show_spinner=False, max_entries=8)
def calculate(mode, speed, url, version, services, sites, step, thresholds, delay):
    router = demo_router(speed) if mode == "demo" else osrm_router(url, version)
    started = perf_counter()
    grid = grid_for(step)
    results, assignments = analyze(router, services, sites, grid, thresholds, delay)
    return (
        results,
        zone_features(grid, assignments),
        len(grid.cells),
        perf_counter() - started,
    )


def main():
    default = ROOT / "data/equipment_inventory_mvp.xlsx"
    if not default.exists():
        default = ROOT / "data/demo_inventory.xlsx"
    if "active_content" not in st.session_state:
        st.session_state.active_content = default.read_bytes()
    with st.sidebar:
        st.markdown("### Geofind")
        st.caption("Пилот · Свердловская область")
        uploaded = st.file_uploader(
            "Обновить таблицы",
            type=["xlsx"],
            help="Одна книга с листами Equipment, Sites, Services",
        )
        if st.button("Применить таблицу", disabled=uploaded is None, width="stretch"):
            try:
                candidate = uploaded.getvalue()
                parse(candidate)
                st.session_state.active_content = candidate
                st.success("Данные обновлены")
            except (DataError, ValueError) as exc:
                st.error(str(exc))
                st.caption("Сохранён предыдущий корректный набор.")
        st.download_button(
            "Скачать текущую книгу",
            st.session_state.active_content,
            file_name="equipment_inventory_mvp.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
    data = parse(st.session_state.active_content)
    sites = data.regional_sites
    boundary = load_boundary(ROOT / "data/sverdlovsk.geojson")
    from shapely.geometry import Point

    outside = [
        r["name"]
        for r in sites.to_dict("records")
        if not boundary.covers(Point(r["longitude"], r["latitude"]))
    ]
    if outside:
        st.error(
            "Площадки отмечены как Свердловская область, но находятся вне границы: "
            + ", ".join(outside)
        )
        return
    with st.sidebar:
        st.divider()
        st.markdown("**Сценарий выезда**")
        active = st.multiselect(
            "Основные сервисные точки",
            list(data.services.site_id),
            default=list(data.services.site_id),
            format_func=lambda sid: data.services.set_index("site_id").loc[sid, "name"],
        )
        extras = st.multiselect(
            "Дополнительные инженеры",
            list(sites[sites.site_id != "ekb"].name),
            help="Условная дополнительная точка в выбранном городе. Проверяет эффект расширения сервиса.",
        )
        mode_label = st.selectbox(
            "Расчёт времени", ["Демо · дорожная модель", "OSRM · автомобильный маршрут"]
        )
        mode = "demo" if mode_label.startswith("Демо") else "osrm"
        speed = st.slider(
            "Скорость на дорогах модели, км/ч", 40, 90, 65, 5, disabled=mode != "demo"
        )
        delay = st.slider("Подготовка к выезду, мин", 0, 120, 0, 15)
        with st.expander("Пороги SLA и точность"):
            green = st.number_input(
                "В SLA, часов", min_value=0.25, max_value=24.0, value=4.0, step=0.25
            )
            yellow = st.number_input(
                "Риск до, часов", min_value=0.5, max_value=36.0, value=6.0, step=0.25
            )
            red = st.number_input(
                "Критично до, часов",
                min_value=0.75,
                max_value=48.0,
                value=8.0,
                step=0.25,
            )
            step = st.select_slider("Шаг покрытия, км", options=[10, 20, 40], value=20)
            tiles = st.checkbox(
                "Внешняя картографическая подложка",
                value=False,
                help="Требует доступа браузера к CartoDB. Автономная карта работает без неё.",
            )
        st.caption(
            "Время прибытия = дорога + подготовка. Ремонт и доступность инженеров не моделируются."
        )
    thresholds = (green, yellow, red)
    services = data.services[data.services.site_id.isin(active)].copy()
    for name in extras:
        row = sites[sites.name == name].iloc[0].copy()
        row["site_id"] = "scenario-" + row.site_id
        row["name"] = "Инженеры · " + name
        row["address"] = "Условная дополнительная точка для сравнения сценариев"
        row["is_demo"] = True
        services = pd.concat([services, pd.DataFrame([row])], ignore_index=True)
    st.markdown(
        '<div class="eyebrow">GEOFIND / SERVICE REACH</div>', unsafe_allow_html=True
    )
    st.title("Урал · контроль доезда")
    st.markdown(
        '<div class="subtle">Свердловская область · площадки, инженеры и доступность по SLA</div>',
        unsafe_allow_html=True,
    )
    if not green < yellow < red:
        st.error("Пороги должны возрастать: SLA < риск < критично.")
        return
    if services.empty:
        st.info("Выберите хотя бы одну сервисную точку.")
        return
    if len(services) > 10:
        st.error("В MVP допускается до 10 сервисных точек.")
        return
    if mode == "demo":
        st.info(
            "Демонстрация: координаты условные, время — модельная оценка по обобщённой дорожной сети. Это не подтверждённое время поездки."
        )
    else:
        st.info(
            "OSRM: время по автомобильному графу. Координаты MVP остаются условными; текущие пробки не учитываются."
        )
    url = os.environ.get("OSRM_URL", "http://127.0.0.1:5000")
    version = os.environ.get("OSRM_DATA_VERSION", "")
    if mode == "osrm":
        signature = hashlib.sha256(
            repr(
                (
                    data.fingerprint,
                    active,
                    extras,
                    step,
                    thresholds,
                    delay,
                    url,
                    version,
                )
            ).encode()
        ).hexdigest()
        if st.button("Рассчитать дорожный сценарий", type="primary"):
            st.session_state.osrm_signature = signature
        if st.session_state.get("osrm_signature") != signature:
            st.caption(
                "Нажмите «Рассчитать дорожный сценарий». Настройка подключения описана во вкладке «Методика» ниже."
            )
            with st.expander("Подключение OSRM"):
                st.code(
                    "OSRM_URL=http://127.0.0.1:5000\nOSRM_DATA_VERSION=<версия графа и car-профиля>"
                )
            return
    try:
        with st.spinner("Рассчитываем доезд и зоны покрытия…"):
            results, zones, cell_count, elapsed = calculate(
                mode, speed, url, version, services, sites, step, thresholds, delay
            )
    except (RoutingError, ValueError) as exc:
        st.error(str(exc))
        st.caption(
            "Сценарий не рассчитан. Ошибка сервиса не означает недостижимость площадок. Демо-режим доступен отдельно."
        )
        return
    router = demo_router(speed) if mode == "demo" else osrm_router(url, version)
    counts = data.equipment["Site Name"].value_counts()
    results["equipment"] = results.name.map(counts).fillna(0).astype(int)
    reached = results.total_seconds.notna()
    ok = int((results.status == "ok").sum())
    maximum = results.total_seconds.max()
    cols = st.columns(4)
    cols[0].metric(
        "Площадки региона",
        len(results),
        f"{len(services)} точек выезда",
        delta_color="off",
    )
    cols[1].metric(
        "В SLA",
        f"{ok} из {len(results)}",
        f"{ok / len(results):.0%} площадок",
        delta_color="off",
    )
    cols[2].metric(
        "Максимум до прибытия",
        fmt_time(maximum),
        f"Подготовка: {delay} мин",
        delta_color="off",
    )
    cols[3].metric(
        "Оборудование в регионе",
        int(results.equipment.sum()),
        f"{len(data.equipment)} во всей книге",
        delta_color="off",
    )
    if not reached.all():
        st.warning(
            f"Не рассчитано {int((~reached).sum())} площадок: нет пути или привязки в выбранном графе. Они не включены в максимум времени."
        )
    st.caption(
        f"Шаг зон покрытия: {step} км. Изменения сценария применяются автоматически в демо-режиме."
    )
    if extras and active:
        base_services = data.services[data.services.site_id.isin(active)]
        try:
            base, _, _, _ = calculate(
                mode, speed, url, version, base_services, sites, step, thresholds, delay
            )
            base_ok = int((base.status == "ok").sum())
            st.success(
                f"Сравнение с основными сервисами: в SLA было {base_ok} из {len(base)}, стало {ok} из {len(results)}."
            )
        except (RoutingError, ValueError):
            st.caption(
                "Базовый сценарий для сравнения недоступен; показан текущий расчёт."
            )
    map_tab, equipment_tab, method_tab = st.tabs(
        ["Карта и доезд", "Оборудование", "Данные и методика"]
    )
    with map_tab:
        left, right = st.columns([3, 1])
        with right:
            st.markdown("**Маршрут к площадке**")
            selected = st.selectbox(
                "Площадка",
                list(results.site_id),
                format_func=lambda sid: results.set_index("site_id").loc[sid, "name"],
                index=len(results) - 1,
            )
            record = results[results.site_id == selected].iloc[0]
            st.markdown(f"**{LABELS[record.status]}**")
            st.metric("Дорога", fmt_time(record.drive_seconds))
            st.metric("С подготовкой", fmt_time(record.total_seconds))
            st.write(record.service or "Сервис не назначен")
            st.caption(
                "Синяя линия — путь от инженеров к площадке. Номер у маркера обозначает сервисную точку."
            )
        route = None
        if pd.notna(record.service_id):
            origin = services[services.site_id == record.service_id].iloc[0]
            try:
                route = router.route(
                    (origin.latitude, origin.longitude),
                    (record.latitude, record.longitude),
                )
                if route is None:
                    st.warning(
                        "Маршрут для выбранной площадки не получен. Обновите расчёт графа."
                    )
                elif abs(route.seconds - float(record.drive_seconds)) > max(
                    5, 0.01 * float(record.drive_seconds)
                ):
                    st.warning(
                        "Время Route отличается от Table; проверьте версию графа и обновите кэш."
                    )
            except RoutingError as exc:
                st.warning(str(exc))
        roads = json.loads(ROOT.joinpath("data/demo_roads.geojson").read_text())
        map_obj = build_map(
            results,
            services,
            data.equipment,
            zones,
            boundary,
            roads,
            route,
            selected,
            tiles,
        )
        with left:
            legend = "".join(
                f'<span><i class="dot" style="background:{COLORS[key]}"></i>{text}</span>'
                for key, text in [
                    ("ok", f"≤ {green:g} ч"),
                    ("risk", f"{green:g}–{yellow:g} ч"),
                    ("critical", f"{yellow:g}–{red:g} ч"),
                    ("outside", f"> {red:g} ч"),
                    ("no_route", "Нет расчёта"),
                ]
            )
            st.markdown(
                '<div class="legend">' + legend + "</div>", unsafe_allow_html=True
            )
            st.iframe(
                map_obj.get_root().render(),
                height=560,
                alt="Карта площадок и зон доезда",
            )
        if route:
            st.caption(route.note)
        table = results[
            ["name", "service", "drive_seconds", "total_seconds", "status", "equipment"]
        ].copy()
        table["drive_seconds"] = table.drive_seconds.map(fmt_time)
        table["total_seconds"] = table.total_seconds.map(fmt_time)
        table["status"] = table.status.map(LABELS)
        table.columns = [
            "Площадка",
            "Сервис",
            "Дорога",
            "До прибытия",
            "Категория",
            "Оборудование",
        ]
        st.dataframe(table, hide_index=True, width="stretch")
        export = results[
            [
                "site_id",
                "name",
                "latitude",
                "longitude",
                "service_id",
                "service",
                "drive_seconds",
                "total_seconds",
                "status",
                "equipment",
            ]
        ].copy()
        export["calculation_mode"] = mode
        export["is_demo_coordinates"] = True
        export["data_version"] = data.fingerprint
        a, b = st.columns(2)
        a.download_button(
            "Скачать результаты CSV",
            safe_csv(export),
            file_name="ural_coverage.csv",
            mime="text/csv",
        )
        b.download_button(
            "Скачать автономную карту HTML",
            map_obj.get_root().render(),
            file_name="ural_coverage.html",
            mime="text/html",
        )
    with equipment_tab:
        all_cities = st.checkbox("Показать города за пределами пилота")
        relevant = data.sites if all_cities else sites
        names = st.multiselect(
            "Города", list(relevant.name), default=list(relevant.name)
        )
        statuses = st.multiselect(
            "Статусы оборудования",
            sorted(data.equipment.Status.unique()),
            default=sorted(data.equipment.Status.unique()),
        )
        visible = data.equipment[
            data.equipment["Site Name"].isin(names)
            & data.equipment.Status.isin(statuses)
        ]
        st.caption(
            f"{len(visible)} устройств. Исходное оборудование и демонстрационные добавления различаются в Data Source."
        )
        st.dataframe(visible, hide_index=True, width="stretch")
        chart_data = (
            visible["Device Type"]
            .value_counts()
            .rename_axis("Тип")
            .reset_index(name="Устройств")
        )
        chart = (
            alt.Chart(chart_data)
            .mark_bar(color="#197c70")
            .encode(x=alt.X("Тип:N", sort="-y"), y=alt.Y("Устройств:Q"))
        )
        st.altair_chart(chart, width="stretch")
    with method_tab:
        st.markdown("""**Что сравнивается.** Для каждого общего узла сетки выбирается минимум времени «сервис → узел». При равенстве используется устойчивый идентификатор. Для площадок расчёт выполняется отдельно по их координатам. Смена фильтра не меняет исходные таблицы.

**Точность карты.** Зоны приближённые, с указанным шагом сетки. Внутри ячейки время может отличаться. Ячейки обрезаны границей области; раздельные компоненты сохранены. Самые быстрые дороги могут проходить через соседние регионы.

**Демо-модель.** Natural Earth 1:10m — обобщённые дороги, единая выбранная скорость, оба направления и плоские пересечения. Подъезд от точки до дороги моделируется по прямой при 20 км/ч, максимум 25 км. Нет пути в этой модели не доказывает отсутствие реальной дороги. Ни одна оценка не является обещанием SLA.

**OSRM.** Полноценный автомобильный профиль подключается через настройки среды `OSRM_URL` и `OSRM_DATA_VERSION`. Нужен граф области и соседних дорог; привязка ограничена 3 км. Кэш учитывает граф, профиль, координаты и направление. Ошибки API останавливают расчёт и не превращаются в «нет пути».

**Обновление.** Скачайте книгу, измените листы Equipment, Sites, Services и загрузите её слева. Только валидная книга заменит текущие данные. Изменение оборудования не меняет дорожные времена. Уникальные названия связывают оборудование с площадками. Координаты этой версии выбраны для демонстрации; адрес на Буторина не геокодирован.

**SLA.** По умолчанию 4/6/8 часов до прибытия; учитывается заданная подготовка. Пробки, погода, ремонт, графики и загрузка инженеров не моделируются.

**Ресурсы.** Один регион, до 10 сервисов, 2000 площадок и 10000 ячеек; граф не загружается на всю Россию. Таблицы ограничены 50000 строк и 10 МБ на XLSX. Карта и её скрипты работают без внешней подложки.
""")
        st.caption(
            f"Версия загруженной книги: {data.fingerprint[:12]} · ячеек {cell_count} · расчёт {elapsed:.2f} с"
        )
        st.dataframe(data.sites, hide_index=True, width="stretch")
        st.dataframe(services, hide_index=True, width="stretch")
        st.caption(
            "Источники: Natural Earth — public domain; граница области — Natural Earth. См. документацию SOURCES.md."
        )


if __name__ == "__main__":
    main()
