"""Архив прогонов и «выученные» рецепты записи.

runs/<дата_время>_<режим>/<сайт>/NN_<шаг>.png|html|json — что видел бот на каждом шаге
runs/<...>/<сайт>/trace_NN.json                         — что и в каком порядке нажимал (с селекторами)
runs/history.jsonl                                      — краткая история результатов
learned/<сайт>.json                                     — селекторы успешного прохождения формы;
                                                          используются в следующих запусках первыми
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

LEARN_ROLES = ("book_button", "donation_type", "date", "time", "confirm", "consent")
LIST_ROLES = ("date", "time")  # для них сохраняем обобщённый селектор (все дни / все слоты)


class RunArchive:
    def __init__(self, root: Path, mode: str, keep_days: int = 30):
        self.root = root
        self.dir = root / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{mode}"
        self.keep_days = keep_days
        self.history = root / "history.jsonl"
        self._last_result: dict[str, str] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self.cleanup()

    def site_dir(self, site_name: str) -> Path:
        d = self.dir / site_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def record(self, site_name: str, result: str, message: str = "", extra: dict | None = None) -> None:
        """Строка истории. Повторы одного и того же результата подряд не пишем."""
        if self._last_result.get(site_name) == result:
            return
        self._last_result[site_name] = result
        row = {"time": datetime.now().isoformat(timespec="seconds"), "site": site_name,
               "result": result, "message": message[:200], "run": self.dir.name}
        if extra:
            row.update(extra)
        with self.history.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def cleanup(self) -> None:
        if self.keep_days <= 0:
            return
        cutoff = time.time() - self.keep_days * 86400
        for d in self.root.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                log.debug("удалил старый архив %s", d)


class Learned:
    """Рецепт записи для одного сайта, собранный из успешного прогона."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self.data = {}

    def selectors(self, role: str) -> list[str]:
        """CSS из рецепта; для кнопок дополнительно селектор по тексту — он переживает смену вёрстки."""
        out: list[str] = []
        for s in self.data.get("roles", {}).get(role, []):
            if s.get("selector"):
                out.append(s["selector"])
            text = (s.get("text") or "").strip()
            if role not in LIST_ROLES and role != "consent" and s.get("tag") and 0 < len(text) <= 40 and '"' not in text:
                out.append(f'{s["tag"]}:has-text("{text}")')
        return out

    @staticmethod
    def from_trace(site_name: str, trace: list[dict], result: str, account_url: str) -> dict:
        roles: dict[str, list[dict]] = {}
        for step in trace:
            role = step.get("role")
            if role not in LEARN_ROLES:
                continue
            sel = step.get("gsel") if role in LIST_ROLES else step.get("sel")
            if not sel:
                continue
            entry = {"selector": sel, "tag": step.get("tag", ""), "text": step.get("text", ""),
                     "url": step.get("url", ""), "clicked": step.get("clicked", True)}
            if entry not in roles.setdefault(role, []):
                roles[role].append(entry)
        return {"site": site_name, "saved_at": datetime.now().isoformat(timespec="seconds"),
                "result": result, "account_url": account_url, "roles": roles,
                "steps": [{k: s.get(k) for k in ("role", "tag", "text", "sel", "gsel", "url")} for s in trace]}

    def save(self, data: dict) -> None:
        self.data = data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("рецепт записи сохранён: %s (%s)", self.path, ", ".join(data["roles"].keys()) or "нет шагов")
