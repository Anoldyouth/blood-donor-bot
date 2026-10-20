"""Обработка Яндекс SmartCaptcha.

Стратегия по порядку:
1. Если капчи на странице нет — ничего не делаем.
2. Если это чекбокс «Я не робот» — кликаем его сами. Для обычного браузера с
   постоянным профилем этого чаще всего достаточно.
3. Если открылось задание (картинки/слайдер) — отдаём его сервису распознавания
   (rucaptcha/2captcha, метод yandex), если задан CAPTCHA_API_KEY, и подставляем
   полученный токен в поле smart-token. Без ключа задание решить нельзя — бот
   сообщает об этом и сайт пропускается на этом круге.
Ручного решения нет: бот должен работать без участия человека.
"""
from __future__ import annotations

import logging
import time
from enum import Enum

import requests
from playwright.sync_api import Error as PWError, Page

log = logging.getLogger(__name__)

CAPTCHA_HOSTS = ("smartcaptcha.yandexcloud.net", "captcha-api.yandex.ru", "smartcaptcha.cloud.yandex")
CONTAINER_CSS = ".smart-captcha, [data-sitekey], #captcha-container, .captcha-container"
TOKEN_CSS = "input[name='smart-token'], input[name='smart_token'], input[name*='captcha' i][type='hidden']"
CHECKBOX_CSS = "[role='checkbox'], .CheckboxCaptcha-Button, #js-button, button, input[type='checkbox']"
# Задание (advanced challenge) живёт в отдельном iframe/поп-апе
CHALLENGE_HINT = ("advanced", "challenge", "AdvancedCaptcha")

SOLVER_URLS = {
    "rucaptcha": "https://rucaptcha.com",
    "2captcha": "https://2captcha.com",
}


class CaptchaStatus(str, Enum):
    NONE = "none"        # капчи нет
    SOLVED = "solved"    # токен получен / капча исчезла
    FAILED = "failed"    # не смогли решить


DETECT_JS = r"""
() => {
  const q = (sel) => Array.from(document.querySelectorAll(sel));
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'; };
  const frames = q('iframe').filter(f => /captcha|yandex/i.test(f.getAttribute('src') || ''));
  const containers = q(%CONTAINER%);
  const tokens = q(%TOKEN%);
  let sitekey = '';
  for (const c of containers) { sitekey = c.getAttribute('data-sitekey') || sitekey; }
  for (const f of frames) { const m = /sitekey=([^&]+)/.exec(f.getAttribute('src') || ''); if (m) sitekey = sitekey || decodeURIComponent(m[1]); }
  const token = tokens.map(t => t.value || '').find(v => v.length > 0) || '';
  const challenge = frames.some(f => vis(f) && /advanced|challenge/i.test(f.getAttribute('src') || '')) ||
                    q('[class*="AdvancedCaptcha"], [class*="advanced-captcha"]').some(vis);
  const present = frames.some(vis) || containers.some(vis) || challenge;
  return {present, frames: frames.map(f => f.getAttribute('src') || ''), sitekey, token, challenge,
          hasTokenInput: tokens.length > 0};
}
""".replace("%CONTAINER%", repr(CONTAINER_CSS)).replace("%TOKEN%", repr(TOKEN_CSS))


class CaptchaSolver:
    def __init__(self, cfg, site_name: str):
        self.cfg = cfg
        self.site_name = site_name

    # ----------------------------------------------------------------- detect
    def detect(self, page: Page) -> dict:
        try:
            return page.evaluate(DETECT_JS)
        except PWError:
            return {"present": False, "frames": [], "sitekey": "", "token": "", "challenge": False,
                    "hasTokenInput": False}

    def present(self, page: Page) -> bool:
        return bool(self.detect(page)["present"])

    # ------------------------------------------------------------------ solve
    def solve(self, page: Page) -> CaptchaStatus:
        info = self.detect(page)
        if not info["present"]:
            return CaptchaStatus.NONE
        if info["token"]:
            return CaptchaStatus.SOLVED
        log.info("[%s] обнаружена капча (sitekey=%s, challenge=%s)", self.site_name,
                 info["sitekey"] or "?", info["challenge"])

        if not info["challenge"] and self._click_checkbox(page):
            if self._wait_token(page, timeout=12):
                log.info("[%s] капча пройдена по чекбоксу", self.site_name)
                return CaptchaStatus.SOLVED
            info = self.detect(page)
            if not info["present"]:
                return CaptchaStatus.SOLVED

        if self.cfg.captcha_api_key:
            token = self._solve_via_service(page, info["sitekey"])
            if token and self._inject_token(page, token):
                log.info("[%s] капча решена сервисом %s", self.site_name, self.cfg.captcha_solver)
                return CaptchaStatus.SOLVED
            return CaptchaStatus.FAILED

        log.error("[%s] капча требует решения задания, а сервис распознавания не настроен "
                  "(задайте CAPTCHA_API_KEY и captcha.solver в config.yaml)", self.site_name)
        return CaptchaStatus.FAILED

    # ------------------------------------------------------------- strategies
    def _click_checkbox(self, page: Page) -> bool:
        for frame in page.frames:
            if not any(h in frame.url for h in CAPTCHA_HOSTS) and "captcha" not in frame.url.lower():
                continue
            if any(h in frame.url.lower() for h in ("advanced", "challenge")):
                continue
            try:
                box = frame.locator(CHECKBOX_CSS).first
                if box.count() and box.is_visible():
                    log.info("[%s] кликаю чекбокс капчи", self.site_name)
                    box.click(timeout=3000)
                    return True
            except PWError as exc:
                log.debug("checkbox click failed: %s", exc)
        return False

    def _wait_token(self, page: Page, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            info = self.detect(page)
            if info["token"]:
                return True
            if info["challenge"]:
                return False
            time.sleep(0.5)
        return False

    def _inject_token(self, page: Page, token: str) -> bool:
        try:
            n = page.evaluate(
                """([css, token]) => { const els = Array.from(document.querySelectorAll(css));
                     els.forEach(e => { e.value = token; e.dispatchEvent(new Event('input', {bubbles: true}));
                                        e.dispatchEvent(new Event('change', {bubbles: true})); });
                     return els.length; }""", [TOKEN_CSS, token])
        except PWError:
            n = 0
        if not n:
            log.warning("[%s] не нашёл поле smart-token для подстановки токена", self.site_name)
            return False
        return True

    def _solve_via_service(self, page: Page, sitekey: str) -> str | None:
        if not sitekey:
            log.warning("[%s] не удалось определить sitekey капчи — сервис распознавания недоступен", self.site_name)
            return None
        base = (self.cfg.captcha_solver_url or SOLVER_URLS.get(self.cfg.captcha_solver, "")).rstrip("/")
        if not base:
            log.warning("[%s] не задан captcha.solver / solver_url", self.site_name)
            return None
        try:
            r = requests.post(f"{base}/in.php", data={
                "key": self.cfg.captcha_api_key, "method": "yandex",
                "sitekey": sitekey, "pageurl": page.url, "json": 1}, timeout=30).json()
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] сервис капчи недоступен: %s", self.site_name, exc)
            return None
        if r.get("status") != 1:
            log.warning("[%s] сервис капчи отказал: %s", self.site_name, r.get("request"))
            return None
        task_id = r["request"]
        log.info("[%s] задача отправлена в сервис капчи (id=%s), жду до %d с",
                 self.site_name, task_id, self.cfg.captcha_solve_timeout_sec)
        end = time.time() + self.cfg.captcha_solve_timeout_sec
        while time.time() < end:
            time.sleep(5)
            try:
                res = requests.get(f"{base}/res.php", params={
                    "key": self.cfg.captcha_api_key, "action": "get", "id": task_id, "json": 1}, timeout=30).json()
            except Exception as exc:  # noqa: BLE001
                log.debug("poll failed: %s", exc)
                continue
            if res.get("status") == 1:
                return res["request"]
            if res.get("request") != "CAPCHA_NOT_READY":
                log.warning("[%s] сервис капчи вернул ошибку: %s", self.site_name, res.get("request"))
                return None
        log.warning("[%s] сервис капчи не успел за отведённое время", self.site_name)
        return None
