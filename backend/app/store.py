"""Small transactional SQLite JSON store for a single local application process."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Callable


class Store:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / 'tasks.sqlite3'
        self.lock = threading.RLock()
        with self._connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            conn.execute('INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)', ('token', secrets.token_urlsafe(32)))
        with self._connect() as conn:
            self.token = conn.execute('SELECT value FROM settings WHERE key=?', ('token',)).fetchone()[0]

    def _connect(self):
        conn = sqlite3.connect(self.database, timeout=30)
        conn.execute('PRAGMA busy_timeout=30000')
        return conn

    def get(self, task_id: str):
        with self.lock, self._connect() as conn:
            row = conn.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def list(self):
        with self.lock, self._connect() as conn:
            rows = conn.execute('SELECT data FROM tasks ORDER BY rowid DESC').fetchall()
            return [json.loads(row[0]) for row in rows]

    def put(self, task: dict):
        with self.lock, self._connect() as conn:
            conn.execute('INSERT INTO tasks(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                         (task['id'], json.dumps(task, ensure_ascii=False)))
        return task

    def mutate(self, task_id: str, change: Callable[[dict], object]):
        with self.lock, self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            task = json.loads(row[0])
            result = change(task)
            conn.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(task, ensure_ascii=False), task_id))
            return task, result
