from __future__ import annotations

import shutil
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from benchmark_orchestrator.app import build_app
from benchmark_orchestrator.config import OrchestratorConfig
from tests.fake_runtime import result, write_json
from tests.helpers import seed_controlled_run


def test_health_static_and_structured_validation(config: OrchestratorConfig) -> None:
    app = build_app(config)
    with TestClient(app, base_url="http://127.0.0.1:8875") as client:
        health = client.get("/api/health")
        index = client.get("/")
        invalid = client.post(
            "/api/runs/preview", json={"groups": [], "datasets": [], "agent": {}}
        )
    assert health.json() == {"status": "ok", "schema_version": 1}
    assert "Benchmark Orchestrator" in index.text
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
    assert invalid.json()["error"]["request_id"]
    assert '<script defer src="/app.js?v=4"></script>' in index.text
    assert 'id="previewButton" type="submit"' in index.text
    assert 'id="startButton" type="button"' in index.text
    assert 'id="count-xtb_xyz">20</b>' in index.text


def test_preview_rejects_failed_runtime_preflight(
    config: OrchestratorConfig,
) -> None:
    app = build_app(config)
    app.state.service.adapter.inspect_capabilities = AsyncMock(
        return_value={
            "ready": False,
            "checks": [{"name": "openclaw", "ok": False, "message": "not found"}],
        }
    )
    app.state.service.adapter.execute_preview = AsyncMock()

    with TestClient(app, base_url="http://127.0.0.1:8875") as client:
        response = client.post(
            "/api/runs/preview",
            json={
                "groups": ["single_llm_skills_on"],
                "datasets": ["verifier_grounded_rdkit"],
                "agent": {"model": "openai/gpt-5.5", "thinking": "high"},
            },
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_unavailable"
    assert response.json()["error"]["details"]["failed_checks"] == [
        {"name": "openclaw", "ok": False, "message": "not found"}
    ]
    app.state.service.adapter.execute_preview.assert_not_awaited()


def test_create_rejects_failed_runtime_preflight(
    config: OrchestratorConfig,
) -> None:
    app = build_app(config)
    app.state.service.adapter.inspect_capabilities = AsyncMock(
        return_value={
            "ready": False,
            "checks": [{"name": "openclaw", "ok": False, "message": "not found"}],
        }
    )

    with TestClient(app, base_url="http://127.0.0.1:8875") as client:
        response = client.post(
            "/api/runs",
            json={
                "preview_id": "preview",
                "spec_sha256": "a" * 64,
                "request_id": "request-create",
            },
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_unavailable"


def test_cross_origin_mutation_is_rejected(config: OrchestratorConfig) -> None:
    app = build_app(config)
    with TestClient(app, base_url="http://127.0.0.1:8875") as client:
        response = client.post(
            "/api/runs/preview",
            json={},
            headers={"Origin": "http://evil.example", "Host": "127.0.0.1:8875"},
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_denied"


def test_body_limit_is_enforced_before_json_parse(config: OrchestratorConfig) -> None:
    app = build_app(config)
    with TestClient(app, base_url="http://127.0.0.1:8875") as client:
        response = client.post(
            "/api/runs/preview",
            content=b"{}",
            headers={
                "Content-Length": str(1_048_577),
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_mutation_rejects_unconfigured_host(config: OrchestratorConfig) -> None:
    app = build_app(config)
    with TestClient(app, base_url="http://127.0.0.1:8875") as client:
        response = client.post(
            "/api/runs/preview", json={}, headers={"Host": "example.test"}
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "host_denied"


def test_historical_run_without_control_sidecar_exposes_tasks(
    config: OrchestratorConfig,
) -> None:
    run = config.run_root / "formal/verifier-grounded-rdkit/model/historical-run"
    payload = result("single_llm_skills_on", "rdkit_qed_max_001")
    write_json(run / "per-record/single_llm_skills_on/rdkit-qed-max-001.json", payload)
    write_json(
        run / "results.json",
        {"schema_version": 3, "records": 1, "results": [payload], "groups": []},
    )
    write_json(
        run / "progress/state.json",
        {"status": "completed", "total": 1, "completed": 1, "groups": {}},
    )
    app = build_app(config)
    with TestClient(app, base_url="http://127.0.0.1:8875") as client:
        tasks = client.get("/api/runs/historical-run/tasks")
        snapshot = client.get("/api/runs/historical-run")
    assert tasks.status_code == 200
    assert tasks.json()[0]["checkpoint"] == "committed"
    assert tasks.json()[0]["result"]["evaluation"]["passed"] is None
    assert snapshot.json()["control"] is None


def test_deleted_completed_run_is_removed_from_run_list(
    config: OrchestratorConfig,
) -> None:
    run = config.run_root / "formal/verifier-grounded-rdkit/model/completed-run"
    payload = result("single_llm_skills_on", "rdkit_qed_max_001")
    write_json(run / "per-record/single_llm_skills_on/record.json", payload)
    app = build_app(config)
    frozen = seed_controlled_run(
        app.state.service.registry, run, run_id="completed-run"
    )
    control = app.state.service.registry.load_control(frozen.run_id).model_copy(
        update={"state": "completed"}
    )
    app.state.service.registry.save_control(control)

    with TestClient(app, base_url="http://127.0.0.1:8875") as client:
        assert [item["run_id"] for item in client.get("/api/runs").json()] == [
            "completed-run"
        ]
        shutil.rmtree(run)
        assert client.get("/api/runs").json() == []


def test_active_run_without_artifacts_remains_in_run_list(
    config: OrchestratorConfig,
) -> None:
    app = build_app(config)
    frozen = seed_controlled_run(
        app.state.service.registry,
        config.run_root / "formal/verifier-grounded-rdkit/model/active-run",
        run_id="active-run",
    )
    control = app.state.service.registry.load_control(frozen.run_id).model_copy(
        update={"state": "running"}
    )
    app.state.service.supervisor.reconcile = AsyncMock(return_value=control)

    with TestClient(app, base_url="http://127.0.0.1:8875") as client:
        response = client.get("/api/runs")

    assert response.status_code == 200
    assert response.json()[0]["run_id"] == "active-run"
    assert response.json()[0]["control"]["state"] == "running"


def test_active_run_list_uses_frozen_selection_for_progress_total(
    config: OrchestratorConfig,
) -> None:
    app = build_app(config)
    run = config.run_root / "formal/verifier-grounded-rdkit/model/active-run"
    records = tuple(f"rdkit_qed_max_{index:03d}" for index in range(1, 11))
    frozen = seed_controlled_run(
        app.state.service.registry,
        run,
        run_id="active-run",
        records=records,
    )
    for record_id in records[:2]:
        write_json(
            run / "per-record/single_llm_skills_on" / f"{record_id}.json",
            result("single_llm_skills_on", record_id),
        )
    write_json(
        run / "progress/state.json",
        {"status": "running", "total": 10, "completed": 2, "groups": {}},
    )
    control = app.state.service.registry.load_control(frozen.run_id).model_copy(
        update={"state": "running"}
    )
    app.state.service.supervisor.reconcile = AsyncMock(return_value=control)

    with TestClient(app, base_url="http://127.0.0.1:8875") as client:
        response = client.get("/api/runs")

    assert response.status_code == 200
    assert response.json()[0]["progress"]["completed"] == 2
    assert response.json()[0]["progress"]["total"] == 10
