"""Работа с одним сайтом записи: вход, поиск кнопок записи, прохождение формы.

Структура страниц сайтов заранее неизвестна, поэтому бот работает эвристически:
собирает все видимые кликабельные элементы, читает их текст и выбирает нужные
по регулярным выражениям (вид донации, дата, время, кнопка подтверждения).
Любой шаг можно переопределить CSS-селекторами в config.yaml -> selectors.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from playwright.sync_api import BrowserContext, Error as PWError, Page, TimeoutError as PWTimeout

import hashlib
from datetime import datetime

from .archive import Learned, RunArchive
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


GRAMPUS = {
    "table": "table.needs-table",
    "row": "table.needs-table tr.dates-table__item",
    "date": "td.table-item__date",
    "free_btn": 'td.table-item__btn [data-src="#modal-reservation"]',
    "forbidden_btn": 'td.table-item__btn [data-src="#modal-forbidden"]',
    "time_select": 'select[name="time-reservation"]:visible',
    "send": ".modal-reservation__wrapper [form-send]:visible",
    "my_list": "#modal-list-reservation .modal-list-reservation__body",
    "my_entry": "#modal-list-reservation .modal-list-reservation__body > :not(.not-found), "
                "#modal-list-reservation .cancel-entry",
}

# Вход без навигации: окно записи и токен капчи остаются на странице. Сессия на сайтах живёт
# 30–60 с от входа, а при ошибке отправки сайт сам уводит страницу на ?status=false,
# поэтому перед «Отправить» входим заново этим запросом (~1 с) и проверяем REST (/wp-json/).
FAST_LOGIN_JS = """async ([log, pwd, to, verify]) => {
  const r = await fetch('/auth.php', {method: 'POST', credentials: 'include', redirect: 'manual',
                                      body: new URLSearchParams({log, pwd, redirect_to: to})});
  if (!verify) return r.type === 'opaqueredirect' ? 200 : r.status;
  return (await fetch('/wp-json/', {credentials: 'include'})).status;
}"""

SEND_LOCK_TTL_SEC = 30 * 60


def _send_lock_path(cfg: Config) -> Path:
    return Path(cfg.state_file).parent / "send.lock"


def acquire_send_lock(cfg: Config, owner: str) -> str | None:
    """Одна заявка на все процессы: второй отправитель заблокировал бы аккаунт.

    None — блокировка взята; иначе строка о том, кто её держит."""
    path = _send_lock_path(cfg)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > SEND_LOCK_TTL_SEC:
                    path.unlink()
                    continue
                return path.read_text(encoding="utf-8").strip() or "?"
            except OSError:
                return "?"
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"pid {os.getpid()} {datetime.now():%H:%M:%S} {owner}")
        return None
    return "?"


def release_send_lock(cfg: Config) -> None:
    try:
        _send_lock_path(cfg).unlink()
    except OSError:
        pass


RX = {
    "grampus_error": re.compile(r"ошибк|не удалось|отклон|заблокир|недоступн|попробуйте", re.I),
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
    "day_num": re.compile(r"^\s*\d{1,2}\s*$"),
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
  const cssEsc = v => (window.CSS && CSS.escape) ? CSS.escape(v) : String(v).replace(/([^\w-])/g, '\\$1');
  const STATE_RX = /^(active|selected|checked|current|chosen|disabled|inactive|hover|focus|open|show|hidden|visible|busy|past|unavailable|today)$/i;
  const part = (el, withIndex) => {
    if (el.id && !/\d{3,}/.test(el.id)) return '#' + cssEsc(el.id);
    let p = el.tagName.toLowerCase();
    const name = el.getAttribute('name');
    if (name) p += '[name="' + name + '"]';
    const type = el.getAttribute('type');
    if (el.tagName === 'INPUT' && type) p += '[type="' + type + '"]';
    const classes = Array.from(el.classList).filter(c => !STATE_RX.test(c) && !/\d{3,}/.test(c)).slice(0, 3);
    if (classes.length) p += '.' + classes.map(cssEsc).join('.');
    if (withIndex && el.parentElement) {
      const sib = Array.from(el.parentElement.children).filter(c => c.tagName === el.tagName);
      if (sib.length > 1) p += ':nth-of-type(' + (sib.indexOf(el) + 1) + ')';
    }
    return p;
  };
  const build = (el, general) => {
    // general=true: без порядковых индексов — селектор на весь список (все дни, все слоты)
    let cur = el, parts = [], depth = 0;
    while (cur && cur.nodeType === 1 && cur !== document.body && depth < 6) {
      const p = part(cur, !general);
      parts.unshift(p);
      const sel = parts.join(' > ');
      if (p.startsWith('#')) return sel;
      try {
        const n = document.querySelectorAll(sel).length;
        if (!general && n === 1) return sel;
        if (general && n >= 1 && n <= 60 && (depth >= 1 || /[.#\[]/.test(p))) return sel;
      } catch (e) {}
      cur = cur.parentElement; depth++;
    }
    return parts.join(' > ');
  };
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
    const inCal = !!el.closest('[class*=calendar i], [class*=datepicker i], [class*=picker i], table');
    out.push({idx: i, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
              text: it.text.slice(0, 200), cls: it.cls.slice(0, 200), disabled, selected,
              href: el.getAttribute('href') || '', name: el.getAttribute('name') || '',
              value: (el.getAttribute('value') || '').slice(0, 100),
              sel: build(el, false), gsel: build(el, true), inCal});
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
    sel: str = ""
    gsel: str = ""
    inCal: bool = False
    source: str = "heuristic"   # heuristic | custom | learned — как элемент был найден

    @property
    def is_input_like(self) -> bool:
        return self.tag in ("a", "button", "input") or "btn" in self.cls or "button" in self.cls


class SiteClient:
    def __init__(self, cfg: Config, site: Site, context: BrowserContext, archive: RunArchive | None = None):
        self.cfg = cfg
        self.site = site
        self.context = context
        self.page: Page = context.new_page()
        self.page.set_default_timeout(cfg.timeout_ms)
        self.archive = archive
        self.dump_dir = archive.site_dir(site.name) if archive else None
        self.dump_counter = 0
        self._last_dump_hash: dict[str, str] = {}
        self.learned = Learned(cfg.learned_dir / f"{site.name}.json") if cfg.use_learned else None
        if self.learned and self.learned.data:
            log.info("[%s] использую рецепт из %s (%s)", site.name, self.learned.path,
                     self.learned.data.get("saved_at", ""))
        self.trace: list[dict] = []
        self.attempt_no = 0
        self._last_custom: tuple[str, str, str] | None = None
        self.type_rx = re.compile(cfg.donation_type_regex, re.I)
        self.last_message = ""
        self.captcha = CaptchaSolver(cfg, site.name)
        self.login_failed_reason = Result.LOGIN_FAILED
        self.logged_in_at = 0.0  # time.time() последнего входа; 0 — неизвестно
        self.picked_time: str | None = None
        if self.dump_dir:
            self.page.on("request", self._log_request)
            self.page.on("response", self._log_response)

    # ---------------------------------------------------- журнал AJAX-запросов
    NET_RX = re.compile(r"admin-ajax|wp-json|form-v2|reserv|booking", re.I)

    def _net_write(self, entry: dict) -> None:
        entry["at"] = datetime.now().isoformat(timespec="milliseconds")
        with open(self.dump_dir / "network.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    SECRET_RX = re.compile(r"((?:pwd|pass\w*|user_password\w*)=)[^&\s]*", re.I)

    def _net_wanted(self, req) -> bool:
        if req.resource_type not in ("xhr", "fetch", "document"):
            return False
        return req.method != "GET" or req.resource_type != "document" and bool(self.NET_RX.search(req.url))

    def _log_request(self, req) -> None:
        if not self._net_wanted(req):
            return
        try:
            post = req.post_data
            if post and self.cfg.password:
                post = self.SECRET_RX.sub(r"\1***", post).replace(self.cfg.password, "***")
            self._net_write({"dir": "req", "method": req.method, "url": req.url,
                             "headers": {k: v for k, v in req.headers.items() if k.lower() != "cookie"},
                             "post": post})
        except Exception:  # noqa: BLE001
            pass

    def _log_response(self, resp) -> None:
        req = resp.request
        if not self._net_wanted(req):
            return
        try:
            body = resp.text()[:20000]
        except Exception:  # noqa: BLE001
            body = None
        try:
            self._net_write({"dir": "resp", "method": req.method, "url": resp.url, "status": resp.status,
                             "body": body})
        except Exception:  # noqa: BLE001
            pass

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

    def record(self, role: str, el: Element | None = None, selector: str = "", source: str | None = None) -> None:
        entry = {"time": datetime.now().isoformat(timespec="milliseconds"), "role": role,
                 "source": source or (el.source if el is not None else "heuristic"), "url": self.page.url}
        if el is not None:
            entry.update({"tag": el.tag, "text": el.text[:120], "cls": el.cls[:120], "sel": el.sel, "gsel": el.gsel})
        if selector:
            entry["sel"] = selector
        self.trace.append(entry)

    def click(self, el: Element, why: str, role: str = "") -> None:
        log.info("[%s] клик: %s <%s> «%s»  [%s]", self.site.name, why, el.tag, el.text[:60], el.sel)
        self.record(role or why, el)
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
        try:
            html = self.page.content()
        except PWError:
            return
        digest = hashlib.sha1(html.encode("utf-8", "ignore")).hexdigest()
        if self._last_dump_hash.get(label) == digest:
            return  # страница не изменилась с прошлого дампа этого шага — не засоряем архив
        self._last_dump_hash[label] = digest
        self.dump_counter += 1
        base = self.dump_dir / f"{self.dump_counter:02d}_{label}"
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        base.with_suffix(".html").write_text(html, encoding="utf-8")
        try:
            self.page.screenshot(path=str(base) + ".png", full_page=True)
        except PWError as exc:
            log.debug("screenshot failed: %s", exc)
        try:
            els = [e.__dict__ for e in self.collect()]
            selects = self.page.evaluate(SELECTS_JS)
            (base.with_suffix(".json")).write_text(
                json.dumps({"url": self.page.url, "elements": els, "selects": selects},
                           ensure_ascii=False, indent=1), encoding="utf-8")
        except PWError as exc:
            log.debug("elements dump failed: %s", exc)
        log.info("[%s] дамп сохранён: %s.*", self.site.name, base)

    ROLE_RX = {"book_button": "book", "confirm": "confirm", "login_submit": "login_submit"}

    def _role_text_ok(self, key: str, text: str) -> bool:
        if key == "donation_type":
            return bool(self.type_rx.search(text))
        rx = self.ROLE_RX.get(key)
        if rx is None:
            return True
        return bool(RX[rx].search(text)) and not RX["cancel"].search(text)

    def custom(self, key: str):
        """Локатор по селектору из config.yaml (приоритет) или из выученного рецепта."""
        sources = [("custom", css) for css in self.cfg.selectors.get(key, [])]
        if self.learned:
            sources += [("learned", css) for css in self.learned.selectors(key)]
        for source, css in sources:
            try:
                l = self.page.locator(css).first
                if not l.count() or not l.is_visible():
                    continue
                if source == "learned":
                    text = (l.inner_text(timeout=500) or l.get_attribute("value") or "").strip()
                    if not self._role_text_ok(key, text):
                        log.debug("[%s] learned %s=%s: текст «%s» не подходит", self.site.name, key, css, text)
                        continue
                self._last_custom = (key, css, source)
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
                    self.click(el, "ссылка входа", role="login_link")
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
        self.logged_in_at = time.time()
        return True

    def fast_relogin(self, verify: bool = True) -> bool:
        """Вход запросом из текущей страницы, без перехода. True — REST сайта снова отвечает 200.

        verify=False — без проверки REST (−0,6 с): прямо перед отправкой, 401 всё равно поймаем."""
        t = time.time()
        try:
            status = self.page.evaluate(FAST_LOGIN_JS, [self.cfg.login, self.cfg.password, self.account_url, verify])
        except PWError as exc:
            log.warning("[%s] быстрый вход: ошибка %s", self.site.name, str(exc).splitlines()[0])
            return False
        ok = status == 200
        log.info("[%s] быстрый вход за %.1f с: %s", self.site.name, time.time() - t,
                 "ок" if ok else f"REST ответил {status}")
        if ok:
            self.logged_in_at = time.time()
        return ok

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
        if self.password_visible():
            return False  # форма входа: в правилах есть «отменить запись» — это не запись
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
                        el.source = self._last_custom[2] if self._last_custom else "custom"
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
                self.click(e, "чекбокс согласия", role="consent")
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
        self.trace = []
        self.attempt_no += 1
        try:
            result = self._attempt()
        except PWError as exc:
            log.warning("[%s] ошибка браузера: %s", self.site.name, str(exc).splitlines()[0])
            self.dump("error")
            result = Result.ERROR
        self._finish_attempt(result)
        return result

    def _finish_attempt(self, result: Result) -> None:
        if self.archive:
            self.archive.record(self.site.name, result.value, self.last_message)
            if self.trace:
                path = self.dump_dir / f"trace_{self.attempt_no:02d}.json"
                path.write_text(json.dumps({
                    "site": self.site.name, "result": result.value, "message": self.last_message,
                    "finished": datetime.now().isoformat(timespec="seconds"), "steps": self.trace,
                }, ensure_ascii=False, indent=1), encoding="utf-8")
                log.info("[%s] журнал действий: %s", self.site.name, path)
        if self.learned is not None and result in (Result.BOOKED, Result.DRY_RUN) and self.trace:
            self.learned.save(Learned.from_trace(self.site.name, self.trace, result.value, self.account_url))

    def _attempt(self) -> Result:
        if not self.ensure_logged_in():
            return self.login_failed_reason
        self.page.goto(self.account_url, wait_until="domcontentloaded")
        self._settle()
        if self.password_visible():
            log.info("[%s] сессия слетела — вхожу заново", self.site.name)
            if self.fast_relogin():
                self.page.goto(self.account_url, wait_until="domcontentloaded")
                self._settle()
            if self.password_visible():
                if not self.login():
                    return self.login_failed_reason
                self.page.goto(self.account_url, wait_until="domcontentloaded")
                self._settle()
        self.dump("account")

        if self.page.locator(GRAMPUS["table"]).count():
            return self._attempt_grampus()

        if self.has_active_booking():
            self.last_message = _snippet(self.body_text(), RX["has_booking"])
            log.info("[%s] в кабинете уже есть запись: %s", self.site.name, self.last_message)
            return Result.ALREADY_BOOKED

        els = self.collect()
        buttons = self.booking_buttons(els)
        if not buttons:
            log.info("[%s] кнопок записи нет (всего кликабельных элементов: %d)", self.site.name, len(els))
            return Result.NO_BUTTON

        self.click(buttons[0], "кнопка записи", role="book_button")
        return self._walk_form()

    # ------------------------------------------- движок «Сервис записи» (grampus)
    def _attempt_grampus(self) -> Result:
        """Сайты Центра крови: таблица «Мастер записи» → модалка с <select> времени → «Отправить».

        Разметка (donor-mos*.online):
          tr.dates-table__item > td.table-item__date «22/10/2026» + td.table-item__btn
          кнопка свободной даты: button[data-src="#modal-reservation"][data-select="<select name=time-reservation>…"]
          мест нет: button[data-src="#modal-forbidden"]; дата не для записи: «-»
          заявки: #modal-list-reservation (.not-found — записей нет, .cancel-entry — есть)
          успех: редирект на ?message=Вы успешно отправили заявку…&status=true
        """
        g = GRAMPUS
        if self.page.locator(g["my_entry"]).count():
            self.last_message = " ".join(self.page.locator(g["my_list"]).first.inner_text().split())[:200]
            log.info("[%s] в кабинете уже есть заявка: %s", self.site.name, self.last_message)
            return Result.ALREADY_BOOKED

        rows = self.page.locator(g["row"])
        slots: list[tuple[str, object]] = []
        forbidden = 0
        for i in range(rows.count()):
            row = rows.nth(i)
            day = row.locator(g["date"]).first.inner_text().strip()
            if row.locator(g["free_btn"]).count():
                slots.append((day, row.locator(g["free_btn"]).first))
            elif row.locator(g["forbidden_btn"]).count():
                forbidden += 1
        if not slots:
            log.info("[%s] свободных дат нет (дат в таблице: %d, «места закончились»: %d)",
                     self.site.name, rows.count(), forbidden)
            return Result.NO_BUTTON

        def day_key(item):
            d, m, y = (item[0].split("/") + ["0", "0", "0"])[:3]
            return (int(y or 0), int(m or 0), int(d or 0))
        slots.sort(key=day_key)
        day, _ = slots[-1] if self.cfg.date_preference == "latest" else slots[0]
        log.info("[%s] свободные даты: %s → беру %s", self.site.name, ", ".join(s[0] for s in slots), day)
        if self.dump_dir:
            for d, b in slots:
                raw = b.get_attribute("data-select") or ""
                (self.dump_dir / f"slot_{d.replace('/', '-')}.html").write_text(
                    b.evaluate("e => e.outerHTML") + "\n\n<!-- data-select -->\n" + raw, encoding="utf-8")
        failed = self._grampus_fill(day)
        if failed:
            return failed
        self.dump("form_filled")

        if not self.pass_captcha():
            return Result.CAPTCHA_FAILED
        if self.cfg.dry_run:
            log.info("[%s] DRY RUN: «Отправить» не нажимаю (%s)", self.site.name, self.last_message)
            return Result.DRY_RUN
        token = self.captcha.detect(self.page)["token"]

        holder = self._wait_send_lock()
        if holder:
            log.warning("[%s] заявку уже отправляет другой процесс (%s) — не отправляю вторую",
                        self.site.name, holder)
            self.last_message = f"параллельная отправка: {holder}"
            return Result.NO_BUTTON
        result = Result.STUCK
        try:
            # Сессия живёт 30–60 с от входа и активностью не продлевается, а капча с заданием
            # решается 30–60 с: перед отправкой входим заново запросом, не трогая открытое окно.
            # Если так не вышло — полный вход, форма заново, токен капчи переносим в неё.
            age = time.time() - self.logged_in_at
            if age > self.cfg.session_ttl_sec and not self.fast_relogin():
                log.info("[%s] сессии %.0f с — полный перелогин перед отправкой", self.site.name, age)
                failed = self._grampus_relogin(day, token)
                if failed:
                    result = failed
                    return result
            status = self._grampus_send()
            if status == 401:
                log.warning("[%s] сайт ответил 401 (сессия слетела) — перелогин и повторная отправка",
                            self.site.name)
                failed = self._grampus_relogin(day, token)
                if failed:
                    result = failed
                    return result
                status = self._grampus_send()
            result = self._grampus_result(status)
            return result
        finally:
            if result != Result.BOOKED:
                release_send_lock(self.cfg)

    def _grampus_fill(self, day: str) -> Result | None:
        """Открывает окно записи на дату day и выбирает время. None — форма заполнена."""
        g = GRAMPUS
        btn = None
        rows = self.page.locator(g["row"])
        for i in range(rows.count()):
            row = rows.nth(i)
            if row.locator(g["date"]).first.inner_text().strip() == day:
                btn = row.locator(g["free_btn"]).first
                break
        if btn is None or not btn.count():
            self.last_message = f"{day}: мест уже нет"
            log.info("[%s] %s", self.site.name, self.last_message)
            return Result.NO_SLOTS
        self.record("date", None, g["free_btn"], "grampus")
        btn.click()

        select = self.page.locator(g["time_select"])
        try:
            select.first.wait_for(state="visible", timeout=10000)
        except PWError:
            self.dump("no_time_select")
            self.last_message = f"{day}: окно выбора времени не появилось"
            return Result.STUCK
        options = [o for o in select.first.evaluate(
            "s => [...s.options].map(o => ({value: o.value, text: o.text.trim(), disabled: o.disabled}))")
            if o["value"] and not o["disabled"]]
        if not options:
            self.dump("no_times")
            self.last_message = f"{day}: нет свободного времени"
            return Result.NO_SLOTS
        pick = options[0]
        # при повторном заполнении держимся уже выбранного времени
        for pref in ([self.picked_time] if self.picked_time else []) + self.cfg.preferred_times:
            hit = next((o for o in options if o["text"].startswith(pref) or o["value"].startswith(pref)), None)
            if hit:
                pick = hit
                break
        select.first.select_option(pick["value"])
        self.record("time", None, g["time_select"], "grampus")
        log.info("[%s] %s: время %s (из %s)", self.site.name, day, pick["text"],
                 ", ".join(o["text"] for o in options))
        self.picked_time = pick["value"]
        self.last_message = f"{day} {pick['text']}"
        return None

    def _grampus_relogin(self, day: str, token: str) -> Result | None:
        """Новый вход, заново дата/время и уже полученный токен капчи.

        Сначала быстрый вход запросом; не вышло — cookie сайта удаляются и вход через форму."""
        if not self.fast_relogin():
            host = re.sub(r"^https?://", "", self.site.base).split("/")[0].split(":")[0]
            self.context.clear_cookies(domain=host)
            if not self.login():
                return self.login_failed_reason
        self.page.goto(self.account_url, wait_until="domcontentloaded")
        self._settle()
        failed = self._grampus_fill(day)
        if failed:
            return failed
        if token:
            if not self.captcha.inject_token(self.page, token):
                return Result.CAPTCHA_FAILED
        elif not self.pass_captcha():
            return Result.CAPTCHA_FAILED
        self.dump("form_refilled")
        return None

    def _grampus_send(self) -> int | None:
        """Жмёт «Отправить»; HTTP-статус /form-v2/send или None, если запроса не было."""
        g = GRAMPUS
        try:
            with self.page.expect_response(lambda r: "form-v2/send" in r.url, timeout=30000) as info:
                self.page.locator(g["send"]).first.click()
                self.record("final_confirm", None, g["send"], "grampus")
            status = info.value.status
        except PWError:
            return None
        log.info("[%s] ответ на отправку: %s", self.site.name, status)
        return status

    def _grampus_result(self, status: int | None) -> Result:
        if status is not None and status >= 400:
            self.dump("after_send")
            self.last_message = f"{self.last_message}: сайт ответил {status}"
            log.warning("[%s] %s", self.site.name, self.last_message)
            return self._grampus_verify()
        try:
            self.page.wait_for_url(re.compile(r"[?&]status="), timeout=15000)
        except PWError:
            self.dump("after_send")
            return self._grampus_verify()
        self._settle()
        self.dump("after_send")
        m = re.search(r"[?&]status=(\w+)", self.page.url)
        text = " ".join(self.body_text().split())
        if m and m.group(1) == "true":
            self.last_message = f"{self.last_message}. Заявка отправлена, ждёт подтверждения врача"
            return Result.BOOKED
        self.last_message = (f"{self.last_message}: сайт отклонил заявку — "
                             f"{_snippet(text, RX['grampus_error'])}")
        log.warning("[%s] %s", self.site.name, self.last_message)
        return Result.ERROR

    def _wait_send_lock(self) -> str | None:
        """Берёт send.lock; если его держит другой сайт — ждёт: его отправка могла не пройти."""
        owner = f"{self.site.name} {self.last_message}"
        end = time.time() + self.cfg.send_lock_wait_sec
        holder = acquire_send_lock(self.cfg, owner)
        if holder:
            log.info("[%s] send.lock занят (%s) — жду до %.0f с", self.site.name, holder,
                     self.cfg.send_lock_wait_sec)
        while holder and time.time() < end:
            time.sleep(1)
            holder = acquire_send_lock(self.cfg, owner)
        return holder

    def _grampus_verify(self) -> Result:
        """Редиректа не было: проверяем «Список моих записей»."""
        self.page.goto(self.account_url, wait_until="domcontentloaded")
        self._settle()
        self.dump("verify")
        if self.page.locator(GRAMPUS["my_entry"]).count():
            return Result.BOOKED
        return Result.STUCK

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
                    self.record("donation_type", selector=self._last_custom[1], source=self._last_custom[2])
                    log.info("[%s] клик: вид донации (%s: %s)", self.site.name, *self._last_custom[2:0:-1])
                    c.click()
                    self._settle()
                    clicked_type = True
                    continue
                typed = [e for e in els
                         if self.type_rx.search(e.text) and not e.disabled and not e.selected
                         and len(e.text) <= (40 if e.tag in ("li", "td", "label") else 80)]
                if typed:
                    self.click(typed[0], "вид донации", role="donation_type")
                    clicked_type = True
                    continue

            # 2. Дата
            dates = self._custom_list("date") or [
                e for e in els if not e.disabled and len(e.text) <= 30 and (
                    RX["date_text"].match(e.text)
                    or (RX["day_num"].match(e.text) and (e.inCal or "day" in e.cls or "date" in e.cls)))]
            if dates and not any(e.selected for e in dates):
                self.click(self._pick_date(dates), "дата", role="date")
                continue

            # 3. Время
            times = self._custom_list("time") or [
                e for e in els if RX["time_text"].match(e.text) and not e.disabled and len(e.text) <= 20]
            if times and not any(e.selected for e in times):
                self.click(self._pick_time(times), "время", role="time")
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
                    if confirm_el is not None:
                        self.record("confirm", confirm_el)
                    else:
                        self.record("confirm", selector=self._last_custom[1], source=self._last_custom[2])
                    self.trace[-1]["clicked"] = False
                    self.dump("dry_run_stop")
                    return Result.DRY_RUN
                if confirm is not None:
                    self.record("confirm", selector=self._last_custom[1], source=self._last_custom[2])
                    log.info("[%s] клик: подтверждение «%s» (%s: %s)", self.site.name, text,
                             self._last_custom[2], self._last_custom[1])
                    confirm.click()
                    self._settle(1000)
                else:
                    self.click(confirm_el, "подтверждение", role="confirm")
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
        css_list = [(css, "custom") for css in self.cfg.selectors.get(key, [])]
        if self.learned:
            css_list += [(css, "learned") for css in self.learned.selectors(key)]
        if not css_list:
            return []
        for css, source in css_list:
            try:
                self.page.locator(css).evaluate_all(
                    f"els => els.forEach(e => e.setAttribute('data-dbot-{key}', '{source}'))")
            except PWError:
                continue
        out: list[Element] = []
        for e in self.collect():
            marked = self.page.locator(f'[data-dbot-idx="{e.idx}"][data-dbot-{key}]')
            if marked.count() and not e.disabled:
                e.source = marked.first.get_attribute(f"data-dbot-{key}") or "custom"
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
        if self.password_visible():
            # Выкинуло на форму входа: текст правил там упоминает «отменить запись» — это не запись
            return Result.STUCK
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
