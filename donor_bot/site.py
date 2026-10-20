"""Работа с одним сайтом записи: вход, поиск кнопок записи, прохождение формы.

Структура страниц сайтов заранее неизвестна, поэтому бот работает эвристически:
собирает все видимые кликабельные элементы, читает их текст и выбирает нужные
по регулярным выражениям (вид донации, дата, время, кнопка подтверждения).
Любой шаг можно переопределить CSS-селекторами в config.yaml -> selectors.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from playwright.sync_api import BrowserContext, Error as PWError, Page, TimeoutError as PWTimeout

from .captcha import CaptchaSolver, CaptchaStatus
from .config import Config, Site

log = logging.getLogger(__name__)


class Result(str, Enum):
    BOOKED = "booked"                  # запись создана
    ALREADY_BOOKED = "already_booked"  # в кабинете уже есть активная запись
    NO_BUTTON = "no_button"            # кнопок записи нет (слоты не открыты / не нужны)
    NO_SLOTS = "no_slots"              # кнопка есть, но свободных дат/времени нет
    DRY_RUN = "dry_run"                # дошли до подтверждения, но не нажимали (dry_run)
    LOGIN_FAILED = "login_failed"
    CAPTCHA_FAILED = "captcha_failed"  # капча не прошла автоматически
    STUCK = "stuck"                    # не поняли, что нажимать дальше
    ERROR = "error"


RX = {
    "login_link": re.compile(r"\bвход\b|войти|личный кабинет|авториз|login|sign ?in", re.I),
    "login_submit": re.compile(r"войти|\bвход\b|авториз|login|sign ?in", re.I),
    "logged_in": re.compile(r"выход|выйти|logout", re.I),
    "book": re.compile(r"запис|бронир", re.I),
    "cancel": re.compile(r"отмен|удал|назад|выход|выйти|закрыть", re.I),
    "confirm": re.compile(r"подтвер|заброни|записаться|записать|сохран|отправ|далее|продолж|выбрать|ok|готово", re.I),
    "final_confirm": re.compile(r"подтвер|заброни|записаться|записать|отправ", re.I),
    "success": re.compile(
        r"вы записан|успешн|запись (создан|оформлен|сохранен|подтвержд|добавлен)|"
        r"заявка (принят|создан|отправлен|сохранен)|забронирован|спасибо", re.I),
    "has_booking": re.compile(
        r"ваша запись|ваши записи|активная запись|текуща\w запись|предстоящ\w+ (запись|донац)|"
        r"отменить (запись|бронь)|вы записаны|запись на \d{1,2}[./]\d{1,2}", re.I),
    "no_slots": re.compile(r"нет (свободн|доступн)|мест нет|запись недоступн|свободных мест нет|временно недоступн", re.I),
    "login_error": re.compile(r"неверн|неправильн|ошибк|не найден|заблокир|блокир", re.I),
    "consent": re.compile(r"соглас|ознаком|правил|подтвержда", re.I),
    "time_text": re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*(?:[-–—]\s*\d{1,2}[:.]\d{2})?\s*$"),
    "date_text": re.compile(
        r"^\s*(?:(?:пн|вт|ср|чт|пт|сб|вс|понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)[,.]?\s*)?"
        r"(\d{1,2})(?:[./](\d{1,2})(?:[./](\d{2,4}))?|\s+(янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)\w*)"
        r"(?:\s*\d{4})?\s*(?:[,.]?\s*(?:пн|вт|ср|чт|пт|сб|вс)\.?)?\s*$", re.I),
}

MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6, "июл": 7,
          "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}

# JS: собрать видимые кликабельные элементы и пометить их атрибутом data-dbot-idx.
COLLECT_JS = r"""
() => {
  const sel = 'a, button, input[type=submit], input[type=button], input[type=radio], ' +
              'input[type=checkbox], label, li, td, option, [role=button], [role=option], ' +
              '[role=tab], [role=radio], [role=menuitem], [onclick], [class*=btn], [class*=button], ' +
              '[class*=day], [class*=date], [class*=slot], [class*=time], [class*=card], [class*=item]';
  document.querySelectorAll('[data-dbot-idx]').forEach(e => e.removeAttribute('data-dbot-idx'));
  const els = Array.from(document.querySelectorAll(sel));
  const textOf = el => {
    if (el.tagName === 'INPUT') return (el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
    if (el.tagName === 'OPTION') return (el.textContent || '').trim();
    return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  };
  const info = [];
  for (const el of els) {
    if (el.tagName === 'OPTION') continue;              // select обрабатываем отдельно
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width === 0 || r.height === 0 || st.visibility === 'hidden' || st.display === 'none') continue;
    const text = textOf(el);
    const cls = (typeof el.className === 'string' ? el.className : '').toLowerCase();
    info.push({el, text, cls, noPointer: st.pointerEvents === 'none'});
  }
  const out = [];
  let i = 0;
  for (const it of info) {
    // Пропускаем контейнер, если внутри есть кликабельный потомок с тем же текстом
    const dup = info.some(o => o.el !== it.el && it.el.contains(o.el) && o.text === it.text && it.text.length > 0);
    if (dup) continue;
    const el = it.el;
    const disabled = !!el.disabled || it.noPointer || el.getAttribute('aria-disabled') === 'true' ||
      /(^|[\s_-])(disabled|inactive|unavailable|past|busy|closed|locked|muted|empty|not-available|nav-prev|nav-next)([\s_-]|$)/.test(it.cls);
    const selected = !!el.checked || el.getAttribute('aria-selected') === 'true' ||
      el.getAttribute('aria-checked') === 'true' ||
      /(^|[\s_-])(active|selected|checked|current|chosen)([\s_-]|$)/.test(it.cls);
    el.setAttribute('data-dbot-idx', String(i));
    out.push({idx: i, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
              text: it.text.slice(0, 200), cls: it.cls.slice(0, 200), disabled, selected,
              href: el.getAttribute('href') || '', name: el.getAttribute('name') || '',
              value: (el.getAttribute('value') || '').slice(0, 100)});
    i++;
  }
  return out;
}
"""

SELECTS_JS = r"""
() => Array.from(document.querySelectorAll('select')).map((s, i) => {
  const r = s.getBoundingClientRect();
  const st = getComputedStyle(s);
  const visible = r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  s.setAttribute('data-dbot-sel', String(i));
  return {idx: i, name: s.name || '', visible, value: s.value,
          options: Array.from(s.options).map(o => ({value: o.value, text: (o.textContent || '').trim(), disabled: o.disabled}))};
})
"""


@dataclass
class Element:
    idx: int
    tag: str
    type: str
    text: str
    cls: str
    disabled: bool
    selected: bool
    href: str = ""
    name: str = ""
    value: str = ""

    @property
    def is_input_like(self) -> bool:
        return self.tag in ("a", "button", "input") or "btn" in self.cls or "button" in self.cls


class SiteClient:
    def __init__(self, cfg: Config, site: Site, context: BrowserContext, dump_dir: Path | None = None):
        self.cfg = cfg
        self.site = site
        self.context = context
        self.page: Page = context.new_page()
        self.page.set_default_timeout(cfg.timeout_ms)
        self.dump_dir = dump_dir
        self.dump_counter = 0
        self.type_rx = re.compile(cfg.donation_type_regex, re.I)
        self.last_message = ""
        self.captcha = CaptchaSolver(cfg, site.name)
        self.login_failed_reason = Result.LOGIN_FAILED

    # ------------------------------------------------------------------ utils
    @property
    def account_url(self) -> str:
        return self.cfg.account_url(self.site)

    def _settle(self, ms: int = 300) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=self.cfg.timeout_ms)
            self.page.wait_for_load_state("load", timeout=2000)
        except PWTimeout:
            pass
        self.page.wait_for_timeout(ms)

    def body_text(self) -> str:
        try:
            return self.page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except PWError:
            return ""

    def collect(self) -> list[Element]:
        try:
            raw = self.page.evaluate(COLLECT_JS)
        except PWError as exc:
            log.debug("[%s] collect failed: %s", self.site.name, exc)
            return []
        return [Element(**r) for r in raw]

    def loc(self, el: Element):
        return self.page.locator(f'[data-dbot-idx="{el.idx}"]').first

    def click(self, el: Element, why: str) -> None:
        log.info("[%s] клик: %s <%s> «%s»", self.site.name, why, el.tag, el.text[:60])
        target = self.loc(el)
        try:
            target.scroll_into_view_if_needed(timeout=2000)
        except PWError:
            pass
        try:
            target.click(timeout=self.cfg.timeout_ms)
        except PWError:
            target.click(timeout=self.cfg.timeout_ms, force=True)
        self._settle()

    def dump(self, label: str) -> None:
        """Сохраняет скриншот, HTML и список элементов (режим разведки)."""
        if not self.dump_dir:
            return
        self.dump_counter += 1
        base = self.dump_dir / f"{self.dump_counter:02d}_{label}"
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.page.screenshot(path=str(base) + ".png", full_page=True)
        except PWError as exc:
            log.debug("screenshot failed: %s", exc)
        try:
            (base.with_suffix(".html")).write_text(self.page.content(), encoding="utf-8")
        except PWError as exc:
            log.debug("content failed: %s", exc)
        try:
            els = [e.__dict__ for e in self.collect()]
            selects = self.page.evaluate(SELECTS_JS)
            (base.with_suffix(".json")).write_text(
                json.dumps({"url": self.page.url, "elements": els, "selects": selects},
                           ensure_ascii=False, indent=1), encoding="utf-8")
        except PWError as exc:
            log.debug("elements dump failed: %s", exc)
        log.info("[%s] дамп сохранён: %s.*", self.site.name, base)

    def custom(self, key: str):
        """Локатор по пользовательскому селектору из config.yaml, если задан и виден."""
        for css in self.cfg.selectors.get(key, []):
            try:
                l = self.page.locator(css).first
                if l.count() and l.is_visible():
                    return l
            except PWError:
                continue
        return None

    # ---------------------------------------------------------------- captcha
    def pass_captcha(self) -> bool:
        """Если на странице капча — решает её автоматически. False = не удалось."""
        status = self.captcha.solve(self.page)
        if status == CaptchaStatus.FAILED:
            self.last_message = "капча не решена автоматически"
            self.dump("captcha_failed")
            return False
        if status == CaptchaStatus.SOLVED:
            self._settle(300)
        return True

    # ------------------------------------------------------------------ login
    def password_visible(self) -> bool:
        try:
            l = self.custom("password_input") or self.page.locator("input[type=password]").first
            return l.count() > 0 and l.is_visible()
        except PWError:
            return False

    def is_logged_in(self) -> bool:
        if self.password_visible():
            return False
        text = self.body_text()
        if RX["logged_in"].search(text):
            return True
        # Нет формы пароля и видим кабинет/запись — считаем, что вошли
        return bool(RX["book"].search(text) or RX["has_booking"].search(text))

    def login(self) -> bool:
        self.login_failed_reason = Result.LOGIN_FAILED
        log.info("[%s] открываю %s", self.site.name, self.account_url)
        self.page.goto(self.account_url, wait_until="domcontentloaded")
        self._settle()
        self.dump("account_initial")

        if not self.password_visible():
            # Возможно, нужна кнопка «Войти», чтобы показать форму
            for el in self.collect():
                if RX["login_link"].search(el.text) and not el.disabled and el.is_input_like:
                    self.click(el, "ссылка входа")
                    break
        if not self.password_visible():
            if self.is_logged_in():
                log.info("[%s] уже авторизованы", self.site.name)
                return True
            log.warning("[%s] не нашёл форму входа на %s", self.site.name, self.page.url)
            self.dump("login_form_not_found")
            return False

        login_input = self.custom("login_input")
        if login_input is None:
            for css in ("input[type=email]", "input[name*=mail i]", "input[name*=login i]",
                        "input[name*=user i]", "input[id*=mail i]", "input[id*=login i]",
                        "input[type=tel]", "input[type=text]"):
                l = self.page.locator(css)
                for i in range(min(l.count(), 5)):
                    cand = l.nth(i)
                    if cand.is_visible():
                        login_input = cand
                        break
                if login_input is not None:
                    break
        if login_input is None:
            log.error("[%s] не нашёл поле логина", self.site.name)
            self.dump("login_input_not_found")
            return False

        pwd = self.custom("password_input") or self.page.locator("input[type=password]").first
        login_input.fill(self.cfg.login)
        pwd.fill(self.cfg.password)
        self.dump("login_filled")
        if not self.pass_captcha():
            self.login_failed_reason = Result.CAPTCHA_FAILED
            return False

        submit = self.custom("login_submit")
        if submit is None:
            form_submit = self.page.locator("form:has(input[type=password]) button[type=submit], "
                                            "form:has(input[type=password]) input[type=submit], "
                                            "form:has(input[type=password]) button").first
            if form_submit.count() and form_submit.is_visible():
                submit = form_submit
        if submit is None:
            for el in self.collect():
                if RX["login_submit"].search(el.text) and el.is_input_like and not el.disabled:
                    submit = self.loc(el)
                    break
        if submit is not None:
            submit.click()
        else:
            pwd.press("Enter")
        self._settle(1000)
        self.dump("after_login")

        if self.password_visible() and self.captcha.present(self.page) and not self.captcha.detect(self.page)["token"]:
            # Капча появилась только после отправки формы — решаем и отправляем ещё раз
            log.info("[%s] после отправки формы входа появилась капча", self.site.name)
            if not self.pass_captcha():
                self.login_failed_reason = Result.CAPTCHA_FAILED
                return False
            if self.page.locator("input[type=password]").first.input_value() == "":
                self.page.locator("input[type=password]").first.fill(self.cfg.password)
            (submit.click() if submit is not None else pwd.press("Enter"))
            self._settle(1000)
            self.dump("after_login_retry")
        if self.password_visible():
            text = self.body_text()
            m = RX["login_error"].search(text)
            log.error("[%s] вход не удался%s", self.site.name,
                      f" (на странице: «{text[max(0, m.start()-40):m.end()+60].strip()}»)" if m else "")
            return False
        log.info("[%s] вход выполнен", self.site.name)
        return True

    def ensure_logged_in(self) -> bool:
        try:
            if not self.page.url.startswith(self.site.base):
                self.page.goto(self.account_url, wait_until="domcontentloaded")
                self._settle()
            if self.is_logged_in():
                return True
            return self.login()
        except PWError as exc:
            log.warning("[%s] ошибка при входе: %s", self.site.name, exc)
            return False

    # ---------------------------------------------------------------- booking
    def has_active_booking(self) -> bool:
        l = self.custom("has_booking")
        if l is not None:
            return True
        return bool(RX["has_booking"].search(self.body_text()))

    def booking_buttons(self, els: list[Element]) -> list[Element]:
        l = self.custom("book_button")
        if l is not None:
            # Пользовательский селектор: ищем соответствующий элемент среди собранных
            try:
                l.evaluate("e => e.setAttribute('data-dbot-custom', '1')")
                for el in els:
                    if self.page.locator(f'[data-dbot-idx="{el.idx}"][data-dbot-custom="1"]').count():
                        return [el]
            except PWError:
                pass
        out = [e for e in els
               if RX["book"].search(e.text) and not RX["cancel"].search(e.text)
               and not e.disabled and e.is_input_like and len(e.text) <= 80]
        # Кнопки с упоминанием нужного вида донации — первыми
        out.sort(key=lambda e: 0 if self.type_rx.search(e.text) else 1)
        return out

    def _pick_date(self, cands: list[Element]) -> Element:
        def key(e: Element):
            m = RX["date_text"].match(e.text)
            day = int(m.group(1)) if m else 99
            month = 0
            if m and m.group(2):
                month = int(m.group(2))
            elif m and m.group(4):
                month = MONTHS.get(m.group(4).lower()[:3], 0)
            return (month, day)
        ordered = sorted(cands, key=key)
        return ordered[-1] if self.cfg.date_preference == "latest" else ordered[0]

    def _pick_time(self, cands: list[Element]) -> Element:
        def minutes(e: Element) -> int:
            m = RX["time_text"].match(e.text)
            return int(m.group(1)) * 60 + int(m.group(2)) if m else 10**6
        for pref in self.cfg.preferred_times:
            for e in cands:
                if e.text.strip().startswith(pref):
                    return e
        return min(cands, key=minutes)

    def _handle_selects(self) -> bool:
        """Выбирает значения в <select>: вид донации по регулярке, иначе первый непустой вариант."""
        changed = False
        try:
            selects = self.page.evaluate(SELECTS_JS)
        except PWError:
            return False
        for s in selects:
            if not s["visible"]:
                continue
            loc = self.page.locator(f'select[data-dbot-sel="{s["idx"]}"]')
            options = [o for o in s["options"] if not o["disabled"] and o["value"] != ""]
            if not options:
                continue
            typed = [o for o in options if self.type_rx.search(o["text"])]
            if typed and s["value"] != typed[0]["value"]:
                loc.select_option(typed[0]["value"])
                log.info("[%s] select %s: выбрал «%s»", self.site.name, s["name"], typed[0]["text"])
                changed = True
            elif s["value"] in ("", None) or all(o["value"] != s["value"] for o in s["options"]):
                # Ничего не выбрано — берём первый вариант (даты/время обычно отсортированы)
                pick = options[-1] if self.cfg.date_preference == "latest" else options[0]
                loc.select_option(pick["value"])
                log.info("[%s] select %s: выбрал «%s»", self.site.name, s["name"], pick["text"])
                changed = True
        if changed:
            self._settle()
        return changed

    def _check_consents(self, els: list[Element]) -> bool:
        changed = False
        for e in els:
            if e.tag == "input" and e.type == "checkbox" and not e.selected and not e.disabled:
                self.click(e, "чекбокс согласия")
                changed = True
        return changed

    def _check_messages(self) -> Result | None:
        text = self.body_text()
        if self.custom("success") is not None or RX["success"].search(text):
            self.last_message = _snippet(text, RX["success"])
            return Result.BOOKED
        if RX["no_slots"].search(text):
            self.last_message = _snippet(text, RX["no_slots"])
            return Result.NO_SLOTS
        return None

    def attempt(self) -> Result:
        """Одна попытка записи. Возвращает Result."""
        self.last_message = ""
        try:
            return self._attempt()
        except PWError as exc:
            log.warning("[%s] ошибка браузера: %s", self.site.name, str(exc).splitlines()[0])
            self.dump("error")
            return Result.ERROR

    def _attempt(self) -> Result:
        if not self.ensure_logged_in():
            return self.login_failed_reason
        self.page.goto(self.account_url, wait_until="domcontentloaded")
        self._settle()
        self.dump("account")

        if self.has_active_booking():
            self.last_message = _snippet(self.body_text(), RX["has_booking"])
            log.info("[%s] в кабинете уже есть запись: %s", self.site.name, self.last_message)
            return Result.ALREADY_BOOKED

        els = self.collect()
        buttons = self.booking_buttons(els)
        if not buttons:
            log.info("[%s] кнопок записи нет (всего кликабельных элементов: %d)", self.site.name, len(els))
            return Result.NO_BUTTON

        self.click(buttons[0], "кнопка записи")
        return self._walk_form()

    def _walk_form(self, max_steps: int = 10) -> Result:
        """Проходит форму записи шаг за шагом, пока не увидит подтверждение."""
        clicked_type = False
        for step in range(1, max_steps + 1):
            self.dump(f"form_step{step}")
            if not self.pass_captcha():
                return Result.CAPTCHA_FAILED
            msg = self._check_messages()
            if msg == Result.BOOKED:
                log.info("[%s] похоже, запись создана: %s", self.site.name, self.last_message)
                return Result.BOOKED
            if msg == Result.NO_SLOTS:
                log.info("[%s] свободных слотов нет: %s", self.site.name, self.last_message)
                return Result.NO_SLOTS

            if self._handle_selects():
                continue

            els = self.collect()
            if self._check_consents(els):
                continue

            # 1. Вид донации
            if not clicked_type:
                c = self.custom("donation_type")
                if c is not None:
                    c.click()
                    self._settle()
                    clicked_type = True
                    continue
                typed = [e for e in els
                         if self.type_rx.search(e.text) and not e.disabled and not e.selected
                         and len(e.text) <= (40 if e.tag in ("li", "td", "label") else 80)]
                if typed:
                    self.click(typed[0], "вид донации")
                    clicked_type = True
                    continue

            # 2. Дата
            dates = self._custom_list("date") or [
                e for e in els if RX["date_text"].match(e.text) and not e.disabled and len(e.text) <= 30]
            if dates and not any(e.selected for e in dates):
                self.click(self._pick_date(dates), "дата")
                continue

            # 3. Время
            times = self._custom_list("time") or [
                e for e in els if RX["time_text"].match(e.text) and not e.disabled and len(e.text) <= 20]
            if times and not any(e.selected for e in times):
                self.click(self._pick_time(times), "время")
                continue

            # 4. Подтверждение / далее
            confirm = self.custom("confirm")
            confirm_el = None
            if confirm is None:
                cands = [e for e in els if RX["confirm"].search(e.text) and not RX["cancel"].search(e.text)
                         and not e.disabled and e.is_input_like and len(e.text) <= 60]
                cands.sort(key=lambda e: 0 if RX["final_confirm"].search(e.text) else 1)
                if cands:
                    confirm_el = cands[0]
            if confirm is not None or confirm_el is not None:
                if confirm_el is not None:
                    text = confirm_el.text
                else:
                    try:
                        text = (confirm.inner_text(timeout=1000) or confirm.get_attribute("value") or "").strip()
                    except PWError:
                        text = ""
                    text = text or "custom confirm"
                is_final = bool(RX["final_confirm"].search(text)) or len(self.cfg.selectors.get("confirm", [])) > 0 and text == "custom confirm"
                if self.cfg.dry_run and is_final:
                    log.info("[%s] DRY RUN: остановился перед кнопкой «%s»", self.site.name, text)
                    self.dump("dry_run_stop")
                    return Result.DRY_RUN
                if confirm is not None:
                    log.info("[%s] клик: подтверждение (custom selector)", self.site.name)
                    confirm.click()
                    self._settle(1000)
                else:
                    self.click(confirm_el, "подтверждение")
                    self._settle(400)
                if is_final:
                    self._settle(500)
                    msg = self._check_messages()
                    if msg == Result.BOOKED:
                        log.info("[%s] запись создана: %s", self.site.name, self.last_message)
                        return Result.BOOKED
                    if msg == Result.NO_SLOTS:
                        return Result.NO_SLOTS
                    if self.has_active_booking():
                        self.last_message = _snippet(self.body_text(), RX["has_booking"])
                        return Result.BOOKED
                continue

            log.warning("[%s] шаг %d: не понял, что нажимать дальше (url=%s)", self.site.name, step, self.page.url)
            self.dump("stuck")
            return self._finish_unknown()
        log.warning("[%s] превышено число шагов формы", self.site.name)
        return self._finish_unknown()

    def _custom_list(self, key: str) -> list[Element]:
        """Элементы, подходящие под пользовательский селектор (date/time)."""
        if key not in self.cfg.selectors:
            return []
        out: list[Element] = []
        for css in self.cfg.selectors[key]:
            try:
                self.page.locator(css).evaluate_all(
                    f"els => els.forEach(e => e.setAttribute('data-dbot-{key}', '1'))")
            except PWError:
                continue
        for e in self.collect():
            if self.page.locator(f'[data-dbot-idx="{e.idx}"][data-dbot-{key}="1"]').count() and not e.disabled:
                out.append(e)
        return out

    def _finish_unknown(self) -> Result:
        """Форма не пройдена до понятного конца: проверяем кабинет — вдруг запись всё же создана."""
        try:
            self.page.goto(self.account_url, wait_until="domcontentloaded")
            self._settle()
        except PWError:
            return Result.STUCK
        self.dump("verify")
        if self.has_active_booking():
            self.last_message = _snippet(self.body_text(), RX["has_booking"])
            log.info("[%s] в кабинете появилась запись: %s", self.site.name, self.last_message)
            return Result.BOOKED
        return Result.STUCK

    def close(self) -> None:
        try:
            self.page.close()
        except PWError:
            pass


def _snippet(text: str, rx: re.Pattern, width: int = 80) -> str:
    m = rx.search(text)
    if not m:
        return ""
    return text[max(0, m.start() - 20): m.end() + width].replace("\n", " ").strip()
