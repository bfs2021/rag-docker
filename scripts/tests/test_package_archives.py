"""Distribution archives hold only the tracked files of HEAD (#384).

Host-side, no stack: each script runs in a scratch git repository, and
package-offline.sh talks to a stub `docker` first on PATH, never to a daemon.

Run: python3 scripts/tests/test_package_archives.py
"""
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ('package.sh', 'package-offline.sh')
REFUSAL = 'not the top level of a git checkout'
COMMITTED_README = 'Scratch project.\n'
GIT = ['git', '-c', 'user.name=Package Test', '-c', 'user.email=package-test@example.invalid',
       '-c', 'commit.gpgsign=false', '-c', 'init.defaultBranch=develop']
# The scratch repository and both scripts read no system or global (or XDG) git
# config, so a user's core.hooksPath or attributes can't change the result (#447).
GIT_ISOLATION = {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}

# Answers what package-offline.sh asks of docker, and logs each call.
STUB_DOCKER = r'''#!/usr/bin/env bash
echo "$*" >> "$STUB_DOCKER_LOG"
case "$1 $2" in
  "compose config") echo '{"name":"scratch"}' ;;
  "image inspect"|"volume inspect") ;;
  save*) printf 'stub image layers' ;;
  run*)
    while [ $# -gt 0 ]; do
      case "$1" in -v) case "$2" in *:/out) touch "${2%:/out}/ollama_models.tar.gz" ;; esac; shift ;; esac
      shift
    done ;;
  *) echo "stub docker: unexpected call: $*" >&2; exit 1 ;;
esac
'''

# Untracked or ignored content that must never reach an archive.
UNTRACKED = (
    '.env', '.env.local', 'tools/.env.production', 'rag-docker-offline.tar', 'old.zip',
    'offline/images.tar.gz', '.claude/worktrees/fix-1/README.md', 'dev-docs/issue-1/analysis.md',
    '0', '.DS_Store', 'notes.txt', 'ingest-inbox/doc.pdf', 'exports/pkg.tar.gz',
)
EXECUTABLE = ('install-offline.sh', 'tools/run.sh')


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


class PackageArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.git_env = dict(os.environ, **GIT_ISOLATION)
        self.repo = self.tmp / 'project'
        self.repo.mkdir()
        for name in SCRIPTS + ('.gitignore',):
            shutil.copy(ROOT / name, self.repo / name)
        write(self.repo / 'install-offline.sh', '#!/usr/bin/env bash\necho install\n', 0o755)
        write(self.repo / 'README.md', COMMITTED_README)
        write(self.repo / 'ingest-inbox' / '.gitkeep', '')
        write(self.repo / 'exports' / '.gitkeep', '')
        write(self.repo / 'tools' / 'run.sh', '#!/usr/bin/env bash\n', 0o755)
        write(self.repo / 'tools' / 'data.txt', 'data\n')
        self.git('init', '-q')
        self.git('add', '-A')
        self.git('add', '-f', 'ingest-inbox/.gitkeep', 'exports/.gitkeep')
        self.git('commit', '-q', '-m', 'scratch')
        self.tracked = set(self.git('ls-files').split())
        for name in UNTRACKED:
            write(self.repo / name, 'must not ship\n')
        with open(self.repo / 'README.md', 'a') as handle:
            handle.write('UNCOMMITTED EDIT\n')
        self.assertEqual(set(self.git('ls-files').split()), self.tracked)
        bin_dir = self.tmp / 'bin'
        write(bin_dir / 'docker', STUB_DOCKER, 0o755)
        self.log = self.tmp / 'docker.log'
        self.env = dict(self.git_env, PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
                        STUB_DOCKER_LOG=str(self.log))

    def git(self, *args, cwd=None):
        return subprocess.run(GIT + list(args), cwd=cwd or self.repo, env=self.git_env, check=True,
                              capture_output=True, text=True).stdout

    def run_script(self, directory, script, out):
        return subprocess.run(['bash', str(directory / script), str(out)], cwd=self.tmp, env=self.env,
                              capture_output=True, text=True, timeout=120)

    def assert_ran(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def expected_bundle(self):
        return ({'rag-docker/' + name for name in self.tracked}
                | {'rag-docker/offline/images.tar.gz', 'rag-docker/offline/ollama_models.tar.gz'})

    def assert_archives_hold_the_tracked_files(self, directory):
        zip_out, bundle_out = self.tmp / 'check.zip', self.tmp / 'check.tar'
        self.assert_ran(self.run_script(directory, 'package.sh', zip_out))
        self.assert_ran(self.run_script(directory, 'package-offline.sh', bundle_out))
        with zipfile.ZipFile(zip_out) as archive:
            self.assertEqual({i.filename for i in archive.infolist() if not i.filename.endswith('/')},
                             self.tracked)
        with tarfile.open(bundle_out) as archive:
            self.assertEqual({m.name for m in archive.getmembers() if m.isfile()}, self.expected_bundle())

    def test_zip_holds_exactly_the_tracked_files(self):
        out = self.tmp / 'out.zip'
        self.assert_ran(self.run_script(self.repo, 'package.sh', out))
        with zipfile.ZipFile(out) as archive:
            infos = {i.filename: i for i in archive.infolist() if not i.filename.endswith('/')}
            self.assertEqual(set(infos), self.tracked)
            self.assertEqual(archive.read('README.md').decode(), COMMITTED_README)
        for name in EXECUTABLE:
            with self.subTest(executable=name):
                self.assertTrue((infos[name].external_attr >> 16) & stat.S_IXUSR)

    def test_offline_bundle_holds_tracked_files_and_offline_payload(self):
        out = self.tmp / 'bundle.tar'
        self.assert_ran(self.run_script(self.repo, 'package-offline.sh', out))
        expected = {'rag-docker/' + name for name in self.tracked}
        expected |= {'rag-docker/offline/images.tar.gz', 'rag-docker/offline/ollama_models.tar.gz'}
        with tarfile.open(out) as archive:
            files = {m.name: m for m in archive.getmembers() if m.isfile()}
            self.assertEqual(set(files), expected)
            self.assertEqual(archive.extractfile('rag-docker/README.md').read().decode(), COMMITTED_README)
        for name in EXECUTABLE:
            with self.subTest(executable=name):
                self.assertTrue(files['rag-docker/' + name].mode & stat.S_IXUSR)

    def test_refuses_outside_a_git_top_level(self):
        plain = self.tmp / 'extracted'
        nested = self.repo / 'tools' / 'copy'
        for directory in (plain, nested):
            directory.mkdir(parents=True)
            for script in SCRIPTS:
                shutil.copy(ROOT / script, directory / script)
                out = self.tmp / f'{directory.name}-{script}.out'
                with self.subTest(directory=directory.name, script=script):
                    result = self.run_script(directory, script, out)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(REFUSAL, result.stderr)
                    self.assertFalse(out.exists())
        calls = self.log.read_text() if self.log.exists() else ''
        self.assertEqual(calls, '', 'docker was called before the refusal')

    def test_runs_in_a_checkout_reached_through_a_symlink(self):
        link = self.tmp / 'linked'
        link.symlink_to(self.repo, target_is_directory=True)
        self.assert_archives_hold_the_tracked_files(link)

    def test_ignores_the_users_git_config(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        hostile = Path(temp.name).resolve()
        marker = hostile / 'hook-ran'
        write(hostile / 'hooks' / 'pre-commit', f'#!/bin/sh\ntouch "{marker}"\nexit 1\n', 0o755)
        write(hostile / 'attributes', 'README.md export-ignore\n')
        write(hostile / 'gitconfig', f'[core]\n\thooksPath = {hostile / "hooks"}\n'
                                     f'\tattributesFile = {hostile / "attributes"}\n')
        with mock.patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(hostile / 'gitconfig')):
            # A fresh scratch repository, built while the caller's global config is hostile.
            self.setUp()
            self.assert_archives_hold_the_tracked_files(self.repo)
        self.assertIn('README.md', self.tracked)
        self.assertFalse(marker.exists(), 'the global pre-commit hook ran')


class EmbeddedCopyTests(unittest.TestCase):
    def test_embedded_copies_match(self):
        text = (ROOT / 'IMPLEMENTATION.md').read_text()
        for name in SCRIPTS + ('scripts/verify/01_infrastructure.sh', 'scripts/verify/README.md',
                               'scripts/tests/test_package_archives.py'):
            with self.subTest(file=name):
                fence = '````' if name.endswith('.md') else '```'
                language = 'markdown' if name.endswith('.md') else 'bash' if name.endswith('.sh') else 'python'
                header = f'### {name}\n\n'
                start = text.index(header) + len(header)
                start = text.index(f'{fence}{language}\n', start) + len(fence + language) + 1
                end = text.index('\n' + fence + '\n', start)
                self.assertEqual(text[start:end], (ROOT / name).read_text().rstrip('\n'))


if __name__ == '__main__':
    unittest.main()
