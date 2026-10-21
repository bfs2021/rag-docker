"""ollama/entrypoint.sh skips models already present, pulls with a timeout and retries (issue #399).

Feature-review item PL:G8. Runs on the host with Python's standard library and bash
(`16_api_paths.sh`, run by `01_infrastructure.sh`):

    python3 scripts/tests/test_ollama_entrypoint.py

The real script runs with stub `ollama`, `timeout` and `sleep` commands first on PATH:
nothing is downloaded and nothing waits. The stubs log every call, so each test reads
back what the script ran.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "ollama" / "entrypoint.sh"
MODELS = ("phi3.5", "nomic-embed-text")

# `ollama`: `serve` exits at once; `list` fails while list-fail tokens remain, then
# prints a header and one line per pulled model; `pull <m>` fails while fail-<m>
# tokens remain, else records <m> as present.
OLLAMA = r"""#!/bin/bash
state="$STUB_STATE"
echo "ollama $*" >> "$state/calls"
take() {  # take <file>: consume one token from <file>, succeed if there was one
  local n; n=$(cat "$1" 2>/dev/null || echo 0)
  [ "$n" -gt 0 ] || return 1
  echo $((n - 1)) > "$1"
}
case "$1" in
  serve) exit 0 ;;
  list)
    take "$state/list-fail" && exit 1
    echo "NAME                    ID              SIZE      MODIFIED"
    for m in "$state"/models/*; do
      [ -e "$m" ] && echo "$(basename "$m"):latest    0123456789ab    2.2 GB    1 day ago"
    done
    exit 0 ;;
  pull)
    take "$state/fail-$2" && { echo "pull failed" >&2; exit 1; }
    touch "$state/models/$2"
    exit 0 ;;
esac
exit 2
"""

# `timeout <seconds> <command...>`: exits 124, as a timed-out command does, while
# hang-<model> tokens remain, else runs the command.
TIMEOUT = r"""#!/bin/bash
state="$STUB_STATE"
echo "timeout $*" >> "$state/calls"
n=$(cat "$state/hang-$4" 2>/dev/null || echo 0)
if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$state/hang-$4"; exit 124; fi
shift
exec "$@"
"""

SLEEP = r"""#!/bin/bash
echo "sleep $*" >> "$STUB_STATE/calls"
"""


class EntrypointTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp, True)
        self.bin = Path(temp) / "bin"
        self.state = Path(temp) / "state"
        (self.state / "models").mkdir(parents=True)
        self.bin.mkdir()
        for name, text in (("ollama", OLLAMA), ("timeout", TIMEOUT), ("sleep", SLEEP)):
            path = self.bin / name
            path.write_text(text)
            path.chmod(0o755)

    def present(self, *models):
        for model in models:
            (self.state / "models" / model).touch()

    def tokens(self, name, count):
        (self.state / name).write_text(str(count))

    def run_entrypoint(self):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "STUB_STATE": str(self.state),
               "HOME": str(self.state)}
        result = subprocess.run(["bash", str(ENTRYPOINT)], env=env, capture_output=True,
                                text=True, timeout=60)
        calls_file = self.state / "calls"
        calls = calls_file.read_text().splitlines() if calls_file.exists() else []
        return result, calls

    def pulls(self, calls):
        return [c for c in calls if c.startswith("timeout ")]


class EntrypointTests(EntrypointTestCase):
    def test_models_already_present_are_not_pulled(self):
        self.present(*MODELS)
        result, calls = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pulls(calls), [])
        self.assertNotIn("ollama pull", "\n".join(calls))
        for model in MODELS:
            self.assertIn(f"ollama: {model} already present, skipping pull.", result.stdout)

    def test_only_the_missing_model_is_pulled(self):
        self.present("nomic-embed-text")
        result, calls = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pulls(calls), ["timeout 7200 ollama pull phi3.5"])
        self.assertIn("ollama: nomic-embed-text already present, skipping pull.", result.stdout)

    def test_missing_models_are_pulled_under_a_two_hour_timeout(self):
        # The server isn't ready for the first two `ollama list` calls.
        self.tokens("list-fail", 2)
        result, calls = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pulls(calls), [f"timeout 7200 ollama pull {m}" for m in MODELS])
        self.assertEqual([c for c in calls if c.startswith("ollama pull")],
                         [f"ollama pull {m}" for m in MODELS])
        for model in MODELS:
            self.assertIn(f"ollama: {model} ready.", result.stdout)
        # Nothing is pulled before the server answers: two failed `ollama list` calls,
        # 2 s apart, come first.
        first_pull = calls.index("timeout 7200 ollama pull phi3.5")
        self.assertEqual(calls[:first_pull].count("sleep 2"), 2)

    def test_a_failed_or_timed_out_pull_is_retried_after_15_seconds(self):
        self.present("nomic-embed-text")
        self.tokens("hang-phi3.5", 1)   # the first attempt times out
        self.tokens("fail-phi3.5", 1)   # the second fails
        result, calls = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pulls(calls), ["timeout 7200 ollama pull phi3.5"] * 3)
        self.assertEqual([c for c in calls if c == "sleep 15"], ["sleep 15"] * 2)
        for attempt in (1, 2):
            self.assertIn(f"ollama: pull of phi3.5 failed or timed out (attempt {attempt}/5), "
                          "retrying in 15s...", result.stdout)
        self.assertIn("ollama: phi3.5 ready.", result.stdout)

    def test_five_failed_attempts_stop_the_container(self):
        self.tokens("fail-phi3.5", 99)
        result, calls = self.run_entrypoint()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.pulls(calls), ["timeout 7200 ollama pull phi3.5"] * 5)
        self.assertEqual(calls.count("sleep 15"), 4)
        self.assertIn("ERROR: failed to pull phi3.5 after 5 attempts.", result.stderr)
        # Nothing more is pulled.
        self.assertNotIn("nomic-embed-text", "\n".join(calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
