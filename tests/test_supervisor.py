from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from benchmark_orchestrator.artifacts import ArtifactReader
from benchmark_orchestrator.config import OrchestratorConfig
from benchmark_orchestrator.models import RuntimeCommand
from benchmark_orchestrator.registry import FileControlRegistry
from benchmark_orchestrator.runtime_adapter import sanitized_env
from benchmark_orchestrator.supervisor import LocalRunSupervisor
from tests.fake_runtime import result, write_json
from tests.helpers import seed_controlled_run


FAKE_RUNTIME = Path(__file__).with_name("fake_runtime.py")


async def wait_terminal(
    registry: FileControlRegistry, run_id: str, timeout: float = 5
) -> object:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        control = registry.load_control(run_id)
        if control.state not in {"starting", "running", "cancelling"}:
            return control
        await asyncio.sleep(0.03)
    raise AssertionError("supervisor did not reach terminal state")


def command(
    output: Path, pairs: str, *, delay: float = 0, merge: bool = False
) -> RuntimeCommand:
    argv = [
        sys.executable,
        str(FAKE_RUNTIME),
        "--output",
        str(output),
        "--pairs",
        pairs,
        "--delay",
        str(delay),
    ]
    if merge:
        argv.append("--merge")
    return RuntimeCommand(
        argv=tuple(argv), cwd=str(FAKE_RUNTIME.parent), env=sanitized_env()
    )


async def test_supervisor_completes_only_with_final_artifacts(
    config: OrchestratorConfig,
) -> None:
    registry = FileControlRegistry(config.control_root)
    output = config.run_root / "formal/demo/model/run-complete"
    frozen = seed_controlled_run(registry, output, run_id="run-complete")
    supervisor = LocalRunSupervisor(
        config.launcher, registry, ArtifactReader(config.run_root)
    )
    started = await supervisor.start(
        frozen.run_id,
        command(output, "single_llm_skills_on:rdkit_qed_max_001"),
        kind="start",
        request_id="request-start",
    )
    assert started.state == "running"
    assert started.invocations[0].pid
    completed = await wait_terminal(registry, frozen.run_id)
    assert completed.state == "completed"
    assert completed.invocations[0].exit_code == 0
    assert completed.active_invocation_id is None


async def test_cancel_sends_graceful_term_and_preserves_control_state(
    config: OrchestratorConfig,
) -> None:
    registry = FileControlRegistry(config.control_root)
    output = config.run_root / "formal/demo/model/run-cancel"
    frozen = seed_controlled_run(registry, output, run_id="run-cancel")
    supervisor = LocalRunSupervisor(
        config.launcher, registry, ArtifactReader(config.run_root)
    )
    await supervisor.start(
        frozen.run_id,
        command(output, "single_llm_skills_on:rdkit_qed_max_001", delay=5),
        kind="start",
        request_id="request-start",
    )
    cancelling = await supervisor.cancel(frozen.run_id, "request-cancel")
    assert cancelling.state == "cancelling"
    assert cancelling.invocations[0].cancel_requested_at
    terminal = await wait_terminal(registry, frozen.run_id)
    assert terminal.state == "cancelled"
    assert terminal.invocations[0].exit_code != 0


async def test_resume_boundary_skips_existing_failure_checkpoint(
    config: OrchestratorConfig,
) -> None:
    registry = FileControlRegistry(config.control_root)
    output = config.run_root / "formal/demo/model/run-resume"
    frozen = seed_controlled_run(
        registry,
        output,
        run_id="run-resume",
        records=("rdkit_qed_max_001", "rdkit_sa_min_002"),
    )
    existing_path = output / "per-record/single_llm_skills_on/rdkit-qed-max-001.json"
    existing = result("single_llm_skills_on", "rdkit_qed_max_001")
    existing["run_lifecycle_status"] = "failed"
    write_json(existing_path, existing)
    before = existing_path.read_bytes()
    control = registry.load_control(frozen.run_id).model_copy(
        update={"state": "failed"}
    )
    registry.save_control(control)
    supervisor = LocalRunSupervisor(
        config.launcher, registry, ArtifactReader(config.run_root)
    )
    await supervisor.start(
        frozen.run_id,
        command(
            output,
            "single_llm_skills_on:rdkit_qed_max_001,single_llm_skills_on:rdkit_sa_min_002",
            merge=True,
        ),
        kind="resume",
        request_id="request-resume",
    )
    terminal = await wait_terminal(registry, frozen.run_id)
    assert terminal.state == "completed"
    assert terminal.invocations[0].kind == "resume"
    assert existing_path.read_bytes() == before
    aggregate = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert {item["record_id"] for item in aggregate["results"]} == {
        "rdkit_qed_max_001",
        "rdkit_sa_min_002",
    }
