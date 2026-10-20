from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path


class State:
    """Хранит факт последней успешной записи, чтобы не записываться дважды."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self.data = {}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def paused_until(self) -> date | None:
        raw = self.data.get("paused_until")
        return date.fromisoformat(raw) if raw else None

    def is_paused(self, today: date) -> bool:
        until = self.paused_until
        return until is not None and today <= until

    def record_success(self, site: str, details: str, paused_until: date) -> None:
        self.data["last_success"] = {
            "site": site,
            "details": details,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        self.data["paused_until"] = paused_until.isoformat()
        self.save()

    def clear_pause(self) -> None:
        self.data.pop("paused_until", None)
        self.save()
