from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class SiteConf:
    name: str
    url: str


@dataclass
class Config:
    sites: list[SiteConf]
    login: str
    password: str
    open_time: str = "12:00"           # МСК; слоты открываются за 3 дня до даты донации
    start_before_sec: float = 180      # токен rucaptcha решается 30–70 с; 27.10 слот был в 11:59:09
    window_sec: float = 300            # к 12:01:30 места обычно уже разобраны
    poll_interval_sec: float = 0.35    # новый запрос кабинета каждые столько секунд…
    poll_parallel: int = 3             # …и до стольких одновременно (сервер отвечает 0,5–2,5 с)
    session_refresh_sec: float = 15    # сессия живёт 30–60 с от входа
    token_max_age_sec: float = 60      # старше — новый токен капчи
    service_token_max_age_sec: float = 300   # токен живёт 7–10 мин (замер 28.10)
    priority_wait_sec: float = 2       # сколько сайт пониже ждёт ответа сайтов повыше, найдя слот
    preferred_times: list[str] = field(default_factory=list)
    dry_run: bool = False
    pause_days: int = 14
    headless: bool = True
    state_file: Path = Path("state.json")
    log_file: Path = Path("bot.log")
    captcha_api_key: str = ""          # rucaptcha — для сайтов, где вместо галочки задание
    telegram_token: str = ""
    telegram_chat_id: str = ""


def load_config(path: str = "config.yaml", env: str = ".env") -> Config:
    load_dotenv(env)
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    sites = [SiteConf(**s) for s in raw.pop("sites")]
    for key in ("state_file", "log_file"):
        if key in raw:
            raw[key] = Path(raw[key])
    try:
        login, password = os.environ["DONOR_LOGIN"], os.environ["DONOR_PASSWORD"]
    except KeyError as exc:
        raise SystemExit(f"Не задан {exc.args[0]} в {env}") from None
    return Config(sites=sites, login=login, password=password,
                  captcha_api_key=os.getenv("CAPTCHA_API_KEY", ""),
                  telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
                  telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""), **raw)
