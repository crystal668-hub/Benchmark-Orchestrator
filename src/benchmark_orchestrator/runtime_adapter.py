from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .config import OrchestratorConfig
from .models import (
    FrozenRun,
    MINIMAX_M3_THINKING_LEVELS,
    OrchestratorError,
    RunSpec,
    RecordRange,
    RuntimeCommand,
    SelectionSpec,
    SelectedRecord,
    THINKING_LEVELS,
    thinking_levels_for_model,
)

CLI_PREFIX = ("uv", "run", "--project")
MVP_GROUPS = ("single_llm_skills_on", "single_llm_skills_off")
REQUIRED_CLI_FLAGS = (
    "--groups",
    "--datasets",
    "--output-dir",
    "--record-ids",
    "--limit",
    "--offset",
    "--single-agent-model",
    "--single-agent-thinking",
    "--single-timeout",
    "--single-timeout-retries",
    "--single-timeout-retry-backoff-seconds",
    "--no-timeout",
    "--no-analysis",
    "--max-concurrent-groups",
    "--inter-wave-delay-seconds",
    "--print-selected-records",
    "--exact-output-dir",
    "--merge-existing-per-record",
)
_RECORD_NUMBER = re.compile(r"(?<!\d)(\d{3})(?!\d)")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_argv(argv: Sequence[str]) -> str:
    return hashlib.sha256(b"\0".join(item.encode("utf-8") for item in argv)).hexdigest()


def slugify(value: str, *, limit: int = 64) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower() or "item"
    if len(cleaned) <= limit:
        return cleaned
    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[: limit - 9]}-{digest}".strip("-")


def sanitized_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    if extra:
        env.update(extra)
    return env


def ensure_contained(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise OrchestratorError(
            "path_outside_root", "Resolved path is outside its configured root"
        )
    return resolved


class CanonicalCliRuntimeAdapter:
    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config
        self.workspace_root = config.workspace_root
        self.run_root = config.run_root
        self.release_path = (
            self.workspace_root
            / "benchmarking/resources/verifier_grounded/release.json"
        )

    @property
    def openclaw_config_path(self) -> Path:
        configured = os.environ.get("OPENCLAW_CONFIG_PATH")
        if configured:
            return Path(configured).expanduser().resolve()
        return self.workspace_root.parent / "openclaw.json"

    def model_catalog(self) -> tuple[list[dict[str, str]], str, str]:
        path = self.openclaw_config_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            defaults = payload["agents"]["defaults"]
            configured_models = defaults["models"]
            default_model = defaults["model"]["primary"]
            default_thinking = defaults.get("thinkingDefault", "high")
            if not isinstance(configured_models, dict) or not configured_models:
                raise TypeError("agents.defaults.models must be a non-empty mapping")
            if not isinstance(default_model, str) or not default_model.strip():
                raise TypeError("agents.defaults.model.primary must be a string")
            if default_thinking not in (*THINKING_LEVELS, *MINIMAX_M3_THINKING_LEVELS):
                raise ValueError(
                    "agents.defaults.thinkingDefault must be one of "
                    + ", ".join(THINKING_LEVELS)
                )
            models = []
            for model_id, settings in configured_models.items():
                if not isinstance(model_id, str) or not model_id.strip():
                    raise TypeError("model ids must be non-empty strings")
                alias = settings.get("alias") if isinstance(settings, dict) else None
                provider, _, short_id = model_id.partition("/")
                models.append(
                    {
                        "id": model_id,
                        "label": alias.strip()
                        if isinstance(alias, str) and alias.strip()
                        else short_id or model_id,
                        "provider": provider if short_id else "default",
                    }
                )
            if default_model not in configured_models:
                raise ValueError("primary model is not present in agents.defaults.models")
            supported_default_levels = thinking_levels_for_model(default_model)
            if default_thinking not in supported_default_levels:
                default_thinking = supported_default_levels[-1]
            return models, default_model, default_thinking
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OrchestratorError(
                "runtime_unavailable",
                f"Invalid OpenClaw model config at {path}: {exc}",
                status_code=503,
            ) from exc

    @property
    def base_argv(self) -> tuple[str, ...]:
        return (
            *CLI_PREFIX,
            str(self.workspace_root),
            "python",
            "-m",
            "benchmarking.workflow.cli",
        )

    def release_identity(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.release_path.read_text(encoding="utf-8"))
            tracks = payload["tracks"]
            wheel = payload["wheel"]
            return {
                "version": str(payload["version"]),
                "wheel_sha256": str(wheel["sha256"]),
                "datasets": [
                    {
                        "id": str(track["dataset"]),
                        "task_count": int(track["task_count"]),
                    }
                    for track in tracks.values()
                ],
            }
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OrchestratorError(
                "runtime_unavailable",
                f"Invalid VGB release manifest: {exc}",
                status_code=503,
            ) from exc

    async def runtime_revision(self) -> tuple[str | None, bool]:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.workspace_root),
            "rev-parse",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        revision = stdout.decode().strip() if process.returncode == 0 else None
        dirty_process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.workspace_root),
            "status",
            "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        dirty_stdout, _ = await dirty_process.communicate()
        return revision, bool(
            dirty_stdout.strip()
        ) if dirty_process.returncode == 0 else False

    async def inspect_capabilities(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, message: str) -> None:
            checks.append({"name": name, "ok": ok, "message": message})

        check("workspace_root", self.workspace_root.is_dir(), str(self.workspace_root))
        check(
            "workspace_pyproject",
            (self.workspace_root / "pyproject.toml").is_file(),
            "pyproject.toml",
        )
        check(
            "canonical_module",
            (self.workspace_root / "benchmarking/workflow/cli.py").is_file(),
            "benchmarking.workflow.cli",
        )
        check("uv", shutil.which("uv") is not None, shutil.which("uv") or "not found")
        check(
            "openclaw",
            shutil.which("openclaw") is not None,
            shutil.which("openclaw") or "not found",
        )
        try:
            help_process = await asyncio.create_subprocess_exec(
                *self.base_argv,
                "--help",
                cwd=self.workspace_root,
                env=sanitized_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            help_stdout, _ = await asyncio.wait_for(
                help_process.communicate(), timeout=60
            )
            help_text = help_stdout.decode("utf-8", errors="replace")
            missing_flags = [
                flag for flag in REQUIRED_CLI_FLAGS if flag not in help_text
            ]
            check(
                "canonical_cli_contract",
                help_process.returncode == 0 and not missing_flags,
                "all required flags present"
                if not missing_flags
                else f"missing: {', '.join(missing_flags)}",
            )
        except (OSError, TimeoutError) as exc:
            check("canonical_cli_contract", False, str(exc))
        try:
            release = self.release_identity()
            check("vgb_release", True, release["version"])
        except OrchestratorError as exc:
            release = {"version": "unknown", "wheel_sha256": "", "datasets": []}
            check("vgb_release", False, exc.message)
        try:
            models, default_model, default_thinking = self.model_catalog()
            check("openclaw_models", True, f"{len(models)} models from {self.openclaw_config_path}")
        except OrchestratorError as exc:
            models, default_model, default_thinking = [], "", ""
            check("openclaw_models", False, exc.message)
        revision, dirty = await self.runtime_revision()
        return {
            "schema_version": 1,
            "ready": all(item["ok"] for item in checks),
            "workspace_root": str(self.workspace_root),
            "runtime_revision": revision,
            "runtime_dirty": dirty,
            "groups": list(MVP_GROUPS),
            "datasets": release["datasets"],
            "thinking_levels": list(THINKING_LEVELS),
            "thinking_levels_by_model": {
                model["id"]: list(thinking_levels_for_model(model["id"]))
                for model in models
            },
            "models": models,
            "default_model": default_model,
            "default_thinking": default_thinking,
            "vgb_release": {
                "version": release["version"],
                "wheel_sha256": release["wheel_sha256"],
            },
            "checks": checks,
        }

    def _selector_argv(
        self,
        spec: RunSpec,
        *,
        record_ids: Sequence[str] | None = None,
        apply_window: bool = True,
    ) -> list[str]:
        argv = [
            *self.base_argv,
            "--groups",
            ",".join(spec.groups),
            "--datasets",
            ",".join(spec.datasets),
        ]
        selected_record_ids = (
            spec.selection.record_ids if record_ids is None else record_ids
        )
        if selected_record_ids:
            argv += ["--record-ids", ",".join(selected_record_ids)]
        if apply_window and spec.selection.offset:
            argv += ["--offset", str(spec.selection.offset)]
        if apply_window and spec.selection.limit is not None:
            argv += ["--limit", str(spec.selection.limit)]
        return argv

    def build_preview_command(self, spec: RunSpec) -> RuntimeCommand:
        return RuntimeCommand(
            argv=tuple([*self._selector_argv(spec), "--print-selected-records"]),
            cwd=str(self.workspace_root),
            env=sanitized_env(),
        )

    @staticmethod
    def parse_preview(stdout: str) -> list[SelectedRecord]:
        try:
            payload = json.loads(stdout)
            if not isinstance(payload, list):
                raise TypeError("preview root must be a list")
            records = [SelectedRecord.model_validate(item) for item in payload]
            if not records:
                raise ValueError("preview selected no records")
            return records
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise OrchestratorError(
                "runtime_contract_error",
                f"Cannot parse canonical preview output: {exc}",
                status_code=502,
            ) from exc

    @staticmethod
    def _select_dataset_records(
        records: Sequence[SelectedRecord], record_ids: Sequence[str], dataset: str
    ) -> list[SelectedRecord]:
        if not record_ids:
            return list(records)

        selected: list[SelectedRecord] = []
        selected_ids: set[str] = set()
        for requested_id in record_ids:
            matches = [
                record for record in records if record.record_id == requested_id
            ]
            if not matches and requested_id.isdecimal():
                matches = [
                    record
                    for record in records
                    if record.record_id.endswith(f"_{requested_id}")
                ]
            if not matches:
                raise OrchestratorError(
                    "selection_invalid",
                    f"Unknown record id {requested_id!r} in {dataset}",
                    status_code=422,
                )
            if len(matches) > 1:
                raise OrchestratorError(
                    "selection_invalid",
                    f"Ambiguous record id {requested_id!r} in {dataset}",
                    status_code=422,
                )
            record = matches[0]
            if record.record_id in selected_ids:
                raise OrchestratorError(
                    "selection_invalid",
                    f"Record selection contains duplicate id {record.record_id!r}",
                    status_code=422,
                )
            selected_ids.add(record.record_id)
            selected.append(record)
        return selected

    @staticmethod
    def _select_dataset_record_range(
        records: Sequence[SelectedRecord], record_range: RecordRange, dataset: str
    ) -> list[SelectedRecord]:
        start = int(record_range.start)
        end = int(record_range.end)
        selected = [
            record
            for record in records
            if any(
                start <= int(number) <= end
                for number in _RECORD_NUMBER.findall(record.record_id)
            )
        ]
        if not selected:
            raise OrchestratorError(
                "selection_invalid",
                f"No records found in range {record_range.start}-{record_range.end} in {dataset}",
                status_code=422,
            )
        return selected

    def output_location(
        self, spec: RunSpec, timestamp: str
    ) -> tuple[str, str, str, Path]:
        benchmark = slugify(
            spec.datasets[0] if len(spec.datasets) == 1 else "mixed-datasets"
        )
        model = slugify(spec.agent.model.rsplit("/", 1)[-1])
        run_id = f"{benchmark}-{model}-{timestamp}"
        output_dir = ensure_contained(
            self.run_root / "formal" / benchmark / model / run_id, self.run_root
        )
        return benchmark, model, run_id, output_dir

    def build_run_command(self, frozen: FrozenRun, *, resume: bool) -> RuntimeCommand:
        spec = frozen.spec
        if (
            spec.selection.record_ids_by_dataset
            or spec.selection.record_ranges_by_dataset
        ):
            argv = self._selector_argv(
                spec,
                record_ids=[record.record_id for record in frozen.selected_records],
                apply_window=False,
            )
        else:
            argv = self._selector_argv(spec)
        argv += ["--single-agent-model", spec.agent.model]
        argv += ["--single-agent-thinking", spec.agent.thinking]
        execution = spec.execution
        if execution.timeout_seconds is None:
            argv.append("--no-timeout")
        else:
            argv += ["--single-timeout", str(execution.timeout_seconds)]
        argv += ["--single-timeout-retries", str(execution.timeout_retries)]
        argv += [
            "--single-timeout-retry-backoff-seconds",
            ",".join(str(value) for value in execution.timeout_retry_backoff_seconds),
        ]
        argv += ["--max-concurrent-groups", str(execution.max_concurrent_groups)]
        argv += ["--inter-wave-delay-seconds", str(execution.inter_wave_delay_seconds)]
        if not execution.analysis:
            argv.append("--no-analysis")
        output_dir = ensure_contained(Path(frozen.output_dir), self.run_root)
        argv += ["--exact-output-dir", str(output_dir)]
        if resume:
            argv.append("--merge-existing-per-record")
        return RuntimeCommand(
            argv=tuple(argv), cwd=str(self.workspace_root), env=sanitized_env()
        )

    async def execute_preview(
        self, spec: RunSpec, *, timeout_seconds: float = 120
    ) -> list[SelectedRecord]:
        if not (
            spec.selection.record_ids_by_dataset
            or spec.selection.record_ranges_by_dataset
        ):
            return await self._execute_preview(spec, timeout_seconds=timeout_seconds)

        records: list[SelectedRecord] = []
        for dataset in spec.datasets:
            dataset_spec = spec.model_copy(
                update={
                    "datasets": [dataset],
                    "selection": SelectionSpec(),
                }
            )
            available_records = await self._execute_preview(
                dataset_spec, timeout_seconds=timeout_seconds
            )
            selected: list[SelectedRecord] = []
            direct_ids = spec.selection.record_ids_by_dataset.get(dataset, [])
            if direct_ids:
                selected.extend(
                    self._select_dataset_records(available_records, direct_ids, dataset)
                )
            record_range = spec.selection.record_ranges_by_dataset.get(dataset)
            if record_range:
                selected.extend(
                    self._select_dataset_record_range(
                        available_records, record_range, dataset
                    )
                )
            if not direct_ids and not record_range:
                selected = list(available_records)
            selected_ids = {record.record_id for record in records}
            for record in selected:
                if record.record_id in selected_ids:
                    raise OrchestratorError(
                        "selection_invalid",
                        f"Record selection contains duplicate id {record.record_id!r}",
                        status_code=422,
                    )
                selected_ids.add(record.record_id)
                records.append(record)

        duplicate_ids = sorted(
            record_id
            for record_id in {record.record_id for record in records}
            if sum(record.record_id == record_id for record in records) > 1
        )
        if duplicate_ids:
            raise OrchestratorError(
                "selection_invalid",
                "Canonical runtime cannot disambiguate record id(s) across datasets: "
                + ", ".join(duplicate_ids),
                status_code=422,
            )

        records = records[spec.selection.offset :]
        if spec.selection.limit is not None:
            records = records[: spec.selection.limit]
        if not records:
            raise OrchestratorError(
                "selection_invalid", "No benchmark records selected.", status_code=422
            )
        return records

    async def _execute_preview(
        self, spec: RunSpec, *, timeout_seconds: float
    ) -> list[SelectedRecord]:
        command = self.build_preview_command(spec)
        try:
            process = await asyncio.create_subprocess_exec(
                *command.argv,
                cwd=command.cwd,
                env=command.env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except (OSError, TimeoutError) as exc:
            raise OrchestratorError(
                "runtime_unavailable", f"Preview process failed: {exc}", status_code=503
            ) from exc
        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()[-2000:]
            raise OrchestratorError(
                "selection_invalid", message or "Record preview failed", status_code=422
            )
        return self.parse_preview(stdout.decode("utf-8", errors="strict"))
