"""Small durable state store for write safety metadata.

The store deliberately contains no provider credentials.  It is suitable for
multiple server processes on one host: SQLite transactions provide atomic
idempotency claims and optimistic job checkpoints, while WAL keeps reads from
blocking short writes.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional


_SENSITIVE_KEY = re.compile(r"(token|secret|password|authorization|credential|api[_-]?key)", re.I)
_SENSITIVE_TEXT = re.compile(r"(?i)(bearer\s+|ntn_|secret[_-]?key[=:]\s*)[^\s,;]+")


class StateConflict(RuntimeError):
    """A job checkpoint was changed by another worker."""


def redact(value: Any, max_string: int = 512) -> Any:
    """Return bounded JSON-safe audit data with credential-like values removed."""
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if _SENSITIVE_KEY.search(str(k))
                         else redact(v, max_string)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, max_string) for item in value[:200]]
    if isinstance(value, tuple):
        return [redact(item, max_string) for item in value[:200]]
    if isinstance(value, str):
        value = _SENSITIVE_TEXT.sub(r"\1[REDACTED]", value)
        return value if len(value) <= max_string else value[:max_string] + "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:max_string]


class SQLiteStateStore:
    """Durable idempotency, audit, and resumable-job state in one SQLite DB."""

    def __init__(self, path: str = ":memory:", idempotency_ttl: int = 86400,
                 audit_enabled: bool = False, audit_retention: int = 10000):
        if not isinstance(path, str) or not path:
            raise ValueError("state database path must be a non-empty string")
        self.path = path
        self.idempotency_ttl = max(1, min(int(idempotency_ttl), 31_536_000))
        self.audit_enabled = bool(audit_enabled)
        self.audit_retention = max(1, min(int(audit_retention), 1_000_000))
        self._memory_connection = None
        if path == ":memory:":
            self._memory_connection = sqlite3.connect(":memory:", timeout=10.0)
            self._memory_connection.row_factory = sqlite3.Row
        if path != ":memory:":
            target = Path(path).expanduser()
            # State can contain request results and job inputs, but never tokens.
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch(mode=0o600, exist_ok=True)
                os.chmod(target, 0o600)
            except OSError as exc:
                raise ValueError(f"cannot initialize state database '{target}': {exc}") from exc
            self.path = str(target)
        self._initialize()

    @classmethod
    def from_env(cls) -> "SQLiteStateStore":
        explicit = os.environ.get("NOTION_STATE_DB")
        path = explicit or os.path.join(
            os.path.expanduser("~"), ".cache", "notion-agent-kit", "state.sqlite3")
        def integer(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default
        enabled = os.environ.get("NOTION_AUDIT_ENABLED", "0").strip().lower() in {
            "1", "true", "yes", "on"
        }
        kwargs = {"idempotency_ttl": integer("NOTION_IDEMPOTENCY_TTL_SECONDS", 86400),
                  "audit_enabled": enabled,
                  "audit_retention": integer("NOTION_AUDIT_RETENTION", 10000)}
        try:
            return cls(path, **kwargs)
        except (ValueError, OSError, sqlite3.Error):
            if explicit:
                raise
            # Some managed runners expose a read-only synthetic HOME. Keep
            # default startup safe by using the runtime-provided scratch area;
            # explicit NOTION_STATE_DB paths never fall back silently.
            fallback = os.path.join(os.environ.get("TMPDIR", "/tmp"),
                                    "notion-agent-kit", "state.sqlite3")
            return cls(fallback, **kwargs)

    def _connect(self) -> sqlite3.Connection:
        if self._memory_connection is not None:
            return self._memory_connection
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS idempotency (
                    scope TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (scope, idem_key)
                );
                CREATE INDEX IF NOT EXISTS idempotency_expiry ON idempotency(expires_at);
                CREATE TABLE IF NOT EXISTS operation_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    target_id TEXT,
                    payload_hash TEXT,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_created ON operation_audit(created_at);
                CREATE TABLE IF NOT EXISTS bulk_jobs (
                    job_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def _purge_expired(self, db: sqlite3.Connection, now: Optional[float] = None) -> None:
        db.execute("DELETE FROM idempotency WHERE expires_at <= ?", (now or time.time(),))

    def idempotency_lookup(self, scope: str, key: str, payload_hash: str):
        """Return ``('replay', result)``, ``('conflict', None)``, or ``None``."""
        now = time.time()
        with self._connect() as db:
            self._purge_expired(db, now)
            row = db.execute(
                "SELECT payload_hash, result_json FROM idempotency WHERE scope=? AND idem_key=?",
                (scope, key),
            ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != payload_hash:
            return ("conflict", None)
        return ("replay", json.loads(row["result_json"]))

    def idempotency_store(self, scope: str, key: str, payload_hash: str, result: dict,
                          ttl: Optional[int] = None):
        """Atomically store a result; a concurrent different payload is a conflict."""
        now = time.time()
        expires = now + (self.idempotency_ttl if ttl is None else max(1, int(ttl)))
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._purge_expired(db, now)
            row = db.execute(
                "SELECT payload_hash, result_json FROM idempotency WHERE scope=? AND idem_key=?",
                (scope, key),
            ).fetchone()
            if row is not None:
                if row["payload_hash"] != payload_hash:
                    return ("conflict", None)
                return ("replay", json.loads(row["result_json"]))
            db.execute(
                "INSERT INTO idempotency(scope, idem_key, payload_hash, result_json, expires_at, created_at) VALUES(?,?,?,?,?,?)",
                (scope, key, payload_hash, encoded, expires, now),
            )
        return ("stored", dict(result))

    def record_audit(self, operation: str, result: dict, target_id: Optional[str] = None,
                     payload_hash: Optional[str] = None) -> None:
        if not self.audit_enabled:
            return
        data = redact(result)
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
        # Do not allow a single result to defeat bounded retention.
        encoded = encoded[:65536]
        with self._connect() as db:
            db.execute(
                "INSERT INTO operation_audit(operation,target_id,payload_hash,result_json,created_at) VALUES(?,?,?,?,?)",
                (operation, str(target_id)[:256] if target_id is not None else None,
                 payload_hash, encoded, time.time()),
            )
            db.execute(
                "DELETE FROM operation_audit WHERE id <= COALESCE((SELECT MAX(id) FROM operation_audit) - ?, -1)",
                (self.audit_retention,),
            )

    def list_audit(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), self.audit_retention))
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,operation,target_id,payload_hash,result_json,created_at FROM operation_audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"id": row["id"], "operation": row["operation"],
                 "target_id": row["target_id"], "payload_hash": row["payload_hash"],
                 "result": json.loads(row["result_json"]), "created_at": row["created_at"]}
                for row in rows]

    def create_job(self, request_hash: str, state: dict, job_id: Optional[str] = None) -> dict:
        job_id = job_id or str(uuid.uuid4())
        now = time.time()
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
        with self._connect() as db:
            db.execute(
                "INSERT INTO bulk_jobs(job_id,request_hash,state_json,version,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (job_id, request_hash, encoded, 0, now, now),
            )
        return {"job_id": job_id, "request_hash": request_hash, "state": dict(state), "version": 0}

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM bulk_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return {"job_id": row["job_id"], "request_hash": row["request_hash"],
                "state": json.loads(row["state_json"]), "version": row["version"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def update_job(self, job_id: str, state: dict, expected_version: int) -> dict:
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
        now = time.time()
        with self._connect() as db:
            result = db.execute(
                "UPDATE bulk_jobs SET state_json=?, version=version+1, updated_at=? WHERE job_id=? AND version=?",
                (encoded, now, job_id, expected_version),
            )
            if result.rowcount != 1:
                raise StateConflict(f"bulk job '{job_id}' was advanced by another worker")
        return {"job_id": job_id, "state": dict(state), "version": expected_version + 1}

    def close(self) -> None:
        # Connections are intentionally short-lived; this method is a lifecycle hook.
        return None
