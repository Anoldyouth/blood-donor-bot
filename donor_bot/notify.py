from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


def notify(cfg, text: str) -> None:
    """Пишет сообщение в лог и, если настроено, отправляет в Telegram."""
    log.info("NOTIFY: %s", text)
    if not (cfg.telegram_token and cfg.telegram_chat_id):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
            json={"chat_id": cfg.telegram_chat_id, "text": text},
            timeout=15,
        ).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось отправить уведомление в Telegram: %s", exc)
