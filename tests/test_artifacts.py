from __future__ import annotations

from pathlib import Path

import pytest

from benchmark_orchestrator.artifacts import ArtifactReader, STATUS_AXES
from benchmark_orchestrator.models import OrchestratorError
from tests.fake_runtime import result, write_json


def demo_run(
    root: Path, run_id: str = "demo-run", *, record_id: str = "rdkit_qed_max_001"
) -> Path:
    run = root / "formal" / "verifier-grounded-rdkit" / "model" / run_id
    payload = result("single_llm_skills_on", record_id)
    write_json(
        run / "per-record/single_llm_skills_on" / f"{record_id.replace('_', '-')}.json",
        payload,
    )
    write_json(
        run / "results.json",
        {
            "schema_version": 3,
            "records": 1,
            "results": [payload],
            "groups": [],
            "summary": {},
        },
    )
    write_json(
        run / "progress/state.json",
        {"status": "completed", "total": 1, "completed": 1, "groups": {}},
    )
    return run


def test_discovers_classified_run_without_descending_into_recovery(
    tmp_path: Path,
) -> None:
    run = demo_run(tmp_path, "primary")
    demo_run(run / "recovery", "snapshot")
    reader = ArtifactReader(tmp_path)
    assert [path.name for path in reader.candidate_run_dirs()] == ["primary"]


def test_checkpoint_uses_payload_identity_and_flags_corruption(tmp_path: Path) -> None:
    run = demo_run(tmp_path)
    corrupt = run / "per-record/single_llm_skills_on/corrupt.json"
    corrupt.write_text("{broken", encoding="utf-8")
    committed, invalid = ArtifactReader(tmp_path).checkpoint_state("demo-run")
    assert ("single_llm_skills_on", "rdkit_qed_max_001") in committed
    assert invalid == ["per-record/single_llm_skills_on/corrupt.json"]


def test_malformed_results_falls_back_without_validating_final_artifacts(
    tmp_path: Path,
) -> None:
    run = demo_run(tmp_path)
    (run / "results.json").write_text("{broken", encoding="utf-8")
    reader = ArtifactReader(tmp_path)

    assert reader.load_results(run)[0]["record_id"] == "rdkit_qed_max_001"
    assert not reader.final_artifacts_valid(
        "demo-run", [("single_llm_skills_on", "rdkit_qed_max_001")]
    )


def test_artifact_reader_does_not_hide_programming_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = demo_run(tmp_path)
    reader = ArtifactReader(tmp_path)

    def fail_json_load(_path: Path) -> None:
        raise RuntimeError("programming defect")

    monkeypatch.setattr("benchmark_orchestrator.artifacts._load_json", fail_json_load)

    with pytest.raises(RuntimeError, match="programming defect"):
        reader.load_results(run)
    with pytest.raises(RuntimeError, match="programming defect"):
        reader.checkpoint_state("demo-run")
    with pytest.raises(RuntimeError, match="programming defect"):
        reader.final_artifacts_valid(
            "demo-run", [("single_llm_skills_on", "rdkit_qed_max_001")]
        )


def test_record_api_preserves_continuous_score_and_status_axes(tmp_path: Path) -> None:
    demo_run(tmp_path)
    reader = ArtifactReader(tmp_path)
    rows = reader.list_records("demo-run")
    detail = reader.get_record("demo-run", "rdkit-qed-max-001")
    assert rows[0]["score"] == 0.75
    assert rows[0]["primary_metric"] == "verifier_score"
    assert all(axis in rows[0] for axis in STATUS_AXES)
    assert detail["groups"][0]["evaluation"]["passed"] is None


def test_progress_reconciles_stale_state_with_checkpoints(tmp_path: Path) -> None:
    run = demo_run(tmp_path)
    write_json(
        run / "progress/state.json",
        {"status": "running", "total": 9, "completed": 0, "groups": {}},
    )
    progress = ArtifactReader(tmp_path).progress(
        "demo-run",
        expected_pairs=[("single_llm_skills_on", "rdkit_qed_max_001")],
    )
    assert progress["total"] == 1
    assert progress["completed"] == 1
    assert progress["groups"]["single_llm_skills_on"]["completed_count"] == 1


def test_asset_containment_and_protected_runtime_config(tmp_path: Path) -> None:
    run = demo_run(tmp_path)
    write_json(run / "runtime-config/secret.json", {"token": "secret"})
    reader = ArtifactReader(tmp_path)
    path, media_type = reader.resolve_asset("demo-run", "results.json")
    assert path == (run / "results.json").resolve()
    assert media_type == "application/json"
    with pytest.raises(OrchestratorError, match="not allowed"):
        reader.resolve_asset("demo-run", "runtime-config/secret.json")
    with pytest.raises(OrchestratorError):
        reader.resolve_asset("demo-run", "progress/../../outside")


def test_duplicate_run_ids_are_ambiguous(tmp_path: Path) -> None:
    demo_run(tmp_path / "one", "same")
    demo_run(tmp_path / "two", "same")
    with pytest.raises(OrchestratorError, match="Ambiguous"):
        ArtifactReader(tmp_path).run_dir("same")
