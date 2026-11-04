"""Serve the demo page and API with linked OpenTelemetry spans and JSON logs."""

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Flush pending spans and close request logs when the server stops."""
    try:
        yield
    finally:
        app.state.provider.shutdown()
        for handler in app.state.logger.handlers[:]:
            handler.close()
            app.state.logger.removeHandler(handler)


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Record each response while its automatic HTTP server span is active."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Link the response, its JSON log and its trace by the same identifiers."""
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path == "/healthz":
            return response
        context = trace.get_current_span().get_span_context()
        trace_id = f"{context.trace_id:032x}"
        response.headers["X-Trace-ID"] = trace_id
        error_type = getattr(request.state, "error_type", None)
        request.app.state.logger.info(
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "service": "demo-fastapi",
                    "level": "ERROR" if response.status_code >= 500 else "INFO",
                    "message": "Demo file cannot be read"
                    if error_type
                    else "Demo request completed",
                    "route": request.url.path,
                    "status": response.status_code,
                    "error_type": error_type,
                    "trace_id": trace_id,
                    "span_id": f"{context.span_id:016x}",
                }
            )
        )
        return response


def home(request: Request) -> FileResponse:
    """Serve the page independently from the demo data dependency."""
    return FileResponse(request.app.state.page_file, media_type="text/html")


def health() -> dict[str, str]:
    """Report that the process is responding without testing the data volume."""
    return {"status": "ok"}


def check(request: Request) -> JSONResponse:
    """Read only the fixed demo file and record the operation in a child span."""
    span = trace.get_current_span()
    trace_id = f"{span.get_span_context().trace_id:032x}"
    try:
        with (
            request.app.state.tracer.start_as_current_span("read demo message"),
            request.app.state.message_file.open() as message_file,
        ):
            message = message_file.read(4096).strip()
    except OSError as error:
        request.state.error_type = type(error).__name__
        span.set_status(Status(StatusCode.ERROR, "Demo file cannot be read"))
        span.set_attribute("error.type", request.state.error_type)
        return JSONResponse(
            {
                "status": "error",
                "error": "dependency_unavailable",
                "trace_id": trace_id,
            },
            status_code=503,
        )
    return JSONResponse({"status": "ok", "message": message, "trace_id": trace_id})


def create_app(
    message_file: Path = Path("/srv/demo/message.txt"),
    log_file: Path = Path("/var/log/labstack-fastapi/requests.jsonl"),
    provider: TracerProvider | None = None,
) -> FastAPI:
    """Build the same instrumented application for Uvicorn and request tests."""
    if provider is None:
        provider = TracerProvider(
            resource=Resource.create({"service.name": "demo-fastapi"})
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint="http://127.0.0.1:4318/v1/traces", timeout=3),
                schedule_delay_millis=500,
            )
        )
    app = FastAPI(title="Labstack demo", lifespan=lifespan)
    app.state.provider = provider
    app.state.tracer = provider.get_tracer("labstack.demo")
    app.state.message_file = message_file
    app.state.page_file = Path(__file__).with_name("index.html")
    logger = logging.getLogger(f"demo-fastapi.{id(provider)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(
        RotatingFileHandler(log_file, maxBytes=1024 * 1024, backupCount=2)
    )
    logger.addHandler(logging.StreamHandler())
    app.state.logger = logger
    app.add_middleware(RequestLogMiddleware)
    app.add_api_route("/", home, methods=["GET"], include_in_schema=False)
    app.add_api_route("/healthz", health, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/check", check, methods=["GET"])
    # Keep one HTTP server span and the useful file span, without ASGI send/receive noise.
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls="/healthz",
        exclude_spans=["send", "receive"],
    )
    return app
