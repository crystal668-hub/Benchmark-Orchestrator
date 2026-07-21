from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


GroupId = Literal["single_llm_skills_on", "single_llm_skills_off"]
DatasetId = Literal[
    "verifier_grounded_rdkit",
    "verifier_grounded_xtb_xyz",
    "verifier_grounded_property_calculation",
]
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]
ControlState = Literal[
    "created",
    "starting",
    "running",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]
ACTIVE_STATES = {"starting", "running", "cancelling"}
TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}
_RECORD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _trim_unique(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    normalized = [item.strip() if isinstance(item, str) else item for item in value]
    if len(normalized) != len({str(item) for item in normalized}):
        raise ValueError("items must not contain duplicates")
    return normalized


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SelectionSpec(StrictModel):
    record_ids: list[str] = Field(default_factory=list)
    offset: int = Field(default=0, ge=0)
    limit: int | None = Field(default=None, ge=1)

    @field_validator("record_ids", mode="before")
    @classmethod
    def normalize_record_ids(cls, value: Any) -> Any:
        return _trim_unique(value)

    @field_validator("record_ids")
    @classmethod
    def validate_record_ids(cls, value: list[str]) -> list[str]:
        for record_id in value:
            if not _RECORD_ID.fullmatch(record_id):
                raise ValueError(f"invalid record id: {record_id!r}")
        return value


class AgentSpec(StrictModel):
    model: str = Field(min_length=1, max_length=200)
    thinking: ThinkingLevel = "high"

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(
                "model must be non-empty and contain no control characters"
            )
        return value


class ExecutionSpec(StrictModel):
    timeout_seconds: int | None = Field(default=900, ge=1)
    timeout_retries: int = Field(default=3, ge=0, le=10)
    timeout_retry_backoff_seconds: list[float] = Field(
        default_factory=lambda: [5, 15, 45]
    )
    max_concurrent_groups: int = Field(default=1, ge=1, le=2)
    inter_wave_delay_seconds: int = Field(default=0, ge=0, le=3600)
    analysis: bool = False

    @field_validator("timeout_retry_backoff_seconds")
    @classmethod
    def validate_backoff(cls, value: list[float]) -> list[float]:
        if any(not math.isfinite(item) or item < 0 for item in value):
            raise ValueError("retry backoff values must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def validate_backoff_length(self) -> "ExecutionSpec":
        if len(self.timeout_retry_backoff_seconds) < self.timeout_retries:
            raise ValueError(
                "retry backoff must include at least timeout_retries values"
            )
        return self


class RunSpec(StrictModel):
    schema_version: Literal[1] = 1
    name: str | None = Field(default=None, max_length=120)
    groups: list[GroupId] = Field(min_length=1, max_length=2)
    datasets: list[DatasetId] = Field(min_length=1)
    selection: SelectionSpec = Field(default_factory=SelectionSpec)
    agent: AgentSpec
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)

    @field_validator("groups", "datasets", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> Any:
        return _trim_unique(value)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class SelectedRecord(StrictModel):
    record_id: str
    dataset: str
    subset: str | None = None
    eval_kind: str | None = None
    source_file: str | None = None
    prompt_preview: str | None = None


class FrozenRun(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    run_category: Literal["formal", "temporary"]
    benchmark_slug: str
    model_slug: str
    output_dir: str
    workspace_root: str
    spec: RunSpec
    spec_sha256: str
    selected_records: list[SelectedRecord]
    selected_pairs: list[tuple[str, str]]
    runtime_revision: str | None = None
    runtime_dirty: bool = False
    vgb_release_version: str
    vgb_wheel_sha256: str
    created_at: str


class Invocation(StrictModel):
    invocation_id: str
    request_id: str
    kind: Literal["start", "resume"]
    state: ControlState
    pid: int | None = None
    pgid: int | None = None
    process_started_at: str | None = None
    process_executable: str | None = None
    process_fingerprint: str | None = None
    argv_sha256: str
    launcher_log: str
    log_truncated: bool = False
    ownership: Literal["attached", "detached"] = "attached"
    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None
    terminating_signal: int | None = None
    cancel_requested_at: str | None = None
    cancel_request_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class RunControl(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    state: ControlState
    active_invocation_id: str | None = None
    invocations: list[Invocation] = Field(default_factory=list)
    updated_at: str


class RuntimeCommand(StrictModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    argv: tuple[str, ...]
    cwd: str
    env: dict[str, str]


class PreviewSnapshot(StrictModel):
    schema_version: Literal[1] = 1
    preview_id: str
    spec_sha256: str
    normalized_spec: RunSpec
    records: list[SelectedRecord]
    task_count: int
    group_count: int
    execution_count: int
    runtime_revision: str | None
    runtime_dirty: bool
    vgb_release_version: str
    vgb_wheel_sha256: str
    created_at: str
    expires_at: str


class CommandRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")


class CreateRunRequest(CommandRequest):
    preview_id: str = Field(min_length=1, max_length=200)
    spec_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


class MetadataPatch(StrictModel):
    alias: str | None = Field(default=None, max_length=120)
    favorite: bool | None = None
    hidden: bool | None = None


class AnnotationCreate(StrictModel):
    run_id: str
    record_id: str | None = None
    group_id: str | None = None
    note: str = Field(default="", max_length=10000)
    status: str = Field(default="open", max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=30)
    manual_verdict: str | None = Field(default=None, max_length=80)


class AnnotationPatch(StrictModel):
    note: str | None = Field(default=None, max_length=10000)
    status: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = Field(default=None, max_length=30)
    manual_verdict: str | None = Field(default=None, max_length=80)


class OrchestratorError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
