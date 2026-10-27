import html
from datetime import datetime

from donor_bot.bot import MSK, next_open
from donor_bot.config import Config
from donor_bot.site import BookingForm, parse_account

FORM = """
<input type="hidden" name="need-id" value="32926" />
<input type="hidden" name="user-id" value="1" />
<input type="hidden" name="confirm-url" data-value="https://x/wp-admin/admin-ajax.php?action=confirm_reservation&date=20261030&time=" value="" />
<input type="hidden" name="reject-url" data-value="https://x/wp-admin/admin-ajax.php?action=reject_reservation&date=20261030&time=" value="" />
<input type="hidden" name="date-reservation" value="30/10/2026">
<select name="time-reservation" required>
<option disabled selected value>Выбрать время</option><option value="08:00">08:00</option><option value="09:00">09:00</option></select>
"""


def row(day: str, btn: str) -> str:
    return (f'<tr class="dates-table__item table-item"><td class="table-item__date">\n  {day}  </td>'
            f'<td class="table-item__bloodTypes"><b>А(II),Rh+</b></td><td class="table-item__btn">{btn}</td></tr>')


def page(rows: str, bookings: str = '<div class="not-found">нет записей</div>') -> str:
    return (f'<table class="needs-table">{rows}</table>'
            f'<div class="modal-reservation__wrapper"><div class="smart-captcha" data-hl="ru" data-sitekey="ysc1_KEY"></div></div>'
            f'<div class="modal-list-reservation__body">{bookings}</div>')


FREE = f'<button class="btn-primary" data-fancybox="" data-src="#modal-reservation" data-select="{html.escape(FORM)}"> Записаться </button>'
SOLD = '<button class="btn-primary" data-fancybox="" data-src="#modal-forbidden"> Записаться </button>'
PAGE = page(row("29/10/2026", "-") + row("31/10/2026", FREE) + row("30/10/2026", FREE) + row("28/10/2026", SOLD))


def test_parse_account():
    acc = parse_account(PAGE)
    assert [r.date for r in acc.rows] == ["29/10/2026", "31/10/2026", "30/10/2026", "28/10/2026"]
    assert [r.date for r in acc.free] == ["30/10/2026", "31/10/2026"]
    assert acc.row("28/10/2026").sold_out and not acc.row("29/10/2026").sold_out
    assert acc.has_booking is False
    assert acc.sitekey == "ysc1_KEY"


def test_booking_and_logged_out():
    assert parse_account(page("", '<div class="item"><a class="cancel-entry">Отменить</a></div>')).has_booking
    assert parse_account("<form id=auth-form>Войти</form>") is None


def test_booking_payload():
    form = BookingForm.parse(parse_account(PAGE).free[0].form)
    assert form.times == ["08:00", "09:00"]
    assert form.pick_time([]) == "08:00"
    assert form.pick_time(["10:00", "09:00"]) == "09:00"
    payload = dict((k, v[1]) for k, v in form.payload("09:00", "TOKEN"))
    assert list(payload) == ["need-id", "user-id", "confirm-url", "reject-url", "date-reservation",
                             "time-reservation", "smart-token", "formsubmit"]
    assert payload["time-reservation"] == "09:00"
    assert payload["confirm-url"] == ('<a href="https://x/wp-admin/admin-ajax.php?action=confirm_reservation'
                                      '&date=20261030&time=09:00">Подтвердить бронирование</a>')
    assert payload["reject-url"].endswith('time=09:00">Отклонить бронирование</a>')
    assert payload["smart-token"] == "TOKEN" and payload["formsubmit"] == "true"


def test_next_open():
    cfg = Config(sites=[], login="", password="")
    assert next_open(cfg, datetime(2026, 11, 10, 9, 0, tzinfo=MSK)) == datetime(2026, 11, 10, 12, 0, tzinfo=MSK)
    assert next_open(cfg, datetime(2026, 11, 10, 12, 3, tzinfo=MSK)) == datetime(2026, 11, 10, 12, 0, tzinfo=MSK)
    assert next_open(cfg, datetime(2026, 11, 10, 12, 6, tzinfo=MSK)) == datetime(2026, 11, 11, 12, 0, tzinfo=MSK)
