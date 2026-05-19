"""TTL-aware AI response cache with optional JSON file persistence."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.logging import get_logger

log = get_logger("ai_cache", service_name="AICache")


class AICache:
    """In-memory cache with per-entry TTL and optional disk backing."""

    def __init__(
        self,
        *,
        ttl_seconds: int,
        file_path: Optional[Path] = None,
    ) -> None:
        self._ttl = max(60, ttl_seconds)
        self._file_path = file_path
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not self._file_path:
            return
        path = Path(self._file_path)
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                with self._lock:
                    self._data = self._prune_loaded(raw)
        except (OSError, json.JSONDecodeError, TypeError) as e:
            log.warning("AI cache file load failed, starting empty: %s", e)

    def _prune_loaded(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        out: Dict[str, Dict[str, Any]] = {}
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            exp = v.get("_expires_at")
            if isinstance(exp, (int, float)) and exp > now:
                out[k] = v
        return out

    def _persist_unlocked(self) -> None:
        if not self._file_path:
            return
        path = Path(self._file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, path)

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            exp = entry.get("_expires_at", 0)
            if not isinstance(exp, (int, float)) or exp <= now:
                del self._data[key]
                return None
            return entry.get("value")

    def set(self, key: str, value: Any) -> None:
        expires_at = time.time() + self._ttl
        with self._lock:
            self._data[key] = {"value": value, "_expires_at": expires_at}
            self._persist_unlocked()

    def prune(self) -> None:
        now = time.time()
        with self._lock:
            stale = [k for k, v in self._data.items() if float(v.get("_expires_at", 0)) <= now]
            for k in stale:
                del self._data[k]
            if stale and self._file_path:
                self._persist_unlocked()
