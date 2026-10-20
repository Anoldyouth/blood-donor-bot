"""Планировщик: ждёт 12:00 по Москве и в течение окна пытается записаться."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from .archive import RunArchive
from .config import Config
from .notify import notify
from .site import Result, SiteClient
from .state import State

log = logging.getLogger(__name__)

STOP_RESULTS = {Result.BOOKED, Result.ALREADY_BOOKED, Result.DRY_RUN}


def parse_hhmm(value: str) -> tuple[int, int]:
    hh, mm = value.strip().split(":")
    return int(hh), int(mm)


def next_window_start(cfg: Config, now: datetime | None = None) -> datetime:
    """Момент начала следующего окна (time минус start_before_sec) в таймзоне cfg."""
    tz = ZoneInfo(cfg.timezone)
    now = now.astimezone(tz) if now else datetime.now(tz)
    hh, mm = parse_hhmm(cfg.time)
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0) - timedelta(seconds=cfg.start_before_sec)
    if start <= now:
        start += timedelta(days=1)
    return start


STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
"""

CONTEXT_ARGS = dict(
    locale="ru-RU",
    timezone_id="Europe/Moscow",
    viewport={"width": 1280, "height": 900},
    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
)


class BrowserSession:
    """Браузер на время окна. С profile_dir — один постоянный контекст (cookies сохраняются
    между запусками, что снижает частоту заданий капчи); иначе — отдельный контекст на сайт."""

    def __init__(self, pw, cfg: Config):
        self.cfg = cfg
        self.browser = None
        self.shared = None
        kwargs = {"headless": cfg.headless, "slow_mo": cfg.slow_mo_ms,
                  "ignore_default_args": ["--enable-automation"]}
        if cfg.channel:
            kwargs["channel"] = cfg.channel
        if cfg.profile_dir:
            cfg.profile_dir.mkdir(parents=True, exist_ok=True)
            self.shared = pw.chromium.launch_persistent_context(str(cfg.profile_dir), **kwargs, **CONTEXT_ARGS)
            self.shared.add_init_script(STEALTH_JS)
        else:
            self.browser = pw.chromium.launch(**kwargs)

    def new_context(self):
        if self.shared is not None:
            return self.shared
        ctx = self.browser.new_context(**CONTEXT_ARGS)
        ctx.add_init_script(STEALTH_JS)
        return ctx

    def drop_context(self, ctx) -> None:
        if ctx is not self.shared:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        for obj in (self.shared, self.browser):
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass


def run_window(cfg: Config, state: State, window_min: float | None = None,
               mode: str = "run", sites: list[str] | None = None,
               archive_root: Path | None = None) -> Result | None:
    """Пытается записаться на сайтах по приоритету, пока не выйдет окно или не будет успеха."""
    window_min = cfg.window_min if window_min is None else window_min
    tz = ZoneInfo(cfg.timezone)
    hh, mm = parse_hhmm(cfg.time)
    now = datetime.now(tz)
    open_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if open_at < now - timedelta(minutes=window_min):
        open_at = now  # запущено вручную вне расписания — отсчёт окна от текущего момента
    deadline = max(open_at, now) + timedelta(minutes=window_min)
    chosen = [s for s in cfg.sites if not sites or s.name in sites]

    final: Result | None = None
    archive = RunArchive(archive_root or cfg.archive_dir, mode, cfg.archive_keep_days)
    log.info("архив прогона: %s", archive.dir)
    with sync_playwright() as pw:
        session = BrowserSession(pw, cfg)
        clients: dict[str, SiteClient] = {}

        def client_for(site) -> SiteClient:
            c = clients.get(site.name)
            if c is None:
                c = SiteClient(cfg, site, session.new_context(), archive=archive)
                clients[site.name] = c
            return c

        try:
            # Предварительный вход на все сайты до открытия слотов
            for site in chosen:
                ok = client_for(site).ensure_logged_in()
                log.info("[%s] предварительный вход: %s", site.name, "ок" if ok else "НЕ УДАЛСЯ")

            rounds = 0
            while True:
                now = datetime.now(tz)
                if now < open_at:
                    wait = min((open_at - now).total_seconds(), 5)
                    time.sleep(max(wait, 0.2))
                    continue
                rounds += 1
                log.info("--- круг %d (%s), до конца окна %d с", rounds, now.strftime("%H:%M:%S"),
                         int((deadline - now).total_seconds()))
                for site in chosen:
                    client = client_for(site)
                    result = client.attempt()
                    log.info("[%s] результат: %s %s", site.name, result.value,
                             f"— {client.last_message}" if client.last_message else "")
                    if result in STOP_RESULTS:
                        final = result
                        _on_stop(cfg, state, site.name, result, client.last_message)
                        return final
                    if result == Result.ERROR:
                        # Пересоздаём страницу/контекст, если браузер «сломался»
                        client.close()
                        session.drop_context(client.context)
                        clients.pop(site.name, None)
                if datetime.now(tz) >= deadline:
                    log.info("окно попыток закончилось, записаться не удалось")
                    return None
                time.sleep(cfg.retry_interval_sec)
        finally:
            for c in clients.values():
                c.close()
                session.drop_context(c.context)
            session.close()


def _on_stop(cfg: Config, state: State, site_name: str, result: Result, message: str) -> None:
    today = date.today()
    if result == Result.BOOKED:
        state.record_success(site_name, message, today + timedelta(days=cfg.pause_days_after_success))
        notify(cfg, f"✅ Записались на сдачу тромбоцитов: {site_name}. {message}".strip())
    elif result == Result.ALREADY_BOOKED:
        notify(cfg, f"ℹ️ На сайте {site_name} уже есть активная запись: {message}".strip())
    elif result == Result.DRY_RUN:
        log.info("DRY RUN завершён на сайте %s — запись не создавалась", site_name)


def run_forever(cfg: Config, state: State) -> None:
    tz = ZoneInfo(cfg.timezone)
    log.info("планировщик запущен: каждый день в %s (%s), сайты: %s",
             cfg.time, cfg.timezone, ", ".join(s.name for s in cfg.sites))
    while True:
        start = next_window_start(cfg)
        log.info("следующая попытка: %s", start.strftime("%Y-%m-%d %H:%M:%S %Z"))
        while True:
            remaining = (start - datetime.now(tz)).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 300))
        if state.is_paused(date.today()):
            log.info("пауза после успешной записи до %s — пропускаю день", state.paused_until)
            time.sleep(120)
            continue
        try:
            run_window(cfg, state)
        except Exception as exc:  # noqa: BLE001
            log.exception("сбой окна записи: %s", exc)
            notify(cfg, f"⚠️ Бот упал с ошибкой: {exc}")
        time.sleep(120)  # чтобы не запуститься дважды в одну минуту
