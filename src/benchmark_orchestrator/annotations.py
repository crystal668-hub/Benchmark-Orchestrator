from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .models import (
    AnnotationCreate,
    AnnotationPatch,
    MetadataPatch,
    OrchestratorError,
    utc_now,
)


class AnnotationStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS run_metadata (
                    run_id TEXT PRIMARY KEY, alias TEXT NOT NULL DEFAULT '',
                    favorite INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS annotations (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, record_id TEXT, group_id TEXT,
                    note TEXT NOT NULL, status TEXT NOT NULL, tags_json TEXT NOT NULL,
                    manual_verdict TEXT, deleted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _annotation(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["tags"] = json.loads(payload.pop("tags_json"))
        payload["deleted"] = bool(payload["deleted"])
        return payload

    def metadata(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM run_metadata WHERE run_id = ?", (run_id,)
            ).fetchone()
        return (
            dict(row)
            if row
            else {"run_id": run_id, "alias": "", "favorite": False, "hidden": False}
        )

    def patch_metadata(self, run_id: str, patch: MetadataPatch) -> dict[str, Any]:
        current = self.metadata(run_id)
        values = patch.model_dump(exclude_none=True)
        current.update(values)
        updated_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO run_metadata(run_id, alias, favorite, hidden, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET alias=excluded.alias, favorite=excluded.favorite, hidden=excluded.hidden, updated_at=excluded.updated_at",
                (
                    run_id,
                    current.get("alias", ""),
                    bool(current.get("favorite")),
                    bool(current.get("hidden")),
                    updated_at,
                ),
            )
        return {**current, "run_id": run_id, "updated_at": updated_at}

    def list_annotations(
        self, *, run_id: str | None = None, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            values.append(run_id)
        if not include_deleted:
            clauses.append("deleted = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM annotations{where} ORDER BY created_at", values
            ).fetchall()
        return [self._annotation(row) for row in rows]

    def create(self, request: AnnotationCreate) -> dict[str, Any]:
        annotation_id = uuid.uuid4().hex
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    annotation_id,
                    request.run_id,
                    request.record_id,
                    request.group_id,
                    request.note,
                    request.status,
                    json.dumps(request.tags, ensure_ascii=False),
                    request.manual_verdict,
                    0,
                    now,
                    now,
                ),
            )
        return self.get(annotation_id)

    def get(self, annotation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM annotations WHERE id = ?", (annotation_id,)
            ).fetchone()
        if row is None:
            raise OrchestratorError(
                "annotation_not_found", "Unknown annotation", status_code=404
            )
        return self._annotation(row)

    def update(self, annotation_id: str, patch: AnnotationPatch) -> dict[str, Any]:
        current = self.get(annotation_id)
        current.update(patch.model_dump(exclude_none=True))
        with self._connect() as connection:
            connection.execute(
                "UPDATE annotations SET note=?, status=?, tags_json=?, manual_verdict=?, updated_at=? WHERE id=?",
                (
                    current["note"],
                    current["status"],
                    json.dumps(current["tags"], ensure_ascii=False),
                    current.get("manual_verdict"),
                    utc_now(),
                    annotation_id,
                ),
            )
        return self.get(annotation_id)

    def delete(self, annotation_id: str) -> dict[str, Any]:
        self.get(annotation_id)
        with self._connect() as connection:
            connection.execute(
                "UPDATE annotations SET deleted=1, updated_at=? WHERE id=?",
                (utc_now(), annotation_id),
            )
        return self.get(annotation_id)
