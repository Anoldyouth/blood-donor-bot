"""python -m donor_bot run | once | check"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from .bot import run_next, run_window
from .captcha import Captcha, CaptchaError
from .config import Config, load_config
from .site import Site


async def check(cfg: Config) -> None:
    """Вход, кабинет и токен капчи на каждом сайте — без заявки."""
    async with Captcha(cfg.headless) as captcha:
        for conf in cfg.sites:
            site = Site(conf.name, conf.url, cfg.login, cfg.password)
            try:
                await site.login()
                acc = await site.account(full=True)
                rows = ", ".join(r.date + ("✓" if r.form else "×" if r.sold_out else "") for r in acc.rows)
                widget = await captcha.widget(site.url, acc.sitekey, cfg.token_max_age_sec)
                try:
                    await widget.refresh()
                    cap = "чекбокс даёт токен"
                except CaptchaError as exc:
                    cap = f"{exc}; {'токены возьмёт rucaptcha' if cfg.captcha_api_key else 'сайт будет пропущен'}"
                print(f"{site.name}: вход ок; заявка в кабинете: {'есть' if acc.has_booking else 'нет'}; "
                      f"даты: {rows or '—'}; капча: {cap}")
            except Exception as exc:  # noqa: BLE001
                print(f"{site.name}: ошибка: {exc}")
            finally:
                await site.close()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")   # консоль Windows не печатает «×»
    p = argparse.ArgumentParser(prog="donor_bot", description="Запись на донацию тромбоцитов")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--env", default=".env")
    p.add_argument("--site", action="append", help="только эти сайты (name из config)")
    p.add_argument("--headful", action="store_true", help="показать окно браузера с капчей")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="дождаться ближайшего open_time, ловить слот и выйти")
    once = sub.add_parser("once", help="ловить слот прямо сейчас")
    once.add_argument("--window", type=float, default=60, help="секунд (по умолчанию 60)")
    once.add_argument("--dry-run", action="store_true", help="найти слот, но заявку не отправлять")
    sub.add_parser("check", help="проверить вход и капчу на всех сайтах")
    args = p.parse_args()

    cfg = load_config(args.config, args.env)
    if args.site:
        cfg.sites = [s for s in cfg.sites if s.name in args.site]
    if args.headful:
        cfg.headless = False
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(cfg.log_file, encoding="utf-8")])
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.cmd == "run":
        logging.getLogger("donor_bot").info("сайты: %s", ", ".join(s.name for s in cfg.sites))
        asyncio.run(run_next(cfg, cfg.sites))
    elif args.cmd == "once":
        cfg.dry_run = cfg.dry_run or args.dry_run
        now = time.time()
        asyncio.run(run_window(cfg, cfg.sites, now, now + args.window))
    elif args.cmd == "check":
        asyncio.run(check(cfg))


if __name__ == "__main__":
    main()
