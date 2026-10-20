"""Имитация сайта записи для локального теста эвристик бота (без сети)."""
from __future__ import annotations

import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOGIN = "test@example.com"
PASSWORD = "Secret123"

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Сервис записи</title>
<style>.disabled{{color:#aaa;pointer-events:none}} .btn{{display:inline-block;padding:6px 12px;border:1px solid #333}}</style>
</head><body><h1>Сервис записи</h1>{body}
<footer><p>Бронирование записи не является активной записью, пока не будет подтверждено врачом Центра крови.</p></footer>
</body></html>"""


class FakeState:
    def __init__(self):
        self.slots_open = True
        self.booking: dict | None = None
        self.sessions: set[str] = set()
        self.confirm_count = 0


STATE = FakeState()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # тише
        pass

    def _send(self, html: str, status=HTTPStatus.OK, headers: dict | None = None):
        body = PAGE.format(body=html).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to: str, headers: dict | None = None):
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", to)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _logged_in(self) -> bool:
        cookie = self.headers.get("Cookie", "")
        return "sid=ok" in cookie

    def _form(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n).decode("utf-8") if n else ""
        return {k: v[0] for k, v in parse_qs(data).items()}

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path.rstrip("/") or "/"

        if path == "/admin/open":
            STATE.slots_open = q.get("v", "1") == "1"
            return self._send("ok")
        if path == "/admin/reset":
            STATE.booking = None
            STATE.slots_open = True
            STATE.confirm_count = 0
            return self._send("ok")

        if path in ("/", "/account"):
            if not self._logged_in():
                return self._send("""
                <div class="login"><h2>Вход в личный кабинет</h2>
                <form method="post" action="/login">
                  <label>Email или номер донора <input name="login" type="text"></label>
                  <label>Пароль <input name="password" type="password"></label>
                  <button type="submit" class="btn">Войти</button>
                </form><a href="/forgot-pass/">Сброс пароля</a></div>""")
            if STATE.booking:
                b = STATE.booking
                return self._send(f"""
                <nav><a href="/logout">Выйти</a></nav><h2>Личный кабинет</h2>
                <div class="my-record"><h3>Ваша запись</h3>
                <p>{b['type']}, {b['date']} в {b['time']}</p>
                <a class="btn" href="/cancel">Отменить запись</a></div>""")
            if not STATE.slots_open:
                return self._send("""
                <nav><a href="/logout">Выйти</a></nav><h2>Личный кабинет</h2>
                <p>Если запись на донацию для Вас недоступна — компонентов крови Вашей группы достаточно.</p>""")
            return self._send("""
                <nav><a href="/logout">Выйти</a></nav><h2>Личный кабинет</h2>
                <p>Донор № 12345, группа A(II) Rh+</p>
                <a class="btn record" href="/record/">Записаться на донацию</a>""")

        if not self._logged_in():
            return self._redirect("/account/")

        if path == "/record":
            return self._send("""
                <h2>Шаг 1. Вид донации</h2>
                <form method="get" action="/record/date/">
                  <label><input type="radio" name="type" value="blood"> Цельная кровь</label>
                  <label><input type="radio" name="type" value="plt"> Тромбоциты</label>
                  <label><input type="radio" name="type" value="plasma"> Плазма</label>
                  <button type="submit" class="btn">Далее</button>
                </form>""")
        if path == "/record/date":
            t = q.get("type", "")
            if t != "plt":
                return self._send(f"<p>Выбран тип: {t or 'не выбран'}. Нет свободных мест.</p>")
            return self._send("""
                <h2>Шаг 2. Дата</h2><ul class="calendar">
                <li><a class="day disabled" href="#">21.10.2026</a></li>
                <li><a class="day" href="/record/time/?type=plt&date=23.10.2026">23.10.2026</a></li>
                <li><a class="day" href="/record/time/?type=plt&date=24.10.2026">24.10.2026</a></li>
                </ul>""")
        if path == "/record/time":
            d = q.get("date", "")
            return self._send(f"""
                <h2>Шаг 3. Время на {d}</h2><div class="slots">
                <button class="slot busy" disabled>08:30</button>
                <a class="slot" href="/record/confirm/?type=plt&date={d}&time=10:00">10:00</a>
                <a class="slot" href="/record/confirm/?type=plt&date={d}&time=09:00">09:00</a>
                </div>""")
        if path == "/record/confirm":
            return self._send(f"""
                <h2>Проверьте данные</h2>
                <p>Тромбоциты, {q.get('date')} в {q.get('time')}</p>
                <form method="post" action="/record/confirm/">
                  <input type="hidden" name="date" value="{q.get('date')}">
                  <input type="hidden" name="time" value="{q.get('time')}">
                  <label><input type="checkbox" name="agree" required> Я ознакомлен с правилами</label>
                  <button type="submit" class="btn">Подтвердить запись</button>
                  <a class="btn" href="/account/">Отмена</a>
                </form>""")
        if path == "/logout":
            return self._redirect("/account/", {"Set-Cookie": "sid=; Max-Age=0; Path=/"})
        return self._send("Страница не найдена", HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        form = self._form()
        if path == "/login":
            if form.get("login") == LOGIN and form.get("password") == PASSWORD:
                return self._redirect("/account/", {"Set-Cookie": "sid=ok; Path=/"})
            return self._send("""<div class="alert">Неверный логин или пароль</div>
                <form method="post" action="/login"><input name="login" type="text">
                <input name="password" type="password"><button type="submit">Войти</button></form>""")
        if path == "/record/confirm":
            if not self._logged_in():
                return self._redirect("/account/")
            if form.get("agree") != "on":
                return self._send("<p class='alert'>Нужно согласие с правилами</p>")
            STATE.confirm_count += 1
            STATE.booking = {"type": "Тромбоциты", "date": form["date"], "time": form["time"]}
            return self._send(f"""<div class="alert alert-success">
                Вы успешно записаны на {form['date']} {form['time']}. Ожидайте подтверждения врача.</div>
                <a href="/account/">В личный кабинет</a>""")
        return self._send("bad request", HTTPStatus.BAD_REQUEST)


def start_server(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    srv, url = start_server(8765)
    print(f"fake site: {url}/account/  (логин {LOGIN} / {PASSWORD})")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
