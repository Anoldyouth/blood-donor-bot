"""Токены Яндекс SmartCaptcha: кликом по галочке, а где Яндекс требует задание — от rucaptcha.

Сервер проверяет smart-token у Яндекса, а галочку проходит лишь настоящий JS-движок, поэтому
нужен браузер. Страница с виджетом подставляется на адрес самого сайта (page.route): браузер
не входит в кабинет и не мешает HTTP-сессии бота. Токен одноразовый и живёт 7–10 минут
(замер 28.10), поэтому токены rucaptcha (30–70 с на решение) заказываются заранее.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from playwright.async_api import Browser, Page, Playwright, async_playwright

WIDGET_PATH = "/__donor_bot_captcha__"
WIDGET_HTML = """<!doctype html><meta charset="utf-8">
<script src="https://smartcaptcha.yandexcloud.net/captcha.js" defer></script>
<div class="smart-captcha" data-sitekey="{sitekey}" data-hl="ru"></div>"""
CHECKBOX_FRAME = "iframe[data-testid=checkbox-iframe]"
CHECKBOX = "[role=checkbox], .CheckboxCaptcha-Button"
TOKEN_JS = "() => document.querySelector('input[name=smart-token]')?.value || ''"
RUCAPTCHA = "https://rucaptcha.com"

log = logging.getLogger("donor_bot")


class CaptchaError(Exception):
    pass


class NoToken(CaptchaError):
    """Токен сервиса ещё не готов — сайт не выбывает, просто пока не может записаться."""


class Captcha:
    """Один браузер на все сайты, своя вкладка с виджетом на каждый."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw: Playwright | None = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> Captcha:
        self._pw = await async_playwright().start()
        # Без этих флагов и с «HeadlessChrome» в User-Agent Яндекс вместо галочки даёт задание
        self._browser = await self._pw.chromium.launch(
            headless=self.headless, channel="chromium",
            args=["--disable-blink-features=AutomationControlled"])
        return self

    async def __aexit__(self, *exc) -> None:
        await self._browser.close()
        await self._pw.stop()

    async def widget(self, site_url: str, sitekey: str, max_age: float) -> Widget:
        ua = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              f"(KHTML, like Gecko) Chrome/{self._browser.version} Safari/537.36")
        page = await self._browser.new_page(user_agent=ua, viewport={"width": 1280, "height": 800})
        url = site_url.rstrip("/") + WIDGET_PATH
        body = WIDGET_HTML.format(sitekey=sitekey)
        await page.route(url, lambda route: route.fulfill(body=body, content_type="text/html"))
        await page.goto(url)
        return Widget(page, max_age)


class Widget:
    """Токен кликом по галочке (≈0,3–1 с); держится наготове, старше max_age — обновляется."""
    ready = True

    def __init__(self, page: Page, max_age: float):
        self.page = page
        self.max_age = max_age
        self.token = ""
        self.got_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def age(self) -> float:
        return time.time() - self.got_at if self.token else float("inf")

    async def refresh(self) -> str:
        """Новый токен (≈0,3–1 с). CaptchaError — Яндекс показал задание вместо галочки."""
        async with self._lock:
            return await self._refresh()

    async def _refresh(self) -> str:
        p = self.page
        if await p.evaluate(TOKEN_JS):
            await p.evaluate("() => window.smartCaptcha.reset()")
        await p.frame_locator(CHECKBOX_FRAME).locator(CHECKBOX).first.click(timeout=10_000)
        try:
            await p.wait_for_function(TOKEN_JS, timeout=8_000)
        except Exception:  # noqa: BLE001
            raise CaptchaError("чекбокс не дал токен — Яндекс требует решить задание") from None
        self.token, self.got_at = await p.evaluate(TOKEN_JS), time.time()
        return self.token

    async def tend(self) -> None:
        if self.age > self.max_age:
            await self.refresh()

    async def take(self) -> str:
        await self.tend()
        token, self.token = self.token, ""
        return token


class ServiceTokens:
    """Токены от rucaptcha с запасом: к моменту записи готовый токен уже лежит.

    Держит не меньше `stock` токенов, которые проживут ещё ≥ lead секунд, считая заказанные."""

    def __init__(self, name: str, api_key: str, sitekey: str, pageurl: str,
                 max_age: float = 300, lead: float = 150, stock: int = 2):
        self.name = name
        self._order = {"key": api_key, "method": "yandex", "sitekey": sitekey, "pageurl": pageurl, "json": 1}
        self.max_age, self.lead, self.stock = max_age, lead, stock
        self.tokens: list[tuple[str, float]] = []
        self.pending: set[asyncio.Task] = set()
        self.http = httpx.AsyncClient(base_url=RUCAPTCHA, timeout=30)

    @property
    def ready(self) -> bool:
        return any(time.time() - got < self.max_age for _, got in self.tokens)

    async def tend(self) -> None:
        now = time.time()
        self.tokens = [(t, got) for t, got in self.tokens if now - got < self.max_age]
        lasting = sum(now - got < self.max_age - self.lead for _, got in self.tokens)
        for _ in range(self.stock - lasting - len(self.pending)):
            task = asyncio.create_task(self._solve())
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)

    async def take(self) -> str:
        """Самый старый из живых токенов. CaptchaError — готового нет (решение ждать слишком долго)."""
        await self.tend()
        if not self.tokens:
            raise NoToken("нет готового токена rucaptcha")
        return self.tokens.pop(0)[0]

    async def close(self) -> None:
        for task in self.pending:
            task.cancel()
        await self.http.aclose()

    async def _solve(self) -> None:
        t0 = time.time()
        try:
            r = (await self.http.post("/in.php", data=self._order)).json()
            if r.get("status") != 1:
                raise RuntimeError(r.get("request"))
            task_id = r["request"]
            while time.time() - t0 < 180:
                await asyncio.sleep(3)
                r = (await self.http.get("/res.php", params={
                    "key": self._order["key"], "action": "get", "id": task_id, "json": 1})).json()
                if r.get("status") == 1:
                    self.tokens.append((r["request"], time.time()))
                    log.info("[%s] токен rucaptcha готов за %.0f с", self.name, time.time() - t0)
                    return
                if r.get("request") != "CAPCHA_NOT_READY":
                    raise RuntimeError(r.get("request"))
            raise RuntimeError("не решили за 180 с")
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            log.warning("[%s] rucaptcha: %s", self.name, exc)
            await asyncio.sleep(5)   # не заказывать в цикле, если сервис отказывает
