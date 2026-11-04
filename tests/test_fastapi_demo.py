"""Check real requests, file failures and their linked telemetry."""

import importlib.util
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode


@pytest.fixture
def application(tmp_path):
    """Use the guest application with local files and an in-memory span exporter."""
    path = Path(__file__).parents[1] / "provision/observability/assets/fastapi_app.py"
    spec = importlib.util.spec_from_file_location("fastapi_demo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    message = tmp_path / "message.txt"
    message.write_text("Labstack demo is healthy\n")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    log = tmp_path / "requests.jsonl"
    app = module.create_app(message_file=message, log_file=log, provider=provider)
    return app, message, exporter, log


def test_request_links_http_span_file_span_and_json_log(application):
    app, _, exporter, log = application
    trace_id = "1234567890abcdef1234567890abcdef"
    with TestClient(app) as client:
        response = client.get(
            "/api/check", headers={"traceparent": f"00-{trace_id}-0123456789abcdef-01"}
        )
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "message": "Labstack demo is healthy",
        "trace_id": trace_id,
    }
    assert response.headers["X-Trace-ID"] == trace_id
    spans = exporter.get_finished_spans()
    server = next(span for span in spans if span.kind == SpanKind.SERVER)
    child = next(span for span in spans if span.name == "read demo message")
    assert server.name == "GET /api/check"
    assert child.parent.span_id == server.context.span_id
    assert child.context.trace_id == int(trace_id, 16)
    event = json.loads(log.read_text().splitlines()[-1])
    assert event["trace_id"] == trace_id
    assert event["span_id"] == f"{server.context.span_id:016x}"
    assert event["status"] == 200


@pytest.mark.parametrize(
    "fault,error_type",
    [("missing", "FileNotFoundError"), ("unreadable", "PermissionError")],
)
def test_file_failure_preserves_service_and_records_its_cause(
    application, fault, error_type
):
    app, message, exporter, log = application
    if fault == "missing":
        message.unlink()
    else:
        if os.geteuid() == 0:
            pytest.skip("Permission checks need an unprivileged process")
        message.chmod(0)
    try:
        with TestClient(app) as client:
            assert client.get("/").status_code == 200
            assert client.get("/healthz").status_code == 200
            response = client.get("/api/check")
        assert response.status_code == 503
        assert response.json()["error"] == "dependency_unavailable"
        spans = exporter.get_finished_spans()
        child = next(span for span in spans if span.name == "read demo message")
        assert child.status.status_code == StatusCode.ERROR
        assert any(
            event.attributes["exception.type"] == error_type for event in child.events
        )
        server = next(
            span
            for span in spans
            if span.name == "GET /api/check" and span.kind == SpanKind.SERVER
        )
        assert server.status.status_code == StatusCode.ERROR
        event = json.loads(log.read_text().splitlines()[-1])
        assert event["error_type"] == error_type
        assert event["trace_id"] == response.json()["trace_id"]
        assert event["status"] == 503
        assert (message.exists()) == (fault != "missing")
    finally:
        if message.exists():
            message.chmod(0o644)


def test_client_cannot_choose_another_file(application):
    app, _, _, _ = application
    with TestClient(app) as client:
        response = client.get("/api/check?path=/etc/passwd")
    assert response.status_code == 200
    assert response.json()["message"] == "Labstack demo is healthy"
