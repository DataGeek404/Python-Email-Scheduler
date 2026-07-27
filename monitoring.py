"""Shared monitoring storage for the scheduler worker and admin dashboard."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


PROJECT_DIR = Path(__file__).resolve().parent
COMMAND_ACTIONS = {
    "send_now",
    "check_config",
    "check_smtp",
    "dry_run",
    "pause",
    "resume",
}
COMMAND_STATUSES = {"queued", "running", "completed", "failed", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def database_url_from_environment() -> str:
    return (
        os.getenv("MONITOR_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or f"sqlite:///{(PROJECT_DIR / 'scheduler_monitor.db').as_posix()}"
    )


class MonitoringStore:
    """Small PostgreSQL/SQLite store with no ORM dependency."""

    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or database_url_from_environment()
        self.is_postgres = self.database_url.startswith(("postgres://", "postgresql://"))
        self.is_sqlite = self.database_url.startswith("sqlite:///")
        if not self.is_postgres and not self.is_sqlite:
            raise ValueError("Monitoring URL must use postgresql:// or sqlite:///")

    @property
    def backend_name(self) -> str:
        return "postgresql" if self.is_postgres else "sqlite"

    def _query(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_postgres else sql

    @contextmanager
    def connect(self):
        if self.is_postgres:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as error:
                raise RuntimeError(
                    "PostgreSQL monitoring requires psycopg; install requirements.txt"
                ) from error
            url = self.database_url.replace("postgres://", "postgresql://", 1)
            connection = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        else:
            raw_path = self.database_url[len("sqlite:///") :]
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = PROJECT_DIR / path
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        id_column = (
            "BIGSERIAL PRIMARY KEY"
            if self.is_postgres
            else "INTEGER PRIMARY KEY AUTOINCREMENT"
        )
        statements = [
            """
            CREATE TABLE IF NOT EXISTS scheduler_state (
                id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                paused INTEGER NOT NULL DEFAULT 0,
                pid INTEGER,
                worker_id TEXT,
                schedule_time TEXT,
                timezone_name TEXT,
                next_run TEXT,
                last_attempt TEXT,
                last_success TEXT,
                last_error TEXT,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                config_json TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS monitor_events (
                id {id_column},
                created_at TEXT NOT NULL,
                level TEXT NOT NULL,
                module TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS deliveries (
                id {id_column},
                source TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                to_count INTEGER NOT NULL,
                cc_count INTEGER NOT NULL,
                attachment TEXT NOT NULL,
                error TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS admin_commands (
                id {id_column},
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                payload_json TEXT,
                result TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_events_created ON monitor_events(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_events_level ON monitor_events(level)",
            "CREATE INDEX IF NOT EXISTS idx_deliveries_completed ON deliveries(completed_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_commands_status "
            "ON admin_commands(status, requested_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_pending_action "
            "ON admin_commands(action) WHERE status IN ('queued', 'running')",
        ]
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                with self.connect() as connection:
                    for statement in statements:
                        connection.execute(statement)
                return
            except Exception as error:
                last_error = error
                if attempt < 4:
                    time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"Unable to initialize monitoring database: {last_error}")

    def healthcheck(self) -> tuple[bool, str]:
        try:
            with self.connect() as connection:
                row = connection.execute("SELECT 1 AS healthy").fetchone()
            return bool(row), f"{self.backend_name} connected"
        except Exception as error:
            return False, str(error)

    def upsert_state(self, values: Mapping[str, Any]) -> None:
        allowed = {
            "state",
            "paused",
            "pid",
            "worker_id",
            "schedule_time",
            "timezone_name",
            "next_run",
            "last_attempt",
            "last_success",
            "last_error",
            "started_at",
            "config_json",
        }
        data = {key: values.get(key) for key in allowed}
        data["paused"] = 1 if data.get("paused") else 0
        data["state"] = data.get("state") or "unknown"
        data["updated_at"] = utc_now()
        columns = [*allowed, "updated_at"]
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{column}=excluded.{column}" for column in columns)
        sql = (
            f"INSERT INTO scheduler_state (id, {', '.join(columns)}) "
            f"VALUES (1, {placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        with self.connect() as connection:
            connection.execute(
                self._query(sql), tuple(data.get(column) for column in columns)
            )

    def get_state(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone()
        return dict(row) if row else None

    def set_paused(self, paused: bool) -> None:
        with self.connect() as connection:
            connection.execute(
                self._query(
                    "UPDATE scheduler_state SET paused=?, updated_at=? WHERE id=1"
                ),
                (1 if paused else 0, utc_now()),
            )

    def record_event(
        self,
        level: str,
        module: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                self._query(
                    "INSERT INTO monitor_events "
                    "(created_at, level, module, message, details_json) "
                    "VALUES (?, ?, ?, ?, ?)"
                ),
                (
                    utc_now(),
                    level.upper()[:20],
                    module[:100],
                    message[:10000],
                    json.dumps(details, default=str) if details else None,
                ),
            )

    def list_events(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
        level: str = "",
        module: str = "",
        search: str = "",
    ) -> tuple[list[dict[str, Any]], int]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if level:
            conditions.append("level = ?")
            parameters.append(level.upper())
        if module:
            conditions.append("module = ?")
            parameters.append(module)
        if search:
            conditions.append("LOWER(message) LIKE ?")
            parameters.append(f"%{search.lower()}%")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        page = max(page, 1)
        per_page = min(max(per_page, 1), 500)
        offset = (page - 1) * per_page
        with self.connect() as connection:
            total_row = connection.execute(
                self._query(f"SELECT COUNT(*) AS count FROM monitor_events{where}"),
                tuple(parameters),
            ).fetchone()
            rows = connection.execute(
                self._query(
                    "SELECT * FROM monitor_events"
                    f"{where} ORDER BY id DESC LIMIT ? OFFSET ?"
                ),
                (*parameters, per_page, offset),
            ).fetchall()
        return [dict(row) for row in rows], int(dict(total_row)["count"])

    def event_modules(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT module FROM monitor_events ORDER BY module"
            ).fetchall()
        return [dict(row)["module"] for row in rows]

    def iter_events(
        self, *, level: str = "", module: str = "", search: str = ""
    ) -> Iterator[dict[str, Any]]:
        """Stream every matching event in reverse chronological order."""
        conditions: list[str] = []
        parameters: list[Any] = []
        if level:
            conditions.append("level = ?")
            parameters.append(level.upper())
        if module:
            conditions.append("module = ?")
            parameters.append(module)
        if search:
            conditions.append("LOWER(message) LIKE ?")
            parameters.append(f"%{search.lower()}%")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            cursor = connection.execute(
                self._query(
                    f"SELECT * FROM monitor_events{where} ORDER BY id DESC"
                ),
                tuple(parameters),
            )
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                for row in rows:
                    yield dict(row)

    def record_delivery(
        self,
        *,
        source: str,
        started_at: str,
        status: str,
        to_count: int,
        cc_count: int,
        attachment: str,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                self._query(
                    "INSERT INTO deliveries "
                    "(source, started_at, completed_at, status, to_count, "
                    "cc_count, attachment, error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    source,
                    started_at,
                    utc_now(),
                    status,
                    to_count,
                    cc_count,
                    attachment,
                    error,
                ),
            )

    def list_deliveries(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                self._query(
                    "SELECT * FROM deliveries ORDER BY id DESC LIMIT ?"
                ),
                (min(max(limit, 1), 1000),),
            ).fetchall()
        return [dict(row) for row in rows]

    def queue_command(
        self,
        action: str,
        requested_by: str,
        payload: Mapping[str, Any] | None = None,
    ) -> int:
        if action not in COMMAND_ACTIONS:
            raise ValueError(f"Unsupported admin action: {action}")
        try:
            with self.connect() as connection:
                cursor = connection.execute(
                    self._query(
                        "INSERT INTO admin_commands "
                        "(action, status, requested_by, requested_at, payload_json) "
                        "VALUES (?, 'queued', ?, ?, ?) RETURNING id"
                    ),
                    (
                        action,
                        requested_by,
                        utc_now(),
                        json.dumps(payload or {}, default=str),
                    ),
                )
                row = cursor.fetchone()
        except Exception as error:
            with self.connect() as connection:
                pending = connection.execute(
                    self._query(
                        "SELECT id FROM admin_commands WHERE action=? "
                        "AND status IN ('queued', 'running') LIMIT 1"
                    ),
                    (action,),
                ).fetchone()
            if pending:
                raise ValueError(
                    f"Action {action!r} already has a queued or running command"
                ) from error
            raise
        return int(dict(row)["id"])

    def claim_commands(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                self._query(
                    "SELECT * FROM admin_commands WHERE status='queued' "
                    "ORDER BY id LIMIT ?"
                ),
                (min(max(limit, 1), 50),),
            ).fetchall()
        claimed: list[dict[str, Any]] = []
        for row in rows:
            command = dict(row)
            with self.connect() as connection:
                cursor = connection.execute(
                    self._query(
                        "UPDATE admin_commands SET status='running', started_at=? "
                        "WHERE id=? AND status='queued'"
                    ),
                    (utc_now(), command["id"]),
                )
                was_claimed = cursor.rowcount == 1
            if was_claimed:
                command["status"] = "running"
                claimed.append(command)
        return claimed

    def recover_running_commands(self) -> int:
        """Fail orphaned commands after a worker restart to avoid duplicate sends."""
        with self.connect() as connection:
            cursor = connection.execute(
                self._query(
                    "UPDATE admin_commands SET status='failed', completed_at=?, "
                    "result='Worker restarted before completion; verify delivery before retrying' "
                    "WHERE status='running'"
                ),
                (utc_now(),),
            )
            recovered = cursor.rowcount
        return recovered

    def finish_command(self, command_id: int, succeeded: bool, result: str) -> None:
        status = "completed" if succeeded else "failed"
        if status not in COMMAND_STATUSES:
            raise ValueError("Invalid command status")
        with self.connect() as connection:
            connection.execute(
                self._query(
                    "UPDATE admin_commands SET status=?, completed_at=?, result=? WHERE id=?"
                ),
                (status, utc_now(), result[:10000], command_id),
            )

    def cancel_command(self, command_id: int, requested_by: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                self._query(
                    "UPDATE admin_commands SET status='cancelled', completed_at=?, result=? "
                    "WHERE id=? AND status='queued'"
                ),
                (utc_now(), f"Cancelled by {requested_by}", command_id),
            )
            changed = cursor.rowcount == 1
        return changed

    def list_commands(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                self._query(
                    "SELECT * FROM admin_commands ORDER BY id DESC LIMIT ?"
                ),
                (min(max(limit, 1), 1000),),
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_stats(self) -> dict[str, int]:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with self.connect() as connection:
            row = connection.execute(
                self._query(
                    "SELECT "
                    "COUNT(*) AS total_24h, "
                    "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_24h, "
                    "SUM(CASE WHEN status!='success' THEN 1 ELSE 0 END) AS failed_24h "
                    "FROM deliveries WHERE completed_at >= ?"
                ),
                (since,),
            ).fetchone()
            queued = connection.execute(
                "SELECT COUNT(*) AS count FROM admin_commands "
                "WHERE status IN ('queued', 'running')"
            ).fetchone()
        values = dict(row)
        return {
            "total_24h": int(values.get("total_24h") or 0),
            "success_24h": int(values.get("success_24h") or 0),
            "failed_24h": int(values.get("failed_24h") or 0),
            "pending_commands": int(dict(queued)["count"]),
        }


class DatabaseLogHandler(logging.Handler):
    """Persist formatted application logs without recursively logging failures."""

    def __init__(self, store: MonitoringStore):
        super().__init__(level=logging.NOTSET)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.store.record_event(
                record.levelname,
                record.name,
                self.format(record),
                {"pathname": record.pathname, "line": record.lineno},
            )
        except Exception:
            self.handleError(record) if logging.raiseExceptions else None


def attach_database_logging(logger: logging.Logger, store: MonitoringStore) -> None:
    if any(isinstance(handler, DatabaseLogHandler) for handler in logger.handlers):
        return
    handler = DatabaseLogHandler(store)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
