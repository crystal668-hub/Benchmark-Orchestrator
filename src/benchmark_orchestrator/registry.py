from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .models import FrozenRun, OrchestratorError, PreviewSnapshot, RunControl


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write(
        path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def atomic_write_yaml(path: Path, payload: Any) -> None:
    _atomic_write(
        path,
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8"),
    )


class FileControlRegistry:
    def __init__(self, control_root: Path) -> None:
        self.root = control_root.expanduser().resolve()
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        os.chmod(self.runs_root, 0o700)
        self._lock_stream: Any = None
        self._run_locks: dict[str, asyncio.Lock] = {}

    def acquire_backend_lock(self) -> None:
        if self._lock_stream is not None:
            return
        lock_path = self.root / "backend.lock"
        stream = lock_path.open("a+b")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise OrchestratorError(
                "backend_already_running",
                f"Another Backend owns {lock_path}",
                status_code=503,
            ) from exc
        self._lock_stream = stream

    def release_backend_lock(self) -> None:
        if self._lock_stream is None:
            return
        fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_UN)
        self._lock_stream.close()
        self._lock_stream = None

    def run_lock(self, run_id: str) -> asyncio.Lock:
        return self._run_locks.setdefault(run_id, asyncio.Lock())

    def run_dir(self, run_id: str) -> Path:
        if not run_id or Path(run_id).name != run_id or "/" in run_id or "\\" in run_id:
            raise OrchestratorError("invalid_request", "Invalid run id")
        path = (self.runs_root / run_id).resolve()
        if not path.is_relative_to(self.runs_root):
            raise OrchestratorError(
                "path_outside_root", "Control path escaped control root"
            )
        return path

    def create_run(
        self, frozen: FrozenRun, preview: PreviewSnapshot, control: RunControl
    ) -> None:
        run_dir = self.run_dir(frozen.run_id)
        if run_dir.exists() and any(run_dir.iterdir()):
            raise OrchestratorError(
                "run_conflict", "Run control directory already exists", status_code=409
            )
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write_yaml(run_dir / "spec.yaml", frozen.model_dump(mode="json"))
        atomic_write_json(run_dir / "preview.json", preview.model_dump(mode="json"))
        atomic_write_json(run_dir / "control.json", control.model_dump(mode="json"))

    def load_frozen(self, run_id: str) -> FrozenRun:
        path = self.run_dir(run_id) / "spec.yaml"
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            return FrozenRun.model_validate(payload)
        except FileNotFoundError as exc:
            raise OrchestratorError(
                "run_not_found", f"Unknown run: {run_id}", status_code=404
            ) from exc
        except Exception as exc:
            raise OrchestratorError(
                "invalid_sidecar",
                f"Invalid frozen spec for {run_id}: {exc}",
                status_code=409,
            ) from exc

    def load_preview(self, run_id: str) -> PreviewSnapshot:
        path = self.run_dir(run_id) / "preview.json"
        try:
            return PreviewSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise OrchestratorError(
                "invalid_sidecar",
                f"Invalid preview for {run_id}: {exc}",
                status_code=409,
            ) from exc

    def load_control(self, run_id: str) -> RunControl:
        path = self.run_dir(run_id) / "control.json"
        try:
            return RunControl.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise OrchestratorError(
                "run_not_found", f"Unknown controlled run: {run_id}", status_code=404
            ) from exc
        except Exception as exc:
            raise OrchestratorError(
                "invalid_sidecar",
                f"Invalid control state for {run_id}: {exc}",
                status_code=409,
            ) from exc

    def save_control(self, control: RunControl) -> None:
        atomic_write_json(
            self.run_dir(control.run_id) / "control.json",
            control.model_dump(mode="json"),
        )

    def mutate_control(
        self, run_id: str, mutate: Callable[[RunControl], RunControl]
    ) -> RunControl:
        updated = mutate(self.load_control(run_id))
        self.save_control(updated)
        return updated

    def list_controlled_run_ids(self) -> list[str]:
        return sorted(
            path.name
            for path in self.runs_root.iterdir()
            if (path / "control.json").is_file()
        )

    def find_request(self, request_id: str) -> tuple[FrozenRun, RunControl] | None:
        for run_id in self.list_controlled_run_ids():
            control = self.load_control(run_id)
            if any(
                invocation.request_id == request_id
                for invocation in control.invocations
            ):
                return self.load_frozen(run_id), control
        return None

    def invocation_log_path(self, run_id: str, invocation_id: str) -> Path:
        if not invocation_id or Path(invocation_id).name != invocation_id:
            raise OrchestratorError("invalid_request", "Invalid invocation id")
        path = self.run_dir(run_id) / "invocations" / invocation_id / "launcher.log"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    def read_log(
        self, run_id: str, invocation_id: str, *, offset: int, limit: int
    ) -> dict[str, Any]:
        control = self.load_control(run_id)
        invocation = next(
            (
                item
                for item in control.invocations
                if item.invocation_id == invocation_id
            ),
            None,
        )
        if invocation is None:
            raise OrchestratorError(
                "invocation_not_found", "Unknown invocation", status_code=404
            )
        if offset < 0 or limit < 1 or limit > 65_536:
            raise OrchestratorError(
                "invalid_request", "Log offset/limit is out of range"
            )
        path = self.invocation_log_path(run_id, invocation_id)
        if not path.exists():
            return {"data": "", "offset": offset, "next_offset": offset, "eof": True}
        size = path.stat().st_size
        with path.open("rb") as stream:
            stream.seek(min(offset, size))
            data = stream.read(limit)
        next_offset = min(offset, size) + len(data)
        return {
            "data": data.decode("utf-8", errors="replace"),
            "offset": offset,
            "next_offset": next_offset,
            "eof": next_offset >= size,
            "log_truncated": invocation.log_truncated,
        }
