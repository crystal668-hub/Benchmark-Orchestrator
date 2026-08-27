from __future__ import annotations

from pathlib import Path

from benchmark_orchestrator.models import (
    FrozenRun,
    PreviewSnapshot,
    RunControl,
    RunSpec,
    SelectedRecord,
    utc_now,
)
from benchmark_orchestrator.registry import FileControlRegistry
from benchmark_orchestrator.runtime_adapter import sha256_json


def make_spec(**updates: object) -> RunSpec:
    payload: dict[str, object] = {
        "groups": ["single_llm_skills_on"],
        "datasets": ["verifier_grounded_rdkit"],
        "agent": {"model": "qwen3.5-plus", "thinking": "high"},
    }
    payload.update(updates)
    return RunSpec.model_validate(payload)


def seed_controlled_run(
    registry: FileControlRegistry,
    output_dir: Path,
    *,
    run_id: str = "verifier-grounded-rdkit-qwen3-5-plus-20260721-120000",
    records: tuple[str, ...] = ("rdkit_qed_max_001",),
) -> FrozenRun:
    spec = make_spec(selection={"record_ids": list(records)})
    selected = [
        SelectedRecord(
            record_id=item,
            dataset="verifier_grounded_rdkit",
            subset="verifier_grounded_rdkit",
        )
        for item in records
    ]
    digest = sha256_json(spec.model_dump(mode="json"))
    frozen = FrozenRun(
        run_id=run_id,
        run_category="formal",
        benchmark_slug="verifier-grounded-rdkit",
        model_slug="qwen3-5-plus",
        output_dir=str(output_dir),
        workspace_root=str(output_dir.parent),
        spec=spec,
        spec_sha256=digest,
        selected_records=selected,
        selected_pairs=[("single_llm_skills_on", item) for item in records],
        runtime_revision="revision",
        vgb_release_version="0.7.0",
        vgb_wheel_sha256="a" * 64,
        created_at=utc_now(),
    )
    preview = PreviewSnapshot(
        preview_id="preview",
        spec_sha256=digest,
        normalized_spec=spec,
        records=selected,
        task_count=len(records),
        group_count=1,
        execution_count=len(records),
        runtime_revision="revision",
        runtime_dirty=False,
        vgb_release_version="0.7.0",
        vgb_wheel_sha256="a" * 64,
        created_at=utc_now(),
        expires_at="2099-01-01T00:00:00Z",
    )
    registry.create_run(
        frozen,
        preview,
        RunControl(run_id=run_id, state="created", updated_at=utc_now()),
    )
    return frozen
