import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pandas as pd

REGION = "Свердловская область"
EQUIPMENT_COLUMNS = [
    "Site Name",
    "Device Name",
    "Device Type",
    "Serial Number",
    "Status",
    "Vendor",
]
SITE_COLUMNS = [
    "site_id",
    "name",
    "latitude",
    "longitude",
    "region",
    "address",
    "is_demo",
]
MAX_ROWS = 50000


class DataError(ValueError):
    pass


@dataclass
class Dataset:
    equipment: pd.DataFrame
    sites: pd.DataFrame
    services: pd.DataFrame
    fingerprint: str

    @property
    def regional_sites(self):
        return self.sites[self.sites.region == REGION].copy()


def _locations(df, label):
    missing = set(SITE_COLUMNS) - set(df.columns)
    if missing:
        raise DataError(f"{label}: отсутствуют столбцы {', '.join(sorted(missing))}")
    df = df.copy()
    for col in ["site_id", "name", "region"]:
        df[col] = df[col].fillna("").astype(str).str.strip()
        if df[col].eq("").any():
            raise DataError(f"{label}: пустое поле {col}")
    if df.site_id.duplicated().any() or df.name.duplicated().any():
        raise DataError(f"{label}: повторяющийся site_id или name")
    for col, low, high in [("latitude", -90, 90), ("longitude", -180, 180)]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
        if not df[col].between(low, high).all():
            raise DataError(f"{label}: некорректные координаты {col}")
    df["address"] = df.address.fillna("").astype(str)
    bools = {
        "true": True,
        "false": False,
        "1": True,
        "0": False,
        "да": True,
        "нет": False,
    }
    df["is_demo"] = df.is_demo.astype(str).str.strip().str.lower().map(bools)
    if df.is_demo.isna().any():
        raise DataError(f"{label}: is_demo должен быть true/false")
    return df


def validate(equipment, sites, services, fingerprint="manual"):
    if max(len(equipment), len(sites), len(services)) > MAX_ROWS:
        raise DataError(f"Не более {MAX_ROWS} строк в листе")
    if len(sites) > 2000 or len(services) > 10:
        raise DataError("MVP: максимум 2000 площадок и 10 сервисных точек")
    missing = set(EQUIPMENT_COLUMNS) - set(equipment.columns)
    if missing:
        raise DataError(
            f"Оборудование: отсутствуют столбцы {', '.join(sorted(missing))}"
        )
    eq = equipment.copy()
    for col in EQUIPMENT_COLUMNS:
        eq[col] = eq[col].fillna("").astype(str).str.strip()
        if eq[col].eq("").any():
            raise DataError(f"Оборудование: пустое поле {col}")
    if eq["Serial Number"].duplicated().any():
        raise DataError("Повторяющийся серийный номер")
    if eq.duplicated(["Site Name", "Device Name"]).any():
        raise DataError("Повтор устройства внутри площадки")
    sites = _locations(sites, "Sites")
    services = _locations(services, "Services")
    if not len(services):
        raise DataError("Нужна хотя бы одна сервисная точка")
    if set(sites.site_id) & set(services.site_id):
        raise DataError("Идентификаторы площадок и сервисных точек должны различаться")
    unmatched = set(eq["Site Name"]) - set(sites.name)
    if unmatched:
        raise DataError(f"Нет координат для площадок: {', '.join(sorted(unmatched))}")
    if not (sites.region == REGION).any():
        raise DataError(f"Нет площадок в регионе {REGION}")
    from shapely.geometry import Point

    from .coverage import load_boundary

    boundary = load_boundary(
        Path(__file__).resolve().parents[1] / "data/sverdlovsk.geojson"
    )
    outside = [
        r["name"]
        for r in sites[sites.region == REGION].to_dict("records")
        if not boundary.covers(Point(r["longitude"], r["latitude"]))
    ]
    if outside:
        raise DataError(
            "Площадки вне границы Свердловской области: " + ", ".join(outside)
        )
    return Dataset(eq, sites, services, fingerprint)


def read_workbook(content):
    raw = Path(content).read_bytes() if isinstance(content, (str, Path)) else content
    if len(raw) > 10 * 1024 * 1024:
        raise DataError("Файл больше 10 МБ")
    # Bound uncompressed OOXML size before pandas/openpyxl parses it.
    from zipfile import BadZipFile, ZipFile

    try:
        with ZipFile(BytesIO(raw)) as z:
            if sum(x.file_size for x in z.infolist()) > 50 * 1024 * 1024:
                raise DataError("Распакованный XLSX больше 50 МБ")
        with pd.ExcelFile(BytesIO(raw)) as book:
            required = {"Equipment", "Sites", "Services"}
            if not required.issubset(book.sheet_names):
                raise DataError(
                    "Нужны листы Equipment, Sites, Services. Скачайте шаблон в приложении."
                )
            sheets = {
                name: pd.read_excel(book, sheet_name=name, nrows=MAX_ROWS + 1)
                for name in required
            }
    except DataError:
        raise
    except (BadZipFile, ValueError, OSError, KeyError, EOFError) as exc:
        raise DataError("Не удалось прочитать XLSX: проверьте формат файла") from exc
    return validate(
        sheets["Equipment"],
        sheets["Sites"],
        sheets["Services"],
        hashlib.sha256(raw).hexdigest(),
    )


def write_workbook(equipment, sites, services, notes=None):
    stream = BytesIO()
    with pd.ExcelWriter(stream, engine="openpyxl") as writer:
        for name, df in [
            ("Equipment", equipment),
            ("Sites", sites),
            ("Services", services),
        ]:
            df.to_excel(writer, sheet_name=name, index=False)
        if notes is not None:
            pd.DataFrame({"Описание": notes}).to_excel(
                writer, sheet_name="Readme", index=False
            )
        for sheet in writer.book:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for column in sheet.columns:
                sheet.column_dimensions[column[0].column_letter].width = min(
                    64, max(16, max(len(str(c.value or "")) for c in column) + 2)
                )
                for cell in column:
                    if isinstance(cell.value, str) and cell.value.startswith(
                        ("=", "+", "-", "@")
                    ):
                        cell.data_type = (
                            "s"  # user strings must not become Excel formulas
                        )
            for cell in sheet[1]:
                from openpyxl.styles import Font, PatternFill

                cell.font = Font(color="FFFFFF", bold=True)
                cell.fill = PatternFill("solid", fgColor="197C70")
    return stream.getvalue()


def safe_csv(df):
    clean = df.copy()
    for col in clean:
        clean[col] = clean[col].map(
            lambda v: (
                "'" + v
                if isinstance(v, str) and v.startswith(("=", "+", "-", "@"))
                else v
            )
        )
    return clean.to_csv(index=False).encode("utf-8-sig")
