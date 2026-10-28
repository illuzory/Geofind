"""End-to-end hot imports of three books, approval KPIs and address correction.

Requires the isolated release server started with its own GEOFIND_STATE_DIR.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect, sync_playwright

from geofind.data import read_workbook, write_workbook

ROOT = Path(__file__).resolve().parents[1]


def run(port, backend):
    paths = [
        ROOT / "data/ural_inventory.xlsx",
        ROOT / "examples/02_ural_changed_addresses.xlsx",
        ROOT / "examples/03_ural_changed_addresses.xlsx",
    ]
    output = ROOT / "artifacts/15_sites_validation"
    output.mkdir(exist_ok=True, parents=True)
    base = f"http://127.0.0.1:{port}"
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
        )
        context = browser.new_context(viewport={"width": 1600, "height": 1500})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base, wait_until="networkidle")
        page.get_by_role("combobox", name="Расчёт времени").click()
        label = (
            "OSRM · автомобильный маршрут"
            if backend == "osrm"
            else "Демо · дорожная модель"
        )
        page.get_by_role("option", name=label, exact=True).click()
        if backend == "osrm":
            page.get_by_role(
                "button", name="Рассчитать дорожный сценарий", exact=True
            ).click()

        def metric(name):
            return (
                page.get_by_test_id("stMetric")
                .filter(has_text=name)
                .get_by_test_id("stMetricValue")
            )

        def verify(expected_count, eligible, approval):
            expect(metric("Оборудование в регионе")).to_have_text(
                str(expected_count), timeout=45000
            )
            expect(metric("В SLA")).to_have_text(f"10 из {eligible}", timeout=45000)
            expect(metric("По согласованию")).to_have_text(str(approval))
            frame = page.frame_locator('iframe[title="Карта площадок и зон доезда"]')
            expect(frame.locator(".city-label")).to_have_count(15)
            if not page.get_by_role(
                "button", name="Проверить адреса", exact=True
            ).is_visible():
                page.get_by_text("Проверка адресов и координат", exact=True).click()
            page.get_by_role("button", name="Проверить адреса", exact=True).click()
            expect(
                page.get_by_text("Согласованы адрес и координаты: 16 из 16", exact=True)
            ).to_be_visible(timeout=15000)

        verify(120, 11, 4)
        page.screenshot(path=str(output / "dashboard_base.png"), full_page=True)
        for path, count, eligible, approval in [
            (paths[1], 121, 10, 5),
            (paths[2], 122, 11, 4),
        ]:
            page.locator("input[type=file]").set_input_files(str(path))
            apply = page.get_by_role("button", name="Применить таблицу", exact=True)
            expect(apply).to_be_enabled()
            apply.click()
            # No manual recalculate, reload, service restart or re-login.
            verify(count, eligible, approval)
            page.screenshot(path=str(output / f"dashboard_{count}.png"), full_page=True)
        with page.expect_download() as downloaded:
            page.get_by_role("button", name="Скачать текущую книгу", exact=True).click()
        loaded = read_workbook(Path(downloaded.value.path()))
        expected = read_workbook(paths[2])
        assert loaded.sites.address.tolist() == expected.sites.address.tolist()
        assert loaded.services.address.tolist() == expected.services.address.tolist()
        # A plausible regional coordinate error is detected, then corrected explicitly.
        bad_sites = expected.sites.copy()
        bad_sites.loc[bad_sites.site_id == "ekb", "latitude"] += 0.02
        raw = write_workbook(expected.equipment, bad_sites, expected.services)
        page.locator("input[type=file]").set_input_files(
            {
                "name": "coordinate-error.xlsx",
                "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "buffer": raw,
            }
        )
        page.get_by_role("button", name="Применить таблицу", exact=True).click()
        bad_fingerprint = read_workbook(raw).fingerprint[:8]
        expect(page.get_by_text(re.compile("Версия " + bad_fingerprint))).to_be_visible(
            timeout=45000
        )
        expect(metric("Оборудование в регионе")).to_have_text("122", timeout=45000)
        if not page.get_by_role(
            "button", name="Проверить адреса", exact=True
        ).is_visible():
            page.get_by_text("Проверка адресов и координат", exact=True).click()
        page.get_by_role("button", name="Проверить адреса", exact=True).click()
        expect(
            page.get_by_text("Согласованы адрес и координаты: 15 из 16", exact=True)
        ).to_be_visible()
        page.get_by_role(
            "button", name="Применить предложенные координаты", exact=True
        ).click()
        expect(
            page.get_by_role(
                "button", name="Применить предложенные координаты", exact=True
            )
        ).not_to_be_visible()
        verify(122, 11, 4)
        # Persistence is checked in a fresh browser session.
        other = browser.new_context(viewport={"width": 1600, "height": 1200})
        fresh = other.new_page()
        fresh.goto(base, wait_until="networkidle")
        fresh.get_by_role("combobox", name="Расчёт времени").click()
        fresh.get_by_role("option", name=label, exact=True).click()
        if backend == "osrm":
            fresh.get_by_role(
                "button", name="Рассчитать дорожный сценарий", exact=True
            ).click()
        expect(
            fresh.get_by_test_id("stMetric")
            .filter(has_text="Оборудование в регионе")
            .get_by_test_id("stMetricValue")
        ).to_have_text("122", timeout=45000)
        other.close()
        # Invalid import must not replace the valid current dataset.
        page.locator("input[type=file]").set_input_files(
            {
                "name": "broken.xlsx",
                "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "buffer": b"not an xlsx",
            }
        )
        expect(
            page.get_by_role("button", name="Применить таблицу", exact=True)
        ).to_be_disabled()
        expect(metric("Оборудование в регионе")).to_have_text("122")
        # Restore the base by using the normal version picker.
        page.get_by_text("Версии и восстановление", exact=True).click()
        version_box = page.get_by_role("combobox", name="Сохранённая версия")
        version_box.fill("ural_inventory")
        page.get_by_role("option").filter(has_text="ural_inventory.xlsx").first.click()
        page.get_by_role(
            "button", name="Восстановить выбранную версию", exact=True
        ).click()
        expect(metric("Оборудование в регионе")).to_have_text("120", timeout=45000)
        expect(metric("В SLA")).to_have_text("10 из 11")
        assert not errors, errors
        report = {
            "backend": backend,
            "hot_import_counts": [120, 121, 122],
            "sla_denominators": [11, 10, 11],
            "approval_counts": [4, 5, 4],
            "address_checks": [16, 16, 16],
            "coordinate_mismatch_detected_and_explicitly_corrected": True,
            "persisted_in_new_browser": True,
            "invalid_import_keeps_previous": True,
            "rollback_to_base": True,
            "javascript_errors": errors,
        }
        (output / "browser_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        print("PASS", json.dumps(report, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8502)
    parser.add_argument("--backend", choices=["demo", "osrm"], default="osrm")
    args = parser.parse_args()
    run(args.port, args.backend)
