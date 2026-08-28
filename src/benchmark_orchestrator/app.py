from __future__ import annotations

import argparse
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from .annotations import AnnotationStore
from .api import create_api
from .artifacts import ArtifactReader
from .config import OrchestratorConfig, load_config
from .registry import FileControlRegistry
from .runtime_adapter import CanonicalCliRuntimeAdapter
from .service import RunService
from .supervisor import LocalRunSupervisor


def build_service(config: OrchestratorConfig) -> RunService:
    registry = FileControlRegistry(config.control_root)
    artifacts = ArtifactReader(config.run_root)
    adapter = CanonicalCliRuntimeAdapter(config)
    supervisor = LocalRunSupervisor(config.launcher, registry, artifacts)
    annotations = AnnotationStore(config.control_root / "annotations.sqlite")
    return RunService(adapter, supervisor, registry, artifacts, annotations)


def build_app(config: OrchestratorConfig):
    service = build_service(config)
    static_dir = Path(__file__).with_name("static")
    app = create_api(
        service,
        static_dir=static_dir,
        configured_host=config.http.host,
        configured_port=config.http.port,
    )

    @asynccontextmanager
    async def lifespan(_app) -> AsyncIterator[None]:
        service.registry.acquire_backend_lock()
        await service.supervisor.reconcile_all()
        try:
            yield
        finally:
            service.registry.release_backend_lock()

    app.router.lifespan_context = lifespan
    app.state.service = service
    app.state.config = config
    return app


def reload_app():
    """Create the application for Uvicorn's reload worker process."""
    config_path = os.environ.get(
        "BENCHMARK_ORCHESTRATOR_CONFIG", "~/.benchmark-orchestrator/orchestrator.yaml"
    )
    return build_app(load_config(config_path))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark Orchestrator local control plane"
    )
    parser.add_argument(
        "--config",
        default="~/.benchmark-orchestrator/orchestrator.yaml",
        help="Path to orchestrator YAML configuration",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    # The reloader imports the application in a child process, so pass the
    # explicitly selected config path through an application-specific env var.
    os.environ["BENCHMARK_ORCHESTRATOR_CONFIG"] = str(config_path)
    uvicorn.run(
        "benchmark_orchestrator.app:reload_app",
        factory=True,
        host=config.http.host,
        port=config.http.port,
        log_level="info",
        reload=True,
    )


if __name__ == "__main__":
    main()
