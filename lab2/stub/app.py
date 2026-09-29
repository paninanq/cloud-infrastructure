"""Учебная заглушка products с метриками, JSON-логами и OpenTelemetry-трейсами."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.request import urlopen

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CollectorRegistry, Counter, Histogram, make_asgi_app


SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "products-observability-demo")
PORT = int(os.getenv("PORT", "8000"))
DELAY_SECONDS = 2
LOAD_REQUESTS = 25
KNOWN_PATHS = {
    "/",
    "/autoservices",
    "/maintenance-appointments/error",
    "/maintenance-appointments/delay",
    "/maintenance-appointments/load",
}

registry = CollectorRegistry()
http_requests = Counter(
    "products_demo_http_requests_total",
    "HTTP requests served by the products demo service.",
    labelnames=("method", "path", "status"),
    registry=registry,
)
http_errors = Counter(
    "products_demo_http_errors_total",
    "HTTP responses with a 5xx status served by the products demo service.",
    labelnames=("path",),
    registry=registry,
)
http_duration = Histogram(
    "products_demo_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    labelnames=("method", "path"),
    registry=registry,
)


class JsonLogFormatter(logging.Formatter):
    """Format each log record as one JSON object with the active trace ID."""

    def format(self, record: logging.LogRecord) -> str:
        span_context = trace.get_current_span().get_span_context()
        trace_id = f"{span_context.trace_id:032x}" if span_context.is_valid else None
        return json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname.lower(),
                "message": record.getMessage(),
                "trace_id": trace_id,
            },
            ensure_ascii=False,
        )


logger = logging.getLogger(SERVICE_NAME)
logger.setLevel(logging.INFO)
logger.propagate = False
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(JsonLogFormatter())
logger.addHandler(stream_handler)

tracer_provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
if os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer(SERVICE_NAME)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Flush buffered spans when the demo server shuts down."""
    yield
    tracer_provider.force_flush(timeout_millis=5_000)
    tracer_provider.shutdown()


app = FastAPI(title="Pochini products observability demo", lifespan=lifespan)
app.mount("/metrics", make_asgi_app(registry=registry))


@app.middleware("http")
async def record_http_observations(request: Request, call_next):
    """Record RED metrics and one trace-correlated structured log per request."""
    path = request.url.path
    if path == "/metrics" or path.startswith("/metrics/"):
        return await call_next(request)

    metric_path = path if path in KNOWN_PATHS else "unmatched"
    started_at = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        http_requests.labels(request.method, metric_path, str(status_code)).inc()
        if status_code >= 500:
            http_errors.labels(metric_path).inc()
        http_duration.labels(request.method, metric_path).observe(time.perf_counter() - started_at)
        logger.info("HTTP request completed: %s %s -> %s", request.method, path, status_code)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Show buttons that trigger the three observability scenarios."""
    return """<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Pochini products — observability demo</title>
    <style>
      body { max-width: 720px; margin: 3rem auto; padding: 0 1rem; font: 16px system-ui, sans-serif; }
      button { margin: .25rem; padding: .7rem 1rem; cursor: pointer; }
      pre { min-height: 3rem; padding: 1rem; background: #f2f4f7; white-space: pre-wrap; }
    </style>
  </head>
  <body>
    <h1>Products observability demo</h1>
    <p>Сценарии для учебного API автосервисов и записей на обслуживание.</p>
    <button data-path="/maintenance-appointments/error">Создать ошибку</button>
    <button data-path="/maintenance-appointments/delay">Создать задержку</button>
    <button data-path="/maintenance-appointments/load">Нагрузка</button>
    <pre id="result" aria-live="polite">Выбери сценарий.</pre>
    <script>
      const output = document.querySelector("#result");
      document.querySelectorAll("button[data-path]").forEach((button) => {
        button.addEventListener("click", async () => {
          output.textContent = "Запрос выполняется…";
          try {
            const response = await fetch(button.dataset.path);
            const body = await response.json();
            output.textContent = `${response.status} ${response.statusText}\\n${JSON.stringify(body, null, 2)}`;
          } catch (error) {
            output.textContent = String(error);
          }
        });
      });
    </script>
  </body>
</html>"""


@app.get("/autoservices")
async def list_autoservices() -> dict[str, list[dict[str, str]]]:
    """Return a small in-memory list representing the products domain."""
    return {
        "autoservices": [
            {"id": "autoservice-1", "name": "Северный гараж", "city": "Москва"},
            {"id": "autoservice-2", "name": "Пятое колесо", "city": "Казань"},
        ]
    }


@app.get("/maintenance-appointments/error")
async def simulate_appointment_error() -> None:
    """Raise a synthetic 500 and mark its current span as failed."""
    span = trace.get_current_span()
    span.set_status(Status(StatusCode.ERROR, "synthetic maintenance appointment failure"))
    span.add_event("synthetic_error_injected")
    logger.error("Synthetic maintenance appointment failure requested")
    raise HTTPException(status_code=500, detail="Synthetic maintenance appointment failure")


@app.get("/maintenance-appointments/delay")
async def simulate_appointment_delay() -> dict[str, str | float]:
    """Simulate a slow dependency inside a child span."""
    with tracer.start_as_current_span("slow-dependency") as span:
        span.set_attribute("dependency.name", "maintenance-scheduler")
        await asyncio.sleep(DELAY_SECONDS)
    return {"status": "completed", "simulated_delay_seconds": DELAY_SECONDS}


def _request_local_autoservices() -> int:
    """Make one real HTTP request back to this service over the loopback port."""
    with urlopen(f"http://127.0.0.1:{PORT}/autoservices", timeout=5) as response:
        response.read()
        return response.status


@app.get("/maintenance-appointments/load")
async def generate_load() -> dict[str, int | list[int]]:
    """Issue a batch of concurrent HTTP requests to the local service."""
    statuses = await asyncio.gather(
        *(asyncio.to_thread(_request_local_autoservices) for _ in range(LOAD_REQUESTS))
    )
    return {"requests_sent": len(statuses), "status_codes": list(statuses)}


FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider, excluded_urls=r".*/metrics(?:/.*)?")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
