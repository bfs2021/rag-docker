"""The executable implementation examples must preserve the session boundary."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SessionImplementationTests(unittest.TestCase):
    def test_service_examples_match_validated_implementation(self):
        implementation = (ROOT / 'IMPLEMENTATION.md').read_text()
        for name, fence, language in [('api/services/goldstandard.py', '```', 'python'),
                                      ('api/services/importer.py', '```', 'python'),
                                      ('api/services/packager.py', '```', 'python'),
                                      ('scripts/verify/README.md', '````', 'markdown'),
                                      ('scripts/verify/05_transfer.sh', '```', 'bash'),
                                      ('scripts/verify/15_transfer_paths.sh', '```', 'bash'),
                                      ('scripts/verify/transfer_packages.py', '```', 'python'),
                                      ('api/services/retrieval_config.py', '```', 'python'),
                                      ('api/templates/retrieve.py.tmpl', '```', 'python'),
                                      ('scripts/tests/test_retrieval_import.py', '```', 'python'),
                                      ('scripts/verify/retrieval_settings.py', '```', 'python'),
                                      ('scripts/verify/legacy_retrieval.py', '```', 'python')]:
            with self.subTest(path=name):
                header = '### ' + name + '\n\n' + fence + language + '\n'
                start = implementation.index(header) + len(header)
                end = implementation.index('\n' + fence + '\n', start)
                self.assertEqual(implementation[start:end], (ROOT / name).read_text().rstrip('\n'))


if __name__ == '__main__':
    unittest.main()
