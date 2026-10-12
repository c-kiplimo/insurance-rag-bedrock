"""FastAPI service.

Route handlers are plain ``def`` (not ``async def``): the pipeline makes blocking boto3 and
OpenSearch calls, and FastAPI runs sync handlers in a worker threadpool, so the event loop is
never blocked. Scale with more tasks/workers, not with asyncio.

Authentication/authorisation is enforced upstream by API Gateway (JWT authoriser or IAM).
This service trusts only the network path from the gateway (private ALB + security groups).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from insurance_rag import __version__
from insurance_rag.bootstrap import App, build_app
from insurance_rag.config import Settings, get_settings
from insurance_rag.logging_setup import configure_logging, request_id_var
from insurance_rag.models import QueryRequest, QueryResponse
from insurance_rag.observability import init_tracing, shutdown_tracing
from insurance_rag.orchestrator.pipeline import RagPipeline

log = logging.getLogger(__name__)


def get_pipeline(request: Request) -> RagPipeline:
    app: App = request.app.state.rag
    return app.pipeline


def create_app(settings: Settings | None = None, rag: App | None = None) -> FastAPI:
    """Application factory. Tests pass a prebuilt ``rag``; production builds from settings."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_tracing(settings.langfuse_enabled)
        app.state.rag = rag or build_app(settings)
        log.info("service started", extra={"env": settings.env, "version": __version__})
        yield
        shutdown_tracing()

    prod = settings.env == "prod"
    app = FastAPI(
        title="Insurance Knowledge Retrieval",
        version=__version__,
        lifespan=lifespan,
        # no interactive docs or schema on a production internal API
        docs_url=None if prod else "/docs",
        redoc_url=None,
        openapi_url=None if prod else "/openapi.json",
    )

    @app.middleware("http")
    async def correlation(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["x-request-id"] = rid
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")  # details go to logs, never to the client
        return JSONResponse(
            status_code=500,
            content={"detail": "internal error", "request_id": request_id_var.get()},
        )

    @app.post("/v1/query", response_model=QueryResponse)
    def query(body: QueryRequest, pipeline: RagPipeline = Depends(get_pipeline)) -> QueryResponse:
        return pipeline.answer(body)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness: the process is up. Must not depend on downstream services."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    def readyz(request: Request) -> dict[str, str]:
        """Readiness: OpenSearch is required; the cache is optional (fail-open)."""
        rag_app: App = request.app.state.rag
        if not rag_app.retriever.ping():
            raise HTTPException(status_code=503, detail="opensearch unavailable")
        cache_ok = getattr(rag_app.cache, "ping", lambda: True)()
        return {"status": "ready", "cache": "up" if cache_ok else "degraded"}

    return app
