from __future__ import annotations

from pathlib import Path

import pytest

from benchmark_orchestrator.config import LauncherConfig, OrchestratorConfig


@pytest.fixture
def config(tmp_path: Path) -> OrchestratorConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        "[project]\nname='fake'\nversion='0'\n", encoding="utf-8"
    )
    return OrchestratorConfig(
        workspace_root=workspace,
        run_root=tmp_path / "runtime" / "benchmark-runs",
        control_root=tmp_path / "control",
        launcher=LauncherConfig(
            cancel_grace_seconds=0.15, kill_after_seconds=0.15, max_log_bytes=65536
        ),
    )
