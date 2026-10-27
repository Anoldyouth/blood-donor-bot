"""HTTP-клиент сайта «Сервис записи» (donor-mos*.online, WordPress + плагин grampus).

Вход — POST /auth.php, кабинет — GET /account/, заявка — POST /wp-json/gse/form-v2/send.
Сессия живёт 30–60 с от входа, поэтому её обновляют заранее и перед отправкой.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser

import httpx

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

ROW_RE = re.compile(r'<tr[^>]*dates-table__item.*?</tr>', re.S)
DATE_RE = re.compile(r'table-item__date[^>]*>\s*(\d{2}/\d{2}/\d{4})')
FREE_RE = re.compile(r'data-src="#modal-reservation"[^>]*?data-select="([^"]*)"', re.S)
SITEKEY_RE = re.compile(r'class="smart-captcha"[^>]*data-sitekey="([^"]+)"')
TABLE_END_RE = re.compile(r'<table[^>]*needs-table.*?</table>', re.S)


class SessionExpired(Exception):
    pass


@dataclass
class Row:
    date: str                  # «30/10/2026»
    form: str | None = None    # HTML формы записи (data-select), если дата свободна
    sold_out: bool = False     # «все свободные места закончились»

    @property
    def key(self) -> tuple[int, int, int]:
        d, m, y = map(int, self.date.split("/"))
        return y, m, d


@dataclass
class Account:
    rows: list[Row]
    has_booking: bool | None = None   # None — страница прочитана не полностью
    sitekey: str = ""

    @property
    def free(self) -> list[Row]:
        return sorted((r for r in self.rows if r.form), key=lambda r: r.key)

    def row(self, date: str) -> Row | None:
        return next((r for r in self.rows if r.date == date), None)


def parse_account(page: str) -> Account | None:
    """None — в ответе нет таблицы дат (сессия слетела)."""
    if "needs-table" not in page:
        return None
    rows = []
    for tr in ROW_RE.findall(page):
        m = DATE_RE.search(tr)
        if not m:
            continue
        free = FREE_RE.search(tr)
        rows.append(Row(m.group(1), html.unescape(free.group(1)) if free else None,
                        'data-src="#modal-forbidden"' in tr))
    has_booking = None
    if "modal-list-reservation__body" in page:
        body = page.split("modal-list-reservation__body", 1)[1]
        has_booking = "cancel-entry" in body[:5000] or "not-found" not in body[:300]
    m = SITEKEY_RE.search(page)
    return Account(rows, has_booking, m.group(1) if m else "")


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields: list[tuple[str, str, str]] = []   # name, value, data-value
        self.times: list[str] = []
        self._select = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("name"):
            self.fields.append((a["name"], a.get("value") or "", a.get("data-value") or ""))
        elif tag == "select" and a.get("name"):
            self._select = a["name"]
            self.fields.append((a["name"], "", ""))
        elif tag == "option" and self._select == "time-reservation" and a.get("value") and "disabled" not in a:
            self.times.append(a["value"])

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None


@dataclass
class BookingForm:
    fields: list[tuple[str, str, str]]
    times: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, form_html: str) -> BookingForm:
        p = _FormParser()
        p.feed(form_html)
        return cls(p.fields, p.times)

    def pick_time(self, preferred: list[str]) -> str | None:
        for pref in preferred:
            if pref in self.times:
                return pref
        return self.times[0] if self.times else None

    def payload(self, time_: str, token: str) -> list[tuple[str, tuple[None, str]]]:
        """Поля в том порядке и виде, что шлёт сайт (form-v2.js + account.js)."""
        out = []
        for name, value, data_value in self.fields:
            if name == "time-reservation":
                value = time_
            elif name in ("confirm-url", "reject-url"):
                label = "Подтвердить" if name == "confirm-url" else "Отклонить"
                value = f'<a href="{data_value.replace("time=", "time=" + time_)}">{label} бронирование</a>'
            out.append((name, (None, value)))
        out += [("smart-token", (None, token)), ("formsubmit", (None, "true"))]
        return out


class Site:
    def __init__(self, name: str, url: str, login: str, password: str):
        self.name = name
        self.url = url.rstrip("/")
        self._creds = {"log": login, "pwd": password, "redirect_to": f"{self.url}/account/"}
        self.http = httpx.AsyncClient(base_url=self.url, timeout=15, headers={
            "User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"})
        self.logged_in_at = 0.0

    async def close(self) -> None:
        await self.http.aclose()

    @property
    def session_age(self) -> float:
        return time.time() - self.logged_in_at

    async def login(self) -> None:
        r = await self.http.post("/auth.php", data=self._creds)
        if r.status_code != 302 or not any(c.startswith("wordpress_logged_in") for c in self.http.cookies):
            raise RuntimeError(f"[{self.name}] вход не удался: HTTP {r.status_code}")
        self.logged_in_at = time.time()

    async def account(self, full: bool = False) -> Account:
        """Кабинет. full=False — читает ответ только до конца таблицы дат (≈в 2 раза быстрее)."""
        if full:
            page = (await self.http.get("/account/")).text
        else:
            page = ""
            async with self.http.stream("GET", "/account/", headers={"Cache-Control": "no-cache"}) as r:
                async for chunk in r.aiter_text():
                    page += chunk
                    if "</table>" in page[-len(chunk) - 8:] and TABLE_END_RE.search(page):
                        break
        acc = parse_account(page)
        if acc is None:
            raise SessionExpired
        return acc

    async def send(self, form: BookingForm, time_: str, token: str) -> tuple[int, dict]:
        r = await self.http.post("/wp-json/gse/form-v2/send", files=form.payload(time_, token), headers={
            "X-Requested-With": "XMLHttpRequest", "Referer": f"{self.url}/account/",
            "Accept": "application/json, text/javascript, */*; q=0.01"})
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:300]}
        return r.status_code, body if isinstance(body, dict) else {"raw": body}
