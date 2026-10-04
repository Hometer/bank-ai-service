"""Separately approved V1.5 ledger; never modify the V1.4 approval."""

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.model_budget import BudgetUnavailable

POLICY = (1, "2026-10-04", "deepseek-flash", 1_000_000, 50, 18_400, 6_000, 800)


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise BudgetUnavailable("Invalid budget path")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE policy (version, approved_date, model, cap, requests, reservation, input_cap, output_cap)"
        )
        db.execute("INSERT INTO policy VALUES (?,?,?,?,?,?,?,?)", POLICY)
        db.execute(
            "CREATE TABLE attempts (id INTEGER PRIMARY KEY, started REAL, finished REAL, outcome TEXT, input_tokens INTEGER, output_tokens INTEGER, estimated_microyuan INTEGER)"
        )


class Ledger:
    def __init__(self, path: Path | None) -> None:
        if path is None or path.is_symlink() or not path.is_file():
            raise BudgetUnavailable("Approved V1.5 budget missing")
        self.path = path

    def connection(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=2)
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT * FROM policy").fetchall() != [POLICY]:
                raise BudgetUnavailable("Budget policy changed")
        except Exception:
            db.close()
            raise
        return db

    def reserve(self) -> int:
        db = self.connection()
        try:
            count = db.execute("SELECT count(*) FROM attempts").fetchone()[0]
            frozen = db.execute("SELECT count(*) FROM attempts WHERE outcome='usage_exceeded'").fetchone()[0]
            if frozen or count >= POLICY[4] or (count + 1) * POLICY[5] > POLICY[3]:
                raise BudgetUnavailable("Budget exhausted")
            cursor = db.execute("INSERT INTO attempts(started,outcome) VALUES (?,'reserved')", (time.time(),))
            db.commit()
            assert cursor.lastrowid is not None
            return cursor.lastrowid
        finally:
            db.close()

    def finish(self, ticket: int, outcome: str, usage: Any) -> None:
        usage = usage if isinstance(usage, dict) else {}
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        prompt = prompt if type(prompt) is int and prompt >= 0 else None
        completion = completion if type(completion) is int and completion >= 0 else None
        exceeded = (prompt is not None and prompt > POLICY[6]) or (
            completion is not None and completion > POLICY[7]
        )
        cost = prompt * 2 + completion * 8 if prompt is not None and completion is not None else None
        db = self.connection()
        try:
            changed = db.execute(
                "UPDATE attempts SET finished=?,outcome=?,input_tokens=?,output_tokens=?,estimated_microyuan=? WHERE id=? AND finished IS NULL",
                (time.time(), "usage_exceeded" if exceeded else outcome, prompt, completion, cost, ticket),
            )
            if changed.rowcount != 1:
                raise BudgetUnavailable("Budget record conflict")
            db.commit()
        finally:
            db.close()
        if exceeded:
            raise BudgetUnavailable("Usage exceeded approved limits")

    def snapshot(self) -> dict[str, Any]:
        db = self.connection()
        try:
            count, total = db.execute(
                "SELECT count(*),coalesce(sum(estimated_microyuan),0) FROM attempts"
            ).fetchone()
            return {
                "requests": count,
                "max_requests": 50,
                "cap_yuan": 1,
                "reserved_yuan": count * POLICY[5] / 1_000_000,
                "estimated_yuan": total / 1_000_000,
            }
        finally:
            db.close()
