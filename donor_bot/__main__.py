"""Точка входа: python -m donor_bot <команда>."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import load_config
from .log import setup_logging
from .scheduler import next_window_start, run_forever, run_window
from .state import State

log = logging.getLogger("donor_bot")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="donor_bot", description="Бот записи на сдачу тромбоцитов")
    p.add_argument("--config", default="config.yaml", help="путь к config.yaml")
    p.add_argument("--env", default=".env", help="путь к .env с логином/паролем")
    p.add_argument("--headful", action="store_true", help="показать окно браузера")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="работать постоянно: каждый день в назначенное время пытаться записаться")
    run.add_argument("--site", action="append", help="ограничить сайтами (по name из config), можно несколько")

    once = sub.add_parser("once", help="попытаться записаться прямо сейчас")
    once.add_argument("--window", type=float, default=None, help="минут продолжать попытки (по умолчанию 0.5)")
    once.add_argument("--dry-run", action="store_true", help="дойти до подтверждения, но не нажимать")
    once.add_argument("--site", action="append", help="ограничить сайтами (по name из config), можно несколько")

    recon = sub.add_parser("recon", help="разведка: войти, сохранить скриншоты/HTML всех шагов, ничего не подтверждать")
    recon.add_argument("--site", action="append", help="ограничить сайтами (по name из config)")
    recon.add_argument("--out", default=None, help="папка для дампов (по умолчанию archive.dir из config)")

    sub.add_parser("login-test", help="проверить вход на все сайты")
    sub.add_parser("next", help="показать время следующего запуска")
    sub.add_parser("clear-pause", help="сбросить паузу после успешной записи")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config, args.env)
    if args.headful:
        cfg.headless = False
    setup_logging(cfg.log_file, args.verbose)
    state = State(cfg.state_file)

    if args.cmd not in ("next", "clear-pause") and not (cfg.login and cfg.password):
        log.error("Не заданы DONOR_LOGIN / DONOR_PASSWORD (см. .env.example)")
        return 2

    if args.cmd == "next":
        print(next_window_start(cfg).strftime("%Y-%m-%d %H:%M:%S %Z"))
        if state.is_paused(datetime.now().date()):
            print(f"пауза после записи до {state.paused_until}")
        return 0

    if args.cmd == "clear-pause":
        state.clear_pause()
        print("пауза сброшена")
        return 0

    if args.cmd == "login-test":
        from playwright.sync_api import sync_playwright
        from .scheduler import BrowserSession
        from .site import SiteClient

        ok_all = True
        with sync_playwright() as pw:
            session = BrowserSession(pw, cfg)
            for site in cfg.sites:
                client = SiteClient(cfg, site, session.new_context())
                ok = client.login()
                print(f"{site.name:12s} {site.url:32s} {'OK' if ok else 'FAIL'}")
                ok_all &= ok
                client.close()
                session.drop_context(client.context)
            session.close()
        return 0 if ok_all else 1

    if args.cmd == "recon":
        cfg.dry_run = True
        root = Path(args.out) if args.out else cfg.archive_dir
        result = run_window(cfg, state, window_min=0, mode="recon", sites=args.site, archive_root=root)
        print(f"\nРезультат разведки: {result.value if result else 'ничего не записано'}")
        print(f"Скриншоты, HTML, списки элементов и журнал действий: {root.resolve()} (последняя папка *_recon)")
        return 0

    if args.cmd == "once":
        if args.dry_run:
            cfg.dry_run = True
        window = 0.5 if args.window is None else args.window
        result = run_window(cfg, state, window_min=window, mode="once", sites=args.site)
        print(f"Результат: {result.value if result else 'не записались'}")
        return 0 if result else 1

    if args.cmd == "run":
        if args.site:
            cfg.sites = [s for s in cfg.sites if s.name in args.site]
        run_forever(cfg, state)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
