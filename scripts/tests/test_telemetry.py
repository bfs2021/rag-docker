"""Real SDK/loopback OTLP acceptance for #282; no external service needed."""
import contextlib
import http.server
import io
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2] / 'api'))
from services.telemetry import Config, Runtime, TelemetryConfigError, bootstrap, sanitize_wire, _session, safe_attributes
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.trace import Status, StatusCode

SECRET = 'SENTINEL-secret-prompt-document-/private/file.txt'

@contextlib.contextmanager
def receiver(status=200):
    records = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            records.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(status)
            self.send_header('Location', 'http://127.0.0.1:1/' + SECRET)
            self.end_headers()
            self.wfile.write(SECRET.encode())
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {'RAG_OTEL_ENABLED': 'true', 'RAG_OTEL_ENDPOINT': f'http://127.0.0.1:{server.server_port}',
               'RAG_OTEL_TIMEOUT_MS': '100', 'RAG_OTEL_SHUTDOWN_MS': '500'}, records
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

@contextlib.contextmanager
def private_ca():
    """A throwaway CA and a 127.0.0.1 server certificate it signed, as PEM files."""
    import datetime, ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    now = datetime.datetime.now(datetime.timezone.utc)
    def certificate(subject, issuer, public_key, signing_key, ca):
        builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
                   .public_key(public_key).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(minutes=5))
                   .not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signing_key.public_key()), critical=False))
        if ca:
            builder = builder.add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        else:
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
        return builder.sign(signing_key, hashes.SHA256())
    ca_key, server_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'rag-test-private-ca')])
    ca_cert = certificate(ca_name, ca_name, ca_key.public_key(), ca_key, True)
    server_cert = certificate(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, '127.0.0.1')]),
                              ca_name, server_key.public_key(), ca_key, False)
    with tempfile.TemporaryDirectory() as directory:
        paths = {name: os.path.join(directory, name+'.pem') for name in ('ca', 'server', 'key')}
        Path(paths['ca']).write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        Path(paths['server']).write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
        Path(paths['key']).write_bytes(server_key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        yield paths

@contextlib.contextmanager
def no_file_reads():
    """Fail the test on a file read by os.open, Path.read_bytes, Path.open or builtin open."""
    with contextlib.ExitStack() as stack:
        for target in ('os.open', 'pathlib.Path.read_bytes', 'pathlib.Path.open', 'builtins.open'):
            stack.enter_context(patch(target, side_effect=AssertionError('read '+target)))
        yield

class TelemetryTests(unittest.TestCase):
    def test_disabled_never_constructs_transport_or_reads_secrets(self):
        with patch('services.telemetry._session', side_effect=AssertionError), no_file_reads():
            runtime = bootstrap({'RAG_OTEL_HEADERS_FILE': SECRET, 'RAG_OTEL_ENDPOINT': SECRET})
            self.assertFalse(runtime.config.enabled)
            self.assertEqual(runtime._providers, [])
            self.assertTrue(runtime.force_flush())
            self.assertTrue(runtime.shutdown())

    def test_ambient_sdk_disable_conflict_fails_before_side_effects(self):
        for ambient in ('true', 'TRUE', ' TrUe '):
            with self.subTest(ambient=ambient), patch.dict(os.environ, {'OTEL_SDK_DISABLED':ambient}):
                before = dict(os.environ)
                env = {'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':'http://localhost:4318',
                       'RAG_OTEL_HEADERS_FILE':SECRET, 'OTEL_SDK_DISABLED':'false'}
                with patch('services.telemetry._session', side_effect=AssertionError), no_file_reads():
                    for signals in ({}, {'RAG_OTEL_TRACES':'false'}, {'RAG_OTEL_LOGS':'false'}, {'RAG_OTEL_METRICS':'false'}):
                        with self.assertRaises(TelemetryConfigError) as error:
                            bootstrap({**env, **signals})
                        self.assertEqual(str(error.exception), 'RAG_OTEL_ENABLED conflicts with OTEL_SDK_DISABLED')
                        self.assertNotIn(SECRET, str(error.exception))
                    runtime = bootstrap({**env, 'RAG_OTEL_ENABLED':'false'})
                    self.assertFalse(runtime.config.enabled)
                    self.assertEqual(runtime._providers, [])
                    self.assertTrue(runtime.force_flush())
                    self.assertTrue(runtime.shutdown())
                self.assertEqual(dict(os.environ), before)

    def test_non_disabling_process_env_exports_all_signals_despite_mapping(self):
        for ambient in (None, 'false'):
            with self.subTest(ambient=ambient), patch.dict(os.environ):
                if ambient is None:
                    os.environ.pop('OTEL_SDK_DISABLED', None)
                else:
                    os.environ['OTEL_SDK_DISABLED'] = ambient
                before = dict(os.environ)
                with receiver() as (env, records):
                    runtime = bootstrap({**env, 'OTEL_SDK_DISABLED':'true'})
                    try:
                        with runtime.tracer.start_as_current_span('rag.query'):
                            runtime.logger.emit(body='rag.operation')
                        runtime.meter.create_counter('rag.telemetry.check').add(1)
                        self.assertTrue(runtime.force_flush())
                    finally:
                        self.assertTrue(runtime.shutdown())
                    self.assertEqual({path for path, _ in records}, {'/v1/traces', '/v1/logs', '/v1/metrics'})
                self.assertEqual(dict(os.environ), before)

    def test_supported_runtime_api_hides_raw_providers_and_retains_lifecycle(self):
        with receiver() as (env, records), patch.dict(os.environ, {'OTEL_LOGRECORD_ATTRIBUTE_COUNT_LIMIT':SECRET}):
            for enabled in ('false', 'true'):
                with self.subTest(enabled=enabled):
                    runtime = bootstrap({**env, 'RAG_OTEL_ENABLED':enabled, 'RAG_OTEL_TRACES':'false', 'RAG_OTEL_METRICS':'false'})
                    try:
                        with self.assertRaises(AttributeError):
                            getattr(runtime, 'providers')
                        for name, value in vars(runtime).items():
                            if not name.startswith('_'):
                                self.assertFalse(hasattr(value, 'get_logger'), name)
                        if enabled == 'true':
                            runtime.logger.emit(body=SECRET, attributes={'rag.outcome':'error'}, exception=RuntimeError(SECRET))
                        else:
                            self.assertIsNone(runtime.logger)
                        self.assertTrue(runtime.force_flush())
                    finally:
                        self.assertTrue(runtime.shutdown())
                        self.assertTrue(runtime.shutdown())
            self.assertEqual(len(records), 1)
            payload = records[0][1]
            self.assertNotIn(SECRET.encode(), payload)
            log = ExportLogsServiceRequest.FromString(payload).resource_logs[0].scope_logs[0].log_records[0]
            self.assertEqual({a.key:a.value.string_value for a in log.attributes}, {'rag.outcome':'error'})

    def test_invalid_configuration_is_value_free(self):
        fields = {'ENABLED': '1', 'PROTOCOL': 'grpc', 'ENDPOINT': 'http://user:secret@example.com',
                  'SERVICE_NAME': SECRET, 'SAMPLE_RATIO': 'nan', 'QUEUE_SIZE': '0', 'BATCH_SIZE': '5000',
                  'TIMEOUT_MS': '0', 'INTERVAL_MS': '0', 'SHUTDOWN_MS': '999999', 'LOGS': 'yes'}
        for key, value in fields.items():
            env = {'RAG_OTEL_ENABLED': 'true', 'RAG_OTEL_ENDPOINT': 'http://localhost:4318', 'RAG_OTEL_'+key: value}
            with self.subTest(key=key), self.assertRaises(TelemetryConfigError) as raised:
                Config.from_env(env)
            self.assertNotIn(SECRET, str(raised.exception))
        for endpoint in ('http://localhost/?secret=yes', 'http://localhost/path', 'file:///tmp/a', 'http://localhost:99999'):
            with self.assertRaises(TelemetryConfigError):
                Config.from_env({'RAG_OTEL_ENABLED': 'true', 'RAG_OTEL_ENDPOINT': endpoint})

    def test_header_file_and_repr(self):
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write('{"Authorization":"'+SECRET+'"}');f.flush()
            cfg = Config.from_env({'RAG_OTEL_ENABLED': 'true', 'RAG_OTEL_ENDPOINT': 'https://localhost:4318', 'RAG_OTEL_HEADERS_FILE': f.name})
            self.assertEqual(cfg.headers['Authorization'], SECRET)
            self.assertNotIn(SECRET, repr(cfg))
        with self.assertRaises(TelemetryConfigError) as err:
            Config.from_env({'RAG_OTEL_ENABLED': 'true', 'RAG_OTEL_ENDPOINT': 'https://localhost', 'RAG_OTEL_HEADERS_FILE': SECRET})
        self.assertNotIn(SECRET, str(err.exception))

    def test_credentials_require_https_and_unambiguous_header_identity(self):
        for header in ('Authorization', 'authorization', 'X-Api-Key', 'x-API-key'):
            with tempfile.NamedTemporaryFile(mode='w') as f:
                f.write('{"'+header+'":"'+SECRET+'"}'); f.flush()
                for origin in ('http://localhost:4318', 'http://collector.example', 'https://collector.example'):
                    env = {'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':origin, 'RAG_OTEL_HEADERS_FILE':f.name}
                    if origin.startswith('https:'):
                        cfg = Config.from_env(env)
                        self.assertEqual(list(cfg.headers), ['Authorization' if header.lower() == 'authorization' else 'X-Api-Key'])
                    else:
                        with patch('services.telemetry._session', side_effect=AssertionError), self.assertRaises(TelemetryConfigError) as error:
                            bootstrap(env)
                        self.assertNotIn(SECRET, str(error.exception))
        for names in (('Authorization', 'authorization'), ('X-Api-Key', 'x-api-key'), ('Authorization', 'Authorization')):
            with tempfile.NamedTemporaryFile(mode='w') as f:
                f.write('{"'+names[0]+'":"first","'+names[1]+'":"'+SECRET+'"}'); f.flush()
                with self.assertRaises(TelemetryConfigError):
                    Config.from_env({'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':'https://localhost', 'RAG_OTEL_HEADERS_FILE':f.name})

    def test_remote_trace_state_removed_before_queue_and_at_receiver(self):
        from opentelemetry.trace import SpanContext, TraceFlags, TraceState, NonRecordingSpan, set_span_in_context
        parent = SpanContext(123, 456, True, TraceFlags(1), TraceState([('secret', SECRET)]))
        with receiver() as (env, records):
            runtime = bootstrap({**env, 'RAG_OTEL_LOGS':'false', 'RAG_OTEL_METRICS':'false', 'RAG_OTEL_INTERVAL_MS':'60000'})
            try:
                with runtime.tracer.start_as_current_span('rag.query', context=set_span_in_context(NonRecordingSpan(parent))) as span:
                    child = span.get_span_context()
                processor = runtime._providers[0]._active_span_processor._span_processors[0]._batch_processor
                queued = processor._queue[0]
                for clean, original in ((queued.context, child), (queued.parent, parent)):
                    self.assertFalse(clean.trace_state)
                    self.assertEqual((clean.trace_id, clean.span_id, clean.trace_flags, clean.is_remote),
                                     (original.trace_id, original.span_id, original.trace_flags, original.is_remote))
                self.assertTrue(runtime.force_flush())
                payload = dict(records)['/v1/traces']
                self.assertNotIn(SECRET.encode(), payload)
                exported = ExportTraceServiceRequest.FromString(payload).resource_spans[0].scope_spans[0].spans[0]
                self.assertEqual(int.from_bytes(exported.parent_span_id, 'big'), 456)
                self.assertEqual(int.from_bytes(exported.trace_id, 'big'), 123)
            finally:
                runtime.shutdown()

    def test_metric_admission_invariants_at_real_wire_boundary(self):
        from opentelemetry.proto.metrics.v1.metrics_pb2 import Metric
        def numeric(kind='gauge', value=7):
            metric = Metric(name='rag.telemetry.check')
            data = getattr(metric, kind)
            if kind == 'sum': data.aggregation_temporality = 2
            data.data_points.add(as_double=value)
            return metric
        def histogram(**changes):
            metric = Metric(name='rag.telemetry.check')
            metric.histogram.aggregation_temporality = 2
            values = dict(count=3, sum=4, min=0, max=3, bucket_counts=[1,1,1], explicit_bounds=[1,2])
            values.update(changes)
            metric.histogram.data_points.add(**values)
            return metric
        invalid = [Metric(name='rag.telemetry.check'), numeric(value=float('nan')), numeric(value=float('inf'))]
        for kind in ('gauge', 'sum', 'histogram'):
            metric = histogram() if kind == 'histogram' else numeric(kind)
            getattr(metric, kind).data_points.add().CopyFrom(getattr(metric, kind).data_points[0])
            invalid.append(metric)
            empty = Metric(name='rag.telemetry.check'); getattr(empty, kind).SetInParent(); invalid.append(empty)
        missing = Metric(name='rag.telemetry.check'); missing.gauge.data_points.add(); invalid.append(missing)
        bad_temporality = numeric('sum'); bad_temporality.sum.aggregation_temporality = 0; invalid.append(bad_temporality)
        for changes in (dict(explicit_bounds=[2,1]), dict(explicit_bounds=[1,1]),
                        dict(explicit_bounds=[1,float('inf')]), dict(explicit_bounds=[float('nan'),2]),
                        dict(sum=float('nan')), dict(min=float('-inf')), dict(max=float('inf')),
                        dict(min=4,max=3), dict(count=4), dict(bucket_counts=[1,2]),
                        dict(explicit_bounds=list(range(32)),bucket_counts=[0]*33,count=0)):
            invalid.append(histogram(**changes))
        # Protobuf uint64 fields reject negative values before sanitizer admission.
        for changes in (dict(count=-1), dict(bucket_counts=[-1,2,2])):
            with self.assertRaises(ValueError): histogram(**changes)
        with receiver() as (env, records):
            config = Config.from_env({**env, 'RAG_OTEL_BATCH_SIZE':'1'})
            with _session('metrics', config) as session:
                for bad in invalid:
                    source = ExportMetricsServiceRequest()
                    metrics = source.resource_metrics.add().scope_metrics.add().metrics
                    metrics.add().CopyFrom(bad); metrics.add().CopyFrom(numeric())
                    response = session.post('', data=source.SerializeToString())
                    self.assertEqual(response.status_code, 200)
                    output = ExportMetricsServiceRequest.FromString(records[-1][1]).resource_metrics[0].scope_metrics[0].metrics
                    self.assertEqual(len(output), 1)
                    self.assertEqual(output[0].WhichOneof('data'), 'gauge')
                    self.assertEqual(output[0].gauge.data_points[0].as_double, 7)
                absent_statistics = histogram()
                for field in ('sum', 'min', 'max'):
                    absent_statistics.histogram.data_points[0].ClearField(field)
                for good in (numeric('sum'), histogram(), absent_statistics,
                             histogram(count=0,sum=0,min=0,max=0,bucket_counts=[0,0,0])):
                    source = ExportMetricsServiceRequest()
                    source.resource_metrics.add().scope_metrics.add().metrics.add().CopyFrom(good)
                    session.post('', data=source.SerializeToString())
                    output = ExportMetricsServiceRequest.FromString(records[-1][1]).resource_metrics[0].scope_metrics[0].metrics
                    self.assertEqual(len(output), 1)
                    self.assertEqual(output[0], good)

    def test_real_sdk_all_signals_safe_on_receiver(self):
        with receiver() as (env, records), patch.dict(os.environ, {'OTEL_RESOURCE_ATTRIBUTES': 'secret='+SECRET,
                 'OTEL_SERVICE_NAME': SECRET, 'OTEL_EXPORTER_OTLP_HEADERS': 'authorization='+SECRET}):
            runtime = bootstrap(env)
            try:
                with runtime.tracer.start_as_current_span(SECRET, attributes={'rag.operation': 'query', 'prompt': SECRET, 'rag.outcome': SECRET}) as span:
                    span.add_event(SECRET, {'secret': SECRET})
                    span.set_status(Status(StatusCode.ERROR, SECRET))
                    runtime.logger.emit(body=SECRET, severity_text=SECRET, attributes={'rag.outcome': 'error', 'secret': SECRET})
                counter = runtime.meter.create_counter('rag.telemetry.check', description=SECRET, unit=SECRET)
                for n in range(200):
                    counter.add(1, {'secret': SECRET+str(n), 'trace_id': str(n)})
                runtime.meter.create_counter('secret.metric').add(1, {'secret': SECRET})
                self.assertTrue(runtime.force_flush())
            finally:
                self.assertTrue(runtime.shutdown())
            by_path = dict(records)
            self.assertEqual(set(by_path), {'/v1/traces', '/v1/logs', '/v1/metrics'})
            for body in by_path.values():
                self.assertNotIn(SECRET.encode(), body)
            traces = ExportTraceServiceRequest.FromString(by_path['/v1/traces'])
            resource = {a.key: a.value.string_value for a in traces.resource_spans[0].resource.attributes}
            self.assertEqual(resource, {'service.name':'rag-api', 'service.version':'1.1.0', 'deployment.environment.name':'development'})
            span = traces.resource_spans[0].scope_spans[0].spans[0]
            self.assertEqual(span.name, 'rag.operation')
            self.assertFalse(span.events);self.assertFalse(span.links);self.assertFalse(span.status.message)
            logs = ExportLogsServiceRequest.FromString(by_path['/v1/logs'])
            log = logs.resource_logs[0].scope_logs[0].log_records[0]
            self.assertEqual(log.trace_id, span.trace_id)
            self.assertEqual(log.body.string_value, 'rag.operation')
            metrics = ExportMetricsServiceRequest.FromString(by_path['/v1/metrics'])
            metric = metrics.resource_metrics[0].scope_metrics[0].metrics[0]
            self.assertEqual(metric.name, 'rag.telemetry.check')
            self.assertEqual(len(metric.sum.data_points), 1)
            self.assertFalse(metric.sum.data_points[0].attributes)
            self.assertEqual(metric.sum.data_points[0].as_int, 200)

    def test_exemplar_reservoir_never_receives_dropped_attributes(self):
        from opentelemetry.sdk.metrics._internal.exemplar.exemplar_reservoir import FixedSizeExemplarReservoirABC
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        original_offer = FixedSizeExemplarReservoirABC.offer
        original_export = OTLPMetricExporter.export
        for ambient in (None, 'always_on'):
            with self.subTest(ambient=ambient), patch.dict(os.environ):
                if ambient is None:
                    os.environ.pop('OTEL_METRICS_EXEMPLAR_FILTER', None)
                else:
                    os.environ['OTEL_METRICS_EXEMPLAR_FILTER'] = ambient
                collected = []
                def capture(exporter, metrics_data, *args, **kwargs):
                    collected.append(metrics_data)
                    return original_export(exporter, metrics_data, *args, **kwargs)
                with receiver() as (env, records), patch.object(
                        FixedSizeExemplarReservoirABC, 'offer', autospec=True, side_effect=original_offer) as offer, patch.object(
                        OTLPMetricExporter, 'export', autospec=True, side_effect=capture):
                    runtime = bootstrap({**env, 'RAG_OTEL_LOGS':'false', 'RAG_OTEL_INTERVAL_MS':'60000'})
                    try:
                        counter = runtime.meter.create_counter('rag.telemetry.check')
                        with runtime.tracer.start_as_current_span('rag.query') as span:
                            self.assertTrue(span.get_span_context().trace_flags.sampled)
                            counter.add(2, {'secret':SECRET})
                        counter.add(3, {'secret':SECRET+'-outside-span'})
                        # Before collection/serialization, nothing entered a reservoir.
                        offer.assert_not_called()
                        self.assertTrue(runtime.force_flush())
                        points = [point for data in collected for resource in data.resource_metrics
                                  for scope in resource.scope_metrics for metric in scope.metrics
                                  for point in metric.data.data_points]
                        self.assertTrue(points)
                        for point in points:
                            self.assertEqual(point.value, 5)
                            self.assertFalse(point.attributes)
                            self.assertFalse(point.exemplars)
                        payload = dict(records)['/v1/metrics']
                        self.assertNotIn(SECRET.encode(), payload)
                        metric = ExportMetricsServiceRequest.FromString(payload).resource_metrics[0].scope_metrics[0].metrics[0]
                        self.assertEqual(metric.sum.data_points[0].as_int, 5)
                    finally:
                        self.assertTrue(runtime.shutdown())
                    offer.assert_not_called()

    def test_log_queue_rebuilds_wrapper_and_exception_without_mutating_input(self):
        from opentelemetry._logs import LogRecord, SeverityNumber
        from opentelemetry.sdk._logs import ReadWriteLogRecord, LogRecordLimits
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.util.instrumentation import InstrumentationScope
        from opentelemetry.context import Context
        hostile_resource = Resource({'secret':SECRET}, schema_url=SECRET)
        hostile_scope = InstrumentationScope(SECRET, version=SECRET, schema_url=SECRET, attributes={'secret':SECRET})
        hostile_limits = LogRecordLimits(max_attributes=128, max_attribute_length=1024)
        hostile_limits.private_note = SECRET
        original = LogRecord(body=SECRET, severity_text=SECRET, event_name=SECRET,
                             severity_number=SeverityNumber.ERROR, context=Context({'secret':SECRET}),
                             attributes={'secret':SECRET, 'rag.outcome':'error'})
        wrapper = ReadWriteLogRecord(log_record=original, resource=hostile_resource,
                                     instrumentation_scope=hostile_scope, limits=hostile_limits)
        before = dict(original.__dict__)
        before['attributes'] = dict(original.attributes)
        with receiver() as (env, records):
            runtime = bootstrap({**env, 'RAG_OTEL_TRACES':'false', 'RAG_OTEL_METRICS':'false', 'RAG_OTEL_INTERVAL_MS':'60000'})
            try:
                runtime.logger.emit(record=wrapper)
                try:
                    raise RuntimeError(SECRET)
                except RuntimeError as error:
                    exception_record = LogRecord(body=SECRET, exception=error, attributes={'rag.outcome':'error'})
                    runtime.logger.emit(record=exception_record)
                    self.assertIs(exception_record.exception, error)
                    self.assertEqual(dict(exception_record.attributes), {'rag.outcome':'error'})
                processor = runtime._providers[0]._multi_log_record_processor._log_record_processors[0]._batch_processor
                self.assertEqual(len(processor._queue), 2)
                for queued in processor._queue:
                    self.assertEqual(dict(queued.resource.attributes), {'service.name':'rag-api', 'service.version':'1.1.0', 'deployment.environment.name':'development'})
                    self.assertFalse(queued.resource.schema_url)
                    self.assertEqual(queued.instrumentation_scope.name, 'rag.telemetry')
                    self.assertFalse(queued.instrumentation_scope.version)
                    self.assertFalse(queued.instrumentation_scope.schema_url)
                    self.assertFalse(queued.instrumentation_scope.attributes)
                    self.assertIsNot(queued.limits, hostile_limits)
                    self.assertNotIn(SECRET, repr(vars(queued.limits)))
                    self.assertFalse(queued.log_record.context)
                    self.assertIsNone(queued.log_record.exception)
                    self.assertEqual(queued.log_record.body, 'rag.operation')
                    self.assertFalse(queued.log_record.severity_text)
                    self.assertFalse(queued.log_record.event_name)
                    self.assertEqual(dict(queued.log_record.attributes), {'rag.outcome':'error'})
                self.assertEqual(original.__dict__, before)
                self.assertIs(wrapper.resource, hostile_resource)
                self.assertIs(wrapper.instrumentation_scope, hostile_scope)
                self.assertIs(wrapper.limits, hostile_limits)
                self.assertEqual(hostile_limits.private_note, SECRET)
                self.assertTrue(runtime.force_flush())
                payload = dict(records)['/v1/logs']
                self.assertNotIn(SECRET.encode(), payload)
                logs = ExportLogsServiceRequest.FromString(payload).resource_logs[0].scope_logs[0].log_records
                self.assertEqual(len(logs), 2)
                self.assertIn(SeverityNumber.ERROR.value, [log.severity_number for log in logs])
            finally:
                self.assertTrue(runtime.shutdown())

    def test_emit_forms_preserve_caller_inputs_with_and_without_exceptions(self):
        from types import MappingProxyType
        from opentelemetry._logs import LogRecord, SeverityNumber
        from opentelemetry.sdk._logs import ReadWriteLogRecord
        from opentelemetry.attributes import BoundedAttributes
        from opentelemetry.context import Context
        from opentelemetry.trace import SpanContext, NonRecordingSpan, TraceFlags, set_span_in_context
        context = set_span_in_context(NonRecordingSpan(SpanContext(123, 456, False, TraceFlags(1))),
                                      Context({'secret':SECRET}))
        factories = (dict, MappingProxyType,
                     lambda values: BoundedAttributes(maxlen=8, attributes=values, immutable=False))
        with receiver() as (env, records):
            runtime = bootstrap({**env, 'RAG_OTEL_TRACES':'false', 'RAG_OTEL_METRICS':'false', 'RAG_OTEL_INTERVAL_MS':'60000'})
            try:
                processor = runtime._providers[0]._multi_log_record_processor._log_record_processors[0]._batch_processor
                for form in ('wrapper', 'plain', 'keyword'):
                    for has_exception in (False, True):
                        for factory in factories:
                            with self.subTest(form=form, exception=has_exception, mapping=factory):
                                attributes = factory({'rag.outcome':'error', 'secret':SECRET})
                                values = dict(attributes)
                                exception = RuntimeError(SECRET) if has_exception else None
                                fields = dict(timestamp=123, observed_timestamp=456, context=context,
                                              severity_number=SeverityNumber.ERROR, severity_text=SECRET,
                                              body=SECRET, attributes=attributes, event_name=SECRET, exception=exception)
                                source = LogRecord(**fields)
                                wrapper = ReadWriteLogRecord(source) if form == 'wrapper' else None
                                # Public writable records can carry any supported attribute mapping.
                                source.attributes = attributes
                                before = dict(source.__dict__)
                                wrapper_before = dict(wrapper.__dict__) if wrapper else None
                                if form == 'keyword':
                                    runtime.logger.emit(**fields)
                                elif form == 'wrapper':
                                    runtime.logger.emit(record=wrapper)
                                else:
                                    runtime.logger.emit(source)
                                self.assertEqual(source.__dict__, before)
                                self.assertIs(source.attributes, attributes)
                                self.assertEqual(dict(attributes), values)
                                self.assertIs(source.context, context)
                                self.assertIs(source.exception, exception)
                                if wrapper:
                                    self.assertEqual(wrapper.__dict__, wrapper_before)
                                    self.assertIs(wrapper.log_record, source)
                                queued = processor._queue[0].log_record
                                self.assertEqual(dict(queued.attributes), {'rag.outcome':'error'})
                                self.assertEqual((queued.trace_id, queued.span_id), (123,456))
                                self.assertEqual((queued.timestamp, queued.observed_timestamp), (123,456))
                                self.assertEqual(queued.severity_number, SeverityNumber.ERROR)
                                self.assertEqual(queued.body, 'rag.operation')
                                self.assertIsNone(queued.exception)
                                self.assertFalse(queued.context)
                                self.assertFalse(queued.event_name)
                                self.assertFalse(queued.severity_text)
                self.assertEqual(len(processor._queue), 18)
                self.assertTrue(runtime.force_flush())
                payload = dict(records)['/v1/logs']
                self.assertNotIn(SECRET.encode(), payload)
                logs = ExportLogsServiceRequest.FromString(payload).resource_logs[0].scope_logs[0].log_records
                self.assertEqual(len(logs), 18)
            finally:
                self.assertTrue(runtime.shutdown())

    def test_attribute_snapshot_precedes_sdk_delegation_for_all_emit_forms(self):
        from types import MappingProxyType
        from opentelemetry._logs import LogRecord
        from opentelemetry.sdk._logs import ReadWriteLogRecord
        from opentelemetry.attributes import BoundedAttributes
        factories = (lambda values: (values, values),
                     lambda values: (MappingProxyType(values), values),
                     lambda values: (BoundedAttributes(maxlen=8, attributes=values, immutable=False,
                                                       extended_attributes=True), None))
        with receiver() as (env, records):
            runtime = bootstrap({**env, 'RAG_OTEL_TRACES':'false', 'RAG_OTEL_METRICS':'false', 'RAG_OTEL_INTERVAL_MS':'60000'})
            try:
                processor = runtime._providers[0]._multi_log_record_processor._log_record_processors[0]._batch_processor
                for form in ('wrapper', 'plain', 'keyword'):
                    for has_exception in (False, True):
                        for index, factory in enumerate(factories):
                            with self.subTest(form=form, exception=has_exception, mapping=index):
                                attributes, backing = factory({'rag.outcome':'error', 'secret':{'nested':[SECRET]},
                                                               'rag.operation':['query']})
                                if backing is None:
                                    backing = attributes
                                exception = RuntimeError(SECRET) if has_exception else None
                                source = LogRecord(body=SECRET, attributes=attributes, exception=exception)
                                wrapper = ReadWriteLogRecord(source) if form == 'wrapper' else None
                                source.attributes = attributes
                                real_emit = runtime.logger._logger.emit
                                def at_sdk_boundary(record=None, **kwargs):
                                    delegated = (record.log_record if isinstance(record, ReadWriteLogRecord) else record)
                                    snapshot = delegated.attributes if delegated is not None else kwargs['attributes']
                                    self.assertIsNot(snapshot, attributes)
                                    self.assertEqual(snapshot, {'rag.outcome':'error'})
                                    # Simulate a caller write after delegation begins, before SDK normalization.
                                    backing['rag.outcome'] = 'ok'
                                    backing['secret'] = {'nested':[SECRET+'-changed']}
                                    self.assertEqual(snapshot, {'rag.outcome':'error'})
                                    return real_emit(record, **kwargs)
                                with patch.object(runtime.logger._logger, 'emit', side_effect=at_sdk_boundary):
                                    if form == 'keyword':
                                        runtime.logger.emit(body=SECRET, attributes=attributes, exception=exception)
                                    else:
                                        runtime.logger.emit(wrapper if wrapper else source)
                                self.assertIs(source.attributes, attributes)
                                self.assertIs(source.exception, exception)
                                self.assertEqual(attributes['rag.outcome'], 'ok')
                                self.assertNotIn('exception.message', attributes)
                                queued = processor._queue[0].log_record
                                self.assertEqual(dict(queued.attributes), {'rag.outcome':'error'})
                                self.assertIsNone(queued.exception)
                self.assertEqual(len(processor._queue), 18)
                self.assertTrue(runtime.force_flush())
                payload = dict(records)['/v1/logs']
                self.assertNotIn(SECRET.encode(), payload)
                logs = ExportLogsServiceRequest.FromString(payload).resource_logs[0].scope_logs[0].log_records
                self.assertEqual(len(logs), 18)
            finally:
                self.assertTrue(runtime.shutdown())

    def test_all_log_forms_ignore_ambient_attribute_limits(self):
        from opentelemetry._logs import LogRecord
        from opentelemetry.sdk._logs import ReadWriteLogRecord, LogRecordLimits
        limits = LogRecordLimits(max_attributes=16, max_attribute_length=128,
                                 max_log_record_attributes=16, max_log_record_attribute_length=128)
        approved = {'rag.operation':'query', 'rag.outcome':'error', 'error.type':'internal'}
        fields = ('OTEL_LOGRECORD_ATTRIBUTE_COUNT_LIMIT', 'OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT',
                  'OTEL_ATTRIBUTE_COUNT_LIMIT', 'OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT')
        with receiver() as (env, records):
            for field in fields:
                for value in (SECRET, '-1', '0', '1'):
                    with self.subTest(field=field, value=value), patch.dict(os.environ, {field:value}):
                        runtime = bootstrap({**env, 'RAG_OTEL_TRACES':'false', 'RAG_OTEL_METRICS':'false', 'RAG_OTEL_INTERVAL_MS':'60000'})
                        try:
                            processor = runtime._providers[0]._multi_log_record_processor._log_record_processors[0]._batch_processor
                            for form in ('wrapper', 'plain', 'keyword'):
                                for has_exception in (False, True):
                                    attributes = dict(approved)
                                    exception = RuntimeError(SECRET) if has_exception else None
                                    source = LogRecord(body=SECRET, attributes=attributes, exception=exception)
                                    wrapper = ReadWriteLogRecord(source, limits=limits) if form == 'wrapper' else None
                                    source.attributes = attributes
                                    if form == 'keyword':
                                        runtime.logger.emit(body=SECRET, attributes=attributes, exception=exception)
                                    else:
                                        runtime.logger.emit(wrapper if wrapper else source)
                                    self.assertIs(source.attributes, attributes)
                                    self.assertEqual(attributes, approved)
                                    queued = processor._queue[0].log_record
                                    self.assertEqual(dict(queued.attributes), approved)
                                    self.assertIsNone(queued.exception)
                            self.assertEqual(len(processor._queue), 6)
                            self.assertTrue(runtime.force_flush())
                            payload = dict(records)['/v1/logs']
                            self.assertNotIn(SECRET.encode(), payload)
                            logs = ExportLogsServiceRequest.FromString(payload).resource_logs[0].scope_logs[0].log_records
                            self.assertEqual(len(logs), 6)
                            for log in logs:
                                self.assertEqual({a.key:a.value.string_value for a in log.attributes}, approved)
                            self.assertEqual(os.environ[field], value)
                        finally:
                            self.assertTrue(runtime.shutdown())

    def test_nonmapping_attributes_are_empty_for_all_log_forms(self):
        from opentelemetry._logs import LogRecord
        from opentelemetry.sdk._logs import ReadWriteLogRecord
        class HostileNonMapping:
            def __bool__(self): raise AssertionError('truthiness must not be inspected')
            def items(self): raise AssertionError('nonmapping items must not be called')
        malformed = (None, False, True, 0, 1, '', SECRET, [], [SECRET], (), (SECRET,), set(), {SECRET}, HostileNonMapping())
        with receiver() as (env, records):
            runtime = bootstrap({**env, 'RAG_OTEL_TRACES':'false', 'RAG_OTEL_METRICS':'false',
                                 'RAG_OTEL_INTERVAL_MS':'60000', 'RAG_OTEL_BATCH_SIZE':'128'})
            try:
                processor = runtime._providers[0]._multi_log_record_processor._log_record_processors[0]._batch_processor
                for form in ('wrapper', 'plain', 'keyword'):
                    for has_exception in (False, True):
                        for attributes in malformed:
                            with self.subTest(form=form, exception=has_exception, type=type(attributes)):
                                self.assertEqual(safe_attributes(attributes), {})
                                exception = RuntimeError(SECRET) if has_exception else None
                                source = LogRecord(body=SECRET, exception=exception)
                                wrapper = ReadWriteLogRecord(source) if form == 'wrapper' else None
                                source.attributes = attributes
                                if form == 'keyword':
                                    runtime.logger.emit(body=SECRET, attributes=attributes, exception=exception)
                                else:
                                    runtime.logger.emit(wrapper if wrapper else source)
                                self.assertIs(source.attributes, attributes)
                                self.assertIs(source.exception, exception)
                                self.assertFalse(processor._queue[0].log_record.attributes)
                self.assertEqual(len(processor._queue), len(malformed)*6)
                self.assertTrue(runtime.force_flush())
                self.assertNotIn(SECRET.encode(), dict(records)['/v1/logs'])
            finally:
                self.assertTrue(runtime.shutdown())

    def test_adapter_preserves_body_for_processor_schema_extensions(self):
        from opentelemetry._logs import LogRecord
        from opentelemetry.sdk._logs import ReadWriteLogRecord
        runtime = bootstrap({'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':'http://localhost:1',
                             'RAG_OTEL_TRACES':'false', 'RAG_OTEL_METRICS':'false'})
        try:
            body = 'rag.startup.completed'
            for record in (LogRecord(body=body), ReadWriteLogRecord(LogRecord(body=body)), None):
                with patch.object(runtime.logger._logger, 'emit') as emit:
                    runtime.logger.emit(record, body=body)
                    delegated = emit.call_args.args[0]
                    self.assertIsInstance(delegated, ReadWriteLogRecord)
                    self.assertEqual(delegated.log_record.body, body)
        finally:
            self.assertTrue(runtime.shutdown())

    def test_caller_selected_span_scope_is_rebuilt_before_queue(self):
        with receiver() as (env, records):
            runtime = bootstrap({**env, 'RAG_OTEL_LOGS':'false', 'RAG_OTEL_METRICS':'false', 'RAG_OTEL_INTERVAL_MS':'60000'})
            try:
                tracer = runtime._providers[0].get_tracer(SECRET, instrumenting_library_version=SECRET,
                                                       schema_url=SECRET, attributes={'secret':SECRET})
                with tracer.start_as_current_span('rag.query') as original:
                    pass
                self.assertEqual(original.instrumentation_scope.name, SECRET)
                processor = runtime._providers[0]._active_span_processor._span_processors[0]._batch_processor
                queued = processor._queue[0]
                self.assertEqual(queued.instrumentation_scope.name, 'rag.telemetry')
                self.assertFalse(queued.instrumentation_scope.version)
                self.assertFalse(queued.instrumentation_scope.schema_url)
                self.assertFalse(queued.instrumentation_scope.attributes)
                self.assertEqual(queued.context.trace_id, original.context.trace_id)
                self.assertEqual(queued.context.span_id, original.context.span_id)
                self.assertEqual(dict(original.instrumentation_scope.attributes), {'secret':SECRET})
                self.assertTrue(runtime.force_flush())
                self.assertNotIn(SECRET.encode(), dict(records)['/v1/traces'])
            finally:
                self.assertTrue(runtime.shutdown())

    def test_wire_rebuilds_scope_resource_and_free_text(self):
        source = ExportTraceServiceRequest()
        rs = source.resource_spans.add(schema_url=SECRET)
        rs.resource.attributes.add(key='secret').value.string_value = SECRET
        ss = rs.scope_spans.add(schema_url=SECRET);ss.scope.name=SECRET;ss.scope.version=SECRET
        ss.scope.attributes.add(key='secret').value.string_value=SECRET
        span=ss.spans.add(name=SECRET, trace_state=SECRET)
        span.status.message=SECRET
        span.events.add(name=SECRET)
        span.links.add(trace_state=SECRET)
        for key in ('rag.operation','authorization','filename','prompt','exception.stacktrace'):
            span.attributes.add(key=key).value.string_value=SECRET
        body=sanitize_wire(source.SerializeToString(), 'traces', Config())
        self.assertNotIn(SECRET.encode(),body)

    def test_histogram_large_shape_is_dropped_not_truncated(self):
        source=ExportMetricsServiceRequest()
        metric=source.resource_metrics.add().scope_metrics.add().metrics.add(name='rag.telemetry.check')
        point=metric.histogram.data_points.add(count=40, sum=40)
        point.bucket_counts.extend([1]*40);point.explicit_bounds.extend(range(39))
        result=ExportMetricsServiceRequest.FromString(sanitize_wire(source.SerializeToString(),'metrics',Config()))
        self.assertFalse(result.resource_metrics[0].scope_metrics[0].metrics)

    def test_bounded_queue_under_blocked_export(self):
        from opentelemetry.sdk.trace.export import SpanExportResult
        entered=threading.Event();release=threading.Event()
        def blocked(*args):
            entered.set();release.wait(2);return SpanExportResult.FAILURE
        with patch('opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter.export',side_effect=blocked):
            runtime=bootstrap({'RAG_OTEL_ENABLED':'true','RAG_OTEL_ENDPOINT':'http://localhost:1',
                               'RAG_OTEL_LOGS':'false','RAG_OTEL_METRICS':'false','RAG_OTEL_BATCH_SIZE':'1','RAG_OTEL_QUEUE_SIZE':'4'})
            try:
                with runtime.tracer.start_as_current_span('rag.query'):pass
                self.assertTrue(entered.wait(1))
                for _ in range(20):
                    with runtime.tracer.start_as_current_span(SECRET):pass
                processor=runtime._providers[0]._active_span_processor._span_processors[0]._batch_processor
                self.assertEqual(len(processor._queue),4)
                self.assertTrue(all(s.name=='rag.operation' for s in processor._queue))
            finally:release.set();runtime.shutdown()

    def test_outage_no_retry_or_sensitive_diagnostics(self):
        for status in (500, 429, 302):
            with receiver(status) as (env, records):
                stream=io.StringIO();handler=logging.StreamHandler(stream)
                logger=logging.getLogger('opentelemetry');logger.addHandler(handler)
                try:
                    runtime=bootstrap({**env, 'RAG_OTEL_LOGS':'false','RAG_OTEL_METRICS':'false'})
                    started=time.monotonic()
                    with runtime.tracer.start_as_current_span('rag.query'):pass
                    runtime.force_flush();runtime.shutdown()
                    self.assertLess(time.monotonic()-started,1.5)
                    self.assertEqual(len(records),1)
                    self.assertNotIn(SECRET,stream.getvalue())
                    self.assertNotIn(env['RAG_OTEL_ENDPOINT'],stream.getvalue())
                finally:logger.removeHandler(handler)

    def test_sampling_zero_and_signal_toggles(self):
        with receiver() as (env, records):
            runtime=bootstrap({**env,'RAG_OTEL_SAMPLE_RATIO':'0','RAG_OTEL_LOGS':'false','RAG_OTEL_METRICS':'false'})
            with runtime.tracer.start_as_current_span('rag.query'):pass
            self.assertIsNone(runtime.logger);self.assertIsNone(runtime.meter)
            runtime.force_flush();runtime.shutdown();runtime.shutdown()
            self.assertFalse(records)

    def test_api_lifespan_cleans_up_on_failed_startup_and_normal_exit(self):
        import ast
        import asyncio
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        api=Path(os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2]/'api'))
        tree=ast.parse((api/'main.py').read_text())
        function=next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name=='lifespan')
        namespace={'asynccontextmanager':asynccontextmanager,'FastAPI':object}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'main.py','exec'),namespace)
        for fail in (False, True):
            order=[]
            runtime=SimpleNamespace(shutdown=lambda:order.append('shutdown'))
            gold=SimpleNamespace(load_sessions_from_disk=Mock(side_effect=RuntimeError('startup') if fail else lambda:order.append('load')),
                                 reconcile_interrupted_generations=Mock())
            wc=SimpleNamespace(sweep_staging=AsyncMock(return_value=[]),close_client=lambda:order.append('close'))
            def job_module(kind):
                return SimpleNamespace(_jobs=SimpleNamespace(load=lambda:(order.append(kind) or [])))
            importer=job_module('import')
            importer.sweep_interrupted_imports=lambda:[]
            importer.sweep_stale_workdirs=lambda:[]
            service=SimpleNamespace(goldstandard=gold,metrics=SimpleNamespace(load_from_disk=Mock()),
                                    packager=SimpleNamespace(sweep_build_leftovers=lambda:[]),
                                    weaviate_client=wc,importer=importer,exporter=job_module('export'),
                                    ingest_pipeline=job_module('ingest'),tuning=job_module('tune'))
            modules={'services':service,'services.telemetry':SimpleNamespace(bootstrap=lambda:(order.append('bootstrap') or runtime))}
            async def exercise():
                async with namespace['lifespan'](SimpleNamespace(state=SimpleNamespace())):
                    order.append('yield')
            with patch.dict(sys.modules,modules):
                if fail:
                    with self.assertRaises(RuntimeError):asyncio.run(exercise())
                else:asyncio.run(exercise())
            self.assertEqual(order[0],'bootstrap')
            self.assertEqual(order[-2:],['close','shutdown'])
            self.assertEqual('yield' in order,not fail)
            if not fail:
                self.assertEqual(order, ['bootstrap', 'load', 'ingest', 'export', 'import', 'tune',
                                         'yield', 'close', 'shutdown'])

    def test_lifecycle_deadline_single_worker_and_eventual_shutdown(self):
        release=threading.Event()
        class Stalled:
            closed=0
            def force_flush(self, **kwargs):release.wait();return True
            def shutdown(self):self.closed+=1
        runtime=Runtime(Config(enabled=True,shutdown_ms=100));provider=Stalled();runtime._providers=[provider]
        try:
            started=time.monotonic()
            self.assertFalse(runtime.force_flush())
            worker=runtime._worker
            self.assertFalse(runtime.shutdown())
            self.assertIs(worker,runtime._worker)
            self.assertLess(time.monotonic()-started,.5)
        finally:release.set();worker.join(1)
        self.assertEqual(provider.closed,1)
        self.assertTrue(runtime.shutdown())
        self.assertEqual(provider.closed,1)

    def test_reviewer_configuration_bounds_accept_edges_and_reject_neighbours(self):
        base = {'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':'http://localhost:4318/'}
        cfg = Config.from_env(base)
        self.assertEqual(cfg.endpoint, 'http://localhost:4318')
        self.assertEqual((cfg.queue_size, cfg.batch_size, cfg.timeout_ms, cfg.interval_ms, cfg.shutdown_ms, cfg.sample_ratio),
                         (256, 64, 1000, 5000, 3000, 1.0))
        bounds = {'QUEUE_SIZE':(1, 4096), 'BATCH_SIZE':(1, 512), 'TIMEOUT_MS':(100, 10000),
                  'INTERVAL_MS':(1000, 60000), 'SHUTDOWN_MS':(100, 30000)}
        for key, (low, high) in bounds.items():
            extra = {'RAG_OTEL_QUEUE_SIZE':'4096'} if key == 'BATCH_SIZE' else {'RAG_OTEL_BATCH_SIZE':'1'} if key == 'QUEUE_SIZE' else {}
            for value in (low, high):
                with self.subTest(key=key, value=value):
                    Config.from_env({**base, **extra, 'RAG_OTEL_'+key:str(value)})
            for value in (str(low-1), str(high+1), '1.5', ''):
                with self.subTest(key=key, value=value), self.assertRaises(TelemetryConfigError) as raised:
                    Config.from_env({**base, **extra, 'RAG_OTEL_'+key:value})
                self.assertEqual(str(raised.exception), 'Invalid RAG_OTEL_'+key)
        for value in ('0', '1', '0.5'):
            Config.from_env({**base, 'RAG_OTEL_SAMPLE_RATIO':value})
        for value in ('-0.01', '1.01', 'inf', '-inf'):
            with self.subTest(sample=value), self.assertRaises(TelemetryConfigError):
                Config.from_env({**base, 'RAG_OTEL_SAMPLE_RATIO':value})
        with self.assertRaises(TelemetryConfigError) as raised:
            Config.from_env({**base, 'RAG_OTEL_QUEUE_SIZE':'4', 'RAG_OTEL_BATCH_SIZE':'5'})
        self.assertEqual(str(raised.exception), 'RAG_OTEL_BATCH_SIZE exceeds RAG_OTEL_QUEUE_SIZE')
        Config.from_env({**base, 'RAG_OTEL_QUEUE_SIZE':'4', 'RAG_OTEL_BATCH_SIZE':'4'})
        for key in ('SERVICE_NAME', 'SERVICE_VERSION', 'ENVIRONMENT'):
            self.assertEqual(getattr(Config.from_env({**base, 'RAG_OTEL_'+key:'a'*64}),
                                     {'SERVICE_NAME':'service','SERVICE_VERSION':'version','ENVIRONMENT':'environment'}[key]), 'a'*64)
            for value in ('a'*65, '-lead', 'has space', ''):
                with self.subTest(key=key, value=value), self.assertRaises(TelemetryConfigError):
                    Config.from_env({**base, 'RAG_OTEL_'+key:value})
        for endpoint in ('http://localhost:0', 'http://:4318', 'http://local host', 'http://localhost#'+SECRET, ''):
            with self.subTest(endpoint=endpoint), self.assertRaises(TelemetryConfigError) as raised:
                Config.from_env({**base, 'RAG_OTEL_ENDPOINT':endpoint})
            self.assertNotIn(SECRET, str(raised.exception))
        for key in ('ENABLED', 'TRACES', 'LOGS', 'METRICS'):
            for value in ('True', '1', ''):
                with self.subTest(key=key, value=value), self.assertRaises(TelemetryConfigError):
                    Config.from_env({**base, 'RAG_OTEL_'+key:value})

    def test_reviewer_header_file_limits_are_value_free(self):
        cases = ('{"Authorization":"'+'a'*8200+'"}', '{"Authorization":""}', '{"Authorization":"'+'a'*2049+'"}',
                 '{"Authorization":"x\\n'+SECRET+'"}', '{"Authorization":1}', '{"Cookie":"'+SECRET+'"}',
                 '["Authorization","'+SECRET+'"]', 'not json '+SECRET)
        for text in cases:
            with self.subTest(size=len(text)), tempfile.NamedTemporaryFile(mode='w') as f:
                f.write(text); f.flush()
                with self.assertRaises(TelemetryConfigError) as raised:
                    Config.from_env({'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':'https://localhost', 'RAG_OTEL_HEADERS_FILE':f.name})
                self.assertEqual(str(raised.exception), 'Invalid RAG_OTEL_HEADERS_FILE')
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write('{"Authorization":"'+'a'*2048+'","X-Api-Key":"k"}'); f.flush()
            cfg = Config.from_env({'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':'https://localhost', 'RAG_OTEL_HEADERS_FILE':f.name})
            self.assertEqual(set(cfg.headers), {'Authorization', 'X-Api-Key'})

    def test_reviewer_unreachable_or_hanging_collector_is_bounded(self):
        import socket
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); closed_port = probe.getsockname()[1]
        release = threading.Event(); hits = []
        class Hang(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path); release.wait(5)
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Hang)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            for port in (closed_port, server.server_port):
                with self.subTest(port='closed' if port == closed_port else 'hanging'):
                    stream = io.StringIO(); handler = logging.StreamHandler(stream)
                    logger = logging.getLogger('opentelemetry'); logger.addHandler(handler)
                    try:
                        endpoint = f'http://127.0.0.1:{port}'
                        runtime = bootstrap({'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':endpoint,
                                             'RAG_OTEL_TIMEOUT_MS':'100', 'RAG_OTEL_SHUTDOWN_MS':'500'})
                        started = time.monotonic()
                        for _ in range(50):
                            with runtime.tracer.start_as_current_span('rag.query'):
                                runtime.logger.emit(body=SECRET, attributes={'rag.outcome':'error'})
                            runtime.meter.create_counter('rag.telemetry.check').add(1)
                        runtime.force_flush(); runtime.shutdown()
                        self.assertLess(time.monotonic()-started, 2.5)
                        self.assertNotIn(SECRET, stream.getvalue())
                        self.assertNotIn(endpoint, stream.getvalue())
                    finally:
                        logger.removeHandler(handler)
            self.assertTrue(hits)
        finally:
            release.set(); server.shutdown(); server.server_close(); thread.join()

    def test_reviewer_bootstrap_leaves_global_providers_and_root_logging_unchanged(self):
        from opentelemetry import trace, metrics, _logs
        root = logging.getLogger()
        before = (list(root.handlers), root.level, type(trace.get_tracer_provider()),
                  type(metrics.get_meter_provider()), type(_logs.get_logger_provider()))
        with receiver() as (env, records):
            runtime = bootstrap(env)
            try:
                with runtime.tracer.start_as_current_span('rag.query'):
                    logging.getLogger('rag.reviewer').warning(SECRET)
                self.assertTrue(runtime.force_flush())
            finally:
                self.assertTrue(runtime.shutdown())
            after = (list(root.handlers), root.level, type(trace.get_tracer_provider()),
                     type(metrics.get_meter_provider()), type(_logs.get_logger_provider()))
            self.assertEqual(after, before)
            for _, body in records:
                self.assertNotIn(SECRET.encode(), body)
            self.assertNotIn('/v1/logs', {path for path, _ in records})

    def test_ca_file_disabled_invalid_and_https_rule(self):
        with patch('services.telemetry._session', side_effect=AssertionError), no_file_reads(), \
                patch('ssl.create_default_context', side_effect=AssertionError):
            runtime = bootstrap({'RAG_OTEL_CA_FILE': SECRET, 'RAG_OTEL_ENDPOINT': SECRET})
            self.assertFalse(runtime.config.enabled)
            self.assertEqual(runtime._providers, [])
        base = {'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':'https://localhost:4318'}
        with tempfile.TemporaryDirectory() as directory:
            def write(name, data):
                path = os.path.join(directory, SECRET.replace('/', '_')+name)
                Path(path).write_bytes(data)
                return path
            invalid = {'missing': os.path.join(directory, SECRET.replace('/', '_')+'-missing'),
                       'directory': directory, 'empty': write('-empty', b''),
                       'not a certificate': write('-text', SECRET.encode()+b'\n'),
                       'over 1 MiB': write('-large', b'\n'*(1048576+1))}
            for case, path in invalid.items():
                with self.subTest(case=case), self.assertRaises(TelemetryConfigError) as raised:
                    Config.from_env({**base, 'RAG_OTEL_CA_FILE':path})
                self.assertEqual(str(raised.exception), 'Invalid RAG_OTEL_CA_FILE')
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertNotIn(path, str(raised.exception))
            with private_ca() as paths:
                pem = Path(paths['ca']).read_bytes()
                largest = write('-largest', pem + b'\n'*(1048576-len(pem)))
                self.assertEqual(os.path.getsize(largest), 1048576)
                for path in (paths['ca'], largest):
                    cfg = Config.from_env({**base, 'RAG_OTEL_CA_FILE':path})
                    self.assertEqual(cfg.ca_file, path)
                    self.assertNotIn(path, repr(cfg))
                    with self.assertRaises(TelemetryConfigError) as raised:
                        Config.from_env({**base, 'RAG_OTEL_ENDPOINT':'http://localhost:4318', 'RAG_OTEL_CA_FILE':path})
                    self.assertEqual(str(raised.exception), 'RAG_OTEL_CA_FILE requires HTTPS RAG_OTEL_ENDPOINT')
        self.assertEqual(Config.from_env(base).ca_file, '')

    def test_ca_file_trusts_private_ca_for_https_export(self):
        import ssl
        records = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                records.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        with private_ca() as paths:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(paths['server'], paths['key'])
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                env = {'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':f'https://127.0.0.1:{server.server_port}',
                       'RAG_OTEL_LOGS':'false', 'RAG_OTEL_METRICS':'false'}
                for ca_file in (None, paths['ca']):
                    with self.subTest(ca_file='private CA' if ca_file else 'bundled roots'):
                        records.clear()
                        runtime = bootstrap({**env, 'RAG_OTEL_CA_FILE':ca_file} if ca_file else env)
                        try:
                            with runtime.tracer.start_as_current_span('rag.query'):
                                pass
                            runtime.force_flush()
                        finally:
                            runtime.shutdown()
                        self.assertEqual([path for path, _ in records], ['/v1/traces'] if ca_file else [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_reviewer_ca_file_is_the_only_trust_store(self):
        import ssl
        import requests
        records = []; verify = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                records.append(self.path); self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200); self.end_headers()
            def log_message(self, *args):
                pass
        post = requests.Session.post
        def spy(session, url, *args, **kwargs):
            verify.append(kwargs.get('verify'))
            return post(session, url, *args, **kwargs)
        base = {'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':'https://localhost:4318'}
        with tempfile.TemporaryDirectory() as directory:
            unreadable = os.path.join(directory, 'unreadable-'+SECRET.replace('/', '_'))
            Path(unreadable).write_bytes(b'')
            with patch('os.open', side_effect=PermissionError) as opened, self.assertRaises(TelemetryConfigError) as raised:
                Config.from_env({**base, 'RAG_OTEL_CA_FILE':unreadable})
            opened.assert_called()
        self.assertEqual(str(raised.exception), 'Invalid RAG_OTEL_CA_FILE')
        with private_ca() as paths, private_ca() as other, tempfile.TemporaryDirectory() as directory:
            both = os.path.join(directory, 'both.pem')
            Path(both).write_bytes(Path(other['ca']).read_bytes() + Path(paths['ca']).read_bytes())
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(paths['server'], paths['key'])
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                env = {'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':f'https://127.0.0.1:{server.server_port}',
                       'RAG_OTEL_LOGS':'false', 'RAG_OTEL_METRICS':'false'}
                for name, ca_file, delivered in (('unset', None, False), ('other CA only', other['ca'], False),
                                                 ('bundle with both CAs', both, True)):
                    with self.subTest(case=name), patch.object(requests.Session, 'post', spy):
                        records.clear(); verify.clear()
                        runtime = bootstrap({**env, 'RAG_OTEL_CA_FILE':ca_file} if ca_file else env)
                        try:
                            with runtime.tracer.start_as_current_span('rag.query'):
                                pass
                            runtime.force_flush()
                        finally:
                            runtime.shutdown()
                        self.assertEqual(records, ['/v1/traces'] if delivered else [])
                        self.assertTrue(verify)
                        self.assertEqual(set(verify), {ca_file or True})
            finally:
                server.shutdown(); server.server_close(); thread.join()

    def test_non_regular_files_are_refused_without_blocking(self):
        base = {'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':'https://localhost:4318'}
        with tempfile.TemporaryDirectory() as directory:
            fifo = os.path.join(directory, SECRET.replace('/', '_')+'-fifo')
            os.mkfifo(fifo)
            for key, path in (('RAG_OTEL_HEADERS_FILE', fifo), ('RAG_OTEL_HEADERS_FILE', directory),
                              ('RAG_OTEL_CA_FILE', fifo)):
                with self.subTest(key=key, kind='fifo' if path == fifo else 'directory'):
                    outcome = []
                    def load():
                        try:
                            Config.from_env({**base, key:path})
                        except Exception as error:
                            outcome.append(error)
                    worker = threading.Thread(target=load, daemon=True); worker.start()
                    worker.join(5)
                    if worker.is_alive():
                        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                        worker.join(5)
                        self.fail(key+' blocked on a non-regular file')
                    self.assertEqual(len(outcome), 1)
                    self.assertIsInstance(outcome[0], TelemetryConfigError)
                    self.assertEqual(str(outcome[0]), 'Invalid '+key)
                    self.assertNotIn(SECRET, str(outcome[0]))
                    self.assertNotIn(path, str(outcome[0]))

    def test_reviewer_non_regular_paths_are_never_opened(self):
        base = {'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':'https://localhost:4318'}
        with tempfile.TemporaryDirectory() as directory:
            fifo = os.path.join(directory, SECRET.replace('/', '_')+'-fifo')
            os.mkfifo(fifo)
            for key in ('RAG_OTEL_HEADERS_FILE', 'RAG_OTEL_CA_FILE'):
                for kind, path in (('character device', os.devnull), ('fifo', fifo), ('directory', directory)):
                    with self.subTest(key=key, kind=kind):
                        with patch('os.open', side_effect=AssertionError('opened')) as opened, \
                                patch('ssl.create_default_context', side_effect=AssertionError('loaded')), \
                                self.assertRaises(TelemetryConfigError) as raised:
                            Config.from_env({**base, key:path})
                        opened.assert_not_called()
                        self.assertEqual(str(raised.exception), 'Invalid '+key)
                        self.assertNotIn(path, str(raised.exception))

    def test_path_swapped_for_fifo_after_check_is_refused_without_blocking(self):
        base = {'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':'https://localhost:4318'}
        real_open = os.open
        for key in ('RAG_OTEL_HEADERS_FILE', 'RAG_OTEL_CA_FILE'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, SECRET.replace('/', '_')+'-swapped')
                Path(path).write_bytes(b'{}')
                swapped, fired, done = [], [], threading.Event()
                def swap_then_open(name, flags, *args, **kwargs):
                    if name == path and not swapped:
                        os.remove(path)
                        os.mkfifo(path)
                        swapped.append(True)
                    return real_open(name, flags, *args, **kwargs)
                def watchdog():
                    if not done.wait(5):
                        fired.append(True)
                        os.close(real_open(path, os.O_WRONLY))
                threading.Thread(target=watchdog, daemon=True).start()
                try:
                    with patch('os.open', side_effect=swap_then_open), \
                            patch('os.fdopen', side_effect=AssertionError('read')), \
                            patch('ssl.create_default_context', side_effect=AssertionError('loaded')), \
                            self.assertRaises(TelemetryConfigError) as raised:
                        Config.from_env({**base, key:path})
                finally:
                    done.set()
                self.assertEqual(swapped, [True])
                self.assertEqual(fired, [], key+' blocked on a path swapped for a FIFO')
                self.assertEqual(str(raised.exception), 'Invalid '+key)
                self.assertNotIn(path, str(raised.exception))

    def test_header_and_ca_file_descriptors_are_closed_on_failure_and_success(self):
        base = {'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':'https://localhost:4318'}
        real_open = os.open
        def closed(descriptor):
            try:
                os.fstat(descriptor)
            except OSError:
                return True
            return False
        for key in ('RAG_OTEL_HEADERS_FILE', 'RAG_OTEL_CA_FILE'):
            for case in ('read fails', 'swapped for a fifo'):
                with self.subTest(key=key, case=case), tempfile.TemporaryDirectory() as directory:
                    path = os.path.join(directory, SECRET.replace('/', '_')+'-fd')
                    Path(path).write_bytes(b'{}')
                    opened, fired, done = [], [], threading.Event()
                    def recording_open(name, flags, *args, **kwargs):
                        if name == path and case == 'swapped for a fifo':
                            os.remove(path); os.mkfifo(path)
                        descriptor = real_open(name, flags, *args, **kwargs)
                        if name == path:
                            opened.append(descriptor)
                        return descriptor
                    def watchdog():
                        if not done.wait(5):
                            fired.append(True)
                            os.close(real_open(path, os.O_WRONLY))
                    if case == 'swapped for a fifo':
                        threading.Thread(target=watchdog, daemon=True).start()
                    try:
                        with patch('os.open', side_effect=recording_open), \
                                patch('os.fdopen', side_effect=OSError('read')), \
                                patch('ssl.create_default_context', side_effect=AssertionError('loaded')), \
                                self.assertRaises(TelemetryConfigError) as raised:
                            Config.from_env({**base, key:path})
                    finally:
                        done.set()
                    self.assertEqual(fired, [], key+' blocked on a path swapped for a FIFO')
                    self.assertEqual(len(opened), 1)
                    self.assertTrue(closed(opened[0]), key+' leaked its descriptor when '+case)
                    self.assertEqual(str(raised.exception), 'Invalid '+key)
                    self.assertNotIn(path, str(raised.exception))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'headers')
            Path(path).write_text('{"Authorization":"a"}')
            opened = []
            def recording_open(name, flags, *args, **kwargs):
                descriptor = real_open(name, flags, *args, **kwargs)
                if name == path:
                    opened.append(descriptor)
                return descriptor
            with patch('os.open', side_effect=recording_open):
                cfg = Config.from_env({**base, 'RAG_OTEL_HEADERS_FILE':path})
            self.assertEqual(set(cfg.headers), {'Authorization'})
            self.assertEqual(len(opened), 1)
            self.assertTrue(closed(opened[0]), 'RAG_OTEL_HEADERS_FILE leaked its descriptor after a successful read')

    def test_header_file_accepts_8192_bytes_and_rejects_8193(self):
        base = {'RAG_OTEL_ENABLED':'true', 'RAG_OTEL_ENDPOINT':'https://localhost:4318'}
        body = '{"Authorization":"a"}'
        for size, accepted in ((8192, True), (8193, False)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, 'headers')
                Path(path).write_text(body + ' '*(size-len(body)))
                self.assertEqual(os.path.getsize(path), size)
                if accepted:
                    self.assertEqual(set(Config.from_env({**base, 'RAG_OTEL_HEADERS_FILE':path}).headers), {'Authorization'})
                else:
                    with self.assertRaises(TelemetryConfigError) as raised:
                        Config.from_env({**base, 'RAG_OTEL_HEADERS_FILE':path})
                    self.assertEqual(str(raised.exception), 'Invalid RAG_OTEL_HEADERS_FILE')

if __name__ == '__main__':unittest.main()
