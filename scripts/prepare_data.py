"""Create the adapted private workbook or an entirely synthetic public example."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd

from geofind.data import (
    EQUIPMENT_COLUMNS,
    REGION,
    SITE_COLUMNS,
    validate,
    write_workbook,
)

# Deliberately approximate points, not geocoded equipment addresses.
CITIES = [
    ("moscow", "Москва", 55.7512, 37.6184, "Москва", 61),
    ("spb", "Санкт-Петербург", 59.9386, 30.3141, "Санкт-Петербург", 43),
    ("ekb", "Екатеринбург", 56.8389, 60.6057, REGION, 36),
    ("nsk", "Новосибирск", 55.0302, 82.9204, "Новосибирская область", 29),
    ("kras", "Красноярск", 56.0106, 92.8525, "Красноярский край", 23),
    ("rostov", "Ростов-на-Дону", 47.2225, 39.7187, "Ростовская область", 26),
    ("kazan", "Казань", 55.7961, 49.1064, "Республика Татарстан", 8),
    ("tula", "Тула", 54.1931, 37.6173, "Тульская область", 3),
    ("voronezh", "Воронеж", 51.6608, 39.2003, "Воронежская область", 5),
    ("samara", "Самара", 53.1959, 50.1002, "Самарская область", 5),
    ("nn", "Нижний Новгород", 56.3269, 44.0059, "Нижегородская область", 3),
    ("chelyabinsk", "Челябинск", 55.1644, 61.4368, "Челябинская область", 8),
    ("volgograd", "Волгоград", 48.7080, 44.5133, "Волгоградская область", 4),
    ("tagil", "Нижний Тагил", 57.9076, 59.9650, REGION, 6),
    ("kamensk", "Каменск-Уральский", 56.4149, 61.9189, REGION, 6),
    ("serov", "Серов", 59.6049, 60.5756, REGION, 6),
]


def build(source=None):
    sites = pd.DataFrame(
        [
            {
                "site_id": key,
                "name": city,
                "latitude": lat,
                "longitude": lon,
                "region": region,
                "address": "Условная площадка в городе",
                "is_demo": True,
            }
            for key, city, lat, lon, region, _ in CITIES
        ],
        columns=SITE_COLUMNS,
    )
    services = pd.DataFrame(
        [
            {
                "site_id": "service-ekb",
                "name": "Инженеры · Екатеринбург",
                "latitude": 56.8176,
                "longitude": 60.6395,
                "region": REGION,
                "address": "ул. Буторина, 6 — условная привязка, не результат геокодирования",
                "is_demo": True,
            }
        ],
        columns=SITE_COLUMNS,
    )
    if source:
        base = pd.read_excel(source)
        if not set(EQUIPMENT_COLUMNS).issubset(base.columns):
            raise ValueError("Исходная таблица имеет другую схему")
        base = base.copy()
        base["Data Source"] = "Исходная таблица"
        selected = CITIES[-3:]
    else:
        base = pd.DataFrame(columns=EQUIPMENT_COLUMNS + ["Data Source"])
        selected = CITIES
    additions = []
    types = ["Router", "Switch", "Switch", "Server", "Firewall", "UPS"]
    for key, city, lat, lon, region, count in selected:
        for i in range(count):
            additions.append(
                {
                    "Site Name": city,
                    "Device Name": f"DEMO-{key.upper()}-{i + 1:03}",
                    "Device Type": types[i % len(types)],
                    "Serial Number": f"DEMO-{key.upper()}-{i + 1:04}",
                    "Status": "Standby" if i % 5 == 4 else "Active",
                    "Vendor": "Demo",
                    "Data Source": "Демо",
                }
            )
    equipment = pd.concat([base, pd.DataFrame(additions)], ignore_index=True)
    validate(equipment, sites, services)
    notes = [
        "MVP: Свердловская область. Другие города сохранены, но не участвуют в региональном расчёте.",
        "Equipment: исходные строки сохранены; 18 строк трёх новых городов помечены Демо."
        if source
        else "Все устройства в этом публичном шаблоне синтетические.",
        "Новые города: Нижний Тагил, Каменск-Уральский, Серов — по 6 демонстрационных устройств.",
        "Все координаты условные, включая точку инженеров у ул. Буторина, 6; не подтверждают адрес оборудования.",
        "Для обновления редактируйте Equipment, Sites и Services и загрузите книгу в приложение.",
        "Связь оборудования с площадкой: Equipment.Site Name = Sites.name; имена площадок должны быть уникальны.",
        "Сервисные точки могут находиться вне региона, но должны попадать в покрытие выбранного маршрутизатора.",
        "SLA по умолчанию: <=4ч норма; 4–6ч риск; 6–8ч критично; >8ч вне SLA.",
    ]
    return write_workbook(equipment, sites, services, notes)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(build(args.source))
    print(args.output)
