from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from .models import OrchestratorError, StrictModel


class HttpConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8875, ge=1, le=65535)
    poll_interval_ms: int = Field(default=1000, ge=250, le=10000)

    @field_validator("host")
    @classmethod
    def require_loopback(cls, value: str) -> str:
        try:
            is_loopback = ipaddress.ip_address(value).is_loopback
        except ValueError:
            is_loopback = value == "localhost"
        if not is_loopback:
            raise ValueError("MVP HTTP host must be loopback")
        return value


class LauncherConfig(StrictModel):
    max_active_runs: Literal[1] = 1
    cancel_grace_seconds: float = Field(default=15, ge=0.1, le=300)
    kill_after_seconds: float = Field(default=10, ge=0.1, le=300)
    max_log_bytes: int = Field(default=52_428_800, ge=65_536, le=1_073_741_824)


class OrchestratorConfig(StrictModel):
    schema_version: Literal[1] = 1
    workspace_root: Path
    run_root: Path
    control_root: Path
    http: HttpConfig = Field(default_factory=HttpConfig)
    launcher: LauncherConfig = Field(default_factory=LauncherConfig)

    @field_validator("workspace_root", "run_root", "control_root", mode="before")
    @classmethod
    def resolve_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser().resolve()

    @model_validator(mode="after")
    def validate_roots(self) -> "OrchestratorConfig":
        if self.run_root == self.control_root:
            raise ValueError("run_root and control_root must differ")
        if self.run_root.is_relative_to(
            self.control_root
        ) or self.control_root.is_relative_to(self.run_root):
            raise ValueError("run_root and control_root must not contain one another")
        return self


def load_config(path: str | Path) -> OrchestratorConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise OrchestratorError("invalid_config", f"Cannot load config: {exc}") from exc
    if not isinstance(payload, dict):
        raise OrchestratorError("invalid_config", "Config root must be a mapping")
    return OrchestratorConfig.model_validate(payload)
