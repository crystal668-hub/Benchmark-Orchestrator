from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from benchmark_orchestrator.config import OrchestratorConfig
from benchmark_orchestrator.models import FrozenRun, RunSpec, SelectedRecord
from benchmark_orchestrator.runtime_adapter import CanonicalCliRuntimeAdapter
from tests.helpers import make_spec


def test_run_spec_rejects_extra_duplicates_and_unsafe_ids() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RunSpec.model_validate({**make_spec().model_dump(), "raw_args": "--anything"})
    with pytest.raises(ValidationError, match="duplicates"):
        make_spec(groups=["single_llm_skills_on", " single_llm_skills_on "])
    with pytest.raises(ValidationError, match="invalid record id"):
        make_spec(selection={"record_ids": ["../../secret"]})
    with pytest.raises(ValidationError, match="must not be used together"):
        make_spec(
            selection={
                "record_ids": ["rdkit_qed_max_001"],
                "record_ids_by_dataset": {
                    "verifier_grounded_rdkit": ["rdkit_sa_min_002"]
                },
            }
        )
    with pytest.raises(ValidationError, match="record range"):
        make_spec(
            selection={
                "record_ranges_by_dataset": {
                    "verifier_grounded_rdkit": {"start": "012", "end": "002"}
                }
            }
        )
    with pytest.raises(ValidationError, match="unselected dataset"):
        make_spec(
            selection={
                "record_ids_by_dataset": {
                    "verifier_grounded_xtb_xyz": ["xtb_gap_window_001"]
                }
            }
        )
    with pytest.raises(ValidationError, match="invalid record id"):
        make_spec(
            selection={
                "record_ids_by_dataset": {
                    "verifier_grounded_rdkit": ["../../secret"]
                }
            }
        )


def test_run_spec_accepts_vgb_070_easy_property_dataset() -> None:
    spec = make_spec(datasets=["verifier_grounded_property_calculation_easy"])

    assert spec.datasets == ["verifier_grounded_property_calculation_easy"]


def test_run_spec_requires_enough_finite_backoff_values() -> None:
    with pytest.raises(ValidationError, match="at least timeout_retries"):
        make_spec(
            execution={"timeout_retries": 3, "timeout_retry_backoff_seconds": [1, 2]}
        )
    with pytest.raises(ValidationError, match="finite"):
        make_spec(
            execution={
                "timeout_retries": 1,
                "timeout_retry_backoff_seconds": [float("inf")],
            }
        )


def test_record_ids_accept_chinese_comma_separators() -> None:
    spec = make_spec(
        selection={
            "record_ids_by_dataset": {
                "verifier_grounded_rdkit": ["011，012\n013, 014"]
            }
        }
    )

    assert spec.selection.record_ids_by_dataset["verifier_grounded_rdkit"] == [
        "011",
        "012",
        "013",
        "014",
    ]


def adapter_for(config: OrchestratorConfig) -> CanonicalCliRuntimeAdapter:
    return CanonicalCliRuntimeAdapter(config)


def test_model_catalog_reads_selectable_openclaw_models(
    config: OrchestratorConfig,
) -> None:
    config.workspace_root.parent.joinpath("openclaw.json").write_text(
        """{
          "agents": {"defaults": {
            "model": {"primary": "openai/gpt-5.6-sol"},
            "models": {
              "openai/gpt-5.6-sol": {"alias": "GPT-5.6 SOL"},
              "qwen/qwen3.7-max": {}
            }
          }}
        }""",
        encoding="utf-8",
    )
    models, default_model = adapter_for(config).model_catalog()
    assert models == [
        {"id": "openai/gpt-5.6-sol", "label": "GPT-5.6 SOL", "provider": "openai"},
        {"id": "qwen/qwen3.7-max", "label": "qwen3.7-max", "provider": "qwen"},
    ]
    assert default_model == "openai/gpt-5.6-sol"


def frozen_for(config: OrchestratorConfig, spec: RunSpec) -> FrozenRun:
    adapter = adapter_for(config)
    benchmark, model, run_id, output = adapter.output_location(spec, "20260721-120000")
    return FrozenRun(
        run_id=run_id,
        run_category="formal",
        benchmark_slug=benchmark,
        model_slug=model,
        output_dir=str(output),
        workspace_root=str(config.workspace_root),
        spec=spec,
        spec_sha256="a" * 64,
        selected_records=[],
        selected_pairs=[],
        vgb_release_version="0.7.0",
        vgb_wheel_sha256="b" * 64,
        created_at="2026-07-21T00:00:00Z",
    )


def test_cli_mapping_and_resume_only_adds_merge(config: OrchestratorConfig) -> None:
    spec = make_spec(
        groups=["single_llm_skills_on", "single_llm_skills_off"],
        selection={"record_ids": ["rdkit_qed_max_001"], "offset": 2, "limit": 1},
        execution={
            "timeout_seconds": None,
            "timeout_retries": 2,
            "timeout_retry_backoff_seconds": [5, 15],
            "max_concurrent_groups": 2,
            "inter_wave_delay_seconds": 7,
            "analysis": False,
        },
    )
    adapter = adapter_for(config)
    frozen = frozen_for(config, spec)
    start = adapter.build_run_command(frozen, resume=False)
    resume = adapter.build_run_command(frozen, resume=True)

    assert start.argv[:7] == (
        "uv",
        "run",
        "--project",
        str(config.workspace_root),
        "python",
        "-m",
        "benchmarking.workflow.cli",
    )
    assert "--no-timeout" in start.argv
    assert "--single-timeout" not in start.argv
    assert "--no-analysis" in start.argv
    assert "--exact-output-dir" in start.argv
    assert resume.argv == (*start.argv, "--merge-existing-per-record")
    assert "PYTHONPATH" not in start.env
    assert "VIRTUAL_ENV" not in start.env


def test_preview_excludes_execution_flags_and_parser_is_strict(
    config: OrchestratorConfig,
) -> None:
    adapter = adapter_for(config)
    command = adapter.build_preview_command(make_spec())
    assert command.argv[-1] == "--print-selected-records"
    assert "--single-agent-model" not in command.argv
    records = adapter.parse_preview(
        '[{"record_id":"r1","dataset":"verifier_grounded_rdkit"}]'
    )
    assert records[0].record_id == "r1"
    with pytest.raises(Exception, match="preview root must be a list"):
        adapter.parse_preview("{}")


@pytest.mark.asyncio
async def test_dataset_record_selection_resolves_each_dataset_before_windowing(
    config: OrchestratorConfig,
) -> None:
    spec = make_spec(
        datasets=["verifier_grounded_rdkit", "verifier_grounded_xtb_xyz"],
        selection={
            "record_ids_by_dataset": {
                "verifier_grounded_rdkit": ["rdkit_qed_max_001"],
                "verifier_grounded_xtb_xyz": [],
            },
            "offset": 1,
            "limit": 1,
        },
    )
    adapter = adapter_for(config)
    adapter._execute_preview = AsyncMock(
        side_effect=[
            [
                SelectedRecord(
                    record_id="rdkit_qed_max_001",
                    dataset="verifier_grounded_rdkit",
                )
            ],
            [
                SelectedRecord(
                    record_id="xtb_gap_window_001",
                    dataset="verifier_grounded_xtb_xyz",
                ),
                SelectedRecord(
                    record_id="xtb_dipole_window_002",
                    dataset="verifier_grounded_xtb_xyz",
                ),
            ],
        ]
    )

    records = await adapter.execute_preview(spec)

    assert [record.record_id for record in records] == ["xtb_gap_window_001"]
    calls = adapter._execute_preview.await_args_list
    assert calls[0].args[0].selection.record_ids == []
    assert calls[1].args[0].selection.record_ids == []

    frozen = frozen_for(config, spec).model_copy(
        update={"selected_records": records}
    )
    command = adapter.build_run_command(frozen, resume=False)
    record_flag = command.argv.index("--record-ids")
    assert command.argv[record_flag + 1] == "xtb_gap_window_001"
    assert "--offset" not in command.argv
    assert "--limit" not in command.argv


@pytest.mark.asyncio
async def test_dataset_record_range_selects_existing_records_with_gaps(
    config: OrchestratorConfig,
) -> None:
    spec = make_spec(
        selection={
            "record_ranges_by_dataset": {
                "verifier_grounded_rdkit": {"start": "002", "end": "012"}
            }
        }
    )
    adapter = adapter_for(config)
    adapter._execute_preview = AsyncMock(
        return_value=[
            SelectedRecord(
                record_id=f"rdkit_task_{number:03d}",
                dataset="verifier_grounded_rdkit",
            )
            for number in [1, 5, 6, 7, 8, 9, 10, 14]
        ]
    )

    records = await adapter.execute_preview(spec)

    assert [record.record_id for record in records] == [
        "rdkit_task_005",
        "rdkit_task_006",
        "rdkit_task_007",
        "rdkit_task_008",
        "rdkit_task_009",
        "rdkit_task_010",
    ]
    frozen = frozen_for(config, spec).model_copy(update={"selected_records": records})
    command = adapter.build_run_command(frozen, resume=False)
    record_flag = command.argv.index("--record-ids")
    assert command.argv[record_flag + 1] == ",".join(record.record_id for record in records)


@pytest.mark.asyncio
async def test_dataset_record_range_matches_numbers_inside_record_ids(
    config: OrchestratorConfig,
) -> None:
    spec = make_spec(
        datasets=["verifier_grounded_property_calculation"],
        selection={
            "record_ranges_by_dataset": {
                "verifier_grounded_property_calculation": {
                    "start": "015",
                    "end": "020",
                }
            }
        },
    )
    adapter = adapter_for(config)
    adapter._execute_preview = AsyncMock(
        return_value=[
            SelectedRecord(
                record_id=f"property_calc_{number:03d}_task",
                dataset="verifier_grounded_property_calculation",
            )
            for number in [14, 15, 16, 17, 18, 19, 20, 21]
        ]
    )

    records = await adapter.execute_preview(spec)

    assert [record.record_id for record in records] == [
        "property_calc_015_task",
        "property_calc_016_task",
        "property_calc_017_task",
        "property_calc_018_task",
        "property_calc_019_task",
        "property_calc_020_task",
    ]


@pytest.mark.asyncio
async def test_dataset_record_range_ignores_non_task_numbers_in_record_ids(
    config: OrchestratorConfig,
) -> None:
    spec = make_spec(
        datasets=["verifier_grounded_xtb_xyz"],
        selection={
            "record_ranges_by_dataset": {
                "verifier_grounded_xtb_xyz": {"start": "002", "end": "012"}
            }
        },
    )
    adapter = adapter_for(config)
    adapter._execute_preview = AsyncMock(
        return_value=[
            SelectedRecord(
                record_id="xtb_c10_f2_gap_min_016",
                dataset="verifier_grounded_xtb_xyz",
            ),
            SelectedRecord(
                record_id="xtb_dipole_window_002",
                dataset="verifier_grounded_xtb_xyz",
            ),
        ]
    )

    records = await adapter.execute_preview(spec)

    assert [record.record_id for record in records] == ["xtb_dipole_window_002"]


def test_dataset_record_selection_accepts_numeric_record_suffixes(
    config: OrchestratorConfig,
) -> None:
    adapter = adapter_for(config)
    records = [
        SelectedRecord(
            record_id="rdkit_logp_target_011",
            dataset="verifier_grounded_rdkit",
        ),
        SelectedRecord(
            record_id="rdkit_sa_logp_target_012",
            dataset="verifier_grounded_rdkit",
        ),
    ]

    selected = adapter._select_dataset_records(
        records, ["011", "rdkit_sa_logp_target_012"], "verifier_grounded_rdkit"
    )

    assert [record.record_id for record in selected] == [
        "rdkit_logp_target_011",
        "rdkit_sa_logp_target_012",
    ]


def test_output_path_matches_canonical_classification(
    config: OrchestratorConfig,
) -> None:
    adapter = adapter_for(config)
    benchmark, model, run_id, output = adapter.output_location(
        make_spec(
            datasets=["verifier_grounded_rdkit", "verifier_grounded_xtb_xyz"],
            agent={"model": "provider/Qwen 3.7 Max"},
        ),
        "20260721-120000",
    )
    assert benchmark == "mixed-datasets"
    assert model == "qwen-3-7-max"
    assert run_id == "mixed-datasets-qwen-3-7-max-20260721-120000"
    assert output.is_relative_to(config.run_root)


def test_config_rejects_overlapping_roots(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must not contain"):
        OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            run_root=tmp_path / "state",
            control_root=tmp_path / "state/control",
        )
