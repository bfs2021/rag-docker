"""Ensure the copyable implementation matches the tested telemetry foundation."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
FILES = [('api/main.py', 'python'), ('api/services/telemetry.py', 'python'),
         ('api/requirements.in', 'text'), ('api/requirements.txt', 'text'),
         ('scripts/tests/test_telemetry.py', 'python'),
         ('scripts/tests/test_telemetry_implementation.py', 'python'),
         ('scripts/verify/01_infrastructure.sh', 'bash'),
         ('scripts/verify/README.md', 'markdown')]

class EmbeddedTelemetryTests(unittest.TestCase):
    def test_normative_telemetry_dependencies_match_inputs_and_lock(self):
        spec = (ROOT/'SPECIFICATIONS.md').read_text()
        inputs = (ROOT/'api/requirements.in').read_text().splitlines()
        lock = (ROOT/'api/requirements.txt').read_text().splitlines()
        for dependency in ('opentelemetry-sdk==1.44.0', 'opentelemetry-exporter-otlp-proto-http==1.44.0'):
            self.assertIn(dependency, spec)
            self.assertIn(dependency, inputs)
            self.assertIn(dependency, lock)

    def test_embedded_files_match(self):
        document = (ROOT/'IMPLEMENTATION.md').read_text()
        for name, language in FILES:
            with self.subTest(name=name):
                fence = '````' if language == 'markdown' else '```'
                # Existing dependency fences are intentionally plain text.
                header = '### '+name+'\n\n'+fence
                start = document.index('\n', document.index(header)+len(header))+1
                end = document.index('\n'+fence+'\n', start)
                self.assertEqual(document[start:end], (ROOT/name).read_text().rstrip('\n'))

if __name__ == '__main__':
    unittest.main()
