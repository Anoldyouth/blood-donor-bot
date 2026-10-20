from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass
class Site:
    name: str
    url: str

    @property
    def base(self) -> str:
        return self.url.rstrip("/")


@dataclass
class Config:
    sites: list[Site]
    account_path: str
    login: str
    password: str
    timezone: str
    time: str
    start_before_sec: int
    window_min: float
    retry_interval_sec: float
    donation_type_regex: str
    date_preference: str
    preferred_times: list[str]
    dry_run: bool
    pause_days_after_success: int
    headless: bool
    slow_mo_ms: int
    timeout_ms: int
    channel: str | None
    profile_dir: Path | None
    captcha_solver: str
    captcha_solver_url: str
    captcha_api_key: str
    captcha_solve_timeout_sec: int
    selectors: dict[str, list[str]]
    state_file: Path
    log_file: Path
    telegram_token: str
    telegram_chat_id: str
    raw: dict[str, Any] = field(default_factory=dict)

    def account_url(self, site: Site) -> str:
        return site.base + self.account_path


def load_config(path: str | Path = "config.yaml", env_path: str | Path = ".env") -> Config:
    load_dotenv(env_path)
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    sites = [Site(name=s["name"], url=s["url"]) for s in data.get("sites", [])]
    if not sites:
        raise ValueError("В config.yaml не задан ни один сайт (sites)")

    creds = data.get("credentials") or {}
    login = os.getenv("DONOR_LOGIN") or creds.get("login") or ""
    password = os.getenv("DONOR_PASSWORD") or creds.get("password") or ""

    sched = data.get("schedule", {})
    booking = data.get("booking", {})
    browser = data.get("browser", {})
    notify = data.get("notify", {})
    captcha = data.get("captcha", {})

    selectors: dict[str, list[str]] = {}
    for key, value in (data.get("selectors") or {}).items():
        if isinstance(value, str):
            selectors[key] = [value]
        elif value:
            selectors[key] = [str(v) for v in value]

    base_dir = path.resolve().parent
    return Config(
        sites=sites,
        account_path=data.get("account_path", "/account/"),
        login=login,
        password=password,
        timezone=sched.get("timezone", "Europe/Moscow"),
        time=str(sched.get("time", "12:00")),
        start_before_sec=int(sched.get("start_before_sec", 45)),
        window_min=float(sched.get("window_min", 20)),
        retry_interval_sec=float(sched.get("retry_interval_sec", 4)),
        donation_type_regex=booking.get("donation_type_regex", "тромбоцит"),
        date_preference=booking.get("date_preference", "earliest"),
        preferred_times=[str(t) for t in booking.get("preferred_times", []) or []],
        dry_run=bool(booking.get("dry_run", False)),
        pause_days_after_success=int(booking.get("pause_days_after_success", 14)),
        headless=bool(browser.get("headless", True)),
        slow_mo_ms=int(browser.get("slow_mo_ms", 0)),
        timeout_ms=int(browser.get("timeout_ms", 10000)),
        channel=browser.get("channel"),
        profile_dir=(base_dir / browser["profile_dir"]) if browser.get("profile_dir") else None,
        captcha_solver=str(captcha.get("solver", "") or ""),
        captcha_solver_url=str(captcha.get("solver_url", "") or ""),
        captcha_api_key=os.getenv("CAPTCHA_API_KEY") or str(captcha.get("api_key", "") or ""),
        captcha_solve_timeout_sec=int(captcha.get("solve_timeout_sec", 150)),
        selectors=selectors,
        state_file=base_dir / data.get("state_file", "state.json"),
        log_file=base_dir / data.get("log_file", "bot.log"),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN") or notify.get("telegram_bot_token", "") or "",
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or str(notify.get("telegram_chat_id", "") or ""),
        raw=data,
    )
