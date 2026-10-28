"""Address evidence is computed, never trusted from workbook status columns.

OSM verification checks consistency, not official GAR/FIAS validity or ownership.
Bundled reference responses permit repeatable offline validation of examples.
"""

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

ADDRESS_LABELS = {
    "verified": "Адрес и координаты согласованы с OSM",
    "coordinate_mismatch": "Координаты не соответствуют адресу",
    "not_found": "Адрес не найден",
    "ambiguous": "Несколько разных объектов по адресу",
    "details_mismatch": "Не совпали город, улица или дом",
    "invalid_format": "Неполный или противоречивый адрес",
    "not_checked": "Требуется проверка геокодером",
    "service_error": "Геокодер недоступен",
}


def normalize(value):
    text = (
        unicodedata.normalize("NFKC", str(value if value is not None else ""))
        .casefold()
        .replace("ё", "е")
        .replace("пр-т", "проспект")
    )
    return " ".join(re.findall(r"[а-яa-z0-9/]+", text))


def street_key(value):
    words = normalize(value).split()
    kinds = {
        "улица": "ул",
        "ул": "ул",
        "проспект": "пр",
        "пр": "пр",
        "просп": "пр",
        "переулок": "пер",
        "пер": "пер",
        "площадь": "пл",
        "пл": "пл",
        "бульвар": "бул",
        "бул": "бул",
        "шоссе": "ш",
        "ш": "ш",
    }
    kind = next((kinds[w] for w in words if w in kinds), "?")
    return kind + ":" + " ".join(w for w in words if w not in kinds)


def house_key(value):
    return (
        normalize(value)
        .replace(" ", "")
        .translate(str.maketrans("abcehkmoptxy", "авсенкмортху"))
    )


def full_address(row):
    postal = str(row.get("postal_code", "") or "").strip()
    parts = [
        "Россия",
        postal,
        str(row.get("region", "")).strip(),
        "г. " + str(row.get("city", "")).strip(),
        str(row.get("street", "")).strip(),
        "д. " + str(row.get("house", "")).strip(),
    ]
    return ", ".join(part for part in parts if part)


def query_key(row):
    fields = [
        normalize(row.get("country", "Россия")),
        normalize(row.get("region", "")),
        normalize(row.get("city", "")),
        street_key(row.get("street", "")),
        house_key(row.get("house", "")),
        str(row.get("postal_code", "") or ""),
    ]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


def local_issues(row):
    issues = []
    if normalize(row.get("country", "Россия")) not in ("россия", "рф", "ru"):
        issues.append("Пилот поддерживает только адреса России")
    for field in ["address", "region", "city", "street", "house"]:
        if not str(row.get(field, "") or "").strip():
            issues.append("Не заполнено поле " + field)
    postal = str(row.get("postal_code", "") or "").strip()
    if postal and not re.fullmatch(r"\d{6}", postal):
        issues.append("Индекс должен содержать 6 цифр")
    if issues:
        return issues
    address = normalize(row["address"])
    # Check that the human-readable address and structured components agree.
    for field in ["region", "city"]:
        if normalize(row[field]) not in address:
            issues.append("Полный адрес не содержит " + field)
    if not set(street_key(row["street"]).split(":", 1)[1].split()).issubset(
        address.split()
    ):
        issues.append("Улица в полном адресе отличается от street")
    if (
        street_key(row["street"]).split(":", 1)[0]
        != street_key(row["address"]).split(":", 1)[0]
    ):
        issues.append("Различается тип улицы в полном адресе и street")
    normalized_house = house_key(row["house"])
    if not any(house_key(token) == normalized_house for token in address.split()):
        issues.append("Дом в полном адресе отличается от house")
    return issues


def distance_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(a)))


def candidate_matches(row, candidate):
    address = candidate.get("address", {})
    if not isinstance(address, dict):
        return False
    city = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality", "")
    )
    region = address.get("state", "")
    return (
        address.get("country_code") == "ru"
        and (
            not str(row.get("postal_code", "") or "")
            or address.get("postcode") == str(row["postal_code"])
        )
        and (
            normalize(region) == normalize(row["region"])
            or (
                row["region"] == "Свердловская область"
                and address.get("ISO3166-2-lvl4") == "RU-SVE"
            )
        )
        and normalize(city) == normalize(row["city"])
        and street_key(address.get("road") or address.get("pedestrian", ""))
        == street_key(row["street"])
        and house_key(address.get("house_number", "")) == house_key(row["house"])
    )


def evaluate(row, candidates, evidence_source, checked_at):
    result = {
        "site_id": row["site_id"],
        "name": row["name"],
        "address": row["address"],
        "source": evidence_source,
        "checked_at": checked_at,
        "distance_m": None,
        "suggested_latitude": None,
        "suggested_longitude": None,
        "matched_address": None,
    }
    matching = [c for c in candidates if candidate_matches(row, c)]
    if not candidates:
        status = "not_found"
        reason = "Геокодер не вернул объектов по запросу"
    elif not matching:
        status = "details_mismatch"
        reason = "Нельзя подтвердить именно указанный город, улицу и дом"
    else:
        first = matching[0]
        if any(
            distance_m(
                float(first["lat"]),
                float(first["lon"]),
                float(c["lat"]),
                float(c["lon"]),
            )
            > 150
            for c in matching[1:]
        ):
            status = "ambiguous"
            reason = "Нужен ручной выбор объекта: найдено несколько разнесённых точек"
        else:
            c = first
            lat, lon = float(c["lat"]), float(c["lon"])
            gap = distance_m(float(row["latitude"]), float(row["longitude"]), lat, lon)
            result.update(
                distance_m=round(gap, 1),
                suggested_latitude=lat,
                suggested_longitude=lon,
                matched_address=c.get("display_name", ""),
            )
            status = "verified" if gap <= 250 else "coordinate_mismatch"
            reason = (
                "Город, улица и дом совпали; допуск координат 250 м"
                if status == "verified"
                else "Нужно проверить и подтвердить предложенные координаты"
            )
    return {
        **result,
        "status": status,
        "label": ADDRESS_LABELS[status],
        "reason": reason,
    }


class AddressChecker:
    _lock = threading.Lock()
    _last_request = 0.0

    def __init__(
        self,
        cache_path,
        reference_path=None,
        url="https://nominatim.openstreetmap.org",
        session=None,
    ):
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
        ):
            raise ValueError("Удалённый геокодер должен использовать HTTPS")
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Некорректный GEOCODER_URL")
        self.url = url.rstrip("/")
        self.public = parsed.hostname == "nominatim.openstreetmap.org"
        self.session = session or requests.Session()
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.cache_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS address_responses (key TEXT PRIMARY KEY, payload TEXT NOT NULL, checked_at TEXT NOT NULL)"
            )
        self.reference = (
            json.loads(Path(reference_path).read_text())
            if reference_path and Path(reference_path).exists()
            else {"responses": {}}
        )

    def _cached(self, key):
        with sqlite3.connect(self.cache_path) as db:
            row = db.execute(
                "SELECT payload,checked_at FROM address_responses WHERE key=?",
                ("structured-v2|" + self.url + "|" + key,),
            ).fetchone()
        if row and time.time() - datetime.fromisoformat(row[1]).timestamp() < 7 * 86400:
            return json.loads(row[0]), row[1]
        return None

    def check(self, row, allow_network=False, force_refresh=False):
        row = dict(row)
        basic = {
            "site_id": row["site_id"],
            "name": row["name"],
            "address": row.get("address", ""),
            "distance_m": None,
            "suggested_latitude": None,
            "suggested_longitude": None,
            "matched_address": None,
            "source": "local",
            "checked_at": datetime.now(UTC).isoformat(),
        }
        issues = local_issues(row)
        if issues:
            return {
                **basic,
                "status": "invalid_format",
                "label": ADDRESS_LABELS["invalid_format"],
                "reason": "; ".join(issues),
            }
        key = query_key(row)
        cached = None if force_refresh else self._cached(key)
        reference = (
            None
            if force_refresh or self.url != "https://nominatim.openstreetmap.org"
            else self.reference.get("responses", {}).get(key)
        )
        if cached:
            candidates, checked_at = cached
            source = "Кэш геокодера"
        elif reference:
            candidates = reference["candidates"]
            checked_at = reference["checked_at"]
            source = "Сохранённый ответ OSM из примера"
        elif not allow_network:
            return {
                **basic,
                "status": "not_checked",
                "label": ADDRESS_LABELS["not_checked"],
                "reason": "Адреса нет в сохранённых ответах; включите онлайн-проверку",
            }
        else:
            if self.public:
                with AddressChecker._lock:
                    pause = 1.1 - (time.monotonic() - AddressChecker._last_request)
                    if pause > 0:
                        time.sleep(pause)
                    AddressChecker._last_request = time.monotonic()
            try:
                response = self.session.get(
                    self.url + "/search",
                    params={
                        "street": str(row["house"]) + " " + str(row["street"]),
                        "city": row["city"],
                        "state": row["region"],
                        "format": "jsonv2",
                        "addressdetails": 1,
                        "limit": 5,
                        "countrycodes": "ru",
                    },
                    headers={
                        "User-Agent": "Geofind-MVP/0.2 (https://github.com/illuzory/Geofind)"
                    },
                    timeout=(5, 20),
                )
                response.raise_for_status()
                candidates = response.json()
                if not isinstance(candidates, list) or any(
                    not isinstance(c, dict)
                    or "lat" not in c
                    or "lon" not in c
                    or not isinstance(c.get("address"), dict)
                    for c in candidates
                ):
                    raise ValueError("Некорректный ответ геокодера")
                for c in candidates:
                    lat, lon = float(c["lat"]), float(c["lon"])
                    if not (
                        math.isfinite(lat)
                        and math.isfinite(lon)
                        and -90 <= lat <= 90
                        and -180 <= lon <= 180
                    ):
                        raise ValueError("Некорректные координаты ответа")
                checked_at = datetime.now(UTC).isoformat()
                source = "Онлайн OSM / Nominatim"
                with sqlite3.connect(self.cache_path) as db:
                    db.execute(
                        "INSERT OR REPLACE INTO address_responses VALUES (?,?,?)",
                        (
                            "structured-v2|" + self.url + "|" + key,
                            json.dumps(candidates, ensure_ascii=False),
                            checked_at,
                        ),
                    )
            except (requests.RequestException, ValueError, TypeError):
                return {
                    **basic,
                    "status": "service_error",
                    "label": ADDRESS_LABELS["service_error"],
                    "reason": "Геокодер недоступен или вернул некорректный ответ; адрес не признан неверным",
                }
        return evaluate(row, candidates, source, checked_at)
