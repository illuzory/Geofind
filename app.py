import hashlib
import json
import os
from pathlib import Path
from time import perf_counter

import altair as alt
import pandas as pd
import streamlit as st

from geofind.addressing import AddressChecker
from geofind.coverage import (
    COLORS,
    LABELS,
    analyze,
    load_boundary,
    make_grid,
    sla_summary,
    zone_features,
)
from geofind.data import (
    SERVICE_TYPES,
    DataError,
    read_workbook,
    safe_csv,
    write_workbook,
)
from geofind.imports import ImportStore, compare, display_time
from geofind.mapview import build_map, fmt_time
from geofind.routing import DemoRoadRouter, OSRMRouter, RoutingError

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("GEOFIND_STATE_DIR", ROOT / ".cache/runtime"))
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
    if (
        mode == "osrm"
        and router.public_demo
        and (len(grid.cells) > 1000 or len(services) > 3)
    ):
        raise RoutingError(
            "Публичный OSRM: до 1000 ячеек и 3 сервисов. Выберите шаг 20/40 км; для большей нагрузки нужен собственный сервер."
        )
    results, assignments = analyze(router, services, sites, grid, thresholds, delay)
    return (
        results,
        zone_features(grid, assignments),
        len(grid.cells),
        perf_counter() - started,
    )


def address_panel(data, store):
    with st.expander("Проверка адресов и координат", expanded=False):
        st.caption(
            "Проверяется соответствие города, улицы, дома и координат по OSM. Это не официальная проверка ФИАС/ГАР и не подтверждение размещения оборудования."
        )
        online = st.checkbox(
            "Онлайн-проверка новых адресов",
            value=False,
            help="Новые адреса будут отправлены геокодеру; примеры можно проверить по сохранённым ответам без сети.",
        )
        refresh = st.checkbox(
            "Повторно проверить известные адреса онлайн",
            value=False,
            disabled=not online,
            help="Обходит сохранённые ответы и кэш; требует сетевого доступа к геокодеру.",
        )
        if st.button("Проверить адреса"):
            try:
                checker = AddressChecker(
                    STATE_DIR / "addresses.sqlite",
                    ROOT / "data/address_reference.json",
                    os.environ.get(
                        "GEOCODER_URL", "https://nominatim.openstreetmap.org"
                    ),
                )
            except ValueError as exc:
                st.error("Не удалось начать проверку адресов: " + str(exc))
                return
            rows = data.regional_sites.to_dict("records") + data.services.to_dict(
                "records"
            )
            if online and len(rows) > 30:
                st.error(
                    "За один запуск онлайн-проверки — до 30 адресов. Для больших наборов используйте собственный геокодер."
                )
            else:
                with st.spinner("Сверяем адреса и координаты…"):
                    st.session_state.address_report = [
                        checker.check(
                            row, allow_network=online, force_refresh=online and refresh
                        )
                        for row in rows
                    ]
                    st.session_state.address_report_version = data.fingerprint
        if st.session_state.get("address_report_version") == data.fingerprint:
            report = st.session_state.address_report
            table = pd.DataFrame(report)
            table["checked_at"] = table["checked_at"].map(display_time)
            confirmed = int((table.status == "verified").sum())
            st.write(f"Согласованы адрес и координаты: {confirmed} из {len(table)}")
            table["osm_link"] = table.apply(
                lambda r: (
                    f"https://www.openstreetmap.org/?mlat={r.suggested_latitude}&mlon={r.suggested_longitude}#map=18/{r.suggested_latitude}/{r.suggested_longitude}"
                    if pd.notna(r.suggested_latitude)
                    else None
                ),
                axis=1,
            )
            st.dataframe(
                table[
                    [
                        "name",
                        "address",
                        "label",
                        "reason",
                        "distance_m",
                        "matched_address",
                        "source",
                        "checked_at",
                        "osm_link",
                    ]
                ],
                hide_index=True,
                width="stretch",
                column_config={
                    "osm_link": st.column_config.LinkColumn(
                        "На карте OSM", display_text="Открыть"
                    )
                },
            )
            st.download_button(
                "Скачать отчёт адресации",
                safe_csv(table),
                file_name="address_validation.csv",
                mime="text/csv",
            )
            corrections = [r for r in report if r["status"] == "coordinate_mismatch"]
            if corrections:
                st.warning(
                    "Обнаружено расхождение координат. Предложенные адреса и смещение показаны выше; координаты не меняются автоматически."
                )
                if st.button("Применить предложенные координаты"):
                    sites = data.sites.copy()
                    services = data.services.copy()
                    for r in corrections:
                        for frame in [sites, services]:
                            frame.loc[
                                frame.site_id == r["site_id"], ["latitude", "longitude"]
                            ] = [r["suggested_latitude"], r["suggested_longitude"]]
                    raw = write_workbook(data.equipment, sites, services)
                    try:
                        store.apply(
                            raw,
                            "address-correction.xlsx",
                            expected_current=data.fingerprint,
                        )
                    except (DataError, ValueError) as exc:
                        st.error(str(exc))
                    else:
                        st.session_state.active_content = raw
                        st.session_state.recalculate_after_import = (
                            st.session_state.get("auto_update_imports", True)
                        )
                        st.rerun()


def main():
    default = Path(
        os.environ.get("GEOFIND_WORKBOOK", str(ROOT / "data/ural_inventory.xlsx"))
    )
    if not default.exists():
        default = ROOT / "data/equipment_inventory_mvp.xlsx"
    if not default.exists():
        default = ROOT / "data/demo_inventory.xlsx"
    store = ImportStore(STATE_DIR / "imports.sqlite")
    persisted = store.active()
    if persisted is None:
        store.apply(default.read_bytes(), default.name)
        persisted = store.active()
    if "active_content" not in st.session_state:
        st.session_state.active_content = persisted
    with st.sidebar:
        st.markdown("### Geofind")
        st.caption("Пилот · Свердловская область")
        uploaded = st.file_uploader(
            "Обновить таблицы",
            type=["xlsx"],
            help="Одна книга с листами Equipment, Sites, Services. Изменения применяются без перезапуска.",
        )
        auto_update = st.checkbox(
            "Пересчитывать после импорта", value=True, key="auto_update_imports"
        )
        current = parse(st.session_state.active_content)
        candidate = None
        if uploaded is not None:
            try:
                candidate = parse(uploaded.getvalue())
                diff = compare(current, candidate)
                st.caption(
                    f"Изменено площадок: {diff['sites_changed']}; добавлено: {diff['sites_added']}; удалено: {diff['sites_removed']}; устройств: {diff['equipment_delta']:+d}"
                )
                if diff["details"]:
                    with st.expander("Предпросмотр изменений"):
                        st.dataframe(pd.DataFrame(diff["details"]), hide_index=True)
            except (DataError, ValueError) as exc:
                st.error(str(exc))
                st.caption("Текущая версия не изменена.")
        if st.button(
            "Применить таблицу",
            disabled=candidate is None or candidate.fingerprint == current.fingerprint,
            width="stretch",
        ):
            try:
                raw = uploaded.getvalue()
                store.apply(raw, uploaded.name, expected_current=current.fingerprint)
                st.session_state.last_import_diff = compare(current, candidate)
                st.session_state.active_content = raw
                st.session_state.recalculate_after_import = auto_update
                st.session_state.import_notice = "Данные обновлены"
                st.rerun()
            except (DataError, ValueError) as exc:
                st.error(str(exc))
        if st.session_state.get("import_notice"):
            st.success(st.session_state.pop("import_notice"))
        if (
            candidate is not None
            and candidate.fingerprint
            == parse(st.session_state.active_content).fingerprint
        ):
            st.caption("Эта версия книги уже активна.")
        with st.expander("Версии и восстановление"):
            history = store.history()
            selected_version = st.selectbox(
                "Сохранённая версия",
                [r["id"] for r in history],
                format_func=lambda key: next(
                    r["filename"] + " · " + display_time(r["created_at"])
                    for r in history
                    if r["id"] == key
                ),
            )
            if st.button("Восстановить выбранную версию"):
                store.activate(selected_version)
                st.session_state.active_content = store.active()
                st.session_state.recalculate_after_import = auto_update
                st.rerun()
            if st.button("Обновить из хранилища"):
                st.session_state.active_content = store.active()
                st.session_state.recalculate_after_import = auto_update
                st.rerun()
            st.caption(
                "Набор сохраняется на сервере. При параллельном импорте устаревшая версия не перезапишет новую."
            )
        st.download_button(
            "Скачать текущую книгу",
            st.session_state.active_content,
            file_name="ural_inventory.xlsx",
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
            "Расчёт времени",
            ["Демо · дорожная модель", "OSRM · автомобильный маршрут"],
            index=1
            if os.environ.get("OSRM_URL")
            and (
                os.environ.get("OSRM_DATA_VERSION")
                or os.environ.get("OSRM_CACHE_NAMESPACE")
            )
            else 0,
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
        row["service_type"] = "standard"
        row["approval_note"] = ""
        services = pd.concat([services, pd.DataFrame([row])], ignore_index=True)
    st.markdown(
        '<div class="eyebrow">GEOFIND / SERVICE REACH</div>', unsafe_allow_html=True
    )
    st.title("Урал · контроль доезда")
    st.markdown(
        '<div class="subtle">Свердловская область · площадки, инженеры и доступность по SLA</div>',
        unsafe_allow_html=True,
    )
    address_panel(data, store)
    if st.session_state.get("last_import_diff"):
        difference = st.session_state.last_import_diff
        st.caption(
            f"Последний импорт: изменено {difference['sites_changed']} площадок; устройств {difference['equipment_delta']:+d}. Версия {data.fingerprint[:8]}"
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
            "Демонстрационные площадки по выбранным адресам. Время — модельная оценка по обобщённой дорожной сети, не подтверждённое время поездки."
        )
    else:
        st.info(
            "OSRM: время по автомобильному графу. Адреса выбраны для MVP, размещение оборудования там не подтверждено; текущие пробки не учитываются."
        )
    url = os.environ.get("OSRM_URL", "http://127.0.0.1:5000")
    version = os.environ.get("OSRM_DATA_VERSION") or os.environ.get(
        "OSRM_CACHE_NAMESPACE", ""
    )
    recalculate = st.session_state.pop("recalculate_after_import", False)
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
        if st.button("Рассчитать дорожный сценарий", type="primary") or recalculate:
            st.session_state.osrm_signature = signature
        if st.session_state.get("osrm_signature") != signature:
            st.caption(
                "Нажмите «Рассчитать дорожный сценарий», чтобы получить времена и зоны покрытия."
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
    summary = sla_summary(results)
    ok = summary["within_sla"]
    maximum = summary["maximum_seconds"]
    cols = st.columns(5)
    cols[0].metric(
        "Площадки региона",
        len(results),
        f"{len(services)} точек выезда",
        delta_color="off",
    )
    cols[1].metric(
        "В SLA",
        f"{ok} из {summary['eligible']}" if summary["eligible"] else "Не применяется",
        f"{summary['percentage']:.0%} площадок общего SLA"
        if summary["eligible"]
        else "Нет площадок общего SLA",
        delta_color="off",
    )
    cols[2].metric(
        "Максимум общего SLA",
        fmt_time(maximum) if summary["eligible"] else "Не применяется",
        f"Подготовка: {delay} мин",
        delta_color="off",
    )
    cols[3].metric(
        "Оборудование в регионе",
        int(results.equipment.sum()),
        f"{len(data.equipment)} во всей книге",
        delta_color="off",
    )
    cols[4].metric(
        "По согласованию",
        summary["approval_required"],
        "Исключены из общего SLA",
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
            base_summary = sla_summary(base)
            base_ok = base_summary["within_sla"]
            st.success(
                f"Сравнение с основными сервисами: в SLA было {base_ok} из {base_summary['eligible']}, стало {ok} из {summary['eligible']}. По согласованию: {summary['approval_required']} отдельно."
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
            st.caption(record.address)
            if not record.sla_eligible:
                st.info(
                    "Выезд требует согласования и не учитывается в общем SLA. Показано только время дороги и подготовки, срок согласования не оценивается. "
                    + str(record.get("approval_note", ""))
                )
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
                    ("approval_required", "По согласованию"),
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
            [
                "name",
                "address",
                "service_type",
                "service",
                "drive_seconds",
                "total_seconds",
                "status",
                "equipment",
            ]
        ].copy()
        table["drive_seconds"] = table.drive_seconds.map(fmt_time)
        table["total_seconds"] = table.total_seconds.map(fmt_time)
        table["status"] = table.status.map(LABELS)
        table["service_type"] = table.service_type.map(SERVICE_TYPES)
        table.columns = [
            "Площадка",
            "Полный адрес",
            "Тип площадки",
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
                "address",
                "service_type",
                "sla_eligible",
                "approval_note",
                "routing_status",
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
        export["is_demo_coordinates"] = results.is_demo
        export["routing_version"] = version if mode == "osrm" else router.fingerprint
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

**OSRM.** Подключение задаётся через `OSRM_URL` и `OSRM_DATA_VERSION` (версия своего графа) либо `OSRM_CACHE_NAMESPACE` (идентификатор наблюдения публичного demo). Публичный сервер не сообщает версию графа и не гарантирует стабильность; запросы ограничены одним в секунду. Нужен граф области и соседних дорог; привязка ограничена 3 км и проверяется по возвращённому расстоянию для каждой точки отдельно. Кэш учитывает граф, профиль, координаты и направление. Ошибки API останавливают расчёт и не превращаются в «нет пути».

**Обновление.** Скачайте книгу, измените листы Equipment, Sites, Services и загрузите её слева. Только валидная книга заменит текущие данные. Изменение оборудования не меняет дорожные времена. Site ID стабильно связывает импортируемые версии; имена оборудования и площадок должны совпадать. Версии сохраняются в SQLite и могут быть восстановлены. Адреса новых примеров найдены в OSM, однако оборудование в этих домах остаётся демонстрационным; проверка не подтверждает его фактическое размещение.

**SLA.** Площадки approval_required показываются отдельно и исключаются из числителя, знаменателя и максимума общего SLA, даже при коротком времени дороги. Ожидание согласования не рассчитывается. По умолчанию 4/6/8 часов до прибытия; учитывается заданная подготовка. Пробки, погода, ремонт, графики и загрузка инженеров не моделируются.

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
