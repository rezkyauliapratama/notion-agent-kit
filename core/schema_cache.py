"""Database schema cache with TTL and LRU eviction."""

import time
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_TTL = 300
MAX_ENTRIES = 100


class SchemaCache:
    """In-memory cache for database schemas with TTL and LRU eviction."""

    def __init__(self, ttl: int = DEFAULT_TTL, max_entries: int = MAX_ENTRIES):
        self.ttl = ttl
        self.max_entries = max_entries
        self._cache: Dict[str, Dict[str, Any]] = {}

    def get(self, database_id: str) -> Optional[Dict]:
        entry = self._cache.get(database_id)
        if entry:
            if time.time() < entry["expires_at"]:
                entry["last_access"] = time.time()
                return entry["data"]
            else:
                del self._cache[database_id]
        return None

    def set(self, database_id: str, data: Dict):
        if len(self._cache) >= self.max_entries and database_id not in self._cache:
            lru_key = min(self._cache.keys(), key=lambda k: self._cache[k]["last_access"])
            del self._cache[lru_key]
        self._cache[database_id] = {"data": data, "expires_at": time.time() + self.ttl, "last_access": time.time()}

    def invalidate(self, database_id: str):
        if database_id in self._cache:
            del self._cache[database_id]

    def clear(self):
        self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)
