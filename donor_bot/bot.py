"""Окно записи: все сайты параллельно, ровно одна заявка на все сайты.

Отклонённая врачом заявка блокирует аккаунт на 5+ часов, а записи на близкие даты в разных
отделениях не подтверждаются, поэтому после первой принятой заявки остальные сайты
останавливаются, а бот встаёт на паузу.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

import httpx

from .captcha import Captcha, CaptchaError, NoToken, ServiceTokens, Widget
from .config import Config, SiteConf
from .site import BookingForm, Row, SessionExpired, Site

log = logging.getLogger("donor_bot")
MSK = ZoneInfo("Europe/Moscow")


class Outcome(str, Enum):
    BOOKED = "booked"
    UNCERTAIN = "uncertain"             # заявка могла уйти, а проверить не вышло
    DRY_RUN = "dry_run"
    ALREADY_BOOKED = "already_booked"   # в кабинете уже есть заявка
    SOLD_OUT = "sold_out"               # новая дата (сегодня+3) разобрана — ждать нечего
    TIMEOUT = "timeout"
    ERROR = "error"
    SKIPPED = "skipped"                 # капча требует решить задание — сайт пропускаем


@dataclass
class Lane:
    """Что известно о сайте прямо сейчас — по этому сайты пониже уступают сайтам повыше."""
    rank: int                 # 0 — самый приоритетный
    live: bool = True         # сайт ещё ловит
    seen_at: float = 0.0      # когда пришёл последний ответ кабинета
    free: bool = False        # в нём есть свободная дата, и сайт будет пробовать записаться
    booking: bool = False     # сейчас отправляет заявку


class Window:
    def __init__(self, cfg: Config, open_at: float, deadline: float):
        self.cfg = cfg
        self.open_at = open_at
        self.deadline = deadline
        day = datetime.fromtimestamp(open_at, MSK).date() + timedelta(days=3)
        self.target = day.strftime("%d/%m/%Y")
        self.send_lock = asyncio.Lock()
        self.done = asyncio.Event()
        self.booking = ""
        self.lanes: dict[str, Lane] = {}

    async def run(self, sites: list[SiteConf]) -> tuple[Outcome, str]:
        """sites — в порядке приоритета."""
        self.lanes = {s.name: Lane(rank) for rank, s in enumerate(sites)}
        async with Captcha(self.cfg.headless) as captcha:
            results = await asyncio.gather(*(self._site(s, captcha) for s in sites))
        for outcome in (Outcome.BOOKED, Outcome.UNCERTAIN, Outcome.ALREADY_BOOKED, Outcome.DRY_RUN):
            if outcome in results:
                return outcome, self.booking
        return min(results, key=list(Outcome).index), ""

    async def _site(self, conf: SiteConf, captcha: Captcha) -> Outcome:
        site = Site(conf.name, conf.url, self.cfg.login, self.cfg.password)
        lane = self.lanes[conf.name]
        try:
            await site.login()
            acc = await site.account(full=True)
            if acc.has_booking:
                log.info("[%s] в кабинете уже есть заявка", site.name)
                return Outcome.ALREADY_BOOKED
            tokens: Widget | ServiceTokens = await captcha.widget(site.url, acc.sitekey, self.cfg.token_max_age_sec)
            try:
                await tokens.refresh()
            except CaptchaError as exc:
                if not self.cfg.captcha_api_key:
                    raise
                log.info("[%s] %s — беру токены заранее у rucaptcha", site.name, exc)
                tokens = ServiceTokens(site.name, self.cfg.captcha_api_key, acc.sitekey, f"{site.url}/account/",
                                       max_age=self.cfg.service_token_max_age_sec)
            try:
                outcome = await self._watch(site, lane, tokens)
            finally:
                if isinstance(tokens, ServiceTokens):
                    await tokens.close()
        except CaptchaError as exc:
            log.warning("[%s] %s — сайт пропускаю", site.name, exc)
            outcome = Outcome.SKIPPED
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] ошибка: %s", site.name, exc)
            outcome = Outcome.ERROR
        finally:
            lane.live = False
            await site.close()
        log.info("[%s] итог: %s", site.name, outcome.value)
        return outcome

    async def _watch(self, site: Site, lane: Lane, tokens: Widget | ServiceTokens) -> Outcome:
        accounts: asyncio.Queue = asyncio.Queue()
        expired = asyncio.Event()
        stats = {"ok": 0, "errors": 0}
        failed_sends = 0

        async def poll():
            try:
                accounts.put_nowait(await site.account())
                stats["ok"] += 1
            except SessionExpired:
                expired.set()
            except httpx.HTTPError:
                stats["errors"] += 1

        async def poller():
            inflight: set[asyncio.Task] = set()
            while True:
                if len(inflight) < self.cfg.poll_parallel:
                    task = asyncio.create_task(poll())
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)
                await asyncio.sleep(self.cfg.poll_interval_sec)

        async def keeper():
            """Сессия — свежая, токен капчи — наготове."""
            while True:
                if site.session_age > self.cfg.session_refresh_sec or (expired.is_set() and site.session_age > 2):
                    try:
                        await site.login()
                        expired.clear()
                    except (httpx.HTTPError, RuntimeError) as exc:
                        log.warning("[%s] вход: %s", site.name, exc)
                await tokens.tend()   # CaptchaError — галочка перестала проходить, сайт выбывает
                await asyncio.sleep(0.5)

        tasks = [asyncio.create_task(poller()), asyncio.create_task(keeper())]
        log.info("[%s] вошёл, токен капчи готов, жду дату %s (приоритет %d)", site.name, self.target,
                 lane.rank + 1)
        last_log = 0.0
        try:
            while time.time() < self.deadline and not self.done.is_set():
                if tasks[1].done():
                    tasks[1].result()   # keeper упал: капча перестала давать галочку
                try:
                    acc = await asyncio.wait_for(accounts.get(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                lane.seen_at = time.time()
                lane.free = bool(acc.free) and tokens.ready and failed_sends < 3
                if lane.free:
                    outcome = await self._book(site, lane, tokens, acc.free[0])
                    if outcome:
                        return outcome
                    failed_sends += 1
                    while not accounts.empty():   # ответы, пришедшие во время отправки, устарели
                        accounts.get_nowait()
                    continue
                row = acc.row(self.target)
                if row and row.sold_out:
                    log.info("[%s] %s — места закончились", site.name, self.target)
                    return Outcome.SOLD_OUT
                if time.time() - last_log > 10:
                    last_log = time.time()
                    log.info("[%s] опрос: ответов %d, ошибок %d — свободных %s (%s)", site.name, stats["ok"],
                             stats["errors"], "есть, но токен капчи ещё не готов" if acc.free else "нет",
                             ", ".join(r.date + ("×" if r.sold_out else "") for r in acc.rows))
            return Outcome.ALREADY_BOOKED if self.done.is_set() else Outcome.TIMEOUT
        finally:
            lane.free = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _yield_to_higher(self, site: Site, lane: Lane, since: float) -> None:
        """Даёт сайтам выше по приоритету записаться первыми: ждёт, пока каждый из них, кто может
        записаться, пришлёт свежий кабинет без свободных дат. Не дольше priority_wait_sec."""
        def blocking() -> list[str]:
            return [name for name, other in self.lanes.items()
                    if other.rank < lane.rank and other.live
                    and (other.seen_at <= since or other.free or other.booking)]

        if not blocking():
            return
        log.info("[%s] сначала жду сайты выше по приоритету: %s", site.name, ", ".join(blocking()))
        end = since + self.cfg.priority_wait_sec
        while time.time() < end and not self.done.is_set() and blocking():
            await asyncio.sleep(0.05)

    async def _book(self, site: Site, lane: Lane, tokens: Widget | ServiceTokens, row: Row) -> Outcome | None:
        """None — заявка не прошла, можно пробовать дальше."""
        t0 = time.time()
        form = BookingForm.parse(row.form)
        slot = form.pick_time(self.cfg.preferred_times)
        if not slot:
            log.info("[%s] %s: нет свободного времени", site.name, row.date)
            return None
        what = f"{site.name} {row.date} {slot}"
        log.info("[%s] свободно %s (время: %s) → беру %s", site.name, row.date, ", ".join(form.times), slot)
        await self._yield_to_higher(site, lane, t0)
        if self.done.is_set():
            return Outcome.ALREADY_BOOKED
        if self.cfg.dry_run:
            log.info("[%s] DRY RUN: заявку не отправляю", site.name)
            self.booking = what
            return Outcome.DRY_RUN

        lane.booking = True
        try:
            async with self.send_lock:
                if self.done.is_set():
                    return Outcome.ALREADY_BOOKED
                if site.session_age > 20:
                    await site.login()
                for _ in range(3):
                    try:
                        token = await tokens.take()   # CaptchaError — галочка не проходит, сайт выбывает
                    except NoToken as exc:
                        log.warning("[%s] %s", site.name, exc)
                        return None
                    try:
                        status, body = await site.send(form, slot, token)
                    except httpx.HTTPError as exc:   # запрос мог дойти до сервера
                        status, body = 0, {"error": repr(exc)}
                    log.info("[%s] заявка %s: HTTP %s за %.1f с: %s", site.name, what, status, time.time() - t0,
                             json.dumps(body, ensure_ascii=False)[:300])
                    if status == 200 and body.get("status") is True:
                        break
                    if status == 401:
                        await site.login()
                        continue
                    if "капч" in str(body.get("error", "")).lower():
                        continue
                    # Неизвестный ответ: пробовать снова можно, только если заявки точно нет
                    try:
                        if (await site.account(full=True)).has_booking:
                            break
                    except (SessionExpired, httpx.HTTPError):
                        log.warning("[%s] не смог проверить, создана ли заявка — останавливаю все сайты", site.name)
                        self.booking = what
                        self.done.set()
                        return Outcome.UNCERTAIN
                    return None
                else:
                    return None
            self.booking = what
            self.done.set()
            return Outcome.BOOKED
        finally:
            lane.booking = False


# ------------------------------------------------------------------ расписание

def load_state(cfg: Config) -> dict:
    try:
        return json.loads(cfg.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def paused_until(cfg: Config) -> date | None:
    raw = load_state(cfg).get("paused_until")
    return date.fromisoformat(raw) if raw else None


def save_state(cfg: Config, state: dict) -> None:
    cfg.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def save_success(cfg: Config, booking: str) -> date:
    until = datetime.now(MSK).date() + timedelta(days=cfg.pause_days)
    save_state(cfg, {"last_success": {"booking": booking, "at": datetime.now(MSK).isoformat(timespec="seconds")},
                     "paused_until": until.isoformat()})
    return until


def clear_pause(cfg: Config) -> None:
    state = load_state(cfg)
    state.pop("paused_until", None)
    save_state(cfg, state)


async def notify(cfg: Config, text: str) -> None:
    log.info("NOTIFY: %s", text)
    if not (cfg.telegram_token and cfg.telegram_chat_id):
        return
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            (await http.post(f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
                             json={"chat_id": cfg.telegram_chat_id, "text": text})).raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Telegram: %s", exc)


async def run_window(cfg: Config, sites: list[SiteConf], open_at: float, deadline: float) -> Outcome:
    outcome, booking = await Window(cfg, open_at, deadline).run(sites)
    if outcome == Outcome.BOOKED:
        until = save_success(cfg, booking)
        await notify(cfg, f"Записался: {booking}. Заявка ждёт подтверждения врача. Пауза до {until:%d.%m}.")
    elif outcome == Outcome.UNCERTAIN:
        until = save_success(cfg, booking + " (не подтверждено)")
        await notify(cfg, f"Заявка {booking} могла уйти, но проверить не вышло — посмотрите кабинет. "
                          f"Пауза до {until:%d.%m} (снять: python -m donor_bot clear-pause).")
    elif outcome == Outcome.DRY_RUN:
        log.info("DRY RUN: дошёл бы до заявки %s", booking)
    else:
        log.info("окно закрыто: %s", outcome.value)
    return outcome


def next_open(cfg: Config, now: datetime) -> datetime:
    h, m = map(int, cfg.open_time.split(":"))
    at = now.astimezone(MSK).replace(hour=h, minute=m, second=0, microsecond=0)
    if now.timestamp() > at.timestamp() + cfg.window_sec:
        at += timedelta(days=1)
    return at


async def run_forever(cfg: Config, sites: list[SiteConf]) -> None:
    while True:
        open_at = next_open(cfg, datetime.now(MSK))
        until = paused_until(cfg)
        if until and open_at.date() <= until:
            log.info("пауза после записи до %s", until)
            open_at = next_open(cfg, datetime.combine(until + timedelta(days=1), datetime.min.time(), MSK))
        start = open_at.timestamp() - cfg.start_before_sec
        log.info("следующее окно: %s MSK (вход в %s)", f"{open_at:%d.%m %H:%M}",
                 datetime.fromtimestamp(start, MSK).strftime("%H:%M:%S"))
        while (left := start - time.time()) > 0:   # кусками: компьютер мог уснуть
            await asyncio.sleep(min(left, 60))
        if paused_until(cfg) and open_at.date() <= paused_until(cfg):
            continue
        await run_window(cfg, sites, open_at.timestamp(), open_at.timestamp() + cfg.window_sec)
        await asyncio.sleep(max(0.0, open_at.timestamp() + cfg.window_sec - time.time()) + 1)
