"""Private OTel lifecycle. No global providers, auto-instrumentation or log bridge.

The final OTLP protobuf boundary is deliberately stricter than ordinary attribute
redaction: arbitrary text is never a telemetry field in this foundation.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import ssl
import stat
import threading
import time
from urllib.parse import urlsplit


class TelemetryConfigError(ValueError):
    """Messages contain field names only, never configuration values."""


def _read_regular_file(path, limit):
    """Return at most limit + 1 bytes of a regular file, never blocking.

    The path is checked before it is opened, so a FIFO, device or directory is
    never opened, and the open descriptor is checked again before any read, so
    a path swapped after the first check is refused too. O_NONBLOCK keeps that
    open from waiting for a FIFO writer.
    """
    if not stat.S_ISREG(Path(path).stat().st_mode):
        raise ValueError
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError
        source = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with source:
        return source.read(limit + 1)


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    endpoint: str = field(default="", repr=False)
    headers: dict = field(default_factory=dict, repr=False)
    service: str = "rag-api"
    version: str = "1.1.0"
    environment: str = "development"
    traces: bool = True
    logs: bool = True
    metrics: bool = True
    sample_ratio: float = 1.0
    queue_size: int = 256
    batch_size: int = 64
    timeout_ms: int = 1000
    interval_ms: int = 5000
    shutdown_ms: int = 3000
    ca_file: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        def value(key, default):
            return env.get("RAG_OTEL_" + key, str(default))
        def boolean(key, default):
            raw = value(key, str(default).lower())
            if raw not in ("true", "false"):
                raise TelemetryConfigError("Invalid RAG_OTEL_" + key)
            return raw == "true"
        if not boolean("ENABLED", False):
            return cls()  # No other configuration or secret file is examined.
        # All pinned SDK providers read the real process environment, even when
        # callers supply a separate RAG configuration mapping. Reject the
        # conflict before creating exporters rather than mutate global state.
        if os.environ.get("OTEL_SDK_DISABLED", "").lower().strip() == "true":
            raise TelemetryConfigError("RAG_OTEL_ENABLED conflicts with OTEL_SDK_DISABLED")
        def number(key, default, low, high, integer=True):
            try:
                raw = value(key, default)
                parsed = int(raw) if integer else float(raw)
                if not math.isfinite(parsed) or not low <= parsed <= high:
                    raise ValueError
                return parsed
            except (ValueError, TypeError, OverflowError):
                raise TelemetryConfigError("Invalid RAG_OTEL_" + key) from None
        def identifier(key, default):
            raw = value(key, default)
            if not isinstance(raw, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", raw):
                raise TelemetryConfigError("Invalid RAG_OTEL_" + key)
            return raw
        if value("PROTOCOL", "http/protobuf") != "http/protobuf":
            raise TelemetryConfigError("Invalid RAG_OTEL_PROTOCOL (requires http/protobuf)")
        endpoint = value("ENDPOINT", "")
        try:
            url = urlsplit(endpoint)
            valid = (url.scheme in ("http", "https") and url.hostname and url.port != 0
                     and not url.username and not url.password and not url.query
                     and not url.fragment and url.path in ("", "/")
                     and not any(c.isspace() for c in endpoint))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise TelemetryConfigError("Invalid RAG_OTEL_ENDPOINT (requires HTTP origin)")
        headers = {}
        secret_file = value("HEADERS_FILE", "")
        if secret_file:
            try:
                raw = _read_regular_file(secret_file, 8192)
                if len(raw) > 8192:
                    raise ValueError
                def header_pairs(pairs):
                    result = {}
                    for key, val in pairs:
                        canonical = {"authorization": "Authorization", "x-api-key": "X-Api-Key"}.get(key.lower())
                        if canonical is None or canonical in result:
                            raise ValueError
                        result[canonical] = val
                    return result
                headers = json.loads(raw, object_pairs_hook=header_pairs)
                if (not isinstance(headers, dict) or len(headers) > 8
                    or any(not isinstance(k, str) or not isinstance(v, str)
                           or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", k)
                           or k.lower() not in ("authorization", "x-api-key")
                           or not v or len(v) > 2048 or any(ord(c) < 32 or ord(c) > 126 for c in v)
                           for k, v in headers.items())):
                    raise ValueError
            except (OSError, ValueError, TypeError):
                raise TelemetryConfigError("Invalid RAG_OTEL_HEADERS_FILE") from None
        if headers and url.scheme != "https":
            raise TelemetryConfigError("RAG_OTEL_HEADERS_FILE requires HTTPS RAG_OTEL_ENDPOINT")
        # Optional private CA bundle, checked once here; requests re-reads the
        # path on new connections. It replaces, not extends, the bundled roots.
        ca_file = value("CA_FILE", "")
        if ca_file:
            try:
                if len(_read_regular_file(ca_file, 1048576)) > 1048576:
                    raise ValueError
                ssl.create_default_context(cafile=ca_file)
            except (OSError, ValueError, TypeError):
                raise TelemetryConfigError("Invalid RAG_OTEL_CA_FILE") from None
            if url.scheme != "https":
                raise TelemetryConfigError("RAG_OTEL_CA_FILE requires HTTPS RAG_OTEL_ENDPOINT")
        queue = number("QUEUE_SIZE", 256, 1, 4096)
        batch = number("BATCH_SIZE", 64, 1, 512)
        if batch > queue:
            raise TelemetryConfigError("RAG_OTEL_BATCH_SIZE exceeds RAG_OTEL_QUEUE_SIZE")
        return cls(True, endpoint.rstrip("/"), headers,
                   identifier("SERVICE_NAME", "rag-api"), identifier("SERVICE_VERSION", "1.1.0"),
                   identifier("ENVIRONMENT", "development"),
                   boolean("TRACES", True), boolean("LOGS", True), boolean("METRICS", True),
                   number("SAMPLE_RATIO", 1, 0, 1, False), queue, batch,
                   number("TIMEOUT_MS", 1000, 100, 10000),
                   number("INTERVAL_MS", 5000, 1000, 60000),
                   number("SHUTDOWN_MS", 3000, 100, 30000), ca_file=ca_file)


# Finite value vocabularies prevent sensitive values hidden under approved keys.
ENUMS = {
    "rag.operation": frozenset(("startup", "query", "ingest", "export", "import", "tuning", "evaluation")),
    "rag.outcome": frozenset(("ok", "error", "cancelled", "partial")),
    "error.type": frozenset(("timeout", "connection", "validation", "internal")),
}
SPAN_NAMES = frozenset("rag." + v for v in ENUMS["rag.operation"])

# Explicit code-owned vocabulary. Raw URL/path values never enter this schema.
ROUTES = frozenset(('DELETE /collections/{name}', 'GET /collections', 'GET /export/job/{job_id}', 'GET /goldstandard/diagnostics', 'GET /goldstandard/download/{filename}', 'GET /goldstandard/session/{session_id}', 'GET /health', 'GET /help/transfer', 'GET /import/job/{job_id}', 'GET /ingest/config/{collection}', 'GET /ingest/job/{job_id}', 'GET /metrics/latency', 'GET /packages', 'GET /retrieval/config/{collection}', 'GET /tune/job/{job_id}', 'GET /tune/{collection}', 'PATCH /goldstandard/session/{session_id}/pair/{pair_id}', 'POST /collections', 'POST /export', 'POST /goldstandard/generate', 'POST /goldstandard/regenerate', 'POST /goldstandard/save', 'POST /import', 'POST /ingest/config', 'POST /ingest/upload', 'POST /query', 'POST /retrieval/config', 'POST /tune/rechunk', 'POST /tune/reembed', 'POST /tune/reindex'))
ENUMS.update({"http.route": ROUTES | {"unmatched"},
              "rag.tuning_operation": frozenset(("rechunk", "reembed", "reindex")),
              "http.status_class": frozenset(("1xx", "2xx", "3xx", "4xx", "5xx"))})
SPAN_NAMES |= ROUTES | frozenset(("rag.request", "rag.reformulate", "rag.retrieval", "rag.synthesis",
    "rag.parse", "rag.chunk", "rag.store", "rag.retain", "rag.package", "rag.validate",
    "rag.rebuild", "rag.pair", "rag.regenerate", "rag.failure_report",
    "ollama.chat", "ollama.embed", "ollama.models", "ollama.health",
    "weaviate.connect", "weaviate.health", "weaviate.create", "weaviate.delete",
    "weaviate.exists", "weaviate.list", "weaviate.meta", "weaviate.config",
    "weaviate.aggregate", "weaviate.query", "weaviate.iterate", "weaviate.batch"))

METRIC_NAMES = frozenset(("rag.telemetry.check",))


def safe_attributes(attributes):
    if not isinstance(attributes, Mapping):
        return {}
    return {k: v for k, v in attributes.items()
            if k in ENUMS and isinstance(v, str) and v in ENUMS[k]}


def _valid_metric(record):
    """Admit a complete dimensionless stream before allocating batch capacity.

    Multiple input points are rejected: stripping their labels would merge
    distinct streams without a defined aggregation policy.
    """
    kind = record.WhichOneof("data")
    if record.name not in METRIC_NAMES or kind not in ("sum", "gauge", "histogram"):
        return False
    data = getattr(record, kind)
    if len(data.data_points) != 1:
        return False
    if kind != "gauge" and data.aggregation_temporality not in (1, 2):
        return False
    point = data.data_points[0]
    if kind != "histogram":
        value = point.WhichOneof("value")
        return value is not None and math.isfinite(getattr(point, value))
    bounds, buckets = point.explicit_bounds, point.bucket_counts
    return (len(bounds) <= 31 and len(buckets) == len(bounds) + 1
            and point.count >= 0 and all(n >= 0 for n in buckets)
            and sum(buckets) == point.count
            and all(math.isfinite(n) for n in bounds)
            and all(a < b for a, b in zip(bounds, bounds[1:]))
            and all(not point.HasField(f) or math.isfinite(getattr(point, f))
                    for f in ("sum", "min", "max"))
            and (not (point.HasField("min") and point.HasField("max"))
                 or point.min <= point.max))


def sanitize_wire(data, signal, config):
    """Rebuild protobuf, dropping all fields not explicitly copied below."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
    types = {"traces": (ExportTraceServiceRequest, "resource_spans", "scope_spans", "spans"),
             "logs": (ExportLogsServiceRequest, "resource_logs", "scope_logs", "log_records"),
             "metrics": (ExportMetricsServiceRequest, "resource_metrics", "scope_metrics", "metrics")}
    cls, resources, scopes, records = types[signal]
    source = cls.FromString(data)
    output = cls()
    resource = getattr(output, resources).add()
    for key, val in (("service.name", config.service), ("service.version", config.version),
                     ("deployment.environment.name", config.environment)):
        attr = resource.resource.attributes.add(key=key)
        attr.value.string_value = val
    scope = getattr(resource, scopes).add()
    scope.scope.name = "rag.telemetry"
    def attrs(src, dst):
        seen = set()
        for a in src:
            if (a.key not in seen and a.value.WhichOneof("value") == "string_value"
                and a.value.string_value in ENUMS.get(a.key, ())):
                dst.add().CopyFrom(a)
                seen.add(a.key)
    count = 0
    for rs in getattr(source, resources):
        for ss in getattr(rs, scopes):
            for record in getattr(ss, records):
                if count >= config.batch_size:
                    break
                if signal == "metrics" and not _valid_metric(record):
                    continue
                count += 1
                dest = getattr(scope, records).add()
                if signal == "traces":
                    dest.name = record.name if record.name in SPAN_NAMES else "rag.operation"
                    dest.trace_id, dest.span_id, dest.parent_span_id = record.trace_id, record.span_id, record.parent_span_id
                    dest.start_time_unix_nano, dest.end_time_unix_nano = record.start_time_unix_nano, record.end_time_unix_nano
                    dest.kind = record.kind
                    dest.status.code = record.status.code
                    attrs(record.attributes, dest.attributes)
                    for link in record.links[:1]:
                        if len(link.trace_id) == 16 and any(link.trace_id) and len(link.span_id) == 8 and any(link.span_id):
                            dest.links.add(trace_id=link.trace_id, span_id=link.span_id, flags=link.flags & 1)
                elif signal == "logs":
                    dest.time_unix_nano, dest.observed_time_unix_nano = record.time_unix_nano, record.observed_time_unix_nano
                    dest.trace_id, dest.span_id = record.trace_id, record.span_id
                    dest.severity_number = record.severity_number
                    dest.body.string_value = "rag.operation"
                    attrs(record.attributes, dest.attributes)
                else:
                    dest.name = record.name
                    kind = record.WhichOneof("data")
                    src_data, dst_data = getattr(record, kind), getattr(dest, kind)
                    if kind != "gauge":
                        dst_data.aggregation_temporality = src_data.aggregation_temporality
                    if kind == "sum":
                        dst_data.is_monotonic = src_data.is_monotonic
                    # No dimensions or exemplars in the foundation; views also
                    # remove labels before SDK aggregation to bound cardinality.
                    for point in src_data.data_points:
                        dp = dst_data.data_points.add()
                        dp.start_time_unix_nano, dp.time_unix_nano = point.start_time_unix_nano, point.time_unix_nano
                        if kind == "histogram":
                            dp.count = point.count
                            for field_name in ("sum", "min", "max"):
                                if point.HasField(field_name):
                                    setattr(dp, field_name, getattr(point, field_name))
                            dp.bucket_counts.extend(point.bucket_counts)
                            dp.explicit_bounds.extend(point.explicit_bounds)
                        else:
                            val = point.WhichOneof("value")
                            if val:
                                setattr(dp, val, getattr(point, val))
    return output.SerializeToString()


def _session(signal, config):
    import requests
    class SafeSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.trust_env = False  # No ambient proxy, netrc, or credentials.
        def post(self, url, data=None, **kwargs):
            result = requests.Response()
            result.status_code = 400  # SDK treats this as non-retryable.
            result._content = b"telemetry export failed"
            try:
                payload = sanitize_wire(data, signal, config)
                # Explicit arguments override ambient OTEL_* TLS/header options;
                # a configured CA file is the only trust store when set.
                self.cookies.clear()
                self.headers.clear()
                self.headers.update({"Content-Type": "application/x-protobuf", **config.headers})
                with super().post(config.endpoint + "/v1/" + signal, data=payload,
                                  timeout=config.timeout_ms / 1000, verify=config.ca_file or True,
                                  allow_redirects=False, stream=True) as response:
                    if 200 <= response.status_code < 300:
                        result.status_code = 200
                        result._content = b""
            except Exception:
                # No exception text, response bodies, destination or headers in
                # SDK diagnostics; normal application logging is untouched.
                pass
            return result
    return SafeSession()


class Runtime:
    def __init__(self, config):
        self.config = config
        self._providers = []
        self.tracer = self.logger = self.meter = None
        self._lock = threading.Lock()
        self._worker = None
        self._closed = False
        self._shutdown_requested = threading.Event()
        self._last_ok = True

    def _run(self, shutdown):
        ok = True
        try:
            for provider in self._providers:
                try:
                    if shutdown:
                        provider.shutdown()
                    else:
                        ok = provider.force_flush(timeout_millis=self.config.shutdown_ms) is not False and ok
                except Exception:
                    ok = False
        finally:
            self._last_ok = ok

    def _lifecycle(self, shutdown):
        with self._lock:
            if shutdown:
                self._closed = True
                self._shutdown_requested.set()
            elif self._closed:
                return False
            if shutdown and getattr(self, "_shutdown_finished", False):
                return self._last_ok
            if self._worker is None:
                def work():
                    closing = shutdown
                    while True:
                        self._run(closing)
                        with self._lock:
                            if not closing and self._shutdown_requested.is_set():
                                closing = True
                                continue
                            if closing:
                                self._shutdown_finished = True
                            self._worker = None
                            return
                self._worker = threading.Thread(target=work, name="rag-telemetry-lifecycle", daemon=True)
                self._worker.start()
            worker = self._worker
        worker.join(self.config.shutdown_ms / 1000)
        return not worker.is_alive() and self._last_ok

    def force_flush(self):
        return True if not self.config.enabled else self._lifecycle(False)

    def shutdown(self):
        return True if not self.config.enabled else self._lifecycle(True)


def bootstrap(env=None):
    config = Config.from_env(env)
    runtime = Runtime(config)
    if not config.enabled:
        return runtime
    from opentelemetry.metrics import NoOpMeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider, SpanLimits
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk._logs import LoggerProvider, ReadWriteLogRecord, LogRecordLimits
    from opentelemetry._logs import LogRecord
    from opentelemetry.context import Context
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider, AlwaysOffExemplarFilter
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.metrics.view import View, DropAggregation
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    class SafeBatchSpans(BatchSpanProcessor):
        def on_end(self, span):
            from opentelemetry.sdk.trace import ReadableSpan
            from opentelemetry.trace import Status, SpanContext
            def clean_context(context):
                return None if context is None else SpanContext(
                    trace_id=context.trace_id, span_id=context.span_id,
                    is_remote=context.is_remote, trace_flags=context.trace_flags)
            clean = ReadableSpan(
                name=span.name if span.name in SPAN_NAMES else "rag.operation",
                context=clean_context(span.context), parent=clean_context(span.parent), resource=resource,
                links=safe_links(span.links),
                attributes=safe_attributes(span.attributes), kind=span.kind,
                status=Status(span.status.status_code), start_time=span.start_time,
                end_time=span.end_time, instrumentation_scope=scope)
            super().on_end(clean)

    class SafeLogger:
        """Keep SDK normalization/exception expansion off caller-owned records."""
        def __init__(self, logger):
            self._logger = logger

        def emit(self, record=None, *, timestamp=None, observed_timestamp=None,
                 context=None, severity_number=None, severity_text=None, body=None,
                 attributes=None, event_name=None, exception=None):
            if record is not None:
                import copy
                target = copy.copy(record.log_record if isinstance(record, ReadWriteLogRecord) else record)
            else:
                target = LogRecord(timestamp=timestamp, observed_timestamp=observed_timestamp,
                                   context=context, severity_number=severity_number,
                                   severity_text=severity_text, body=body, attributes=attributes,
                                   event_name=event_name, exception=exception)
            target.attributes = safe_attributes(target.attributes)
            # Every form must bypass SDK default wrappers, which read ambient
            # limits before our batch processor can sanitize or repair data.
            wrapped = ReadWriteLogRecord(log_record=target, resource=resource,
                                         instrumentation_scope=scope, limits=log_limits)
            return self._logger.emit(wrapped)

    class SafeBatchLogs(BatchLogRecordProcessor):
        def on_emit(self, record):
            # Rebuild both layers: shallow copies retain caller-owned metadata
            # and exception objects, even after the visible attributes are safe.
            source = record.log_record
            clean_record = LogRecord(
                timestamp=source.timestamp, observed_timestamp=source.observed_timestamp,
                severity_number=source.severity_number, body="rag.operation",
                attributes=safe_attributes(source.attributes))
            clean_record.context = Context()
            clean_record.trace_id = source.trace_id
            clean_record.span_id = source.span_id
            clean_record.trace_flags = source.trace_flags
            clean = ReadWriteLogRecord(log_record=clean_record, resource=resource,
                                       instrumentation_scope=scope, limits=log_limits)
            super().on_emit(clean)

    resource = Resource({"service.name": config.service, "service.version": config.version,
                         "deployment.environment.name": config.environment})
    scope = InstrumentationScope("rag.telemetry")
    log_limits = LogRecordLimits(max_attributes=16, max_attribute_length=128,
                                max_log_record_attributes=16, max_log_record_attribute_length=128)
    internal_meter = NoOpMeterProvider()
    def exporter(cls, signal):
        return cls(endpoint=config.endpoint + "/v1/" + signal,
                   headers={"Content-Type": "application/x-protobuf"},
                   timeout=config.timeout_ms / 1000, compression=Compression.NoCompression,
                   session=_session(signal, config), meter_provider=internal_meter)
    batch = dict(max_queue_size=config.queue_size, max_export_batch_size=config.batch_size,
                 schedule_delay_millis=config.interval_ms, export_timeout_millis=config.timeout_ms,
                 meter_provider=internal_meter)
    try:
        if config.traces:
            provider = TracerProvider(resource=resource, shutdown_on_exit=False, meter_provider=internal_meter,
                                     sampler=TraceIdRatioBased(config.sample_ratio),
                                     span_limits=SpanLimits(max_attributes=16, max_events=0, max_links=1,
                                                            max_attribute_length=128))
            runtime._providers.append(provider)
            provider.add_span_processor(SafeBatchSpans(exporter(OTLPSpanExporter, "traces"), **batch))
            runtime.tracer = provider.get_tracer("rag.telemetry")
        if config.logs:
            provider = LoggerProvider(resource=resource, shutdown_on_exit=False, meter_provider=internal_meter)
            runtime._providers.append(provider)
            provider.add_log_record_processor(SafeBatchLogs(exporter(OTLPLogExporter, "logs"), **batch))
            runtime.logger = SafeLogger(provider.get_logger("rag.telemetry"))
        if config.metrics:
            reader = PeriodicExportingMetricReader(exporter(OTLPMetricExporter, "metrics"),
                                                  export_interval_millis=config.interval_ms,
                                                  export_timeout_millis=config.timeout_ms)
            provider = MeterProvider(resource=resource, shutdown_on_exit=False, metric_readers=[reader],
                                     exemplar_filter=AlwaysOffExemplarFilter(),
                                     views=[View(instrument_name="*", aggregation=DropAggregation()),
                                            *[View(instrument_name=name, attribute_keys=set()) for name in METRIC_NAMES]])
            runtime._providers.append(provider)
            runtime.meter = provider.get_meter("rag.telemetry")
    except Exception:
        runtime.shutdown()
        raise TelemetryConfigError("Telemetry initialization failed") from None
    return runtime


# App-scoped manual instrumentation. No global provider or request baggage.
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect

_runtime = ContextVar("rag_telemetry_runtime", default=None)


def safe_links(links):
    from opentelemetry.trace import Link, SpanContext, TraceFlags, TraceState
    result = []
    for link in links[:1]:
        c = link.context
        if c.is_valid:
            clean = SpanContext(c.trace_id, c.span_id, c.is_remote,
                                TraceFlags(int(c.trace_flags) & 1), TraceState())
            result.append(Link(clean))
    return result


def capture():
    runtime = _runtime.get()
    if runtime is None or runtime.tracer is None:
        return runtime, None
    from opentelemetry.trace import get_current_span
    try:
        return runtime, get_current_span().get_span_context()
    except Exception:
        return runtime, None


@contextmanager
def bind(snapshot):
    runtime, parent = snapshot
    token = _runtime.set(runtime)
    attached = None
    try:
        try:
            if runtime is not None and runtime.tracer is not None:
                from opentelemetry import context, trace
                clean = context.Context()
                if parent is not None and parent.is_valid:
                    clean = trace.set_span_in_context(trace.NonRecordingSpan(parent), clean)
                attached = context.attach(clean)
        except Exception:
            pass
        yield
    finally:
        if attached is not None:
            try:
                context.detach(attached)
            except Exception:
                pass
        _runtime.reset(token)


def admitted(fn):
    """Capture now, bind inside actual execution (including raw executors)."""
    snapshot = capture()
    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def run(*args, **kwargs):
            with bind(snapshot):
                return await fn(*args, **kwargs)
    else:
        @wraps(fn)
        def run(*args, **kwargs):
            with bind(snapshot):
                return fn(*args, **kwargs)
    return run


def error_type(exc):
    import asyncio
    import httpx
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)) or isinstance(exc.__cause__, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, (ConnectionError, httpx.TransportError)):
        return "connection"
    if isinstance(exc, ValueError):
        return "validation"
    return "internal"


def attribute(key, value, target=None):
    if key not in ENUMS or value not in ENUMS[key]:
        return
    try:
        if target is None:
            runtime = _runtime.get()
            if runtime is None or runtime.tracer is None:
                return
            from opentelemetry.trace import get_current_span
            target = get_current_span()
        target.set_attribute(key, value)
    except Exception:
        pass


def outcome(value, exc=None, target=None):
    if not isinstance(value, str) or value not in ENUMS["rag.outcome"]:
        return
    runtime = _runtime.get()
    if runtime is None or runtime.tracer is None:
        return
    try:
        from opentelemetry import trace
        span = target if target is not None else trace.get_current_span()
        span.set_attribute("rag.outcome", value)
        if value != "ok":
            span.set_status(trace.Status(trace.StatusCode.ERROR))
        if exc is not None and value != "cancelled":
            span.set_attribute("error.type", error_type(exc))
    except Exception:
        pass


@contextmanager
def span(name, *, links=(), server=False):
    runtime = _runtime.get()
    current = None
    token = None
    try:
        if runtime is not None and runtime.tracer is not None:
            from opentelemetry import trace, context
            kind = trace.SpanKind.SERVER if server else (trace.SpanKind.CLIENT if name.startswith(("ollama.", "weaviate.")) else trace.SpanKind.INTERNAL)
            current = runtime.tracer.start_span(name, kind=kind, links=links,
                                                attributes={"rag.outcome": "ok"})
            token = context.attach(trace.set_span_in_context(current))
    except Exception:
        pass
    try:
        yield current
    except BaseException as exc:
        import asyncio
        outcome("cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "error", exc, current)
        raise
    finally:
        if token is not None:
            try:
                context.detach(token)
            except Exception:
                pass
        if current is not None:
            try:
                current.end()
            except Exception:
                pass


def traced(name):
    def decorate(fn):
        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def run(*args, **kwargs):
                with span(name):
                    return await fn(*args, **kwargs)
        else:
            @wraps(fn)
            def run(*args, **kwargs):
                with span(name):
                    return fn(*args, **kwargs)
        return run
    return decorate


def call(name, fn, /, *args, **kwargs):
    with span(name):
        return fn(*args, **kwargs)


def iterate(fn, /, *args, **kwargs):
    # Keep one bounded span for the iterator, but activate it only while pulling
    # an item: generator yield must never leave context attached in its caller.
    runtime = _runtime.get()
    if runtime is None or runtime.tracer is None:
        yield from fn(*args, **kwargs)
        return
    from opentelemetry import trace, context
    try:
        current = runtime.tracer.start_span("weaviate.iterate", kind=trace.SpanKind.CLIENT,
                                            attributes={"rag.outcome": "ok"})
    except Exception:
        yield from fn(*args, **kwargs)
        return
    iterator = None
    try:
        iterator = iter(fn(*args, **kwargs))
        while True:
            token = None
            try:
                token = context.attach(trace.set_span_in_context(current))
            except Exception:
                pass
            try:
                item = next(iterator)
            except StopIteration:
                return
            finally:
                if token is not None:
                    try:
                        context.detach(token)
                    except Exception:
                        pass
            yield item
    except BaseException as exc:
        import asyncio
        outcome("cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "error", exc, current)
        raise
    finally:
        try:
            current.end()
        except Exception:
            pass


def remote_link(headers):
    value = None
    for key, candidate in headers:
        if key.lower() == b"traceparent":
            if value is not None or len(candidate) != 55:
                return ()
            value = candidate
    if value is None or not re.fullmatch(rb"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", value):
        return ()
    from opentelemetry.trace import SpanContext, TraceFlags, TraceState, Link
    _, tid, sid, flags = value.split(b"-")
    c = SpanContext(int(tid, 16), int(sid, 16), True, TraceFlags(int(flags, 16) & 1), TraceState())
    return (Link(c),) if c.is_valid else ()


class RequestTracing:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        runtime = getattr(getattr(scope.get("app"), "state", None), "telemetry", None)
        if scope["type"] != "http" or runtime is None or runtime.tracer is None:
            return await self.app(scope, receive, send)
        # Always a local root. External IDs can correlate but cannot sample it.
        with bind((runtime, None)):
            with span("rag.request", links=remote_link(scope.get("headers", ())), server=True) as request_span:
                async def traced_send(message):
                    if message["type"] == "http.response.start":
                        status = message["status"]
                        if request_span is not None:
                            attribute("http.status_class", str(status // 100) + "xx", request_span)
                        if status >= 400:
                            outcome("error")
                    await send(message)
                try:
                    await self.app(scope, receive, traced_send)
                except Exception:
                    attribute("http.status_class", "5xx", request_span)
                    raise
                finally:
                    route = getattr(scope.get("route"), "path", "")
                    label = scope.get("method", "") + " " + route
                    if request_span is not None:
                        attribute("http.route", label if label in ROUTES else "unmatched", request_span)
                        if label in ROUTES:
                            try:
                                request_span.update_name(label)
                            except Exception:
                                pass
