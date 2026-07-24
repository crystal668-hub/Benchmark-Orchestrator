from __future__ import annotations

import json
import mimetypes
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import OrchestratorError

STATUS_AXES = (
    "run_lifecycle_status",
    "protocol_completion_status",
    "protocol_acceptance_status",
    "answer_availability",
    "answer_reliability",
    "evaluable",
    "scored",
    "recovery_mode",
    "degraded_execution",
    "execution_error_kind",
)
ALLOWED_ASSET_ROOTS = {
    "results.json",
    "runtime-manifest.json",
    "skill-health.json",
    "web-search-preflight.json",
    "progress",
    "analysis",
    "waves",
    "input-bundles",
    "agent-workspace-archives",
    "agent-workspace-quarantine",
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_json(path: Path, default: Any) -> Any:
    try:
        return _load_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _valid_record(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") in (2, 3)
        and isinstance(payload.get("group_id"), str)
        and isinstance(payload.get("record_id"), str)
    )


class ArtifactReader:
    def __init__(self, run_root: Path) -> None:
        self.run_root = run_root.expanduser().resolve()

    @staticmethod
    def looks_like_run(path: Path) -> bool:
        return any(
            (path / name).exists()
            for name in (
                "results.json",
                "runtime-manifest.json",
                "per-record",
                "waves",
                "progress",
            )
        )

    def candidate_run_dirs(self) -> list[Path]:
        candidates: list[Path] = []
        if not self.run_root.exists():
            return candidates
        if self.looks_like_run(self.run_root):
            return [self.run_root]
        for current, directories, _files in os.walk(self.run_root):
            path = Path(current)
            if path == self.run_root or not self.looks_like_run(path):
                continue
            candidates.append(path.resolve())
            directories.clear()
        return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)

    def run_dir(self, run_id: str) -> Path:
        if Path(run_id).name != run_id:
            raise OrchestratorError("invalid_request", "Invalid run id")
        matches = [path for path in self.candidate_run_dirs() if path.name == run_id]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise OrchestratorError(
                "run_not_found", f"Ambiguous run id: {run_id}", status_code=404
            )
        raise OrchestratorError(
            "run_not_found", f"Unknown run: {run_id}", status_code=404
        )

    @staticmethod
    def _result_files(run_dir: Path) -> Iterable[Path]:
        per_record = run_dir / "per-record"
        return sorted(per_record.glob("*/*.json")) if per_record.is_dir() else []

    def checkpoint_state(
        self, run_id: str
    ) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str]]:
        run_dir = self.run_dir(run_id)
        return self._checkpoint_state_in_dir(run_dir)

    def _checkpoint_state_in_dir(
        self, run_dir: Path
    ) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str]]:
        committed: dict[tuple[str, str], dict[str, Any]] = {}
        invalid: list[str] = []
        for path in self._result_files(run_dir):
            try:
                payload = _load_json(path)
            except (OSError, UnicodeError, json.JSONDecodeError):
                invalid.append(str(path.relative_to(run_dir)))
                continue
            if not _valid_record(payload):
                invalid.append(str(path.relative_to(run_dir)))
                continue
            key = (payload["group_id"], payload["record_id"])
            if key in committed:
                invalid.append(str(path.relative_to(run_dir)))
                continue
            committed[key] = payload
        return committed, invalid

    def checkpoint_state_at(
        self, output_dir: str | Path
    ) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str]]:
        path = Path(output_dir).expanduser().resolve()
        if not path.is_relative_to(self.run_root):
            raise OrchestratorError(
                "path_outside_root", "Output directory escaped run root"
            )
        if not path.exists():
            return {}, []
        return self._checkpoint_state_in_dir(path)

    def final_artifacts_valid(
        self, run_id: str, selected_pairs: Iterable[tuple[str, str]]
    ) -> bool:
        try:
            run_dir = self.run_dir(run_id)
            results = _load_json(run_dir / "results.json")
            progress = _load_json(run_dir / "progress/state.json")
            committed, invalid = self.checkpoint_state(run_id)
        except (OSError, UnicodeError, json.JSONDecodeError, OrchestratorError):
            return False
        return (
            isinstance(results, dict)
            and results.get("schema_version") in (2, 3)
            and isinstance(results.get("results"), list)
            and isinstance(progress, dict)
            and progress.get("status") == "completed"
            and not invalid
            and set(selected_pairs).issubset(committed)
        )

    def load_results(self, run_dir: Path) -> list[dict[str, Any]]:
        results_path = run_dir / "results.json"
        if results_path.is_file():
            payload = _safe_json(results_path, {})
            results = payload.get("results") if isinstance(payload, dict) else None
            if isinstance(results, list):
                return [item for item in results if _valid_record(item)]
        return [
            payload
            for path in self._result_files(run_dir)
            if _valid_record(payload := _safe_json(path, {}))
        ]

    @staticmethod
    def _group_ids(
        payload: dict[str, Any], results: list[dict[str, Any]], run_dir: Path
    ) -> list[str]:
        groups = (
            payload.get("groups") if isinstance(payload.get("groups"), list) else []
        )
        ids = [
            str(item.get("id"))
            for item in groups
            if isinstance(item, dict) and item.get("id")
        ]
        if not ids:
            ids = sorted(
                {str(item["group_id"]) for item in results if item.get("group_id")}
            )
        if not ids and (run_dir / "per-record").is_dir():
            ids = sorted(
                path.name
                for path in (run_dir / "per-record").iterdir()
                if path.is_dir()
            )
        return ids

    def progress(
        self, run_id: str, *, expected_pairs: Iterable[tuple[str, str]] | None = None
    ) -> dict[str, Any]:
        run_dir = self.run_dir(run_id)
        state = _safe_json(run_dir / "progress/state.json", {})
        state = state if isinstance(state, dict) else {}
        committed, invalid = self.checkpoint_state(run_id)
        pairs = set(expected_pairs or committed)
        groups: dict[str, Any] = {}
        raw_groups = (
            state.get("groups") if isinstance(state.get("groups"), dict) else {}
        )
        for group_id in sorted({group for group, _ in pairs} | set(raw_groups)):
            completed_records = sorted(
                record for group, record in committed if group == group_id
            )
            raw = (
                raw_groups.get(group_id)
                if isinstance(raw_groups.get(group_id), dict)
                else {}
            )
            groups[group_id] = {
                **raw,
                "completed_records": completed_records,
                "completed_count": len(completed_records),
                "total_count": sum(1 for group, _ in pairs if group == group_id),
            }
        return {
            **state,
            "source": "progress_state" if state else "per_record",
            "total": len(pairs),
            "completed": len(set(committed) & pairs) if pairs else len(committed),
            "groups": groups,
            "invalid_checkpoints": invalid,
        }

    def list_runs(self) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for run_dir in self.candidate_run_dirs():
            payload = _safe_json(run_dir / "results.json", {})
            payload = payload if isinstance(payload, dict) else {}
            results = self.load_results(run_dir)
            groups = self._group_ids(payload, results, run_dir)
            records = {str(item["record_id"]) for item in results}
            pairs = {
                (str(item["group_id"]), str(item["record_id"])) for item in results
            }
            datasets = sorted(
                {
                    str(item.get("dataset") or "")
                    for item in results
                    if item.get("dataset")
                }
            )
            runs.append(
                {
                    "run_id": run_dir.name,
                    "path": str(run_dir),
                    "generated_at": payload.get("generated_at"),
                    "status": "completed"
                    if (run_dir / "results.json").is_file()
                    else "pending",
                    "record_count": int(payload.get("records") or len(records)),
                    "group_count": len(groups),
                    "groups": groups,
                    "datasets": datasets,
                    "progress": self.progress(run_dir.name, expected_pairs=pairs),
                    "summary": payload.get("summary")
                    if isinstance(payload.get("summary"), dict)
                    else {},
                }
            )
        return runs

    def get_run(
        self, run_id: str, *, expected_pairs: Iterable[tuple[str, str]] | None = None
    ) -> dict[str, Any]:
        run_dir = self.run_dir(run_id)
        payload = _safe_json(run_dir / "results.json", {})
        payload = payload if isinstance(payload, dict) else {}
        results = self.load_results(run_dir)
        groups = self._group_ids(payload, results, run_dir)
        return {
            "run_id": run_id,
            "path": str(run_dir),
            "results_schema_version": payload.get("schema_version"),
            "generated_at": payload.get("generated_at"),
            "groups": groups,
            "progress": self.progress(run_id, expected_pairs=expected_pairs),
            "summary": payload.get("summary")
            if isinstance(payload.get("summary"), dict)
            else {},
            "record_count": len({item["record_id"] for item in results}),
        }

    def list_records(self, run_id: str) -> list[dict[str, Any]]:
        run_dir = self.run_dir(run_id)
        results = self.load_results(run_dir)
        rows: list[dict[str, Any]] = []
        for item in results:
            evaluation = (
                item.get("evaluation")
                if isinstance(item.get("evaluation"), dict)
                else {}
            )
            rows.append(
                {
                    "group_id": item["group_id"],
                    "record_id": item["record_id"],
                    "dataset": item.get("dataset"),
                    "subset": item.get("subset"),
                    "score": evaluation.get(
                        "normalized_score", evaluation.get("score")
                    ),
                    "primary_metric": evaluation.get("primary_metric"),
                    **{axis: item.get(axis) for axis in STATUS_AXES},
                }
            )
        return rows

    def get_record(self, run_id: str, record_id: str) -> dict[str, Any]:
        run_dir = self.run_dir(run_id)
        matching = [
            item
            for item in self.load_results(run_dir)
            if item.get("record_id")
            in {record_id, record_id.replace("-", "_"), record_id.replace("_", "-")}
        ]
        if not matching:
            raise OrchestratorError(
                "record_not_found", f"Unknown record: {record_id}", status_code=404
            )
        first = matching[0]
        return {
            "run_id": run_id,
            "record_id": first["record_id"],
            "dataset": first.get("dataset"),
            "subset": first.get("subset"),
            "prompt": first.get("prompt"),
            "reference_answer": first.get("reference_answer"),
            "groups": matching,
        }

    def resolve_asset(self, run_id: str, asset_path: str) -> tuple[Path, str]:
        run_dir = self.run_dir(run_id)
        relative = Path(asset_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or relative.parts[0] not in ALLOWED_ASSET_ROOTS
        ):
            raise OrchestratorError(
                "asset_access_denied", "Artifact path is not allowed", status_code=403
            )
        resolved = (run_dir / relative).resolve()
        if not resolved.is_relative_to(run_dir) or not resolved.is_file():
            raise OrchestratorError(
                "asset_access_denied",
                "Artifact path is outside the Run",
                status_code=403,
            )
        return resolved, mimetypes.guess_type(resolved.name)[
            0
        ] or "application/octet-stream"
