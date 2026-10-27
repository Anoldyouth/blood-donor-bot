"""Живой браузер для ручного (Claude) управления одним сайтом через локальный HTTP API.

Вход выполняет код бота (логин/пароль из .env, капча — как у бота). Дальше страницей управляют
запросами на 127.0.0.1:<port>:
  GET  /state                 url, заголовок, таблица дат (кратко)
  GET  /text                  видимый текст страницы
  GET  /html?sel=CSS          outerHTML элемента (по умолчанию body)
  GET  /shot                  скриншот → путь к png
  GET  /reload                перезагрузить кабинет (и войти заново, если сессия слетела)
  GET  /relogin               быстрый вход запросом, без перехода (окно записи и капча остаются)
  GET  /watch?sel=SEC         обновлять кабинет раз в секунду до SEC секунд (по умолчанию 60), пока
                              не появится свободная дата (data-select) или незнакомая страница;
                              возвращает состояние и сырой data-select
  POST /js     (тело = JS)    выполнить выражение в странице, вернуть результат
  POST /click  (тело = CSS)   кликнуть первый видимый элемент
  POST /captcha               пройти капчу на странице (как бот)
  POST /lock   (тело = кто)   взять send.lock перед «Отправить»; 409 если занят
Все действия и сетевые AJAX/POST-запросы пишутся в runs/<время>_live/<сайт>/.

Запуск: python tools/live.py --site tsaritsyno [--port 8777]
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from donor_bot.archive import RunArchive  # noqa: E402
from donor_bot.config import load_config  # noqa: E402
from donor_bot.log import setup_logging  # noqa: E402
from donor_bot.scheduler import BrowserSession  # noqa: E402
from donor_bot.site import SiteClient, acquire_send_lock  # noqa: E402

STATE_JS = """() => ({
  url: location.href, title: document.title,
  login_form: [...document.querySelectorAll('input[type=password]')].some(e => e.offsetParent !== null),
  rows: [...document.querySelectorAll('table.needs-table tr.dates-table__item')].map(r => ({
    date: (r.querySelector('.table-item__date')||{}).innerText?.trim(),
    btn: (r.querySelector('.table-item__btn [data-src]')||{}).getAttribute?.('data-src') || r.querySelector('.table-item__btn')?.innerText.trim(),
    has_select: !!r.querySelector('[data-select]'),
  })),
  modal_open: [...document.querySelectorAll('.fancybox__container')].map(c => c.innerText.slice(0, 500)),
  my_list: document.querySelector('#modal-list-reservation .modal-list-reservation__body')?.innerText.trim().slice(0, 300),
})"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--port", type=int, default=8777)
    args = ap.parse_args()

    cfg = load_config(ROOT / "config.yaml", ROOT / ".env")
    cfg.sites = [s for s in cfg.sites if s.name == args.site]
    if not cfg.sites:
        print("нет такого сайта")
        return 2
    cfg.headless = False
    cfg.profile_dir = (cfg.profile_dir / f"live_{args.site}") if cfg.profile_dir else None
    cfg.log_file = ROOT / "live.log"
    setup_logging(cfg.log_file, False)
    site = cfg.sites[0]

    jobs: queue.Queue = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _run(self, kind, arg=""):
            done = queue.Queue()
            jobs.put((kind, arg, done))
            code, payload = done.get()
            body = json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            self._run(u.path.strip("/"), parse_qs(u.query).get("sel", ["body"])[0])

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self._run(urlparse(self.path).path.strip("/"), self.rfile.read(n).decode("utf-8"))

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    with sync_playwright() as pw:
        session = BrowserSession(pw, cfg)
        archive = RunArchive(cfg.archive_dir, "live", cfg.archive_keep_days)
        client = SiteClient(cfg, site, session.new_context(), archive=archive)
        client.ensure_logged_in()
        page = client.page
        shots = 0
        print(f"live: {site.name} готов, API http://127.0.0.1:{args.port}", flush=True)

        def act(kind, arg):
            nonlocal shots
            if kind == "state":
                return page.evaluate(STATE_JS)
            if kind == "text":
                return client.body_text()
            if kind == "html":
                l = page.locator(arg).first
                return l.evaluate("e => e.outerHTML") if l.count() else None
            if kind == "shot":
                shots += 1
                p = client.dump_dir / f"live_{shots:03d}_{datetime.now():%H%M%S}.png"
                page.screenshot(path=str(p), full_page=True)
                return str(p)
            if kind == "reload":
                page.goto(client.account_url, wait_until="domcontentloaded")
                if client.password_visible():
                    client.login()
                    page.goto(client.account_url, wait_until="domcontentloaded")
                return page.evaluate(STATE_JS)
            if kind == "relogin":
                return client.fast_relogin()
            if kind == "watch":
                import time
                limit = time.time() + float(arg if arg != "body" else 60)
                n = 0
                while True:
                    n += 1
                    st = act("reload", "")
                    free = page.locator("[data-select]")
                    if free.count():
                        st["data_select"] = [free.nth(i).get_attribute("data-select") for i in range(free.count())]
                        st["free_btn_html"] = free.first.evaluate("e => e.outerHTML")
                        client.dump("live_free_slot")
                        break
                    if not st["rows"] or st["login_form"]:
                        client.dump("live_unexpected")
                        break
                    if time.time() > limit:
                        break
                    page.wait_for_timeout(700)
                st["reloads"] = n
                return st
            if kind == "js":
                return page.evaluate(arg)
            if kind == "click":
                page.locator(arg).locator("visible=true").first.click(timeout=5000)
                page.wait_for_timeout(300)
                return page.evaluate(STATE_JS)
            if kind == "captcha":
                return client.pass_captcha()
            if kind == "lock":
                holder = acquire_send_lock(cfg, f"live {site.name} {arg}")
                if holder:
                    raise PermissionError(holder)
                return "ok"
            raise ValueError(f"неизвестная команда {kind}")

        while True:
            kind, arg, done = jobs.get()
            try:
                client._net_write({"dir": "cmd", "kind": kind, "arg": arg[:2000]})
                done.put((200, act(kind, arg)))
            except PermissionError as exc:
                done.put((409, {"error": f"send.lock занят: {exc}"}))
            except Exception as exc:  # noqa: BLE001
                done.put((500, {"error": str(exc).splitlines()[0]}))


if __name__ == "__main__":
    sys.exit(main())
