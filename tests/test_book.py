import asyncio
import time

import httpx

from donor_bot.bot import Lane, Outcome, Window
from donor_bot.config import Config
from donor_bot.site import Account, SessionExpired, parse_account

from .test_site import PAGE

ROW = parse_account(PAGE).free[0]


class FakeWidget:
    ready = True

    async def take(self):
        return "TOKEN"


class FakeSite:
    name = "fake"
    session_age = 0.0

    def __init__(self, responses, booked_after=False, check_fails=False):
        self.responses = list(responses)
        self.sent = 0
        self.logins = 0
        self.booked_after = booked_after
        self.check_fails = check_fails

    async def login(self):
        self.logins += 1

    async def send(self, form, slot, token):
        self.sent += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def account(self, full=False):
        if self.check_fails:
            raise SessionExpired
        return Account([], has_booking=self.booked_after)


def window(**lanes):
    w = Window(Config(sites=[], login="", password=""), time.time(), time.time() + 60)
    w.lanes = lanes
    return w


def book(site):
    w = window(fake=Lane(0))
    return asyncio.run(w._book(site, w.lanes["fake"], FakeWidget(), ROW)), w


def test_booked():
    site = FakeSite([(200, {"status": True})])
    outcome, w = book(site)
    assert outcome == Outcome.BOOKED and w.done.is_set() and w.booking == "fake 30/10/2026 08:00"


def test_401_relogin_and_retry():
    site = FakeSite([(401, {"code": "rest_not_logged_in"}), (200, {"status": True})])
    assert book(site)[0] == Outcome.BOOKED and site.logins == 1 and site.sent == 2


def test_rejected_without_booking_can_retry_later():
    site = FakeSite([(200, {"status": False, "error": "места закончились"})])
    outcome, w = book(site)
    assert outcome is None and not w.done.is_set() and site.sent == 1


def test_timeout_but_booking_exists():
    assert book(FakeSite([httpx.ReadTimeout("t")], booked_after=True))[0] == Outcome.BOOKED


def test_timeout_and_cannot_check_stops_everything():
    outcome, w = book(FakeSite([httpx.ReadTimeout("t")], check_fails=True))
    assert outcome == Outcome.UNCERTAIN and w.done.is_set()


def test_second_site_does_not_send_after_first_booked():
    async def both():
        w = window(a=Lane(0), b=Lane(1))
        a, b = FakeSite([(200, {"status": True})]), FakeSite([(200, {"status": True})])
        res = await asyncio.gather(w._book(a, w.lanes["a"], FakeWidget(), ROW),
                                   w._book(b, w.lanes["b"], FakeWidget(), ROW))
        return res, a.sent, b.sent
    res, a_sent, b_sent = asyncio.run(both())
    assert res == [Outcome.BOOKED, Outcome.ALREADY_BOOKED] and (a_sent, b_sent) == (1, 0)


def test_lower_site_yields_to_higher_with_free_slot():
    """Беговая нашла слот первой, но у Шаболовки он тоже есть — записывается Шаболовка."""
    async def scenario():
        w = window(shabolovka=Lane(0), begovaya=Lane(1))
        low, high = FakeSite([(200, {"status": True})]), FakeSite([(200, {"status": True})])
        low_task = asyncio.create_task(w._book(low, w.lanes["begovaya"], FakeWidget(), ROW))
        await asyncio.sleep(0.3)
        w.lanes["shabolovka"].seen_at, w.lanes["shabolovka"].free = time.time(), True
        high_res = await w._book(high, w.lanes["shabolovka"], FakeWidget(), ROW)
        return high_res, await low_task, high.sent, low.sent
    assert asyncio.run(scenario()) == (Outcome.BOOKED, Outcome.ALREADY_BOOKED, 1, 0)


def test_lower_site_goes_when_higher_has_nothing():
    async def scenario():
        w = window(shabolovka=Lane(0), begovaya=Lane(1))
        low = FakeSite([(200, {"status": True})])
        task = asyncio.create_task(w._book(low, w.lanes["begovaya"], FakeWidget(), ROW))
        await asyncio.sleep(0.2)
        w.lanes["shabolovka"].seen_at = time.time()   # свежий ответ без свободных дат
        t0 = time.time()
        return await task, time.time() - t0
    outcome, waited = asyncio.run(scenario())
    assert outcome == Outcome.BOOKED and waited < 0.5


def test_lower_site_does_not_wait_for_skipped_higher():
    w = window(shabolovka=Lane(0, live=False), begovaya=Lane(1))
    t0 = time.time()
    assert asyncio.run(w._book(FakeSite([(200, {"status": True})]), w.lanes["begovaya"], FakeWidget(), ROW))         == Outcome.BOOKED
    assert time.time() - t0 < 0.2


def test_wait_for_silent_higher_site_is_capped():
    w = window(shabolovka=Lane(0), begovaya=Lane(1))
    w.cfg.priority_wait_sec = 0.5
    t0 = time.time()
    assert asyncio.run(w._book(FakeSite([(200, {"status": True})]), w.lanes["begovaya"], FakeWidget(), ROW))         == Outcome.BOOKED
    assert 0.5 <= time.time() - t0 < 0.8


def test_service_tokens_keep_stock():
    from donor_bot.captcha import NoToken, ServiceTokens

    async def scenario():
        st = ServiceTokens("shabolovka", "KEY", "SITEKEY", "https://x/account/", max_age=300, lead=150, stock=2)
        orders = []

        async def solve():
            orders.append(1)
            st.tokens.append((f"T{len(orders)}", time.time()))
        st._solve = solve
        assert not st.ready
        try:
            await st.take()
            raise AssertionError("ждали NoToken")
        except NoToken:
            pass
        await asyncio.sleep(0.01)                    # заказы из take() выполнились
        assert len(orders) == 2 and st.ready
        await st.tend()
        assert len(orders) == 2                     # запаса хватает — новых заказов нет
        st.tokens = [("OLD", time.time() - 200), ("NEW", time.time())]
        await st.tend()
        await asyncio.sleep(0.01)
        assert len(orders) == 3                     # OLD проживёт меньше lead — заказан на замену
        st.tokens.insert(0, ("DEAD", time.time() - 301))
        assert await st.take() == "OLD"             # просроченный выброшен, тратится самый старый живой
        await st.close()
    asyncio.run(scenario())
