"""Real-browser smoke: offline map, scenario switching, XLSX update, download.
Run against the server you started: python scripts/browser_smoke.py --port 8501.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from playwright.sync_api import expect, sync_playwright

from geofind.data import read_workbook, write_workbook

ROOT = Path(__file__).resolve().parents[1]


def run(port, executable, mode="demo"):
    base = f"http://127.0.0.1:{port}"
    output = ROOT / "artifacts"
    output.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=executable, headless=True, args=["--no-sandbox"]
        )
        page = browser.new_page(viewport={"width": 1512, "height": 1320})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        blocked = []

        def route_request(request):
            if request.request.url.startswith(
                (base, base.replace("http", "ws"), "data:", "about:")
            ):
                request.continue_()
            else:
                blocked.append(request.request.url)
                request.abort()

        page.route("**/*", route_request)
        page.goto(base, wait_until="networkidle", timeout=60000)
        page.get_by_role("combobox", name="Расчёт времени").click()
        mode_label = (
            "Демо · дорожная модель"
            if mode == "demo"
            else "OSRM · автомобильный маршрут"
        )
        page.get_by_role("option", name=mode_label, exact=True).click()
        if mode == "osrm":
            page.get_by_role(
                "button", name="Рассчитать дорожный сценарий", exact=True
            ).click()
        expect(page.get_by_text("3 из 4", exact=True)).to_be_visible(timeout=30000)
        frame = page.frame_locator('iframe[title="Карта площадок и зон доезда"]')
        expect(frame.locator(".leaflet-container")).to_be_visible(timeout=10000)
        expect(frame.locator("canvas")).to_have_count(1)
        expect(frame.locator(".service-dot")).to_have_count(1)
        frame.locator(".service-dot").click()
        expect(frame.locator(".leaflet-popup")).to_be_visible()
        frame.locator(".leaflet-popup-close-button").click()
        page.screenshot(
            path=str(
                output / ("dashboard.png" if mode == "demo" else "dashboard-osrm.png")
            ),
            full_page=True,
        )
        page.get_by_role("combobox", name="Дополнительные инженеры").click()
        page.get_by_role(
            "option", name="Серов" if mode == "demo" else "Нижний Тагил", exact=True
        ).click()
        page.get_by_role("combobox", name="Дополнительные инженеры").press("Escape")
        if mode == "osrm":
            page.get_by_role(
                "button", name="Рассчитать дорожный сценарий", exact=True
            ).click()
        expect(page.get_by_text("4 из 4", exact=True)).to_be_visible(timeout=30000)
        expect(frame.locator(".service-dot")).to_have_count(2)
        if mode == "demo":
            expect(page.get_by_text("0 ч 00 мин", exact=True).first).to_be_visible()
        page.screenshot(
            path=str(
                output
                / (
                    "scenario-serov.png"
                    if mode == "demo"
                    else "scenario-tagil-osrm.png"
                )
            ),
            full_page=True,
        )
        # Work with the public template, preserving the expected 272/54 baseline.
        data = read_workbook(ROOT / "data/demo_inventory.xlsx")
        extra = data.equipment[data.equipment["Site Name"] == "Серов"].iloc[[0]].copy()
        extra["Device Name"] = "DEMO-UPLOAD"
        extra["Serial Number"] = "DEMO-UPLOAD-UNIQUE"
        updated = write_workbook(
            pd.concat([data.equipment, extra], ignore_index=True),
            data.sites,
            data.services,
        )
        page.locator("input[type=file]").set_input_files(
            {
                "name": "updated.xlsx",
                "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "buffer": updated,
            }
        )
        apply = page.get_by_role("button", name="Применить таблицу", exact=True)
        try:
            expect(apply).to_be_enabled(timeout=15000)
            apply.click()
        except Exception:
            page.screenshot(path=str(output / "upload-failure.png"), full_page=True)
            print("BLOCKED", blocked, "JS ERRORS", errors)
            print(page.locator("body").inner_text()[:2500])
            raise
        expect(page.get_by_text("Данные обновлены", exact=True)).to_be_visible(
            timeout=15000
        )
        if mode == "osrm":
            page.get_by_role(
                "button", name="Рассчитать дорожный сценарий", exact=True
            ).click()
        expect(
            page.get_by_test_id("stMetric")
            .filter(has_text="Оборудование в регионе")
            .get_by_test_id("stMetricValue")
        ).to_have_text("55")
        with page.expect_download() as downloaded:
            page.get_by_role(
                "button", name="Скачать результаты CSV", exact=True
            ).click()
        path = downloaded.value.path()
        csv = pd.read_csv(path)
        assert csv.equipment.sum() == 55
        assert set(csv.calculation_mode) == {mode}
        assert not errors, errors
        print(
            f"PASS {mode}: Leaflet canvas/popups without external browser requests, baseline 3/4 → scenario 4/4, XLSX update 54 → 55, CSV export; no JS errors."
        )
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--mode", choices=["demo", "osrm"], default="demo")
    args = parser.parse_args()
    run(args.port, args.chromium, args.mode)
