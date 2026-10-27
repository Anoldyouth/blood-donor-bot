"""Сценарий для реального движка donor-mos*.online (таблица «Мастер записи» + модалка времени)."""
from __future__ import annotations

import html
import http.server
import re
import threading
import time

import pytest

from donor_bot.site import Result
from tests.test_flow import browser, make_cfg, pw, run_attempt  # noqa: F401  (фикстуры)

SELECT = html.escape(
    '<select name="time-reservation"><option value="">Выберите время</option>'
    '<option value="09:00">09:00</option><option value="09:30">09:30</option></select>'
    '<input type="hidden" name="confirm-url" data-value="x?time=">', quote=True)

STATE = {"free": True, "entry": False, "sent": None, "sends": 0, "expired_sends": 0, "rejected": 0,
         "logged_out": False, "fast_logins": 0, "token": None, "confirm": None, "sent_at": 0.0}

CAPTCHA_FRAME = """<html><body><button id="js-button" role="checkbox" onclick="
  parent.postMessage({type: 'smart-token', token: 'tok-box-' + Date.now()}, '*')">Я не робот</button></body></html>"""


def account_page() -> str:
    if STATE["free"]:
        btn = (f'<button class="btn-primary" data-fancybox data-src="#modal-reservation" '
               f'data-select="{SELECT}">Забронировать время</button>')
    else:
        btn = '<button class="btn-primary" data-fancybox data-src="#modal-forbidden">Забронировать время</button>'
    entry = ('<div class="item">22/10/2026 09:00 <button class="cancel-entry">Отменить запись</button></div>'
             if STATE["entry"] else '<div class="not-found">К сожалению, на данный момент у Вас нет записей</div>')
    return f"""<html><body>
<button class="btn-top" data-src="#modal-list-reservation">Список моих записей</button>
<a href="/logout" class="btn-primary logout">Выйти</a>
<div id="modal-list-reservation" style="display:none"><div class="modal-list-reservation__body">{entry}</div></div>
<table class="needs-table"><tbody>
<tr class="dates-table__item"><td class="table-item__date">21/10/2026</td><td></td><td class="table-item__btn">-</td></tr>
<tr class="dates-table__item"><td class="table-item__date">23/10/2026</td><td></td><td class="table-item__btn">{btn}</td></tr>
<tr class="dates-table__item"><td class="table-item__date">22/10/2026</td><td></td><td class="table-item__btn">{btn}</td></tr>
</tbody></table>
<div id="modal-reservation" style="display:none"><div class="modal-reservation__wrapper form-v2">
  <div class="modal-reservation__inputs"></div>
  <div class="smart-captcha" data-sitekey="ysc1_fake"><iframe src="/captcha/checkbox?hideChallengeContainer=false"
    width="300" height="60"></iframe><input type="hidden" name="smart-token" value=""></div>
  <button class="btn-primary" form-send>Отправить</button></div></div>
<p>При отмене записи (кнопка «отменить запись»), восстановление невозможно.</p>
<script>
window.smartCaptcha = {{reset: () => {{ document.querySelector('input[name=smart-token]').value = ''; }}}};
window.addEventListener('message', e => {{
  if (e.data && e.data.type === 'smart-token') document.querySelector('input[name=smart-token]').value = e.data.token;
}});
document.querySelectorAll('[data-src="#modal-reservation"]').forEach(b => b.onclick = () => {{
  document.querySelector('.modal-reservation__inputs').innerHTML = b.getAttribute('data-select');
  document.getElementById('modal-reservation').style.display = 'block';
}});
document.querySelector('[form-send]').onclick = () => {{
  const t = document.querySelector('[name=time-reservation]').value;
  fetch('/wp-json/gse/form-v2/send', {{method: 'POST', body: 'time=' + t}}).then(r => {{
    if (r.ok) location.href = '/account/?message=sent&status=true';
  }});
}};
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        if self.path.startswith("/auth.php"):  # вход: как donor-mos*.online — 302 в кабинет
            STATE["logged_out"] = False
            STATE["fast_logins"] += 1
            self.send_response(302)
            self.send_header("Location", "/account/")
            self.end_headers()
            return
        if STATE["expired_sends"] > 0 or STATE["logged_out"]:  # сессия слетела: как donor-mos.online — 401 rest_not_logged_in
            STATE["expired_sends"] -= 1
            STATE["rejected"] += 1
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"code":"rest_not_logged_in"}')
            return
        if 'name="time-reservation"' in data:  # multipart — как отправляет сайт (и быстрый режим)
            field = lambda n: (re.search(rf'name="{n}"\s+(.*?)\s*--', data, re.S) or [None, None])[1]
            STATE["sent"], STATE["token"], STATE["confirm"] = (
                field("time-reservation"), field("smart-token"), field("confirm-url"))
        else:
            STATE["sent"] = data.rsplit("time=", 1)[-1]
        STATE["sent_at"] = time.time()
        STATE["sends"] += 1
        STATE["entry"] = True
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":true}')

    def do_GET(self):
        if self.path.startswith("/captcha/checkbox"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(CAPTCHA_FRAME.encode())
            return
        if self.path.startswith("/wp-json/"):  # REST закрыт для незалогиненных
            self.send_response(401 if STATE["logged_out"] else 200)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        body = account_page().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def grampus():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def reset_state():
    STATE.update(free=True, entry=False, sent=None, sends=0, expired_sends=0, rejected=0,
                 logged_out=False, fast_logins=0, token=None, confirm=None, sent_at=0.0)


def test_grampus_books_earliest_date_preferred_time(grampus, browser, tmp_path):
    cfg = make_cfg(grampus, tmp_path, preferred_times=["09:30"])
    result, client = run_attempt(cfg, browser)
    assert result == Result.BOOKED
    assert STATE["sent"] == "09:30"
    assert client.last_message.startswith("22/10/2026 09:30")


def test_grampus_dry_run_does_not_send(grampus, browser, tmp_path):
    result, _ = run_attempt(make_cfg(grampus, tmp_path, dry_run=True), browser)
    assert result == Result.DRY_RUN
    assert STATE["sent"] is None


def test_grampus_forbidden_is_no_button(grampus, browser, tmp_path):
    STATE["free"] = False
    result, _ = run_attempt(make_cfg(grampus, tmp_path), browser)
    assert result == Result.NO_BUTTON


def test_grampus_existing_entry(grampus, browser, tmp_path):
    STATE["entry"] = True
    result, _ = run_attempt(make_cfg(grampus, tmp_path), browser)
    assert result == Result.ALREADY_BOOKED


def test_grampus_send_lock_prevents_second_request(grampus, browser, tmp_path):
    cfg = make_cfg(grampus, tmp_path, send_lock_wait_sec=1)
    (tmp_path / "send.lock").write_text("pid 1 другой процесс", encoding="utf-8")
    result, client = run_attempt(cfg, browser)
    assert result == Result.NO_BUTTON
    assert STATE["sent"] is None
    assert "другой процесс" in client.last_message


def test_grampus_parallel_sites_send_only_one_request(grampus, tmp_path):
    from donor_bot.config import Site
    from donor_bot.scheduler import run_window
    from donor_bot.state import State

    cfg = make_cfg(grampus, tmp_path, sites=[Site("a", grampus), Site("b", grampus), Site("c", grampus)],
                   parallel_sites=True)
    result = run_window(cfg, State(cfg.state_file), window_min=0.2, mode="once")
    assert result == Result.BOOKED
    assert STATE["sends"] == 1


def test_grampus_relogins_and_resends_after_401(grampus, browser, tmp_path):
    STATE["expired_sends"] = 1
    result, client = run_attempt(make_cfg(grampus, tmp_path, preferred_times=["09:30"]), browser)
    assert result == Result.BOOKED, client.last_message
    assert STATE["rejected"] == 1 and STATE["sends"] == 1
    assert STATE["sent"] == "09:30"
    assert not (tmp_path / "send.lock").exists() or "fake" in (tmp_path / "send.lock").read_text(encoding="utf-8")


def test_grampus_expired_session_fast_relogin_before_send(grampus, browser, tmp_path):
    # 23.10: сессия умерла, пока решалась капча, — отправка без нового входа получила бы 401
    STATE["logged_out"] = True
    result, client = run_attempt(make_cfg(grampus, tmp_path, preferred_times=["09:30"]), browser)
    assert result == Result.BOOKED, client.last_message
    assert STATE["fast_logins"] >= 1
    assert STATE["rejected"] == 0 and STATE["sends"] == 1 and STATE["sent"] == "09:30"


def test_grampus_twice_401_is_not_booked_and_releases_lock(grampus, browser, tmp_path):
    STATE["expired_sends"] = 2
    result, _ = run_attempt(make_cfg(grampus, tmp_path), browser)
    assert result != Result.BOOKED
    assert STATE["sends"] == 0 and STATE["rejected"] == 2
    assert not (tmp_path / "send.lock").exists()


def test_grampus_waits_for_lock_released_by_failed_site(grampus, browser, tmp_path):
    lock = tmp_path / "send.lock"
    lock.write_text("pid 1 другой сайт, отправка не прошла", encoding="utf-8")
    threading.Timer(1.5, lock.unlink).start()
    result, client = run_attempt(make_cfg(grampus, tmp_path, send_lock_wait_sec=10), browser)
    assert result == Result.BOOKED, client.last_message
    assert STATE["sends"] == 1


# ------------------------------------------------------------ быстрый режим (donor_bot/fast.py)
def run_fast(cfg):
    from donor_bot.scheduler import run_window
    from donor_bot.state import State
    out = []  # в своём потоке: в этом уже работает sync Playwright фикстуры browser
    t = threading.Thread(target=lambda: out.append(
        run_window(cfg, State(cfg.state_file), window_min=0.15, mode="once")))
    t.start()
    t.join()
    return out[0] if out else "упал"


def test_fast_books_with_prefetched_token(grampus, tmp_path):
    result = run_fast(make_cfg(grampus, tmp_path, preferred_times=["09:30"]))
    assert result == Result.BOOKED
    assert STATE["sends"] == 1 and STATE["sent"] == "09:30"
    assert STATE["token"].startswith("tok-box-")
    assert STATE["confirm"] == '<a href="x?time=09:30">Подтвердить бронирование</a>'


def test_fast_slot_appearing_later_is_sent_within_seconds(grampus, tmp_path):
    STATE["free"] = False
    opened = []
    threading.Timer(3, lambda: (STATE.update(free=True), opened.append(time.time()))).start()
    result = run_fast(make_cfg(grampus, tmp_path, poll_interval_sec=0.3))
    assert result == Result.BOOKED
    assert STATE["sends"] == 1 and STATE["sent"] == "09:00"
    print(f"slot->send: {STATE['sent_at'] - opened[0]:.2f} s")
    assert STATE["sent_at"] - opened[0] < 2.5


def test_fast_expired_session_relogins_without_401(grampus, tmp_path):
    STATE["logged_out"] = True
    result = run_fast(make_cfg(grampus, tmp_path))
    assert result == Result.BOOKED
    assert STATE["fast_logins"] >= 1 and STATE["rejected"] == 0 and STATE["sends"] == 1


def test_fast_401_resends_once_more(grampus, tmp_path):
    STATE["expired_sends"] = 1
    result = run_fast(make_cfg(grampus, tmp_path))
    assert result == Result.BOOKED
    assert STATE["rejected"] == 1 and STATE["sends"] == 1


def test_fast_existing_entry_sends_nothing(grampus, tmp_path):
    STATE["entry"] = True
    assert run_fast(make_cfg(grampus, tmp_path)) == Result.ALREADY_BOOKED
    assert STATE["sends"] == 0


def test_fast_dry_run_sends_nothing(grampus, tmp_path):
    assert run_fast(make_cfg(grampus, tmp_path, dry_run=True)) == Result.DRY_RUN
    assert STATE["sends"] == 0
