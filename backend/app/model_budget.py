"""One approved local test run: durable reservations, no refunds or implicit reset."""

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

POLICY = (1, 1_000_000, 20, 10_800, 5_000, 100, "deepseek-flash", "2026-10-03")


class BudgetUnavailable(Exception):
    pass


def initialize_budget(path: Path) -> None:
    """Explicit one-time initialization after user approval. Existing ledgers are never replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE policy (
            schema_version INTEGER, limit_microyuan INTEGER, max_requests INTEGER,
            reserve_microyuan INTEGER, input_limit INTEGER, output_limit INTEGER, model TEXT, approved_date TEXT
        )""")
        db.execute("INSERT INTO policy VALUES (?, ?, ?, ?, ?, ?, ?, ?)", POLICY)
        db.execute("""CREATE TABLE attempts (
            id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
            attempt_number INTEGER NOT NULL, outcome TEXT NOT NULL,
            prompt_tokens INTEGER, completion_tokens INTEGER, estimated_microyuan INTEGER,
            status_code INTEGER, knowledge_id TEXT, duration_ms INTEGER
        )""")


class BudgetLedger:
    def __init__(self, path: Path, timeout: float = 5) -> None:
        self.path = path
        self.timeout = max(0, min(5, timeout))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self.path.is_symlink() or not self.path.is_file():
            raise BudgetUnavailable("Approved budget missing")
        db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=self.timeout)
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT * FROM policy").fetchall() != [POLICY]:
                raise BudgetUnavailable("Approved policy changed")
            yield db
            db.commit()
        except (OSError, sqlite3.Error):
            raise BudgetUnavailable("Budget storage unavailable") from None
        finally:
            if db is not None:
                db.close()

    def reserve(self, attempt_number: int) -> int:
        with self.connection() as db:
            count = db.execute("SELECT count(*) FROM attempts").fetchone()[0]
            anomalies = db.execute("SELECT count(*) FROM attempts WHERE outcome = 'usage_exceeded'").fetchone()[0]
            if anomalies or count >= POLICY[2] or (count + 1) * POLICY[3] > POLICY[1]:
                raise BudgetUnavailable("Approved budget exhausted or frozen")
            result = db.execute(
                "INSERT INTO attempts (started_at, attempt_number, outcome) VALUES (?, ?, 'reserved')",
                (time.time(), attempt_number),
            )
            assert result.lastrowid is not None
            return result.lastrowid

    def finish(
        self, ticket: int, outcome: str, usage: Any, status_code: int | None,
        knowledge_id: str | None, duration_ms: int,
    ) -> None:
        prompt = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        completion = usage.get("completion_tokens") if isinstance(usage, dict) else None
        prompt = prompt if type(prompt) is int and prompt >= 0 else None
        completion = completion if type(completion) is int and completion >= 0 else None
        cost = prompt * 2 + completion * 8 if prompt is not None and completion is not None else None
        exceeded = (prompt is not None and prompt > POLICY[4]) or (completion is not None and completion > POLICY[5])
        with self.connection() as db:
            updated = db.execute("""UPDATE attempts SET finished_at=?, outcome=?, prompt_tokens=?,
                completion_tokens=?, estimated_microyuan=?, status_code=?, knowledge_id=?, duration_ms=?
                WHERE id=? AND finished_at IS NULL""",
                (time.time(), "usage_exceeded" if exceeded else outcome, prompt, completion, cost,
                 status_code, knowledge_id, duration_ms, ticket),
            )
            if updated.rowcount != 1:
                raise BudgetUnavailable("Budget record conflict")
        if exceeded:
            raise BudgetUnavailable("Usage exceeded reservation assumptions")

    def snapshot(self) -> dict[str, Any]:
        with self.connection() as db:
            db.row_factory = sqlite3.Row
            attempts = [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY id")]
            costs = [row["estimated_microyuan"] for row in attempts if row["estimated_microyuan"] is not None]
            return {
                "limit_yuan": POLICY[1] / 1_000_000,
                "max_requests": POLICY[2], "requests": len(attempts),
                "reserved_yuan": len(attempts) * POLICY[3] / 1_000_000,
                "estimated_yuan_at_peak_rates": sum(costs) / 1_000_000,
                "unknown_usage_requests": len(attempts) - len(costs), "attempts": attempts,
            }
