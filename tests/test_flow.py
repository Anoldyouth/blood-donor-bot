"""Сквозной тест эвристик бота на имитации сайта (без доступа в интернет)."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from donor_bot.config import Config, Site  # noqa: E402
from donor_bot.scheduler import launch_browser, new_context, next_window_start  # noqa: E402
from donor_bot.site import Result, SiteClient  # noqa: E402
from tests import fake_site  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("SKIP_BROWSER") == "1", reason="browser tests disabled")


def make_cfg(url: str, tmp_path: Path, **over) -> Config:
    base = dict(
        sites=[Site("fake", url)], account_path="/account/",
        login=fake_site.LOGIN, password=fake_site.PASSWORD,
        timezone="Europe/Moscow", time="12:00", start_before_sec=45, window_min=1, retry_interval_sec=1,
        donation_type_regex="тромбоцит", date_preference="earliest", preferred_times=[],
        dry_run=False, pause_days_after_success=14,
        headless=True, slow_mo_ms=0, timeout_ms=5000, channel=None, selectors={},
        state_file=tmp_path / "state.json", log_file=tmp_path / "bot.log",
        telegram_token="", telegram_chat_id="",
    )
    base.update(over)
    return Config(**base)


@pytest.fixture(scope="module")
def server():
    srv, url = fake_site.start_server()
    yield url
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture(autouse=True)
def reset(server):
    requests.get(server + "/admin/reset", timeout=5)


def run_attempt(cfg, browser, dump_dir=None):
    client = SiteClient(cfg, cfg.sites[0], new_context(browser), dump_dir=dump_dir)
    try:
        return client.attempt(), client
    finally:
        client.context.close()


def test_full_booking(server, browser, tmp_path):
    cfg = make_cfg(server, tmp_path, preferred_times=["09:00"])
    result, client = run_attempt(cfg, browser, dump_dir=tmp_path / "dump")
    assert result == Result.BOOKED, client.last_message
    assert fake_site.STATE.booking == {"type": "Тромбоциты", "date": "23.10.2026", "time": "09:00"}
    assert "успешно записаны" in client.last_message
    assert list((tmp_path / "dump").glob("*.png")), "режим дампов должен сохранять скриншоты"


def test_latest_date_and_earliest_time(server, browser, tmp_path):
    cfg = make_cfg(server, tmp_path, date_preference="latest")
    result, _ = run_attempt(cfg, browser)
    assert result == Result.BOOKED
    assert fake_site.STATE.booking["date"] == "24.10.2026"
    assert fake_site.STATE.booking["time"] == "09:00"


def test_already_booked(server, browser, tmp_path):
    fake_site.STATE.booking = {"type": "Тромбоциты", "date": "23.10.2026", "time": "09:00"}
    cfg = make_cfg(server, tmp_path)
    result, client = run_attempt(cfg, browser)
    assert result == Result.ALREADY_BOOKED
    assert "Ваша запись" in client.last_message
    assert fake_site.STATE.confirm_count == 0


def test_no_button_when_slots_closed(server, browser, tmp_path):
    requests.get(server + "/admin/open?v=0", timeout=5)
    cfg = make_cfg(server, tmp_path)
    result, _ = run_attempt(cfg, browser)
    assert result == Result.NO_BUTTON
    assert fake_site.STATE.booking is None


def test_dry_run_does_not_confirm(server, browser, tmp_path):
    cfg = make_cfg(server, tmp_path, dry_run=True)
    result, _ = run_attempt(cfg, browser)
    assert result == Result.DRY_RUN
    assert fake_site.STATE.booking is None
    assert fake_site.STATE.confirm_count == 0


def test_login_failed(server, browser, tmp_path):
    cfg = make_cfg(server, tmp_path, password="wrong")
    result, _ = run_attempt(cfg, browser)
    assert result == Result.LOGIN_FAILED


def test_custom_selectors(server, browser, tmp_path):
    cfg = make_cfg(server, tmp_path, selectors={
        "book_button": ["a.record"], "confirm": ["button[type=submit]"],
        "time": [".slots a.slot"],
    })
    result, _ = run_attempt(cfg, browser)
    assert result == Result.BOOKED


def test_next_window_start():
    cfg = make_cfg("http://x", Path("."), time="12:00", start_before_sec=45)
    tz = ZoneInfo("Europe/Moscow")
    now = datetime(2026, 10, 20, 9, 0, tzinfo=tz)
    assert next_window_start(cfg, now) == datetime(2026, 10, 20, 11, 59, 15, tzinfo=tz)
    now = datetime(2026, 10, 20, 12, 30, tzinfo=tz)
    assert next_window_start(cfg, now) == datetime(2026, 10, 21, 11, 59, 15, tzinfo=tz)
