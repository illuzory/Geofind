"""Build regional examples from saved, actually retrieved OSM responses.

The discovery JSON is input evidence, not instructions. Existing source records
are preserved for the four original regional cities; other regions stay in the
legacy workbook, which is never overwritten by this script.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd

from geofind.addressing import evaluate, full_address, normalize, query_key
from geofind.data import REGION, read_workbook, validate, write_workbook

QUERIES = {
    "ekb": [
        "Екатеринбург проспект Ленина 24а",
        "Екатеринбург улица Малышева 46",
        "Екатеринбург улица 8 Марта 8",
    ],
    "tagil": [
        "Нижний Тагил улица Пархоменко 1а",
        "Нижний Тагил проспект Ленина 1",
        "Нижний Тагил проспект Ленина 31",
    ],
    "kamensk": [
        "Каменск-Уральский улица Ленина 36",
        "Каменск-Уральский улица Ленина 32",
        "Каменск-Уральский проспект Победы 97а",
    ],
    "serov": [
        "Серов улица Ленина 140",
        "Серов улица Ленина 171",
        "Серов улица Льва Толстого 24",
    ],
    "berezovsky": [
        "Берёзовский Свердловская область улица Театральная 9",
        "Берёзовский Свердловская область улица Ленина 11",
        "Берёзовский Свердловская область улица Красных Героев 2",
    ],
    "revda": [
        "Ревда Свердловская область улица Цветников 21",
        "Ревда Свердловская область улица Мира 32",
        "Ревда Свердловская область улица Горького 12",
    ],
    "pervouralsk": [
        "Первоуральск улица Ватутина 41",
        "Первоуральск проспект Ильича 28",
        "Первоуральск улица Ватутина 45",
    ],
    "pyshma": [
        "Верхняя Пышма Успенский проспект 113",
        "Верхняя Пышма улица Красноармейская 1",
        "Верхняя Пышма Успенский проспект 111",
    ],
    "asbest": [
        "Асбест улица Уральская 73",
        "Асбест улица Уральская 71",
        "Асбест улица Уральская 75",
    ],
    "polevskoy": [
        "Полевской улица Ленина 2",
        "Полевской улица Свердлова 19",
        "Полевской улица Карла Маркса 22",
    ],
    "rezh": [
        "Реж улица Красноармейская 16",
        "Реж улица Ленина 3",
        "Реж улица Ленина 5",
    ],
    "nevyansk": [
        "Невьянск улица Кирова 1",
        "Невьянск улица Кирова 2",
        "Невьянск улица Ленина 13",
    ],
    "kirovgrad": [
        "Кировград улица Свердлова 44",
        "Кировград улица Свердлова 35",
        "Кировград улица Свердлова 41",
    ],
    "krasnoufimsk": [
        "Красноуфимск улица Советская 25",
        "Красноуфимск улица Мизерова 98",
        "Красноуфимск улица Мизерова 100",
    ],
    "alapayevsk": [
        "Алапаевск улица Ленина 18",
        "Алапаевск улица Ленина 7",
        "Алапаевск улица Ленина 20",
    ],
    "service-ekb": [
        "Екатеринбург переулок Буторина 6",
        "Екатеринбург переулок Буторина 3",
        "Екатеринбург переулок Буторина 10",
    ],
}
NAMES = {
    "ekb": "Екатеринбург",
    "tagil": "Нижний Тагил",
    "kamensk": "Каменск-Уральский",
    "serov": "Серов",
    "berezovsky": "Берёзовский",
    "revda": "Ревда",
    "pervouralsk": "Первоуральск",
    "pyshma": "Верхняя Пышма",
    "asbest": "Асбест",
    "polevskoy": "Полевской",
    "rezh": "Реж",
    "nevyansk": "Невьянск",
    "kirovgrad": "Кировград",
    "krasnoufimsk": "Красноуфимск",
    "alapayevsk": "Алапаевск",
    "service-ekb": "Инженеры · Екатеринбург",
}


def build(discovery, source, output, variant):
    original = read_workbook(source)
    evidence = json.loads(Path(discovery).read_text())
    if "responses" in evidence:
        evidence = {value["query"]: value for value in evidence["responses"].values()}
    prior_path = Path(__file__).resolve().parents[1] / "data/address_reference.json"
    prior = (
        json.loads(prior_path.read_text()).get("responses", {})
        if prior_path.exists()
        else {}
    )
    previous_dates = {value["query"]: value["checked_at"] for value in prior.values()}
    generated_at = datetime.now(UTC).isoformat()
    sites = []
    services = []
    responses = {}
    approval = (
        {"pervouralsk", "pyshma"}
        if variant == 0
        else (
            {"pervouralsk", "pyshma", "serov"} if variant == 1 else {"pyshma", "revda"}
        )
    )
    approval |= {"krasnoufimsk", "alapayevsk"}
    for sid, qs in QUERIES.items():
        entry = evidence[qs[variant]]
        raw_candidates = entry["candidates"] if isinstance(entry, dict) else entry
        checked = (
            entry.get("checked_at", generated_at)
            if isinstance(entry, dict)
            else previous_dates.get(qs[variant], generated_at)
        )
        expected_city = "Екатеринбург" if sid == "service-ekb" else NAMES[sid]
        candidates = []
        for candidate in raw_candidates:
            details = candidate.get("address", {})
            city = details.get("city") or details.get("town") or details.get("village")
            if (
                details.get("house_number")
                and normalize(city) == normalize(expected_city)
                and (
                    details.get("state") == REGION
                    or details.get("ISO3166-2-lvl4") == "RU-SVE"
                )
            ):
                candidates.append(candidate)
        if not candidates:
            raise ValueError("No building evidence: " + qs[variant])
        c = candidates[0]
        a = c["address"]
        city = a.get("city") or a.get("town") or a.get("village")
        row = {
            "site_id": sid,
            "name": NAMES[sid],
            "latitude": float(c["lat"]),
            "longitude": float(c["lon"]),
            "region": REGION,
            "city": city,
            "street": a.get("road") or a.get("pedestrian"),
            "house": a["house_number"],
            "postal_code": a.get("postcode", "")
            if not (sid == "serov" and variant == 1)
            else "",
            "country": "Россия",
            "is_demo": True,
            "service_type": "approval_required" if sid in approval else "standard",
            "approval_note": "Требуется подтверждение ответственного за допуск на площадку"
            if sid in approval
            else "",
        }
        row["address"] = full_address(row)
        record = evaluate(row, candidates, "Nominatim discovery", checked)
        if record["status"] != "verified":
            raise ValueError(record)
        responses[query_key(row)] = {
            "checked_at": checked,
            "query": qs[variant],
            "candidates": candidates,
        }
        (services if sid == "service-ekb" else sites).append(row)
    sites = pd.DataFrame(sites)
    services = pd.DataFrame(services)
    eq = original.equipment[original.equipment["Site Name"].isin(NAMES.values())].copy()
    for sid in QUERIES:
        if sid == "service-ekb" or eq["Site Name"].eq(NAMES[sid]).any():
            continue
        for i, kind in enumerate(
            ["Router", "Switch", "Server", "Firewall", "UPS", "Storage"], 1
        ):
            eq = pd.concat(
                [
                    eq,
                    pd.DataFrame(
                        [
                            {
                                "Site Name": NAMES[sid],
                                "Device Name": f"DEMO-{sid}-{i}",
                                "Device Type": kind,
                                "Serial Number": f"DEMO-ADDRESS-{sid}-{i}",
                                "Status": "Active",
                                "Vendor": "Demo",
                                "Data Source": "Демо",
                            }
                        ]
                    ),
                ],
                ignore_index=True,
            )
    if variant:
        for i in range(variant):
            eq = pd.concat(
                [
                    eq,
                    pd.DataFrame(
                        [
                            {
                                "Site Name": "Серов",
                                "Device Name": f"DEMO-IMPORT-{i}",
                                "Device Type": "Switch",
                                "Serial Number": f"DEMO-IMPORT-{i}",
                                "Status": "Active",
                                "Vendor": "Demo",
                                "Data Source": "Демо",
                            }
                        ]
                    ),
                ],
                ignore_index=True,
            )
    eq["Site ID"] = eq["Site Name"].map({v: k for k, v in NAMES.items()})
    validate(eq, sites, services)
    notes = [
        f"Региональная книга {variant + 1}: {len(sites)} площадок, {len(eq)} устройств. Все площадки — в Свердловской области.",
        "Оборудование условное; публичные адреса выбраны для испытаний. Наличие реального дома не означает размещения там оборудования.",
        "service_type: standard — общий SLA; approval_required — выезд по согласованию, исключается из числителя, знаменателя и максимума общего SLA.",
        "Отдельное согласование не моделирует срок получения допуска; время дороги всё равно рассчитывается.",
        "Адрес инженеров уточнён по OSM: переулок Буторина, а не улица Буторина.",
        "Адрес и его city/street/house должны совпадать. postal_code необязателен; формат индекса проверяется, официальная принадлежность по ГАР не проверяется.",
        f"Стабильные site_id позволяют сравнить импорт. В книгах 2/3 изменены все {len(sites) + len(services)} адресов, координаты, типы отдельных площадок и количество устройств.",
        "Проверка адресов доступна по сохранённым ответам OSM без сети; для новых адресов включите онлайн-проверку.",
        "Статус проверки из Excel не считается доказательством: приложение вычисляет его заново по ответу геокодера.",
    ]
    raw = write_workbook(eq, sites, services, notes)
    Path(output).write_bytes(raw)
    return responses


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--discovery", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--root", default=".")
    a = p.parse_args()
    root = Path(a.root)
    (root / "examples").mkdir(exist_ok=True, parents=True)
    merged = {}
    paths = [
        root / "data/ural_inventory.xlsx",
        root / "examples/02_ural_changed_addresses.xlsx",
        root / "examples/03_ural_changed_addresses.xlsx",
    ]
    for variant, path in enumerate(paths):
        merged.update(build(a.discovery, a.source, path, variant))
        print(path)
    (root / "data/address_reference.json").write_text(
        json.dumps(
            {
                "provider": "OSM / Nominatim",
                "license": "ODbL / © OpenStreetMap contributors",
                "responses": merged,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
