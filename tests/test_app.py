from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


def find(elements, label):
    return next(x for x in elements if x.label == label)


def test_scenario_ui_changes_coverage_and_zero_is_visible():
    app = AppTest.from_file(ROOT / "app.py", default_timeout=30).run()
    assert not app.exception
    assert find(app.metric, "В SLA").value == "3 из 4"
    find(app.multiselect, "Дополнительные инженеры").select("Серов").run()
    assert not app.exception
    assert find(app.metric, "В SLA").value == "4 из 4"
    assert find(app.metric, "Дорога").value == "0 ч 00 мин"
    find(app.slider, "Подготовка к выезду, мин").set_value(60).run()
    assert not app.exception
    assert find(app.metric, "С подготовкой").value == "1 ч 00 мин"


def test_no_service_and_invalid_thresholds_are_handled():
    app = AppTest.from_file(ROOT / "app.py", default_timeout=30).run()
    find(app.multiselect, "Основные сервисные точки").set_value([]).run()
    assert not app.exception
    assert any("хотя бы одну" in x.value for x in app.info)
    find(app.multiselect, "Основные сервисные точки").set_value(["service-ekb"]).run()
    find(app.number_input, "В SLA, часов").set_value(10).run()
    assert not app.exception
    assert any("Пороги" in x.value for x in app.error)


def test_configured_osrm_waits_for_click_and_limits_public_grid(monkeypatch):
    import requests

    monkeypatch.setenv("OSRM_URL", "https://router.project-osrm.org")
    monkeypatch.setenv("OSRM_CACHE_NAMESPACE", "unit-test-observation")

    def network_must_not_run(*args, **kwargs):
        raise AssertionError("Unexpected live request in a unit test")

    monkeypatch.setattr(requests.Session, "get", network_must_not_run)
    app = AppTest.from_file(ROOT / "app.py", default_timeout=30).run()
    assert not app.exception
    assert find(app.selectbox, "Расчёт времени").value == "OSRM · автомобильный маршрут"
    assert not app.metric
    find(app.select_slider, "Шаг покрытия, км").set_value(10).run()
    find(app.button, "Рассчитать дорожный сценарий").click().run()
    assert not app.exception
    assert any("Публичный OSRM" in x.value for x in app.error)
