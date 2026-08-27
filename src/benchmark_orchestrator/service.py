from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .annotations import AnnotationStore
from .artifacts import ArtifactReader
from .models import (
    ACTIVE_STATES,
    CommandRequest,
    CreateRunRequest,
    FrozenRun,
    OrchestratorError,
    PreviewSnapshot,
    RunControl,
    RunSpec,
    utc_now,
)
from .registry import FileControlRegistry
from .runtime_adapter import CanonicalCliRuntimeAdapter, sha256_json
from .supervisor import LocalRunSupervisor, process_exists


class RunService:
    def __init__(
        self,
        adapter: CanonicalCliRuntimeAdapter,
        supervisor: LocalRunSupervisor,
        registry: FileControlRegistry,
        artifacts: ArtifactReader,
        annotations: AnnotationStore,
    ) -> None:
        self.adapter = adapter
        self.supervisor = supervisor
        self.registry = registry
        self.artifacts = artifacts
        self.annotations = annotations
        self._previews: dict[str, PreviewSnapshot] = {}
        self._global_lock = asyncio.Lock()

    async def capabilities(self) -> dict[str, Any]:
        return await self.adapter.inspect_capabilities()

    def _artifact_status(self, run_id: str, frozen: FrozenRun) -> dict[str, Any]:
        committed, invalid = self.artifacts.checkpoint_state_at(frozen.output_dir)
        selected = set(frozen.selected_pairs)
        committed_count = len(selected & set(committed))
        try:
            progress = self.artifacts.progress(
                run_id, expected_pairs=frozen.selected_pairs
            )
        except OrchestratorError:
            progress = {}
        final_valid = self.artifacts.final_artifacts_valid(
            run_id, frozen.selected_pairs
        )
        if final_valid:
            state = "completed"
        elif invalid:
            state = "invalid"
        elif committed_count:
            state = "partial"
        elif progress.get("status") in ACTIVE_STATES:
            state = "running"
        else:
            state = "pending"
        return {
            "state": state,
            "progress_status": progress.get("status"),
            "selected_count": len(selected),
            "committed_count": committed_count,
            "missing_count": len(selected - set(committed)),
            "invalid_checkpoints": invalid,
        }

    @staticmethod
    def _status_view(
        control_state: str | None, artifact_state: str
    ) -> dict[str, str | None]:
        if control_state is None:
            return {
                "control_state": None,
                "artifact_state": artifact_state,
                "effective_state": artifact_state,
                "consistency": "artifact_only",
            }
        if artifact_state == "completed" and control_state != "completed":
            consistency = (
                "artifact_completed_while_control_active"
                if control_state in ACTIVE_STATES
                else "artifact_completed_after_control_terminal"
            )
            return {
                "control_state": control_state,
                "artifact_state": artifact_state,
                "effective_state": "completed_with_recovery",
                "consistency": consistency,
            }
        if control_state == "completed" and artifact_state != "completed":
            return {
                "control_state": control_state,
                "artifact_state": artifact_state,
                "effective_state": "completed",
                "consistency": "control_completed_artifact_incomplete",
            }
        return {
            "control_state": control_state,
            "artifact_state": artifact_state,
            "effective_state": control_state,
            "consistency": "consistent",
        }

    @staticmethod
    def _record_status(result: dict[str, Any]) -> str | None:
        if result.get("scored") is True:
            return "completed"
        lifecycle = result.get("run_lifecycle_status")
        return lifecycle if isinstance(lifecycle, str) else None

    async def _ensure_runtime_ready(self) -> None:
        capabilities = await self.adapter.inspect_capabilities()
        if capabilities["ready"]:
            return
        failed_checks = [
            item for item in capabilities["checks"] if not item["ok"]
        ]
        raise OrchestratorError(
            "runtime_unavailable",
            "Runtime preflight failed",
            status_code=503,
            details={"failed_checks": failed_checks},
        )

    async def preview(self, spec: RunSpec) -> PreviewSnapshot:
        await self._ensure_runtime_ready()
        records = await self.adapter.execute_preview(spec)
        revision, dirty = await self.adapter.runtime_revision()
        release = self.adapter.release_identity()
        now = datetime.now(UTC)
        normalized = spec.model_dump(mode="json")
        snapshot = PreviewSnapshot(
            preview_id=uuid.uuid4().hex,
            spec_sha256=sha256_json(normalized),
            normalized_spec=spec,
            records=records,
            task_count=len(records),
            group_count=len(spec.groups),
            execution_count=len(records) * len(spec.groups),
            runtime_revision=revision,
            runtime_dirty=dirty,
            vgb_release_version=release["version"],
            vgb_wheel_sha256=release["wheel_sha256"],
            created_at=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            expires_at=(now + timedelta(minutes=10))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        )
        self._previews[snapshot.preview_id] = snapshot
        return snapshot

    def _preview_for_create(self, request: CreateRunRequest) -> PreviewSnapshot:
        preview = self._previews.get(request.preview_id)
        if preview is None:
            raise OrchestratorError(
                "preview_not_found", "Preview does not exist", status_code=404
            )
        expires_at = datetime.fromisoformat(preview.expires_at.replace("Z", "+00:00"))
        if expires_at <= datetime.now(UTC):
            self._previews.pop(request.preview_id, None)
            raise OrchestratorError(
                "preview_not_found", "Preview has expired", status_code=404
            )
        if preview.spec_sha256 != request.spec_sha256:
            raise OrchestratorError("invalid_request", "Preview digest does not match")
        return preview

    async def _ensure_no_active_run(self) -> None:
        for run_id in self.registry.list_controlled_run_ids():
            control = await self.supervisor.reconcile(run_id)
            if control.state in ACTIVE_STATES:
                raise OrchestratorError(
                    "active_run_limit",
                    "MVP allows only one active Run",
                    status_code=503,
                    details={"run_id": run_id},
                )

    async def _identity_matches_preview(self, preview: PreviewSnapshot) -> None:
        revision, dirty = await self.adapter.runtime_revision()
        release = self.adapter.release_identity()
        if (
            revision != preview.runtime_revision
            or dirty != preview.runtime_dirty
            or release["version"] != preview.vgb_release_version
            or release["wheel_sha256"] != preview.vgb_wheel_sha256
        ):
            raise OrchestratorError(
                "runtime_drift",
                "Runtime identity changed after preview; create a new preview",
                status_code=409,
            )

    async def create(self, request: CreateRunRequest) -> dict[str, Any]:
        async with self._global_lock:
            existing = self.registry.find_request(request.request_id)
            if existing:
                frozen, control = existing
                return self._command_response(frozen, control)
            await self._ensure_runtime_ready()
            preview = self._preview_for_create(request)
            await self._identity_matches_preview(preview)
            await self._ensure_no_active_run()
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            benchmark, model, run_id, output_dir = self.adapter.output_location(
                preview.normalized_spec, timestamp
            )
            if output_dir.exists() and any(output_dir.iterdir()):
                raise OrchestratorError(
                    "run_conflict", "Output directory already exists", status_code=409
                )
            if run_id in {path.name for path in self.artifacts.candidate_run_dirs()}:
                raise OrchestratorError(
                    "run_conflict", "Run ID is already present", status_code=409
                )
            frozen = FrozenRun(
                run_id=run_id,
                run_category="formal",
                benchmark_slug=benchmark,
                model_slug=model,
                output_dir=str(output_dir),
                workspace_root=str(self.adapter.workspace_root),
                spec=preview.normalized_spec,
                spec_sha256=preview.spec_sha256,
                selected_records=preview.records,
                selected_pairs=[
                    (group_id, record.record_id)
                    for group_id in preview.normalized_spec.groups
                    for record in preview.records
                ],
                runtime_revision=preview.runtime_revision,
                runtime_dirty=preview.runtime_dirty,
                vgb_release_version=preview.vgb_release_version,
                vgb_wheel_sha256=preview.vgb_wheel_sha256,
                created_at=utc_now(),
            )
            control = RunControl(run_id=run_id, state="created", updated_at=utc_now())
            self.registry.create_run(frozen, preview, control)
            command = self.adapter.build_run_command(frozen, resume=False)
            control = await self.supervisor.start(
                run_id,
                command,
                kind="start",
                request_id=request.request_id,
            )
            return self._command_response(frozen, control)

    @staticmethod
    def _command_response(frozen: FrozenRun, control: RunControl) -> dict[str, Any]:
        return {
            "run_id": frozen.run_id,
            "state": control.state,
            "output_dir": frozen.output_dir,
            "control_url": f"/api/runs/{frozen.run_id}/control",
            "progress_url": f"/api/runs/{frozen.run_id}/progress",
            "control": control.model_dump(mode="json"),
        }

    async def cancel(self, run_id: str, request: CommandRequest) -> dict[str, Any]:
        async with self.registry.run_lock(run_id):
            frozen = self.registry.load_frozen(run_id)
            control = await self.supervisor.cancel(run_id, request.request_id)
            return self._command_response(frozen, control)

    async def _ensure_resume_identity(self, frozen: FrozenRun) -> None:
        revision, dirty = await self.adapter.runtime_revision()
        release = self.adapter.release_identity()
        if (
            revision != frozen.runtime_revision
            or dirty != frozen.runtime_dirty
            or release["version"] != frozen.vgb_release_version
            or release["wheel_sha256"] != frozen.vgb_wheel_sha256
        ):
            raise OrchestratorError(
                "runtime_drift",
                "Runtime or VGB identity changed since the Run was created",
                status_code=409,
            )

    async def resume(self, run_id: str, request: CommandRequest) -> dict[str, Any]:
        async with self._global_lock, self.registry.run_lock(run_id):
            frozen = self.registry.load_frozen(run_id)
            control = await self.supervisor.reconcile(run_id)
            if any(
                item.request_id == request.request_id for item in control.invocations
            ):
                return self._command_response(frozen, control)
            if control.state == "completed":
                raise OrchestratorError(
                    "run_completed", "Completed Run cannot be resumed", status_code=409
                )
            if control.state in ACTIVE_STATES or control.active_invocation_id:
                raise OrchestratorError(
                    "run_active",
                    "Run already has an active invocation",
                    status_code=409,
                )
            if control.state not in {"failed", "cancelled", "interrupted"}:
                raise OrchestratorError(
                    "invalid_state", "Run is not resumable", status_code=409
                )
            for item in control.invocations:
                if item.error_code == "process_identity_mismatch" and process_exists(
                    item.pid
                ):
                    raise OrchestratorError(
                        "process_identity_mismatch",
                        "Conflicting PID still exists; Resume is blocked",
                        status_code=409,
                    )
            await self._ensure_no_active_run()
            await self._ensure_resume_identity(frozen)
            committed, invalid = self.artifacts.checkpoint_state_at(frozen.output_dir)
            if invalid:
                raise OrchestratorError(
                    "invalid_checkpoints",
                    "Run contains invalid per-record checkpoints",
                    status_code=409,
                    details={"paths": invalid},
                )
            missing = set(frozen.selected_pairs) - set(committed)
            if not missing:
                raise OrchestratorError(
                    "no_missing_records",
                    "All selected records already have committed checkpoints",
                    status_code=409,
                )
            command = self.adapter.build_run_command(frozen, resume=True)
            control = await self.supervisor.start(
                run_id,
                command,
                kind="resume",
                request_id=request.request_id,
            )
            return self._command_response(frozen, control)

    async def control(self, run_id: str) -> dict[str, Any]:
        frozen = self.registry.load_frozen(run_id)
        control = await self.supervisor.reconcile(run_id)
        artifact = self._artifact_status(run_id, frozen)
        return {
            **control.model_dump(mode="json"),
            "artifact": artifact,
            "status_view": self._status_view(control.state, artifact["state"]),
            "selected_count": artifact["selected_count"],
            "committed_count": artifact["committed_count"],
            "missing_count": artifact["missing_count"],
            "invalid_checkpoints": artifact["invalid_checkpoints"],
            "can_cancel": control.state in {"starting", "running"}
            and bool(control.active_invocation_id),
            "can_resume": control.state in {"failed", "cancelled", "interrupted"}
            and bool(artifact["missing_count"])
            and not artifact["invalid_checkpoints"],
        }

    async def list_runs(self, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        artifact_runs = {item["run_id"]: item for item in self.artifacts.list_runs()}
        controlled_ids = self.registry.list_controlled_run_ids()
        for run_id in controlled_ids:
            frozen = self.registry.load_frozen(run_id)
            control = await self.supervisor.reconcile(run_id)
            has_artifacts = run_id in artifact_runs
            if not has_artifacts and control.state not in ACTIVE_STATES:
                continue
            entry = artifact_runs.setdefault(
                run_id,
                {
                    "run_id": run_id,
                    "path": frozen.output_dir,
                    "status": "pending",
                    "record_count": len(frozen.selected_records),
                    "group_count": len(frozen.spec.groups),
                    "groups": frozen.spec.groups,
                    "datasets": frozen.spec.datasets,
                    "progress": {
                        "total": len(frozen.selected_pairs),
                        "completed": 0,
                        "groups": {},
                    },
                    "summary": {},
                },
            )
            if has_artifacts:
                entry["progress"] = self.artifacts.progress(
                    run_id, expected_pairs=frozen.selected_pairs
                )
            artifact = self._artifact_status(run_id, frozen)
            entry["control"] = {
                "state": control.state,
                "active_invocation_id": control.active_invocation_id,
            }
            entry["status_view"] = self._status_view(control.state, artifact["state"])
            entry["created_at"] = frozen.created_at
        rows: list[dict[str, Any]] = []
        for entry in artifact_runs.values():
            metadata = self.annotations.metadata(entry["run_id"])
            if metadata.get("hidden") and not include_hidden:
                continue
            if "status_view" not in entry:
                artifact_state = (
                    "completed"
                    if entry.get("status") == "completed"
                    else entry.get("progress", {}).get("status") or "pending"
                )
                entry["status_view"] = self._status_view(None, artifact_state)
            rows.append({**entry, **metadata})
        return sorted(
            rows,
            key=lambda item: item.get("created_at") or "",
            reverse=True,
        )

    async def get_run(self, run_id: str) -> dict[str, Any]:
        control: RunControl | None = None
        frozen: FrozenRun | None = None
        try:
            frozen = self.registry.load_frozen(run_id)
            control = await self.supervisor.reconcile(run_id)
        except OrchestratorError as exc:
            if exc.code != "run_not_found":
                raise
        try:
            snapshot = self.artifacts.get_run(
                run_id, expected_pairs=frozen.selected_pairs if frozen else None
            )
        except OrchestratorError as exc:
            if exc.code != "run_not_found" or frozen is None:
                raise
            committed, invalid = self.artifacts.checkpoint_state_at(frozen.output_dir)
            snapshot = {
                "run_id": run_id,
                "path": frozen.output_dir,
                "groups": frozen.spec.groups,
                "record_count": len(frozen.selected_records),
                "progress": {
                    "total": len(frozen.selected_pairs),
                    "completed": len(committed),
                    "invalid_checkpoints": invalid,
                    "groups": {},
                },
                "summary": {},
            }
        snapshot["control"] = control.model_dump(mode="json") if control else None
        snapshot["frozen"] = frozen.model_dump(mode="json") if frozen else None
        snapshot["metadata"] = self.annotations.metadata(run_id)
        if frozen is not None:
            artifact = self._artifact_status(run_id, frozen)
            snapshot["status_view"] = self._status_view(
                control.state if control else None, artifact["state"]
            )
        else:
            artifact_state = snapshot["progress"].get("status") or "pending"
            snapshot["status_view"] = self._status_view(None, artifact_state)
        return snapshot

    def tasks(self, run_id: str) -> list[dict[str, Any]]:
        try:
            frozen = self.registry.load_frozen(run_id)
        except OrchestratorError as exc:
            if exc.code != "run_not_found":
                raise
            run_dir = self.artifacts.run_dir(run_id)
            return [
                {
                    "group_id": result["group_id"],
                    "record_id": result["record_id"],
                    "selected": True,
                    "checkpoint": "committed",
                    "record_status": self._record_status(result),
                    "result": result,
                }
                for result in self.artifacts.load_results(run_dir)
            ]
        committed, invalid = self.artifacts.checkpoint_state_at(frozen.output_dir)
        invalid_pairs = {
            (parts[1], parts[2].removesuffix(".json"))
            for path in invalid
            if len(parts := Path(path).parts) == 3 and parts[0] == "per-record"
        }
        return [
            {
                "group_id": group_id,
                "record_id": record_id,
                "selected": True,
                "checkpoint": "committed"
                if (group_id, record_id) in committed
                else "invalid"
                if (group_id, record_id.replace("_", "-")) in invalid_pairs
                else "missing",
                "record_status": self._record_status(result)
                if (result := committed.get((group_id, record_id)))
                else None,
                "result": committed.get((group_id, record_id)),
            }
            for group_id, record_id in frozen.selected_pairs
        ]
