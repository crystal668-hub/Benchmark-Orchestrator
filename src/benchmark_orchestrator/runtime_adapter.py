from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import OrchestratorConfig
from .models import (
    FrozenRun,
    OrchestratorError,
    RunSpec,
    RuntimeCommand,
    SelectedRecord,
)


CLI_PREFIX = ("uv", "run", "--project")
MVP_GROUPS = ("single_llm_skills_on", "single_llm_skills_off")
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")
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
            "default_model": "qwen3.5-plus",
            "vgb_release": {
                "version": release["version"],
                "wheel_sha256": release["wheel_sha256"],
            },
            "checks": checks,
        }

    def _selector_argv(self, spec: RunSpec) -> list[str]:
        argv = [
            *self.base_argv,
            "--groups",
            ",".join(spec.groups),
            "--datasets",
            ",".join(spec.datasets),
        ]
        if spec.selection.record_ids:
            argv += ["--record-ids", ",".join(spec.selection.record_ids)]
        if spec.selection.offset:
            argv += ["--offset", str(spec.selection.offset)]
        if spec.selection.limit is not None:
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
