# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Durable local run ledger with transactional queue claiming."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from firefly_forge.models import RepositoryInput, RunInput

Data = dict[str, Any]
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS repositories(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=20000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_repository(self, request: RepositoryInput) -> Data:
        data = {**request.model_dump(), "id": uuid.uuid4().hex, "created_at": now()}
        with self.connection() as db:
            db.execute("INSERT INTO repositories VALUES(?,?)", (data["id"], json.dumps(data)))
        return data

    def repositories(self) -> list[Data]:
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM repositories ORDER BY rowid DESC")]

    def repository(self, identifier: str) -> Data:
        with self.connection() as db:
            row = db.execute("SELECT data FROM repositories WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError("Repository not found")
        return dict(json.loads(row[0]))

    def create_run(self, request: RunInput) -> Data:
        repo = self.repository(request.repository_id)
        data = {
            **request.model_dump(),
            "id": uuid.uuid4().hex,
            "repository_name": repo["name"],
            "status": "queued",
            "created_at": now(),
            "updated_at": now(),
            "verification": [],
            "changed_files": [],
            "diff": "",
            "artifacts": [],
            "error": None,
            "pr_url": None,
            "summary": "",
            "branch": "",
            "base_revision": "",
        }
        with self.connection() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?)", (data["id"], "queued", json.dumps(data)))
        self.event(data["id"], "queued", "Run queued")
        return self.get_run(data["id"])

    def get_run(self, identifier: str) -> Data:
        with self.connection() as db:
            row = db.execute("SELECT data FROM runs WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyError("Run not found")
            data: Data = json.loads(row[0])
            data["events"] = [
                json.loads(event[0])
                for event in db.execute("SELECT data FROM events WHERE run_id=? ORDER BY seq", (identifier,))
            ]
        return data

    def runs(self) -> list[Data]:
        with self.connection() as db:
            identifiers = [row[0] for row in db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 500")]
        return [self.get_run(identifier) for identifier in identifiers]

    def queued(self) -> list[str]:
        with self.connection() as db:
            return [row[0] for row in db.execute("SELECT id FROM runs WHERE status='queued' ORDER BY rowid")]

    def update(self, identifier: str, **fields: Any) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM runs WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyError("Run not found")
            data = json.loads(row[0])
            if data["status"] in TERMINAL and fields.get("status", data["status"]) != data["status"]:
                return
            data.update(fields, updated_at=now())
            db.execute("UPDATE runs SET status=?,data=? WHERE id=?", (data["status"], json.dumps(data), identifier))

    def claim(self, identifier: str) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM runs WHERE id=? AND status='queued'", (identifier,)).fetchone()
            if row is None:
                return False
            data = json.loads(row[0])
            data.update(status="preparing", updated_at=now())
            db.execute("UPDATE runs SET status='preparing',data=? WHERE id=?", (json.dumps(data), identifier))
        return True

    def event(self, identifier: str, stage: str, message: str) -> None:
        data = {"id": uuid.uuid4().hex, "stage": stage, "message": message[:8000], "created_at": now()}
        with self.connection() as db:
            db.execute("INSERT INTO events(run_id,data) VALUES(?,?)", (identifier, json.dumps(data)))

    def finish(self, identifier: str, status: str, *, error: str | None = None) -> None:
        if status not in TERMINAL:
            raise ValueError("Not a terminal run status")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM runs WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyError("Run not found")
            current = json.loads(row[0])
            if current["status"] in TERMINAL:
                return
            current.update(status=status, error=error, updated_at=now())
            db.execute("UPDATE runs SET status=?,data=? WHERE id=?", (status, json.dumps(current), identifier))
            event = {"id": uuid.uuid4().hex, "stage": status, "message": error or f"Run {status}", "created_at": now()}
            db.execute("INSERT INTO events(run_id,data) VALUES(?,?)", (identifier, json.dumps(event)))

    def recover(self) -> int:
        with self.connection() as db:
            identifiers = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM runs WHERE status NOT IN ('queued','succeeded','failed','cancelled','interrupted')"
                )
            ]
        for identifier in identifiers:
            self.finish(
                identifier, "interrupted", error="Worker stopped before completion; retry to start a fresh clone"
            )
        return len(identifiers)
