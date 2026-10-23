"""Host-only check that the verify suites run the test files #386 registered.

Each file below used to exist with no suite running it. This reads the suite
scripts and checks that a command line runs the file and that its exit status
goes straight to `check`. Standard library only; no Docker or stack needed.

Run: python3 scripts/tests/test_suite_registration.py
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / 'scripts/verify'

# (file, suite that runs it, run in the isolated container with the checkout mounted)
REGISTERED = (
    ('scripts/tests/test_settings_validation.py', '07_settings.sh', True),
    ('scripts/tests/test_model_bundle.py', '05_transfer.sh', True),
    ('scripts/verify/model_integrity.py', '05_transfer.sh', False),
    ('scripts/tests/test_chunk_limits.py', '02_ingest.sh', True),
    ('scripts/tests/test_insert_chunks.py', '02_ingest.sh', True),
    ('scripts/tests/test_retrieval_controls.py', '11_retrieval.sh', True),
)


def command_lines(suite):
    """(command, next non-blank line) for every non-comment line of a suite."""
    lines = (VERIFY / suite).read_text().splitlines()
    pairs = []
    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        following = next((later for later in lines[index + 1:] if later.strip()), '')
        pairs.append((line, following))
    return pairs


class SuiteRegistrationTests(unittest.TestCase):
    def test_each_file_is_run_by_its_suite_and_checked(self):
        for name, suite, isolated in REGISTERED:
            with self.subTest(file=name, suite=suite):
                self.assertTrue((ROOT / name).is_file(), name)
                runs = [(line, following) for line, following in command_lines(suite) if name in line]
                self.assertTrue(runs, '{} does not run {}'.format(suite, name))
                line, following = runs[0]
                self.assertTrue(following.lstrip().startswith('check '),
                                '{}: the line after {} is not a check: {!r}'.format(suite, name, following))
                if isolated:
                    # The checkout's own api/ source, the image's dependencies, the
                    # whole repository for the embedded-source checks, no network.
                    self.assertIn('docker run --rm --network none', line)
                    self.assertIn(':/repo:ro', line)
                    self.assertIn('RAG_TEST_API_DIR=/repo/api', line)
                    self.assertIn('python ' + name, line)
                else:
                    self.assertIn('docker compose exec -T api python - < ' + name, line)


if __name__ == '__main__':
    unittest.main()
