from __future__ import annotations

import os
from pathlib import Path

import pytest

from benchmark_orchestrator.annotations import AnnotationStore
from benchmark_orchestrator.models import (
    AnnotationCreate,
    AnnotationPatch,
    MetadataPatch,
    OrchestratorError,
)
from benchmark_orchestrator.registry import FileControlRegistry
from tests.helpers import seed_controlled_run


def test_registry_round_trip_permissions_and_log_paging(tmp_path: Path) -> None:
    registry = FileControlRegistry(tmp_path / "control")
    frozen = seed_controlled_run(registry, tmp_path / "runs/formal/demo/model/run")
    assert registry.load_frozen(frozen.run_id) == frozen
    control_path = registry.run_dir(frozen.run_id) / "control.json"
    assert os.stat(control_path).st_mode & 0o777 == 0o600
    assert os.stat(registry.run_dir(frozen.run_id)).st_mode & 0o777 == 0o700


def test_backend_lock_is_exclusive(tmp_path: Path) -> None:
    first = FileControlRegistry(tmp_path / "control")
    second = FileControlRegistry(tmp_path / "control")
    first.acquire_backend_lock()
    try:
        with pytest.raises(OrchestratorError, match="Another Backend"):
            second.acquire_backend_lock()
    finally:
        first.release_backend_lock()
    second.acquire_backend_lock()
    second.release_backend_lock()


def test_annotation_crud_does_not_touch_execution_artifacts(tmp_path: Path) -> None:
    results = tmp_path / "run/results.json"
    results.parent.mkdir(parents=True)
    results.write_text('{"schema_version":3}\n', encoding="utf-8")
    before = results.read_bytes()
    store = AnnotationStore(tmp_path / "control/annotations.sqlite")
    metadata = store.patch_metadata(
        "run", MetadataPatch(alias="Verifier", favorite=True)
    )
    created = store.create(
        AnnotationCreate(run_id="run", record_id="r1", note="review", tags=["vgb"])
    )
    updated = store.update(
        created["id"], AnnotationPatch(note="checked", status="done")
    )
    deleted = store.delete(created["id"])
    assert metadata["favorite"] is True
    assert updated["note"] == "checked"
    assert deleted["deleted"] is True
    assert store.list_annotations(run_id="run") == []
    assert len(store.list_annotations(run_id="run", include_deleted=True)) == 1
    assert results.read_bytes() == before
