from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TaskStore:
    """把每一次转换任务记在 SQLite 里，关掉页面后仍然在。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    video_id TEXT,
                    title TEXT,
                    album TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    detail TEXT,
                    error TEXT,
                    output_path TEXT,
                    events TEXT NOT NULL DEFAULT '[]',
                    checkpoint TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            if "checkpoint" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN checkpoint TEXT")
            if "started_at" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN started_at TEXT")
            if "finished_at" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN finished_at TEXT")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def create(self, url: str, album: str, output_dir: str) -> dict:
        now = _now()
        events = json.dumps([{"stage": "queued", "detail": "排队", "at": now}], ensure_ascii=False)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tasks (
                    url, album, output_dir, status, stage, detail, events, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', 'queued', '排队', ?, ?, ?)
                """,
                (url, album, output_dir, events, now, now),
            )
            task_id = int(cur.lastrowid)
        return self.get(task_id)

    def update(self, task_id: int, **fields) -> dict:
        allowed = {
            "url",
            "video_id",
            "title",
            "album",
            "output_dir",
            "status",
            "stage",
            "detail",
            "error",
            "output_path",
            "checkpoint",
            "started_at",
            "finished_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"未知字段: {', '.join(sorted(unknown))}")
        with self._lock, self._connect() as conn:
            current = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if current is None:
                raise KeyError(task_id)
            if "checkpoint" in fields and not isinstance(fields["checkpoint"], str):
                value = fields["checkpoint"]
                fields["checkpoint"] = None if value is None else json.dumps(value, ensure_ascii=False)
            status = fields.get("status")
            now = _now()
            if status == "running" and not current["started_at"]:
                fields.setdefault("started_at", now)
            if status in {"done", "failed", "skipped", "stopped"}:
                fields["finished_at"] = now
            if status == "queued" and fields.get("detail") == "从失败处继续":
                fields["started_at"] = None
                fields["finished_at"] = None
            merged = dict(current)
            stage_changed = "stage" in fields and fields["stage"] != merged["stage"]
            detail_changed = "detail" in fields and fields["detail"] != merged["detail"]
            merged.update(fields)
            if stage_changed or detail_changed:
                events = json.loads(merged["events"] or "[]")
                events.append(
                    {
                        "stage": merged["stage"],
                        "detail": merged["detail"] or "",
                        "at": _now(),
                    }
                )
                merged["events"] = json.dumps(events, ensure_ascii=False)
            merged["updated_at"] = _now()
            columns = [
                "video_id",
                "title",
                "status",
                "stage",
                "detail",
                "error",
                "album",
                "output_path",
                "checkpoint",
                "started_at",
                "finished_at",
                "events",
                "updated_at",
            ]
            conn.execute(
                f"UPDATE tasks SET {', '.join(f'{col} = ?' for col in columns)} WHERE id = ?",
                [merged[col] for col in columns] + [task_id],
            )
        return self.get(task_id)

    def get(self, task_id: int) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._dump(row)

    def list_tasks(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
        return [self._dump(row) for row in rows]

    def delete(self, task_id: int) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["status"] == "running":
                raise RuntimeError("正在进行的任务不能删除")
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def find_done(self, video_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM tasks
                WHERE video_id = ? AND status = 'done' AND output_path IS NOT NULL
                ORDER BY id DESC LIMIT 1
                """,
                (video_id,),
            ).fetchone()
        return self._dump(row) if row else None

    def list_requeueable(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status IN ('queued', 'running') ORDER BY id ASC"
            ).fetchall()
        return [self._dump(row) for row in rows]

    @staticmethod
    def _dump(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["events"] = json.loads(data.get("events") or "[]")
        raw_checkpoint = data.get("checkpoint")
        data["checkpoint"] = json.loads(raw_checkpoint) if raw_checkpoint else None
        return data
