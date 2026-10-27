"""Быстрый режим для сайтов «Сервис записи» (donor-mos*.online): всё окно — без навигации.

Почему так: 23.10 слот жил ~10 с (Беговая: появился 12:00:08, к 12:00:17 мест нет), а обычная
попытка тратит секунды на загрузку страницы, клики по окну, капчу после выбора слота и вход
через форму. Здесь:
  * кабинет опрашивается fetch('/account/') из страницы каждые poll_interval_sec, HTML
    разбирается в странице (DOMParser), дампов в горячем пути нет;
  * токен капчи готов заранее: чекбокс в скрытом окне #modal-reservation (≈1 с, через
    smartCaptcha.reset() — свежий), параллельно — запасной токен от сервиса распознавания;
  * сессия (живёт 30–60 с) обновляется быстрым входом каждые session_refresh_sec;
  * найден слот → одна POST-заявка /wp-json/gse/form-v2/send с теми же полями, что шлёт
    сайт (data-select + время + confirm/reject-url + smart-token + formsubmit).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from concurrent.futures import Future

from playwright.sync_api import Error as PWError

from .captcha import _POOL
from .site import Result, SiteClient, release_send_lock

log = logging.getLogger(__name__)

PARSE_JS = """(html) => {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const rows = [...doc.querySelectorAll('table.needs-table tr.dates-table__item')].map(tr => {
    const free = tr.querySelector('td.table-item__btn [data-src="#modal-reservation"][data-select]');
    return {date: (tr.querySelector('td.table-item__date')?.textContent || '').trim(),
            select: free ? free.getAttribute('data-select') : null,
            forbidden: !!tr.querySelector('td.table-item__btn [data-src="#modal-forbidden"]')};
  });
  const list = doc.querySelector('#modal-list-reservation .modal-list-reservation__body');
  const entry = !!doc.querySelector('#modal-list-reservation .cancel-entry') ||
                !!(list && list.querySelector(':scope > :not(.not-found)'));
  return {table: !!doc.querySelector('table.needs-table'), rows, entry,
          entry_text: entry && list ? list.textContent.replace(/\\s+/g, ' ').trim().slice(0, 200) : ''};
}"""

# Разовая полная проверка кабинета (заявка уже есть? сессия жива?)
POLL_JS = """async () => {
  const r = await fetch('/account/', {credentials: 'include', cache: 'no-store'});
  return {http: r.status, ...(%PARSE%)(await r.text())};
}""".replace("%PARSE%", PARSE_JS)

# Фоновый опрос в странице. Сервер отвечает ~1,5 с (TTFB ~0,8 с + медленная отдача), поэтому:
# новый запрос каждые interval мс, не дожидаясь предыдущих (до parallel одновременно), а тело
# читается потоком и обрывается сразу после таблицы дат («Список моих записей» стоит раньше неё).
# Python читает window.__dbot: last — самый свежий ответ, free — первый ответ со свободной датой.
POLLER_JS = """(cfg) => {
  const parse = %PARSE%;
  const S0 = window.__dbot;
  if (S0 && S0.running) { S0.cfg = cfg; return 'updated'; }
  const S = window.__dbot = {running: true, cfg, seq: 0, done: 0, errors: 0, inflight: 0,
                             last: null, free: null, lastError: '', ctrls: new Set()};
  const one = async () => {
    const id = ++S.seq, t0 = Date.now(), ctrl = new AbortController();
    S.inflight++; S.ctrls.add(ctrl);
    try {
      const r = await fetch('/account/', {credentials: 'include', cache: 'no-store', signal: ctrl.signal});
      const reader = r.body.getReader(), dec = new TextDecoder();
      let html = '', cut = false;
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        html += dec.decode(value, {stream: true});
        const i = html.search(/<table[^>]*needs-table/);
        if (i >= 0 && html.indexOf('</table>', i) >= 0) { cut = true; break; }
      }
      if (cut) reader.cancel().catch(() => {});
      const res = {id, http: r.status, ms: Date.now() - t0, at: Date.now(), ...parse(html)};
      if (!S.last || id > S.last.id) S.last = res;
      if (!S.free && res.rows.some(x => x.select)) S.free = res;
      S.done++;
    } catch (e) {
      if (S.running) { S.errors++; S.lastError = String(e).slice(0, 200); }
    } finally { S.inflight--; S.ctrls.delete(ctrl); }
  };
  const tick = () => {
    if (!S.running) return;
    if (S.inflight < S.cfg.parallel) one();
    setTimeout(tick, S.cfg.interval);
  };
  tick();
  return 'started';
}""".replace("%PARSE%", PARSE_JS)

POLLER_STOP_JS = """() => { const S = window.__dbot; if (!S) return;
  S.running = false; S.ctrls.forEach(c => c.abort()); }"""
POLLER_STATE_JS = """() => { const S = window.__dbot;
  return S ? {running: S.running, done: S.done, errors: S.errors, inflight: S.inflight,
              last: S.last, free: S.free, lastError: S.lastError} : null; }"""

# Поля — в порядке и с теми значениями, что даёт eserialize сайта (extends.js) после выбора
# времени (account.js: confirm/reject-url = <a href="…time=ЧЧ:ММ">…</a>).
SEND_JS = """async ([select, time, token]) => {
  const box = document.createElement('div');
  box.innerHTML = select;
  const fd = new FormData();
  for (const el of box.querySelectorAll('input, textarea, select')) {
    if (!el.name) continue;
    let v = el.value;
    if (el.name === 'time-reservation') v = time;
    else if (el.name === 'confirm-url' || el.name === 'reject-url') {
      const url = (el.getAttribute('data-value') || '').replace('time=', 'time=' + time);
      v = `<a href="${url}">${el.name === 'confirm-url' ? 'Подтвердить' : 'Отклонить'} бронирование</a>`;
    }
    fd.append(el.name, v);
  }
  fd.append('smart-token', token);
  fd.append('formsubmit', 'true');
  const r = await fetch('/wp-json/gse/form-v2/send', {method: 'POST', body: fd, credentials: 'include',
    headers: {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json, text/javascript, */*; q=0.01'}});
  return {http: r.status, body: (await r.text()).slice(0, 2000)};
}"""

TIMES_JS = """(select) => {
  const box = document.createElement('div');
  box.innerHTML = select;
  const s = box.querySelector('select[name="time-reservation"]');
  return s ? [...s.options].filter(o => o.value && !o.disabled).map(o => o.value) : [];
}"""

SHOW_CAPTCHA_JS = """() => { const m = document.getElementById('modal-reservation');
  if (!m) return false; m.style.display = 'block'; m.scrollIntoView(); return true; }"""
TOKEN_JS = "() => document.querySelector('#modal-reservation input[name=\"smart-token\"]')?.value || ''"
SITEKEY_JS = "() => document.querySelector('#modal-reservation .smart-captcha')?.getAttribute('data-sitekey') || ''"


def _token_age(token: str, got_at: float) -> float:
    """Возраст токена: в нём зашито время выдачи (base64 «t=1792746022;…»), иначе — по got_at."""
    try:
        m = re.match(r"t=(\d{10})", base64.b64decode(token + "==").decode("latin-1"))
        if m:
            return time.time() - int(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return time.time() - got_at


class Sprinter:
    def __init__(self, client: SiteClient):
        self.c = client
        self.cfg = client.cfg
        self.name = client.site.name
        self.page = client.page
        self.box_token = ("", 0.0)       # токен чекбокса в странице
        self.service_token = ("", 0.0)   # запасной токен от сервиса распознавания
        self.service_future: Future | None = None
        self.sitekey = ""
        self.box_failed = False          # чекбокс не дал токен (задание) — нужен сервис
        self.box_tried_at = 0.0
        self.failed_sends = 0

    # -------------------------------------------------------------- captcha
    def _fresh(self, tok: tuple[str, float]) -> bool:
        return bool(tok[0]) and _token_age(*tok) < self.cfg.captcha_token_max_age_sec

    def refresh_box_token(self) -> bool:
        """Новый токен чекбоксом в скрытом окне записи (≈1 с). False — задание или нет капчи."""
        p = self.page
        self.box_tried_at = time.time()
        try:
            if not p.evaluate(SHOW_CAPTCHA_JS):
                return False
            self.sitekey = self.sitekey or p.evaluate(SITEKEY_JS)
            if p.evaluate(TOKEN_JS):
                p.evaluate("() => window.smartCaptcha && window.smartCaptcha.reset()")
                p.wait_for_timeout(300)
            if not self.c.captcha._click_checkbox(p):
                return False
            end = time.time() + 6
            while time.time() < end:
                tok = p.evaluate(TOKEN_JS)
                if tok:
                    self.box_token = (tok, time.time())
                    self.box_failed = False
                    log.info("[%s] токен капчи готов (чекбокс)", self.name)
                    return True
                if self.c.captcha.detect(p)["challenge"]:
                    log.info("[%s] чекбокс открыл задание — нужен токен сервиса", self.name)
                    break
                time.sleep(0.15)
        except PWError as exc:
            log.warning("[%s] капча: %s", self.name, str(exc).splitlines()[0])
        self.box_failed = True
        return False

    def tend_service(self, allow_new: bool) -> None:
        """Держит запасной токен сервиса: забирает готовый, заказывает новый взамен старого."""
        f = self.service_future
        if f is not None and f.done():
            self.service_future = None
            try:
                tok = f.result()
            except Exception:  # noqa: BLE001
                tok = None
            if tok:
                self.service_token = (tok, time.time())
                log.info("[%s] запасной токен капчи от сервиса готов", self.name)
        # Сервис (~1 ₽, 20–60 с) — только если чекбокс на этом сайте не даёт токен
        # новый заказ — заранее, чтобы к моменту, когда старый токен устареет, был готов свежий
        tok = self.service_token
        aging = not tok[0] or _token_age(*tok) > self.cfg.captcha_token_max_age_sec - 30
        if (allow_new and self.box_failed and self.service_future is None and self.cfg.captcha_api_key
                and self.sitekey and aging):
            self.service_future = _POOL.submit(self.c.captcha._solve_via_service, self.c.account_url, self.sitekey)

    def take_token(self) -> str:
        """Свежий токен к отправке: чекбокс → запасной от сервиса → новый чекбокс → ждём сервис."""
        for tok in (self.box_token, self.service_token):
            if self._fresh(tok):
                return tok[0]
        if self.refresh_box_token():
            return self.box_token[0]
        # устаревший по нашему порогу, но не старый токен сервиса лучше ожидания нового (20–60 с);
        # не примет сайт — book() возьмёт новый
        if self.service_token[0] and _token_age(*self.service_token) < 300:
            return self.service_token[0]
        if self.service_future is not None:
            try:
                tok = self.service_future.result(timeout=self.cfg.captcha_solve_timeout_sec)
            except Exception:  # noqa: BLE001
                tok = None
            self.service_future = None
            if tok:
                return tok
        return ""

    def spend_token(self, token: str) -> None:
        if self.box_token[0] == token:
            self.box_token = ("", 0.0)
        if self.service_token[0] == token:
            self.service_token = ("", 0.0)

    # ----------------------------------------------------------------- main
    def _poller(self, fast: bool) -> None:
        cfg = {"interval": int(self.cfg.poll_interval_sec * 1000) if fast else 2000,
               "parallel": self.cfg.poll_parallel if fast else 1}
        self.page.evaluate(POLLER_JS, cfg)

    def run(self, open_at: float, deadline: float, stop: threading.Event | None) -> Result | None:
        """Всё окно на одном сайте. None — окно вышло без записи."""
        c = self.c
        fast_from = open_at - self.cfg.fast_poll_before_sec
        st = self.page.evaluate(POLL_JS)
        if st["entry"]:
            c.last_message = st["entry_text"]
            log.info("[%s] в кабинете уже есть заявка: %s", self.name, c.last_message)
            return Result.ALREADY_BOOKED
        self.refresh_box_token()
        fast = time.time() >= fast_from
        self._poller(fast)
        last_log = relogin_at = last_full = 0.0
        try:
            while time.time() < deadline:
                if stop is not None and stop.is_set():
                    return None
                now = time.time()
                if not fast and now >= fast_from:
                    fast = True
                    self._poller(True)
                    log.info("[%s] частый опрос: каждые %.2f с, до %d запросов одновременно", self.name,
                             self.cfg.poll_interval_sec, self.cfg.poll_parallel)
                if now - c.logged_in_at > self.cfg.session_refresh_sec:
                    if c.fast_relogin():
                        relogin_at = time.time()
                prefetch = now < open_at + self.cfg.captcha_prefetch_after_sec
                if (prefetch and not self._fresh(self.box_token)
                        and (not self.box_failed or now - self.box_tried_at > 30)):
                    self.refresh_box_token()
                self.tend_service(allow_new=prefetch)

                try:
                    ps = self.page.evaluate(POLLER_STATE_JS)
                except PWError as exc:
                    log.warning("[%s] опрос: %s", self.name, str(exc).splitlines()[0])
                    time.sleep(0.5)
                    continue
                if not ps or not ps["running"]:  # страница перезагрузилась — запускаем заново
                    self._poller(fast)
                    continue
                if ps["free"] and self.failed_sends < self.cfg.max_failed_sends:
                    self.page.evaluate(POLLER_STOP_JS)
                    try:
                        result = self.book([r for r in ps["free"]["rows"] if r["select"]])
                    except PWError as exc:
                        log.warning("[%s] ошибка браузера при записи: %s", self.name, str(exc).splitlines()[0])
                        result = Result.ERROR
                    if result in (Result.BOOKED, Result.DRY_RUN, Result.ALREADY_BOOKED):
                        return result
                    self._poller(fast)
                    continue
                last = ps["last"]
                if last and last["at"] / 1000 > relogin_at + 0.5:
                    if not last["table"]:
                        log.info("[%s] в ответе нет кабинета (HTTP %s) — быстрый вход", self.name, last["http"])
                        relogin_at = time.time()
                        if not c.fast_relogin() and not c.ensure_logged_in():
                            time.sleep(1)
                        else:
                            relogin_at = time.time()
                        continue
                    if last["entry"]:
                        c.last_message = last["entry_text"]
                        log.info("[%s] в кабинете уже есть заявка: %s", self.name, c.last_message)
                        return Result.ALREADY_BOOKED
                if time.time() - last_log > 10:
                    last_log = time.time()
                    rows = ", ".join(f"{r['date']}{'×' if r['forbidden'] else ''}" for r in (last or {}).get("rows", []))
                    log.info("[%s] опрос: ответов %d, ошибок %d, последний %s мс — свободных нет (%s)%s",
                             self.name, ps["done"], ps["errors"], (last or {}).get("ms", "?"), rows,
                             f"; {ps['lastError']}" if ps["errors"] else "")
                time.sleep(0.05)
            return None
        finally:
            try:
                self.page.evaluate(POLLER_STOP_JS)
            except PWError:
                pass

    def book(self, free: list[dict]) -> Result:
        c = self.c
        t0 = time.time()

        def key(r):
            d, m, y = (r["date"].split("/") + ["0", "0", "0"])[:3]
            return (int(y or 0), int(m or 0), int(d or 0))
        free.sort(key=key)
        slot = free[-1] if self.cfg.date_preference == "latest" else free[0]
        day = slot["date"]
        times = self.page.evaluate(TIMES_JS, slot["select"])
        if not times:
            log.info("[%s] %s: в окне записи нет свободного времени", self.name, day)
            return Result.NO_SLOTS
        pick = times[0]
        for pref in self.cfg.preferred_times:
            hit = next((t for t in times if t.startswith(pref)), None)
            if hit:
                pick = hit
                break
        c.last_message = f"{day} {pick}"
        log.info("[%s] свободно: %s → беру %s %s (время: %s)", self.name,
                 ", ".join(r["date"] for r in free), day, pick, ", ".join(times))
        if c.dump_dir:
            (c.dump_dir / f"slot_{day.replace('/', '-')}.html").write_text(slot["select"], encoding="utf-8")

        token = self.take_token()
        if not token:
            c.last_message = f"{day} {pick}: нет токена капчи"
            log.warning("[%s] %s", self.name, c.last_message)
            return Result.CAPTCHA_FAILED
        if self.cfg.dry_run:
            log.info("[%s] DRY RUN: заявку не отправляю (%s)", self.name, c.last_message)
            return Result.DRY_RUN

        holder = c._wait_send_lock()
        if holder:
            c.last_message = f"параллельная отправка: {holder}"
            log.warning("[%s] заявку уже отправляет другой процесс (%s) — не отправляю вторую", self.name, holder)
            return Result.NO_BUTTON
        result = Result.STUCK
        try:
            if time.time() - c.logged_in_at > self.cfg.session_ttl_sec:
                c.fast_relogin(verify=False)
            for attempt in range(3):
                resp = self.page.evaluate(SEND_JS, [slot["select"], pick, token])
                c._net_write({"dir": "fast-send", "day": day, "time": pick, **resp})
                log.info("[%s] заявка %s %s: HTTP %s за %.1f с от обнаружения: %s", self.name, day, pick,
                         resp["http"], time.time() - t0, resp["body"][:300])
                try:
                    ok = resp["http"] == 200 and bool(json.loads(resp["body"]).get("status"))
                except (ValueError, AttributeError):
                    ok = False
                if ok:
                    result = Result.BOOKED
                    c.last_message = f"{day} {pick}. Заявка отправлена, ждёт подтверждения врача"
                    return result
                # Повтор — только если заявки точно нет: иначе вторую не шлём
                st = self.page.evaluate(POLL_JS)
                if st.get("entry"):
                    result = Result.BOOKED
                    c.last_message = f"{day} {pick}. Заявка есть в «Списке моих записей»"
                    return result
                if resp["http"] == 401 or not st.get("table"):
                    log.warning("[%s] сессия слетела (HTTP %s) — быстрый вход и повтор", self.name, resp["http"])
                    if not c.fast_relogin():
                        break
                    continue
                self.spend_token(token)
                if re.search(r"captcha|капч|smart", resp["body"], re.I):
                    log.warning("[%s] сайт не принял капчу — новый токен и повтор", self.name)
                    token = self.take_token()
                    if token:
                        continue
                break
            self.failed_sends += 1
            c.last_message = f"{day} {pick}: заявка не принята — HTTP {resp['http']} {resp['body'][:200]}"
            log.warning("[%s] %s", self.name, c.last_message)
            result = Result.ERROR
            return result
        finally:
            if result != Result.BOOKED:
                release_send_lock(self.cfg)
