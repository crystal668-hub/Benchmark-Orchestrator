from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import signal
import uuid
from dataclasses import dataclass
from pathlib import Path

from .artifacts import ArtifactReader
from .config import LauncherConfig
from .models import (
    ACTIVE_STATES,
    Invocation,
    OrchestratorError,
    RunControl,
    RuntimeCommand,
    utc_now,
)
from .registry import FileControlRegistry
from .runtime_adapter import sha256_argv


@dataclass
class ProcessHandle:
    process: asyncio.subprocess.Process
    drain_task: asyncio.Task[bool]
    wait_task: asyncio.Task[None] | None = None


def process_fingerprint(
    pid: int, started_at: str, executable: str, argv_sha256: str
) -> str:
    payload = f"{pid}\0{started_at}\0{executable}\0{argv_sha256}".encode()
    return hashlib.sha256(payload).hexdigest()


async def process_start_time(pid: int) -> str | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "ps",
            "-p",
            str(pid),
            "-o",
            "lstart=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
    except OSError:
        return None
    value = stdout.decode("utf-8", errors="replace").strip()
    return value if process.returncode == 0 and value else None


def process_exists(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class LocalRunSupervisor:
    def __init__(
        self,
        launcher: LauncherConfig,
        registry: FileControlRegistry,
        artifacts: ArtifactReader,
    ) -> None:
        self.launcher = launcher
        self.registry = registry
        self.artifacts = artifacts
        self._handles: dict[str, ProcessHandle] = {}
        self._escalations: dict[str, asyncio.Task[None]] = {}
        self._detached_monitors: dict[str, asyncio.Task[None]] = {}

    @staticmethod
    def _active_invocation(control: RunControl) -> Invocation | None:
        if control.active_invocation_id is None:
            return None
        return next(
            (
                item
                for item in control.invocations
                if item.invocation_id == control.active_invocation_id
            ),
            None,
        )

    def _replace_invocation(
        self, control: RunControl, updated: Invocation, **control_updates: object
    ) -> RunControl:
        invocations = [
            updated if item.invocation_id == updated.invocation_id else item
            for item in control.invocations
        ]
        return control.model_copy(
            update={
                "invocations": invocations,
                "updated_at": utc_now(),
                **control_updates,
            }
        )

    async def _drain_log(self, stream: asyncio.StreamReader | None, path: Path) -> bool:
        if stream is None:
            return False
        written = 0
        truncated = False
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("ab", buffering=0) as destination:
            os.chmod(path, 0o600)
            while chunk := await stream.read(65_536):
                if written < self.launcher.max_log_bytes:
                    allowed = min(len(chunk), self.launcher.max_log_bytes - written)
                    destination.write(chunk[:allowed])
                    written += allowed
                    truncated = truncated or allowed < len(chunk)
                else:
                    truncated = True
        return truncated

    async def start(
        self,
        run_id: str,
        command: RuntimeCommand,
        *,
        kind: str,
        request_id: str,
    ) -> RunControl:
        control = self.registry.load_control(run_id)
        if control.state in ACTIVE_STATES or control.active_invocation_id is not None:
            raise OrchestratorError(
                "run_active", "Run already has an active invocation", status_code=409
            )

        invocation_id = uuid.uuid4().hex
        log_path = self.registry.invocation_log_path(run_id, invocation_id)
        invocation = Invocation(
            invocation_id=invocation_id,
            request_id=request_id,
            kind=kind,
            state="starting",
            argv_sha256=sha256_argv(command.argv),
            launcher_log=str(log_path),
            started_at=utc_now(),
        )
        control = control.model_copy(
            update={
                "state": "starting",
                "active_invocation_id": invocation_id,
                "invocations": [*control.invocations, invocation],
                "updated_at": utc_now(),
            }
        )
        self.registry.save_control(control)

        try:
            process = await asyncio.create_subprocess_exec(
                *command.argv,
                cwd=command.cwd,
                env=command.env,
                start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            started_at = await process_start_time(process.pid) or utc_now()
            executable = shutil.which(command.argv[0]) or command.argv[0]
            fingerprint = process_fingerprint(
                process.pid, started_at, executable, invocation.argv_sha256
            )
            updated_invocation = invocation.model_copy(
                update={
                    "state": "running",
                    "pid": process.pid,
                    "pgid": os.getpgid(process.pid),
                    "process_started_at": started_at,
                    "process_executable": executable,
                    "process_fingerprint": fingerprint,
                }
            )
            control = self._replace_invocation(
                control,
                updated_invocation,
                state="running",
                active_invocation_id=invocation_id,
            )
            self.registry.save_control(control)
            drain_task = asyncio.create_task(self._drain_log(process.stdout, log_path))
            handle = ProcessHandle(process=process, drain_task=drain_task)
            self._handles[run_id] = handle
            handle.wait_task = asyncio.create_task(
                self._wait(run_id, invocation_id, handle)
            )
            return control
        except OSError as exc:
            failed = invocation.model_copy(
                update={
                    "state": "failed",
                    "finished_at": utc_now(),
                    "error_code": "spawn_failed",
                    "error_message": str(exc),
                }
            )
            control = self._replace_invocation(
                control, failed, state="failed", active_invocation_id=None
            )
            self.registry.save_control(control)
            return control

    async def _wait(
        self, run_id: str, invocation_id: str, handle: ProcessHandle
    ) -> None:
        exit_code = await handle.process.wait()
        log_truncated = await handle.drain_task
        control = self.registry.load_control(run_id)
        invocation = next(
            item for item in control.invocations if item.invocation_id == invocation_id
        )
        cancelled = invocation.cancel_requested_at is not None
        frozen = self.registry.load_frozen(run_id)
        valid_final = self.artifacts.final_artifacts_valid(
            run_id, frozen.selected_pairs
        )
        if cancelled:
            terminal_state = "cancelled"
            error_code = None
            error_message = None
        elif exit_code == 0 and valid_final:
            terminal_state = "completed"
            error_code = None
            error_message = None
        elif exit_code == 0:
            terminal_state = "failed"
            error_code = "invalid_final_artifacts"
            error_message = (
                "Runtime exited successfully without complete final artifacts"
            )
        else:
            terminal_state = "failed"
            error_code = "runtime_exit_nonzero"
            error_message = f"Canonical CLI exited with code {exit_code}"
        updated = invocation.model_copy(
            update={
                "state": terminal_state,
                "finished_at": utc_now(),
                "exit_code": exit_code,
                "log_truncated": log_truncated,
                "error_code": error_code,
                "error_message": error_message,
            }
        )
        control = self._replace_invocation(
            control,
            updated,
            state=terminal_state,
            active_invocation_id=None,
        )
        self.registry.save_control(control)
        self._handles.pop(run_id, None)
        escalation = self._escalations.pop(run_id, None)
        if escalation and escalation is not asyncio.current_task():
            escalation.cancel()

    async def _identity_matches(self, invocation: Invocation) -> bool:
        if not process_exists(invocation.pid) or invocation.pid is None:
            return False
        started_at = await process_start_time(invocation.pid)
        if (
            not started_at
            or not invocation.process_executable
            or not invocation.process_fingerprint
        ):
            return False
        observed = process_fingerprint(
            invocation.pid,
            started_at,
            invocation.process_executable,
            invocation.argv_sha256,
        )
        return observed == invocation.process_fingerprint

    async def cancel(self, run_id: str, request_id: str) -> RunControl:
        control = self.registry.load_control(run_id)
        invocation = self._active_invocation(control)
        if control.state in {"cancelled", "completed", "failed", "interrupted"}:
            return control
        if invocation is None or control.state not in {
            "starting",
            "running",
            "cancelling",
        }:
            raise OrchestratorError(
                "run_not_active", "Run has no cancellable invocation", status_code=409
            )
        if control.state == "cancelling":
            return control
        handle = self._handles.get(run_id)
        if handle is None or invocation.ownership != "attached":
            raise OrchestratorError(
                "invocation_not_owned",
                "This Backend does not own the active invocation",
                status_code=409,
            )
        if not await self._identity_matches(invocation):
            raise OrchestratorError(
                "process_identity_mismatch",
                "Process identity no longer matches",
                status_code=409,
            )

        updated = invocation.model_copy(
            update={
                "state": "cancelling",
                "cancel_requested_at": utc_now(),
                "cancel_request_id": request_id,
                "terminating_signal": signal.SIGTERM,
            }
        )
        control = self._replace_invocation(
            control,
            updated,
            state="cancelling",
            active_invocation_id=invocation.invocation_id,
        )
        self.registry.save_control(control)
        os.kill(invocation.pid, signal.SIGTERM)
        self._escalations[run_id] = asyncio.create_task(self._escalate(run_id, updated))
        return control

    async def _escalate(self, run_id: str, invocation: Invocation) -> None:
        await asyncio.sleep(self.launcher.cancel_grace_seconds)
        if not await self._identity_matches(invocation):
            return
        try:
            os.killpg(invocation.pgid or invocation.pid or 0, signal.SIGTERM)
        except ProcessLookupError:
            return
        await asyncio.sleep(self.launcher.kill_after_seconds)
        if not await self._identity_matches(invocation):
            return
        try:
            os.killpg(invocation.pgid or invocation.pid or 0, signal.SIGKILL)
        except ProcessLookupError:
            return

    async def reconcile(self, run_id: str) -> RunControl:
        control = self.registry.load_control(run_id)
        if control.state not in ACTIVE_STATES:
            return control
        invocation = self._active_invocation(control)
        if invocation is None:
            control = control.model_copy(
                update={"state": "interrupted", "updated_at": utc_now()}
            )
            self.registry.save_control(control)
            return control
        if run_id in self._handles:
            return control
        frozen = self.registry.load_frozen(run_id)
        if not process_exists(invocation.pid):
            terminal = (
                "completed"
                if self.artifacts.final_artifacts_valid(run_id, frozen.selected_pairs)
                else "interrupted"
            )
            updated = invocation.model_copy(
                update={
                    "state": terminal,
                    "finished_at": utc_now(),
                    "ownership": "detached",
                }
            )
            control = self._replace_invocation(
                control, updated, state=terminal, active_invocation_id=None
            )
            self.registry.save_control(control)
            return control
        if not await self._identity_matches(invocation):
            updated = invocation.model_copy(
                update={
                    "state": "interrupted",
                    "ownership": "detached",
                    "error_code": "process_identity_mismatch",
                    "error_message": "PID exists but no longer matches the recorded invocation",
                }
            )
            control = self._replace_invocation(
                control, updated, state="interrupted", active_invocation_id=None
            )
            self.registry.save_control(control)
            return control
        updated = invocation.model_copy(
            update={"state": "running", "ownership": "detached"}
        )
        control = self._replace_invocation(control, updated, state="running")
        self.registry.save_control(control)
        if run_id not in self._detached_monitors:
            self._detached_monitors[run_id] = asyncio.create_task(
                self._monitor_detached(run_id)
            )
        return control

    async def _monitor_detached(self, run_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(1)
                control = self.registry.load_control(run_id)
                invocation = self._active_invocation(control)
                if invocation is None or not process_exists(invocation.pid):
                    await self.reconcile(run_id)
                    return
        finally:
            self._detached_monitors.pop(run_id, None)

    async def reconcile_all(self) -> None:
        for run_id in self.registry.list_controlled_run_ids():
            await self.reconcile(run_id)
