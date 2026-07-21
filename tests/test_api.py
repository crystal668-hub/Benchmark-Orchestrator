from __future__ import annotations

from fastapi.testclient import TestClient

from benchmark_orchestrator.app import build_app
from benchmark_orchestrator.config import OrchestratorConfig
from tests.fake_runtime import result, write_json


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
