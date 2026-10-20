"""Сквозной тест эвристик бота на имитации сайта (без доступа в интернет)."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from donor_bot.archive import RunArchive  # noqa: E402
from donor_bot.config import Config, Site  # noqa: E402
from donor_bot.scheduler import BrowserSession, CONTEXT_ARGS, next_window_start  # noqa: E402
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
        headless=True, slow_mo_ms=0, timeout_ms=5000, channel=None, profile_dir=None,
        captcha_solver="rucaptcha", captcha_solver_url="", captcha_api_key="", captcha_solve_timeout_sec=30,
        selectors={},
        archive_dir=tmp_path / "runs", archive_keep_days=30, learned_dir=tmp_path / "learned", use_learned=True,
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
def pw():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(pw):
    b = pw.chromium.launch(headless=True)
    yield b
    b.close()


@pytest.fixture(autouse=True)
def reset(server):
    requests.get(server + "/admin/reset", timeout=5)


def run_attempt(cfg, browser, archive=None):
    client = SiteClient(cfg, cfg.sites[0], browser.new_context(**CONTEXT_ARGS), archive=archive)
    try:
        return client.attempt(), client
    finally:
        client.context.close()


def test_full_booking(server, browser, tmp_path):
    cfg = make_cfg(server, tmp_path, preferred_times=["09:00"])
    archive = RunArchive(tmp_path / "runs", "test")
    result, client = run_attempt(cfg, browser, archive=archive)
    assert result == Result.BOOKED, client.last_message
    assert fake_site.STATE.booking == {"type": "Тромбоциты", "date": "23.10.2026", "time": "09:00"}
    assert "успешно записаны" in client.last_message
    site_dir = archive.dir / "fake"
    assert list(site_dir.glob("*.png")) and list(site_dir.glob("*.html")) and list(site_dir.glob("*.json"))
    trace = json.loads(next(site_dir.glob("trace_*.json")).read_text(encoding="utf-8"))
    roles = [st["role"] for st in trace["steps"]]
    assert roles == ["book_button", "donation_type", "confirm", "date", "time", "consent", "confirm"], roles
    assert all(st["sel"] for st in trace["steps"]), "у каждого шага должен быть селектор"
    history = (tmp_path / "runs" / "history.jsonl").read_text(encoding="utf-8")
    assert '"result": "booked"' in history
    learned = json.loads((tmp_path / "learned" / "fake.json").read_text(encoding="utf-8"))
    assert set(learned["roles"]) == {"book_button", "donation_type", "confirm", "date", "time", "consent"}
    assert learned["roles"]["date"][0]["selector"] == "a.day", learned["roles"]["date"]
    assert learned["roles"]["time"][0]["selector"] == "a.slot", learned["roles"]["time"]
    assert [c["text"] for c in learned["roles"]["confirm"]] == ["Далее", "Подтвердить запись"]


def test_learned_recipe_is_used_on_next_run(server, browser, tmp_path):
    cfg = make_cfg(server, tmp_path)
    result, _ = run_attempt(cfg, browser)
    assert result == Result.BOOKED
    requests.get(server + "/admin/reset", timeout=5)
    archive = RunArchive(tmp_path / "runs", "test2")
    result, client = run_attempt(cfg, browser, archive=archive)
    assert result == Result.BOOKED
    sources = {st["role"]: st["source"] for st in client.trace}
    assert sources["book_button"] == "learned", client.trace
    assert sources["confirm"] == "learned"
    assert fake_site.STATE.booking["time"] == "09:00"


def test_bad_learned_selector_falls_back_to_heuristics(server, browser, tmp_path):
    (tmp_path / "learned").mkdir()
    (tmp_path / "learned" / "fake.json").write_text(json.dumps({"roles": {
        "book_button": [{"selector": "a[href='/logout']"}],      # текст «Выйти» — не пройдёт проверку
        "confirm": [{"selector": "#no-such-element"}],
    }}), encoding="utf-8")
    cfg = make_cfg(server, tmp_path)
    result, client = run_attempt(cfg, browser)
    assert result == Result.BOOKED
    assert {st["role"]: st["source"] for st in client.trace}["book_button"] == "heuristic"


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
    learned = json.loads((tmp_path / "learned" / "fake.json").read_text(encoding="utf-8"))
    final = learned["roles"]["confirm"][-1]
    assert final["text"] == "Подтвердить запись" and final["clicked"] is False


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


def test_captcha_checkbox_auto_click(server, browser, tmp_path):
    requests.get(server + "/admin/captcha?v=checkbox", timeout=5)
    cfg = make_cfg(server, tmp_path)
    result, client = run_attempt(cfg, browser)
    assert result == Result.BOOKED, client.last_message
    assert fake_site.STATE.last_token == "tok-checkbox"


def test_captcha_challenge_solved_by_service(server, browser, tmp_path):
    requests.get(server + "/admin/captcha?v=challenge", timeout=5)
    cfg = make_cfg(server, tmp_path, captcha_api_key="test-key", captcha_solver_url=server)
    result, client = run_attempt(cfg, browser)
    assert result == Result.BOOKED, client.last_message
    assert fake_site.STATE.last_token == "tok-service"
    assert fake_site.STATE.solver_calls >= 2


def test_captcha_challenge_without_service_fails(server, browser, tmp_path):
    requests.get(server + "/admin/captcha?v=challenge", timeout=5)
    cfg = make_cfg(server, tmp_path)
    result, _ = run_attempt(cfg, browser)
    assert result == Result.CAPTCHA_FAILED
    assert fake_site.STATE.booking is None


def test_persistent_profile_session(server, pw, tmp_path):
    """С profile_dir cookies переживают перезапуск браузера: второй запуск уже авторизован."""
    cfg = make_cfg(server, tmp_path, profile_dir=tmp_path / "profile")
    session = BrowserSession(pw, cfg)
    client = SiteClient(cfg, cfg.sites[0], session.new_context())
    assert client.login()
    client.close()
    session.close()

    session = BrowserSession(pw, cfg)
    client = SiteClient(cfg, cfg.sites[0], session.new_context())
    client.page.goto(server + "/account/")
    assert client.is_logged_in()
    client.close()
    session.close()
