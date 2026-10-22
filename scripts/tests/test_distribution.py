"""The distribution scripts build the archives SPECIFICATIONS.md section 11 describes (issue #387).

`package.sh` and `package-offline.sh` run in a copy of the tracked tree with files planted
that section 11.2 says no archive may carry; the archives they write are then read back.
`package-offline.sh` and `install-offline.sh` run with `docker` and `sleep` replaced by stubs
on PATH that log every call, so nothing is saved, loaded or started and no stack is touched.
`tar`, `gzip` and `zip` are real. The image list is compared with what `docker compose config` resolves for
`docker-compose.yml`, which only reads the file.

Runs on the host with the standard library (Python 3.9+), from the repository root:

    python3 scripts/tests/test_distribution.py

`package.sh` needs `zip` and `git`, and the image check needs `docker compose`; each test is
skipped without them. `15_transfer_paths.sh` runs it.
"""
import gzip
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# What section 11.2 and the scripts exclude, planted where the project has such things.
PLANTED = {
    ".DS_Store": "finder", "ui/.DS_Store": "finder",
    "ui/node_modules/react/index.js": "module", "api/__pycache__/main.cpython-312.pyc": "bytecode",
    "api/stray.pyc": "bytecode", "ui/dist/index.html": "built", "api/.venv/bin/python": "venv",
    "api/venv/bin/python": "venv", ".env": "SECRET=1", "api/.env": "SECRET=1",
    "ui/.env.local": "SECRET=1",
    "ingest-inbox/report.txt": "corpus", "exports/ragpkg-corpus.tar.gz": "corpus",
}
PLANTED_ZIP = {"api/uploads/upload.txt": "upload", "old-build.zip": "archive"}
PLANTED_TAR = {"old-bundle.tar": "archive", "offline/stale-images.tar.gz": "old"}
# section 11.2 excludes `.env*`; a top-level `.env.<suffix>` is checked on its own (see below).
# Planted files are untracked and gitignored, as in a real checkout: `git archive` must leave them out.
# Git ignores the user's and the system's configuration, and takes its identity from the environment.
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
TOP_LEVEL_ENV = ".env.local"

STUB_DOCKER = r"""#!/bin/bash
# Logs each call, one line, each argument quoted, and answers as the test configures.
printf '%q ' "$@" >> "$STUB_LOG"; echo >> "$STUB_LOG"
case "$1 $2" in
  "image inspect")
    for missing in $STUB_MISSING_IMAGES; do [ "$3" = "$missing" ] && exit 1; done; exit 0 ;;
  "volume inspect") [ -n "$STUB_NO_VOLUME" ] && exit 1; exit 0 ;;
  "compose config") echo '{"name": "rag-docker", "services": {}}'; exit 0 ;;
  "compose ps")
    if [ "$3" = "--services" ]; then
      i=0; while [ "$i" -lt "${STUB_RUNNING:-5}" ]; do echo "service$i"; i=$((i+1)); done
    else
      echo "NAME STATUS"
    fi
    exit 0 ;;
  "compose run"|"compose up"|"load -i") exit 0 ;;
esac
case "$1" in
  info) exit "${STUB_INFO_RC:-0}" ;;
  save) shift; printf 'saved: %s' "$*"; exit 0 ;;
  run)
    for arg in "$@"; do
      case "$arg" in *:/out)
        out="${arg%:/out}"; mkdir -p "$STUB_MODELS/models"; echo weights > "$STUB_MODELS/models/blob"
        tar czf "$out/ollama_models.tar.gz" -C "$STUB_MODELS" . ;;
      esac
    done
    exit 0 ;;
esac
echo "stub docker: unexpected call: $*" >&2
exit 99
"""
STUB_SLEEP = """#!/bin/bash
printf 'sleep %s\\n' "$*" >> "$STUB_LOG"
"""


def offline_images():
    """The IMAGES array of package-offline.sh, as written."""
    text = (ROOT / "package-offline.sh").read_text()
    block = re.search(r"^IMAGES=\(\n(.*?)^\)", text, re.S | re.M).group(1)
    return [line.strip() for line in block.splitlines() if line.strip()]


def tracked_files():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True)
    if out.returncode != 0:
        return None
    return [name for name in out.stdout.decode().split("\0") if name]


class Workspace(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name).resolve()
        self.log = self.temp / "docker.log"
        self.log.touch()
        stub = self.temp / "bin"
        stub.mkdir()
        for name, text in (("docker", STUB_DOCKER), ("sleep", STUB_SLEEP)):
            (stub / name).write_text(text)
            (stub / name).chmod(0o755)
        self.env = dict(os.environ, **GIT_ENV, PATH=f"{stub}:{os.environ['PATH']}",
                        STUB_LOG=str(self.log), STUB_MODELS=str(self.temp / "models"))

    def calls(self):
        return [shlex.split(line) for line in self.log.read_text().splitlines() if line.strip()]

    def source_tree(self, planted):
        files = tracked_files()
        if files is None:
            self.skipTest("needs a git checkout")
        tree = self.temp / "src"
        tree.mkdir()
        for name in files:
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if (ROOT / name).is_file():
                shutil.copy2(ROOT / name, target)
        # package.sh and package-offline.sh run `git archive HEAD`: make the copy a repository
        # with the tracked files committed, then plant the excluded files (untracked) on top.
        git = dict(os.environ, **GIT_ENV)
        listed = "\0".join(name for name in files if (ROOT / name).is_file()) + "\0"
        for args, data in ((["init", "-q", "--template="], None),
                           (["add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"], listed),
                           (["commit", "-q", "-m", "tracked files"], None)):
            done = subprocess.run(["git", "-C", str(tree), *args], input=data, text=True,
                                  capture_output=True, env=git)
            self.assertEqual(done.returncode, 0, done.stderr)
        for name, text in planted.items():
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_text(text)
        return tree, [name for name in files if (ROOT / name).is_file()]

    def run_script(self, script, *args, cwd=None, **env):
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                              cwd=cwd or self.temp, env=dict(self.env, **env), timeout=600)

    def assert_contents(self, names, files, prefix, planted):
        names = set(names)
        missing = [name for name in files if prefix + name not in names]
        self.assertEqual(missing, [], "tracked files missing from the archive")
        leaked = [name for name in planted if prefix + name in names]
        self.assertEqual(leaked, [], "excluded files in the archive")
        for keep in ("ingest-inbox/.gitkeep", "exports/.gitkeep"):
            self.assertIn(prefix + keep, names)
        for folder in ("ingest-inbox/", "exports/"):
            inside = sorted(n for n in names if n.startswith(prefix + folder)
                            and n.rstrip("/") != prefix + folder.rstrip("/"))
            self.assertEqual(inside, [prefix + folder + ".gitkeep"])


@unittest.skipUnless(shutil.which("zip"), "needs zip")
class PackageTests(Workspace):
    def build(self, planted):
        tree, files = self.source_tree(planted)
        out = self.temp / "out" / "rag-docker.zip"
        out.parent.mkdir()
        result = self.run_script(tree / "package.sh", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Created {out}", result.stdout)
        with zipfile.ZipFile(out) as archive:
            return archive.namelist(), files

    def test_the_zip_holds_the_tracked_tree_and_nothing_excluded(self):
        planted = dict(PLANTED, **PLANTED_ZIP)
        names, files = self.build(planted)
        self.assert_contents(names, files, "", planted)

    def test_a_top_level_env_file_is_excluded(self):
        names, _files = self.build({TOP_LEVEL_ENV: "SECRET=1"})
        self.assertNotIn(TOP_LEVEL_ENV, names)


class OfflinePackageTests(Workspace):
    def build(self, planted, **env):
        tree, files = self.source_tree(planted)
        out = self.temp / "out" / "bundle.tar"
        out.parent.mkdir(exist_ok=True)
        result = self.run_script(tree / "package-offline.sh", str(out), cwd=tree, **env)
        return result, out, files

    def test_the_bundle_holds_the_source_images_and_weights(self):
        planted = dict(PLANTED, **PLANTED_TAR)
        result, out, files = self.build(planted)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(out) as bundle:
            names = [m.name.rstrip("/") + ("/" if m.isdir() else "") for m in bundle.getmembers()]
            names = [n[2:] if n.startswith("./") else n for n in names]
            self.assertTrue(all(n == "rag-docker/" or n.startswith("rag-docker/") for n in names),
                            names[:5])
            self.assert_contents(names, files, "rag-docker/", planted)
            self.assertIn("rag-docker/install-offline.sh", names)
            images = gzip.decompress(bundle.extractfile("rag-docker/offline/images.tar.gz").read())
            weights = bundle.extractfile("rag-docker/offline/ollama_models.tar.gz").read()
            offline = sorted(n for n in names if n.startswith("rag-docker/offline/"))
        self.assertEqual(images.decode(), "saved: " + " ".join(offline_images()))
        with tarfile.open(fileobj=io.BytesIO(weights), mode="r:gz") as models:
            self.assertIn("./models/blob", models.getnames())
        self.assertEqual(offline, ["rag-docker/offline/", "rag-docker/offline/images.tar.gz",
                                   "rag-docker/offline/ollama_models.tar.gz"])
        calls = self.calls()
        self.assertEqual([c[2] for c in calls if c[:2] == ["image", "inspect"]], offline_images())
        self.assertIn(["volume", "inspect", "rag-docker_ollama_models"], calls)
        save = [c for c in calls if c[0] == "save"]
        self.assertEqual(save, [["save"] + offline_images()])
        run = next(c for c in calls if c[0] == "run")
        self.assertIn("rag-docker_ollama_models:/models:ro", run)
        self.assertIn("ollama/ollama:0.3.14", run)

    def test_a_top_level_env_file_is_excluded(self):
        result, out, _files = self.build({TOP_LEVEL_ENV: "SECRET=1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(out) as bundle:
            names = bundle.getnames()
        self.assertNotIn("rag-docker/" + TOP_LEVEL_ENV, names)

    def test_a_missing_image_stops_before_anything_is_saved(self):
        result, out, _files = self.build({}, STUB_MISSING_IMAGES="nginx:1.29-alpine")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: image 'nginx:1.29-alpine' not found locally.", result.stderr)
        self.assertFalse(out.exists())
        self.assertEqual([c for c in self.calls() if c[0] in ("save", "run")], [])

    def test_a_missing_model_volume_stops_before_anything_is_saved(self):
        result, out, _files = self.build({}, STUB_NO_VOLUME="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: volume 'rag-docker_ollama_models' not found.", result.stderr)
        self.assertFalse(out.exists())
        self.assertEqual([c for c in self.calls() if c[0] in ("save", "run")], [])


class ImageListTests(unittest.TestCase):
    def test_the_offline_images_are_the_images_compose_resolves(self):
        if not shutil.which("docker"):
            self.skipTest("needs the docker CLI")
        env = {k: v for k, v in os.environ.items()
               if k not in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES")}
        if subprocess.run(["docker", "compose", "version"], capture_output=True,
                          env=env).returncode != 0:
            self.skipTest("needs docker compose")
        out = subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"),
                              "--project-directory", str(ROOT), "config", "--images"],
                             capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        resolved = sorted(line.strip() for line in out.stdout.splitlines() if line.strip())
        self.assertEqual(sorted(offline_images()), resolved)


class InstallOfflineTests(Workspace):
    def bundle(self, *offline):
        folder = self.temp / "bundle"
        (folder / "offline").mkdir(parents=True)
        shutil.copy2(ROOT / "install-offline.sh", folder)
        for name in offline:
            (folder / "offline" / name).write_bytes(b"stub")
        return folder

    def install(self, folder, **env):
        return self.run_script(folder / "install-offline.sh", cwd=self.temp, **env)

    def test_without_the_images_it_refuses_naming_them(self):
        result = self.install(self.bundle("ollama_models.tar.gz"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: offline/images.tar.gz missing.", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_without_the_weights_it_refuses_naming_them(self):
        result = self.install(self.bundle("images.tar.gz"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: offline/ollama_models.tar.gz missing.", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_without_docker_running_it_refuses(self):
        result = self.install(self.bundle("images.tar.gz", "ollama_models.tar.gz"), STUB_INFO_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: Docker is not running.", result.stderr)
        self.assertEqual(self.calls(), [["info"]])

    def test_it_loads_restores_and_starts_without_building(self):
        folder = self.bundle("images.tar.gz", "ollama_models.tar.gz")
        result = self.install(folder)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [
            ["info"],
            ["load", "-i", "offline/images.tar.gz"],
            ["compose", "run", "--rm", "--no-deps", "--entrypoint", "sh",
             "-v", f"{folder}/offline:/backup:ro", "ollama", "-c",
             "tar xzf /backup/ollama_models.tar.gz -C /root/.ollama && ls /root/.ollama"],
            ["compose", "up", "-d", "--no-build"],
            ["compose", "ps", "--services", "--filter", "status=running"],
            ["compose", "ps"],
        ])

    def test_its_wait_counts_running_services_and_gives_up_after_60_polls(self):
        # The script says it waits for services to report healthy; it counts running ones.
        # SPECIFICATIONS.md section 11.4 asks for no wait, so this records the behaviour (#387).
        result = self.install(self.bundle("images.tar.gz", "ollama_models.tar.gz"), STUB_RUNNING="4")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        polls = [c for c in calls if c == ["compose", "ps", "--services", "--filter", "status=running"]]
        self.assertEqual(len(polls), 60)
        self.assertEqual(calls.count(["sleep", "5"]), 60)
        self.assertEqual(calls[-1], ["compose", "ps"])
        self.assertIn("Waiting for services to report healthy...", result.stdout)


if __name__ == "__main__":
    unittest.main()
