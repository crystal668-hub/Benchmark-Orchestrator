from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .models import (
    AnnotationCreate,
    AnnotationPatch,
    CommandRequest,
    CreateRunRequest,
    MetadataPatch,
    OrchestratorError,
    RunSpec,
)
from .service import RunService


def _error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    details: object | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "request_id": request_id,
            }
        },
    )


def create_api(
    service: RunService, *, static_dir: Path, configured_host: str, configured_port: int
) -> FastAPI:
    app = FastAPI(title="Benchmark Orchestrator", version="0.1.0", docs_url="/api/docs")
    rate_windows: dict[tuple[str, str], deque[float]] = defaultdict(deque)
    host_values = {
        f"{configured_host}:{configured_port}",
        f"localhost:{configured_port}",
        f"127.0.0.1:{configured_port}",
    }
    if ":" in configured_host:
        host_values.add(f"[{configured_host}]:{configured_port}")

    @app.middleware("http")
    async def local_request_guard(request: Request, call_next):
        request.state.request_id = (
            request.headers.get("x-request-id") or uuid.uuid4().hex
        )
        content_length = request.headers.get("content-length")
        try:
            too_large = bool(content_length and int(content_length) > 1_048_576)
        except ValueError:
            return _error_response(
                400,
                "invalid_request",
                "Invalid Content-Length header",
                request.state.request_id,
            )
        if too_large:
            return _error_response(
                413,
                "request_too_large",
                "Request body exceeds 1 MiB",
                request.state.request_id,
            )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            host = request.headers.get("host", "")
            if host not in host_values:
                return _error_response(
                    403,
                    "host_denied",
                    "Mutation request Host is not allowed",
                    request.state.request_id,
                )
            origin = request.headers.get("origin")
            if origin:
                origin_netloc = urlparse(origin).netloc
                if origin_netloc != host:
                    return _error_response(
                        403,
                        "origin_denied",
                        "Mutation request must be same-origin",
                        request.state.request_id,
                    )
            if request.url.path in {"/api/runs/preview", "/api/runs"}:
                key = (
                    request.client.host if request.client else "local",
                    request.url.path,
                )
                now = time.monotonic()
                window = rate_windows[key]
                while window and now - window[0] >= 60:
                    window.popleft()
                limit = 30 if request.url.path.endswith("/preview") else 10
                if len(window) >= limit:
                    return _error_response(
                        429,
                        "rate_limited",
                        "Too many control requests",
                        request.state.request_id,
                    )
                window.append(now)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(OrchestratorError)
    async def orchestrator_error(
        request: Request, exc: OrchestratorError
    ) -> JSONResponse:
        return _error_response(
            exc.status_code,
            exc.code,
            exc.message,
            request.state.request_id,
            exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            422,
            "invalid_request",
            "Request schema validation failed",
            request.state.request_id,
            jsonable_encoder(exc.errors()),
        )

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "schema_version": 1}

    @app.get("/api/capabilities")
    async def capabilities() -> dict[str, object]:
        return await service.capabilities()

    @app.post("/api/runs/preview")
    async def preview(spec: RunSpec) -> dict[str, object]:
        return (await service.preview(spec)).model_dump(mode="json")

    @app.post("/api/runs", status_code=202)
    async def create_run(request: CreateRunRequest) -> dict[str, object]:
        return await service.create(request)

    @app.get("/api/runs")
    async def list_runs(include_hidden: bool = False) -> list[dict[str, object]]:
        return await service.list_runs(include_hidden=include_hidden)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, object]:
        return await service.get_run(run_id)

    @app.patch("/api/runs/{run_id}")
    async def patch_run(run_id: str, patch: MetadataPatch) -> dict[str, object]:
        await service.get_run(run_id)
        return service.annotations.patch_metadata(run_id, patch)

    @app.get("/api/runs/{run_id}/control")
    async def control(run_id: str) -> dict[str, object]:
        return await service.control(run_id)

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str, command: CommandRequest) -> JSONResponse:
        payload = await service.cancel(run_id, command)
        status_code = (
            200
            if payload["state"] in {"cancelled", "completed", "failed", "interrupted"}
            else 202
        )
        return JSONResponse(status_code=status_code, content=payload)

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str, command: CommandRequest) -> dict[str, object]:
        return await service.resume(run_id, command)

    @app.get("/api/runs/{run_id}/progress")
    async def progress(run_id: str) -> dict[str, object]:
        return (await service.get_run(run_id))["progress"]

    @app.get("/api/runs/{run_id}/tasks")
    async def tasks(run_id: str) -> list[dict[str, object]]:
        return service.tasks(run_id)

    @app.get("/api/runs/{run_id}/records")
    async def records(run_id: str) -> list[dict[str, object]]:
        try:
            return service.artifacts.list_records(run_id)
        except OrchestratorError as exc:
            if exc.code != "run_not_found":
                raise
            return [
                item for item in service.tasks(run_id) if item["result"] is not None
            ]

    @app.get("/api/runs/{run_id}/records/{record_id}")
    async def record(run_id: str, record_id: str) -> dict[str, object]:
        return service.artifacts.get_record(run_id, record_id)

    @app.get("/api/runs/{run_id}/assets/{asset_path:path}")
    async def asset(run_id: str, asset_path: str) -> FileResponse:
        path, media_type = service.artifacts.resolve_asset(run_id, asset_path)
        return FileResponse(path, media_type=media_type, filename=path.name)

    @app.get("/api/runs/{run_id}/control/log")
    async def launcher_log(
        run_id: str,
        invocation_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=65_536, ge=1, le=65_536),
    ) -> dict[str, object]:
        return service.registry.read_log(
            run_id, invocation_id, offset=offset, limit=limit
        )

    @app.get("/api/annotations")
    async def list_annotations(
        run_id: str | None = None, include_deleted: bool = False
    ) -> list[dict[str, object]]:
        return service.annotations.list_annotations(
            run_id=run_id, include_deleted=include_deleted
        )

    @app.post("/api/annotations", status_code=201)
    async def create_annotation(annotation: AnnotationCreate) -> dict[str, object]:
        await service.get_run(annotation.run_id)
        return service.annotations.create(annotation)

    @app.patch("/api/annotations/{annotation_id}")
    async def update_annotation(
        annotation_id: str, patch: AnnotationPatch
    ) -> dict[str, object]:
        return service.annotations.update(annotation_id, patch)

    @app.delete("/api/annotations/{annotation_id}")
    async def delete_annotation(annotation_id: str) -> dict[str, object]:
        return service.annotations.delete(annotation_id)

    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app
