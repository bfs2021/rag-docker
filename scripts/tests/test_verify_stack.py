"""Host-only checks for the verify project harness (#152).

No real Docker command runs here. `docker` and `curl` are replaced by a stub
that records its arguments and returns canned output, so the tests can assert
exactly what scripts/verify/stack.sh and the live-stack guards would ask Docker
to do, and that they refuse before asking anything.

Run: python3 scripts/tests/test_verify_stack.py
"""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / 'scripts/verify'
STACK = VERIFY / 'stack.sh'

STUB = textwrap.dedent(r'''
    #!/usr/bin/env python3
    """Records argv as one JSON line and answers like a tiny Docker."""
    import json, os, sys
    tool = os.path.basename(sys.argv[0])
    args = sys.argv[1:]
    with open(os.environ['STUB_LOG'], 'a') as log:
        log.write(json.dumps([tool, *args]) + '\n')
    if tool == 'curl':
        print(os.environ.get('STUB_CURL_CODE', '000'), end='')
        sys.exit(0)
    state_path = os.environ['STUB_STATE']
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    def once(key, text):
        if not state.get(key):
            state[key] = True
            json.dump(state, open(state_path, 'w'))
            print(text)
    if args[:1] == ['compose']:
        if 'config' in args:
            print(open(os.environ['STUB_CONFIG']).read())
        elif 'up' in args:
            sys.exit(0 if os.environ.get('STUB_UP_OK') else 1)
        elif 'ps' in args:
            print('api exited ')
        sys.exit(0)
    if args[:2] == ['ps', '-aq']:
        once('ps', 'c1')
    elif args[:2] == ['network', 'ls']:
        once('net', 'n1')
    elif args[:2] == ['volume', 'ls']:
        once('vol', 'v1\nrag-verify-ollama-models')
    elif args[:2] == ['image', 'inspect']:
        present = {'rag-verify-api:latest', 'ollama/ollama:0.3.14'} - set(state.get('removed', []))
        sys.exit(0 if args[2] in present else 1)
    elif args[:2] == ['image', 'ls']:
        # Only the verify project's labelled images, as `down` asks for them.
        if 'label=com.docker.compose.project=rag-verify' in args:
            names = os.environ.get('STUB_IMAGES', 'rag-verify-api:latest').split('\n')
            for name in names:
                if name and name not in state.get('removed', []):
                    print(name)
    elif args[:2] == ['image', 'rm']:
        stuck = os.environ.get('STUB_STUCK', '').split('\n')
        state['removed'] = state.get('removed', []) + [a for a in args[2:] if a not in stuck]
        json.dump(state, open(state_path, 'w'))
    elif args[:2] == ['volume', 'inspect']:
        sys.exit(1 if os.environ.get('STUB_NO_LIVE') and args[2] == 'rag-docker_ollama_models' else 0)
    elif args[:1] == ['run']:
        print('models: copied=0 repaired=0 removed=0')
    sys.exit(0)
''').lstrip()


def guard_block():
    source = STACK.read_text()
    blocks = re.findall(r"python3 - [^\n]*<<'GUARDPY'\n(.*?)\nGUARDPY", source, re.S)
    assert len(blocks) == 1, 'stack.sh must hold exactly one GUARDPY block'
    return blocks[0]


def passing_config(checkout, exports, port='8081'):
    """The shape `docker compose -p rag-verify config --format json` gives for
    the base file plus the overlay (#154: networks, commands and the
    read-only binds included)."""
    checkout, exports = str(checkout), str(exports)
    net = {'rag-internal': None}
    healthy = {'condition': 'service_healthy', 'required': True}
    started = {'condition': 'service_started', 'required': True}
    probe = {'test': ['CMD', 'true'], 'interval': '10s', 'timeout': '5s', 'retries': 5}
    return {
        'name': 'rag-verify',
        'networks': {'rag-internal': {'name': 'rag-verify_rag-internal', 'driver': 'bridge', 'ipam': {}}},
        'services': {
            'weaviate': {'image': 'semitechnologies/weaviate:1.39.6', 'command': None, 'entrypoint': None,
                         'environment': {'QUERY_DEFAULTS_LIMIT': '25'}, 'healthcheck': probe, 'networks': net,
                         'volumes': [{'type': 'volume', 'source': 'weaviate_data',
                                      'target': '/var/lib/weaviate', 'volume': {}}]},
            'ollama': {'image': 'ollama/ollama:0.3.14', 'command': None,
                       'entrypoint': ['/bin/bash', '/entrypoint.sh'], 'healthcheck': probe, 'networks': net,
                       'volumes': [{'type': 'volume', 'source': 'ollama_models',
                                    'target': '/root/.ollama', 'volume': {}},
                                   {'type': 'bind', 'source': checkout + '/ollama/entrypoint.sh',
                                    'target': '/entrypoint.sh', 'read_only': True,
                                    'bind': {'create_host_path': True}}]},
            'api': {'image': 'rag-verify-api:latest', 'command': None, 'entrypoint': None,
                    'build': {'context': checkout + '/api', 'dockerfile': 'Dockerfile'},
                    'environment': {'WEAVIATE_HOST': 'weaviate'}, 'healthcheck': probe, 'networks': net,
                    'depends_on': {'ollama': healthy, 'weaviate': healthy},
                    'volumes': [{'type': 'volume', 'source': 'ingest_uploads', 'target': '/app/uploads', 'volume': {}},
                                {'type': 'volume', 'source': 'rag_sources', 'target': '/app/sources', 'volume': {}},
                                {'type': 'bind', 'source': exports, 'target': '/app/exports',
                                 'bind': {'create_host_path': True}},
                                {'type': 'volume', 'source': 'ollama_models', 'target': '/ollama', 'volume': {}}]},
            'ui': {'image': 'rag-verify-ui:latest', 'command': None, 'entrypoint': None, 'networks': net,
                   'build': {'context': checkout + '/ui', 'dockerfile': 'Dockerfile'},
                   'depends_on': {'api': healthy}},
            'proxy': {'image': 'nginx:1.29-alpine', 'command': None, 'entrypoint': None, 'networks': net,
                      'depends_on': {'api': started, 'ui': started},
                      'ports': [{'mode': 'ingress', 'host_ip': '127.0.0.1', 'target': 80,
                                 'published': port, 'protocol': 'tcp'}],
                      'volumes': [{'type': 'bind', 'source': checkout + '/proxy/nginx.conf',
                                   'target': '/etc/nginx/nginx.conf', 'read_only': True, 'bind': {}}]},
        },
        'volumes': {
            'weaviate_data': {'name': 'rag-verify_weaviate_data'},
            'ollama_models': {'name': 'rag-verify-ollama-models', 'external': True},
            'ingest_uploads': {'name': 'rag-verify_ingest_uploads'},
            'rag_sources': {'name': 'rag-verify_rag_sources'},
        },
    }


class Sandbox:
    """Scratch TMPDIR, lock path and stubbed docker/curl for one test."""

    def __init__(self, case):
        self.temp = tempfile.TemporaryDirectory()
        case.addCleanup(self.temp.cleanup)
        self.root = Path(os.path.realpath(self.temp.name))
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for tool in ('docker', 'curl'):
            path = self.bin / tool
            path.write_text(STUB)
            path.chmod(0o755)
        self.tmp = self.root / 'tmp'
        self.tmp.mkdir()
        # The private per-user folder that holds the exports folder (#154).
        self.private = self.tmp / f'rag-verify-{os.getuid()}'
        self.private.mkdir(mode=0o700)
        self.log = self.root / 'stub.log'
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('COMPOSE_', 'RAG_'))}
        self.env.update({
            'PATH': f'{self.bin}:{os.environ["PATH"]}',
            'TMPDIR': str(self.tmp),
            'STUB_LOG': str(self.log),
            'STUB_STATE': str(self.root / 'stub.state'),
            'STUB_CONFIG': str(self.root / 'config.json'),
            'RAG_VERIFY_LOCK': str(self.root / 'verify.lock'),
        })

    @property
    def exports(self):
        return self.private / 'exports'

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def run(self, argv, cwd=ROOT, **extra):
        env = {**self.env, **extra}
        env = {k: v for k, v in env.items() if v is not None}
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


class ConfigGuardTests(unittest.TestCase):
    """S13: the resolved configuration is refused unless it is the verify project's."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(os.path.realpath(self.temp.name))
        self.checkout = self.dir / 'checkout'
        self.exports = self.dir / 'rag-verify-exports'
        self.checkout.mkdir()
        self.exports.mkdir()
        self.block = guard_block()

    def guard(self, config, port='8081'):
        path = self.dir / 'config.json'
        path.write_text(json.dumps(config))
        return subprocess.run(['python3', '-c', self.block, str(self.checkout), str(self.exports), port, str(path)],
                              capture_output=True, text=True)

    def test_verify_configuration_passes(self):
        result = self.guard(passing_config(self.checkout, self.exports))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def variants(self):
        def mutate(change):
            config = passing_config(self.checkout, self.exports)
            change(config)
            return config
        S = lambda c: c['services']
        return [
            ('a', 'live volume name', mutate(lambda c: c['volumes']['weaviate_data'].update(name='rag-docker_weaviate_data'))),
            ('a', 'other external volume', mutate(lambda c: c['volumes']['rag_sources'].update(external=True))),
            ('a', 'driver_opts bind', mutate(lambda c: c['volumes']['rag_sources'].update(driver_opts={'type': 'none', 'o': 'bind', 'device': '/Users'}))),
            ('a', 'model volume not external', mutate(lambda c: c['volumes']['ollama_models'].pop('external'))),
            ('b', 'live api image', mutate(lambda c: S(c)['api'].update(image='rag-docker-api:latest'))),
            ('b', 'live image on another service', mutate(lambda c: S(c)['weaviate'].update(image='rag-docker-weaviate:latest'))),
            ('c', 'second port', mutate(lambda c: S(c)['proxy']['ports'].append({'host_ip': '127.0.0.1', 'target': 443, 'published': '8443', 'protocol': 'tcp'}))),
            ('c', 'all interfaces', mutate(lambda c: S(c)['proxy']['ports'][0].update(host_ip='0.0.0.0'))),
            ('c', 'live port', mutate(lambda c: S(c)['proxy']['ports'][0].update(published='8080'))),
            ('c', 'api publishes', mutate(lambda c: S(c)['api'].update(ports=[{'host_ip': '127.0.0.1', 'target': 8000, 'published': '8081', 'protocol': 'tcp'}]))),
            ('d', 'bind outside the checkout', mutate(lambda c: S(c)['ui'].update(volumes=[{'type': 'bind', 'source': '/Users/x/elsewhere', 'target': '/x'}]))),
            ('e', 'docker socket', mutate(lambda c: S(c)['ui'].update(volumes=[{'type': 'bind', 'source': '/var/run/docker.sock', 'target': '/var/run/docker.sock'}]))),
            ('f', 'checkout exports', mutate(lambda c: S(c)['api']['volumes'][2].update(source=str(self.checkout / 'exports')))),
            ('f', 'exports not mounted', mutate(lambda c: S(c)['api']['volumes'].pop(2))),
        ] + self.allow_list_variants(mutate, S)

    def allow_list_variants(self, mutate, S):
        """#154: one adverse variant per allow-list rule."""
        checkout = str(self.checkout)
        built = lambda image=None: dict({'build': {'context': checkout + '/extra', 'dockerfile': 'Dockerfile'},
                                          'networks': {'rag-internal': None}},
                                         **({'image': image} if image else {}))
        variants = [
            # (g) top level
            ('g', 'top-level secrets', mutate(lambda c: c.update(secrets={'s': {'file': '/etc/passwd'}}))),
            ('g', 'top-level configs', mutate(lambda c: c.update(configs={'s': {'file': '/etc/passwd'}}))),
            ('g', 'extension key', mutate(lambda c: c.update({'x-foo': {}}))),
            ('g', 'another project name', mutate(lambda c: c.update(name='rag-docker'))),
            # (i) build
            ('i', 'build tags', mutate(lambda c: S(c)['api']['build'].update(tags=['rag-docker-api:latest']))),
            ('i', 'build ssh', mutate(lambda c: S(c)['api']['build'].update(ssh=['default']))),
            ('i', 'build secrets', mutate(lambda c: S(c)['api']['build'].update(secrets=['s']))),
            ('i', 'build additional_contexts', mutate(lambda c: S(c)['api']['build'].update(additional_contexts={'x': '/etc'}))),
            ('i', 'build args', mutate(lambda c: S(c)['api']['build'].update(args={'A': '1'}))),
            ('i', 'context outside the checkout', mutate(lambda c: S(c)['api']['build'].update(context='/elsewhere'))),
            ('i', 'relative context', mutate(lambda c: S(c)['api']['build'].update(context='api'))),
            ('i', 'url context', mutate(lambda c: S(c)['api']['build'].update(context='https://github.com/x/y.git'))),
            ('i', 'dockerfile outside the checkout', mutate(lambda c: S(c)['api']['build'].update(dockerfile='/etc/Dockerfile'))),
            # (b) images
            ('b', 'registry-qualified live image', mutate(lambda c: S(c)['weaviate'].update(image='docker.io/library/rag-docker-weaviate:latest'))),
            ('b', 'registry-qualified live api', mutate(lambda c: S(c)['api'].update(image='docker.io/library/rag-docker-api:latest'))),
            ('b', 'built proxy with a live third-party tag', mutate(lambda c: S(c)['proxy'].update(build={'context': checkout + '/proxy', 'dockerfile': 'Dockerfile'}))),
            ('b', 'built service with another verify name', mutate(lambda c: S(c).update(extra=built('rag-verify-other:latest')))),
            # (j) networks
            ('j', 'external network', mutate(lambda c: c['networks']['rag-internal'].update(external=True))),
            ('j', 'live network name', mutate(lambda c: c['networks']['rag-internal'].update(name='rag-docker_rag-internal'))),
            ('j', 'network driver_opts', mutate(lambda c: c['networks']['rag-internal'].update(driver_opts={'a': 'b'}))),
            ('j', 'another driver', mutate(lambda c: c['networks']['rag-internal'].update(driver='overlay'))),
            ('j', 'undeclared service network', mutate(lambda c: S(c)['api'].update(networks={'rag-internal': None, 'live': None}))),
            ('j', 'network options', mutate(lambda c: S(c)['api'].update(networks={'rag-internal': {'aliases': ['x']}}))),
            # (a) volumes
            ('a', 'volume driver', mutate(lambda c: c['volumes']['rag_sources'].update(driver='local'))),
            # (d) mounts and binds
            ('d', 'tmpfs mount', mutate(lambda c: S(c)['ui'].update(volumes=[{'type': 'tmpfs', 'target': '/t'}]))),
            ('d', 'read-write bind inside the checkout', mutate(lambda c: S(c)['ui'].update(volumes=[{'type': 'bind', 'source': checkout + '/ui', 'target': '/x'}]))),
            ('d', 'bind of the checkout exports', mutate(lambda c: S(c)['ui'].update(volumes=[{'type': 'bind', 'source': checkout + '/exports', 'target': '/x', 'read_only': True}]))),
            ('d', 'bind of the checkout root', mutate(lambda c: S(c)['ui'].update(volumes=[{'type': 'bind', 'source': checkout, 'target': '/x', 'read_only': True}]))),
            ('d', 'bind propagation', mutate(lambda c: S(c)['proxy']['volumes'][0].update(bind={'propagation': 'rshared'}))),
            ('d', 'volume options', mutate(lambda c: S(c)['weaviate']['volumes'][0].update(volume={'nocopy': True}))),
            ('d', 'bind of the verify exports by another service', mutate(lambda c: S(c)['ui'].update(volumes=[{'type': 'bind', 'source': str(self.exports), 'target': '/x', 'read_only': True}]))),
        ]
        # (h) service keys outside the base file's
        for key, value in [('volumes_from', ['container:rag-docker-weaviate-1']),
                           ('network_mode', 'host'), ('network_mode', 'container:rag-docker-weaviate-1'),
                           ('privileged', True), ('cap_add', ['SYS_ADMIN']), ('devices', ['/dev/kmsg']),
                           ('pid', 'host'), ('ipc', 'host'), ('security_opt', ['seccomp=unconfined']),
                           ('userns_mode', 'host'), ('secrets', ['s']), ('configs', ['s']),
                           ('deploy', {'resources': {}}), ('pull_policy', 'always'),
                           ('container_name', 'rag-docker-api-1')]:
            variants.append(('h', f'{key}: {value}', mutate(lambda c, k=key, v=value: S(c)['api'].update({k: v}))))
        return variants

    def test_allowed_extra_built_services_pass(self):
        # #154 (S24): a service other than api/ui may build, with its own
        # rag-verify-<service> name or none (compose then names it so).
        for image in (None, 'rag-verify-extra:latest', 'rag-verify-extra'):
            with self.subTest(image=image):
                config = passing_config(self.checkout, self.exports)
                service = {'build': {'context': str(self.checkout / 'extra'), 'dockerfile': 'Dockerfile'},
                           'networks': {'rag-internal': None}}
                if image:
                    service['image'] = image
                config['services']['extra'] = service
                result = self.guard(config)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_each_rule_refuses_its_adverse_variant(self):
        for rule, name, config in self.variants():
            with self.subTest(rule=rule, variant=name):
                result = self.guard(config)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(f'({rule})', result.stdout + result.stderr)

    def test_port_must_match(self):
        result = self.guard(passing_config(self.checkout, self.exports, port='8082'))
        self.assertEqual(result.returncode, 2)


class LiveGuardTests(unittest.TestCase):
    """S18 and S36: suites and all.sh refuse the live stack unless RAG_VERIFY_LIVE=1."""

    def setUp(self):
        self.box = Sandbox(self)

    def assert_refused(self, result):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('live rag-docker stack', result.stdout + result.stderr)
        self.assertEqual(self.box.calls(), [], 'no docker or curl call may come before the refusal')

    def test_live_project_is_refused(self):
        for script in ('all.sh', '02_ingest.sh'):
            with self.subTest(script=script):
                self.assert_refused(self.box.run(['bash', str(VERIFY / script)],
                                                 COMPOSE_PROJECT_NAME='rag-docker', RAG_API='http://localhost:9/api'))

    def test_live_port_is_refused(self):
        for script in ('all.sh', '02_ingest.sh'):
            with self.subTest(script=script):
                self.assert_refused(self.box.run(['bash', str(VERIFY / script)],
                                                 COMPOSE_PROJECT_NAME='rag-verify', RAG_API='http://localhost:8080/api'))
                self.assert_refused(self.box.run(['bash', str(VERIFY / script)],
                                                 COMPOSE_PROJECT_NAME='rag-verify'))

    def test_verify_target_passes_the_guard(self):
        result = self.box.run(['bash', str(VERIFY / 'all.sh')],
                              COMPOSE_PROJECT_NAME='rag-verify', RAG_API='http://localhost:9/api')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('live rag-docker stack', result.stdout + result.stderr)
        self.assertIn('No healthy API', result.stdout)
        result = self.box.run(['bash', str(VERIFY / '02_ingest.sh')],
                              COMPOSE_PROJECT_NAME='rag-verify', RAG_API='http://localhost:9/api')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Cannot reach a healthy API', result.stdout)

    def test_opt_in_warns_and_continues(self):
        result = self.box.run(['bash', str(VERIFY / 'all.sh')], COMPOSE_PROJECT_NAME='rag-docker',
                              RAG_API='http://localhost:9/api', RAG_VERIFY_LIVE='1')
        self.assertIn('warning', result.stderr.lower())
        self.assertIn('No healthy API', result.stdout)

    def test_folder_name_is_normalised_when_no_project_is_set(self):
        checkout = self.box.root / 'Rag-Docker'
        shutil.copytree(VERIFY, checkout / 'scripts/verify')
        result = self.box.run(['bash', str(checkout / 'scripts/verify/all.sh')], RAG_API='http://localhost:9/api')
        self.assert_refused(result)

    def test_dotenv_project_name_is_live(self):
        # #154 (S15): compose reads COMPOSE_PROJECT_NAME from the checkout's
        # .env (optionally after `export `, the last line wins, quotes removed).
        checkout = self.box.root / 'other'
        shutil.copytree(VERIFY, checkout / 'scripts/verify')
        (checkout / '.env').write_text('COMPOSE_PROJECT_NAME=something-else\n'
                                       'export COMPOSE_PROJECT_NAME="rag-docker"\n')
        result = self.box.run(['bash', str(checkout / 'scripts/verify/all.sh')], RAG_API='http://localhost:9/api')
        self.assert_refused(result)

    def test_leading_underscore_is_stripped(self):
        # #154 (S15): compose strips leading `-` and `_` from the folder name.
        checkout = self.box.root / '_rag-docker'
        shutil.copytree(VERIFY, checkout / 'scripts/verify')
        result = self.box.run(['bash', str(checkout / 'scripts/verify/all.sh')], RAG_API='http://localhost:9/api')
        self.assert_refused(result)

    def test_helpers_outside_suites_are_not_guarded(self):
        result = self.box.run(['bash', '-c', f'. "{VERIFY}/lib.sh"; echo sourced-ok'],
                              COMPOSE_PROJECT_NAME='rag-docker')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sourced-ok', result.stdout)


class RestartGuardTests(unittest.TestCase):
    """S19 and D1: restarts never reach rag-docker, whatever RAG_VERIFY_LIVE says."""

    def reason(self, **env):
        box = Sandbox(self)
        result = box.run(['bash', '-c', f'. "{VERIFY}/lib.sh"; restart_refusal_reason'], **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_refusals(self):
        self.assertTrue(self.reason())
        self.assertTrue(self.reason(COMPOSE_PROJECT_NAME='rag-docker'))
        self.assertTrue(self.reason(COMPOSE_PROJECT_NAME='rag-docker', RAG_VERIFY_LIVE='1'))
        self.assertEqual(self.reason(COMPOSE_PROJECT_NAME='rag-verify'), '')

    def test_every_restart_section_asks_first(self):
        for name in ('01_infrastructure.sh', '02_ingest.sh', '03_query.sh', '04_goldstandard.sh', '05_transfer.sh',
                     '16_api_paths.sh'):
            with self.subTest(suite=name):
                text = (VERIFY / name).read_text()
                block = text[text.index('if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]'):]
                ask = block.index('restart_refusal_reason')
                acts = [m.start() for m in re.finditer(r'docker compose[^\n]*(restart|down|up -d)', block)]
                self.assertTrue(acts, 'restart command not found')
                self.assertLess(ask, min(acts))
        self.assertNotIn('COMPOSE_PROJECT_NAME:-rag-docker', (VERIFY / '01_infrastructure.sh').read_text())

    def removal(self, **env):
        box = Sandbox(self)
        result = box.run(['bash', '-c', f'. "{VERIFY}/lib.sh"; volume_removal_refusal_reason'], **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_volume_removal_refusals(self):
        # #465: `down -v` runs only on rag-verify with the overlay in COMPOSE_FILE.
        both = '/c/docker-compose.yml:/h/docker-compose.verify.yml'
        refused = {
            'project unset': dict(COMPOSE_FILE=both),
            'rag-docker': dict(COMPOSE_PROJECT_NAME='rag-docker', COMPOSE_FILE=both),
            'rag-docker, RAG_VERIFY_LIVE=1': dict(COMPOSE_PROJECT_NAME='rag-docker', COMPOSE_FILE=both,
                                                  RAG_VERIFY_LIVE='1'),
            'another project': dict(COMPOSE_PROJECT_NAME='my-rag', COMPOSE_FILE=both),
            'RAG-VERIFY': dict(COMPOSE_PROJECT_NAME='RAG-VERIFY', COMPOSE_FILE=both),
            'COMPOSE_FILE unset': dict(COMPOSE_PROJECT_NAME='rag-verify'),
            'COMPOSE_FILE empty': dict(COMPOSE_PROJECT_NAME='rag-verify', COMPOSE_FILE=''),
            'base file only': dict(COMPOSE_PROJECT_NAME='rag-verify', COMPOSE_FILE='/c/docker-compose.yml'),
            'look-alike suffix': dict(COMPOSE_PROJECT_NAME='rag-verify',
                                      COMPOSE_FILE='/c/docker-compose.yml:/h/docker-compose.verify.yml.bak'),
            'look-alike prefix': dict(COMPOSE_PROJECT_NAME='rag-verify',
                                      COMPOSE_FILE='/c/docker-compose.yml:/h/my-docker-compose.verify.yml'),
            'overlay as a folder': dict(COMPOSE_PROJECT_NAME='rag-verify',
                                        COMPOSE_FILE='/h/docker-compose.verify.yml/x.yml'),
            'other separator': dict(COMPOSE_PROJECT_NAME='rag-verify', COMPOSE_PATH_SEPARATOR=';',
                                    COMPOSE_FILE='/c:/docker-compose.verify.yml.d;/c/docker-compose.yml'),
        }
        for case, env in refused.items():
            with self.subTest(case=case):
                reason = self.removal(**env)
                self.assertTrue(reason)
                if env.get('COMPOSE_PROJECT_NAME') != 'rag-docker':
                    self.assertIn('stack.sh', reason)
        self.assertEqual(self.removal(COMPOSE_PROJECT_NAME='rag-verify', COMPOSE_FILE=both), '')
        self.assertEqual(self.removal(COMPOSE_PROJECT_NAME='rag-verify',
                                      COMPOSE_FILE='/h/docker-compose.verify.yml'), '')
        self.assertEqual(self.removal(COMPOSE_PROJECT_NAME='rag-verify', COMPOSE_PATH_SEPARATOR=';',
                                      COMPOSE_FILE='/c/docker-compose.yml;/h/docker-compose.verify.yml'), '')

    def test_api_paths_asks_before_removing_volumes(self):
        # #465: the first-start check asks the volume guard before `down -v`.
        text = (VERIFY / '16_api_paths.sh').read_text()
        down_v = [m.start() for m in re.finditer(r'docker compose[^\n]*down -v', text)]
        self.assertEqual(len(down_v), 1)
        ask = text.find('$(volume_removal_refusal_reason)')
        self.assertNotEqual(ask, -1, 'the volume guard is not asked')
        self.assertLess(ask, down_v[0])

    def test_infrastructure_checks_the_limit_before_restarting(self):
        # #130: an invalid RAG_RESTART_LIMIT_S fails the check and restarts nothing.
        text = (VERIFY / '01_infrastructure.sh').read_text()
        block = text[text.index('if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]'):]
        down = re.search(r'docker compose[^\n]*down', block)
        self.assertIsNotNone(down, 'restart command not found')
        self.assertIn('restart_limit', block[:down.start()])



class ApiPathsFirstStartTests(unittest.TestCase):
    """#465: suite 16 removes volumes only on the verify project, run with Docker and curl stubbed."""

    BOTH = '/c/docker-compose.yml:/h/docker-compose.verify.yml'

    def run_suite(self, **env):
        box = Sandbox(self)
        result = box.run(['bash', str(VERIFY / '16_api_paths.sh')], STUB_CURL_CODE='200',
                         RAG_API='http://localhost:9/api', RAG_ALLOW_RESTART='1', RAG_SKIP_SLOW='1',
                         RAG_RESTART_LIMIT_S='5', **env)
        compose = [call for call in box.calls() if call[:2] == ['docker', 'compose']]
        removed = [call for call in compose if 'down' in call and '-v' in call]
        restarted = [call for call in compose if 'down' in call and '-v' not in call]
        self.assertTrue(restarted, 'the suite did not reach its restart check: ' + result.stdout + result.stderr)
        return result, removed

    def test_another_project_keeps_its_volumes(self):
        result, removed = self.run_suite(COMPOSE_PROJECT_NAME='my-rag', COMPOSE_FILE=self.BOTH)
        self.assertEqual(removed, [])
        self.assertRegex(result.stdout, r'FAIL\s+a first start from empty volumes[^\n]*rag-verify')

    def test_verify_project_without_the_overlay_keeps_its_volumes(self):
        result, removed = self.run_suite(COMPOSE_PROJECT_NAME='rag-verify', COMPOSE_FILE='/c/docker-compose.yml')
        self.assertEqual(removed, [])
        self.assertRegex(result.stdout, r'FAIL\s+a first start from empty volumes[^\n]*docker-compose\.verify\.yml')

    def test_verify_project_with_the_overlay_removes_them(self):
        result, removed = self.run_suite(COMPOSE_PROJECT_NAME='rag-verify', COMPOSE_FILE=self.BOTH)
        self.assertEqual([call[-2:] for call in removed], [['down', '-v']])
        self.assertIn('-p', removed[0])
        self.assertEqual(removed[0][removed[0].index('-p') + 1], 'rag-verify')


# Weaviate's measured cold start on a copy of real data, rounded up to 30s (#130).
WEAVIATE_START_PERIOD_S = 180
# The restart check's default limit: the start period plus 60s (#130).
RESTART_LIMIT_DEFAULT_S = WEAVIATE_START_PERIOD_S + 60


class WeaviateHealthcheckTests(unittest.TestCase):
    """#130: compose waits out Weaviate's cold start instead of failing its dependants."""

    def test_start_period_covers_the_measured_cold_start(self):
        import yaml
        compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
        self.assertEqual(compose['services']['weaviate']['healthcheck'], {
            'test': ['CMD', 'wget', '-q', '--spider', 'http://localhost:8080/v1/.well-known/ready'],
            'interval': '10s',
            'timeout': '5s',
            'retries': 10,
            'start_period': f'{WEAVIATE_START_PERIOD_S}s',
        })


class WeaviateRaftSnapshotTests(unittest.TestCase):
    """issue #178: Weaviate snapshots its Raft log often, so a start replays only a short tail."""

    def test_snapshot_settings(self):
        import yaml
        compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
        env = compose['services']['weaviate']['environment']
        self.assertEqual(env.get('RAFT_SNAPSHOT_THRESHOLD'), 128)
        self.assertEqual(env.get('RAFT_SNAPSHOT_INTERVAL'), 30)
        self.assertNotIn('RAFT_TRAILING_LOGS', env)


class RestartTimingTests(unittest.TestCase):
    """#130: a configurable restart limit, with the measured time on a pass and a fail."""

    def bash(self, script, **env):
        box = Sandbox(self)
        result = box.run(['bash', '-c', f'. "{VERIFY}/lib.sh"; {script}'], **env)
        return result

    def test_default_limit(self):
        for value in (None, ''):
            with self.subTest(value=value):
                result = self.bash('restart_limit', RAG_RESTART_LIMIT_S=value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, str(RESTART_LIMIT_DEFAULT_S))

    def test_valid_override(self):
        for value, limit in (('45', '45'), ('007', '7'), ('999999', '999999')):
            with self.subTest(value=value):
                result = self.bash('restart_limit', RAG_RESTART_LIMIT_S=value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, limit)

    def test_invalid_values_are_refused(self):
        for value in ('0', '000', '-5', '1.5', 'abc', '10s', ' 30', '1000000', '99999999999999999999'):
            with self.subTest(value=value):
                result = self.bash('restart_limit', RAG_RESTART_LIMIT_S=value)
                self.assertEqual(result.returncode, 1)
                self.assertIn('RAG_RESTART_LIMIT_S', result.stdout)
                self.assertIn(f"'{value}'", result.stdout)

    def test_wait_reports_seconds_once_healthy(self):
        result = self.bash('wait_healthy_timed "$(date +%s)" 30', STUB_CURL_CODE='200',
                           RAG_API='http://localhost:9/api')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r'^[0-9]+$')
        self.assertLessEqual(int(result.stdout), 1)

    def test_wait_measures_from_the_given_start(self):
        # The time counts from `down`, not from the first poll.
        result = self.bash('wait_healthy_timed "$(( $(date +%s) - 5 ))" 30', STUB_CURL_CODE='200',
                           RAG_API='http://localhost:9/api')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(result.stdout, ('5', '6'))

    def test_wait_prints_nothing_at_the_cap(self):
        started = time.monotonic()
        result = self.bash('wait_healthy_timed "$(date +%s)" 2', STUB_CURL_CODE='000',
                           RAG_API='http://localhost:9/api')
        # A missing or failing helper also prints nothing, so check it ran and waited.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertGreaterEqual(time.monotonic() - started, 2)

    def test_results_report_the_time(self):
        cases = (('300 143', 'PASS', 'restart reaches healthy within 300s (took 143s)'),
                 ('300 300', 'PASS', 'restart reaches healthy within 300s (took 300s)'),
                 ('300 412', 'FAIL', 'took 412s'),
                 ("300 ''", 'FAIL', 'not healthy after 600s'))
        for args, verdict, text in cases:
            with self.subTest(args=args):
                result = self.bash(f'restart_timing_check {args}')
                self.assertIn(verdict, result.stdout)
                self.assertIn(text, result.stdout)


class StackArgumentTests(unittest.TestCase):
    """S7 and S8: bad arguments are refused before any Docker command."""

    def test_refusals_before_docker(self):
        cases = [
            (['up'], {'RAG_VERIFY_PORT': '8080'}),
            (['up'], {'RAG_VERIFY_PORT': 'abc'}),
            (['up'], {'RAG_VERIFY_PORT': '80'}),
            (['up'], {'RAG_VERIFY_PORT': '70000'}),
            (['run'], {'RAG_VERIFY_PORT': '08080'}),
            (['up', '--checkout', '/nonexistent'], {}),
            (['up', '--checkout'], {}),
            (['down', '--pull'], {}),
            (['run', '--pull'], {}),
            (['bogus'], {}),
            ([], {}),
        ]
        for argv, env in cases:
            with self.subTest(argv=argv, env=env):
                box = Sandbox(self)
                result = box.run(['bash', str(STACK), *argv], **env)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(box.calls(), [])


class StackDockerCallTests(unittest.TestCase):
    """S6, S12 and S17: what stack.sh asks Docker to do."""

    ALLOWED_LIVE = {'rag-docker_ollama_models', 'rag-docker_ollama_models:/live:ro'}

    def assert_no_live_object(self, calls):
        for call in calls:
            for arg in call:
                if 'rag-docker' in arg:
                    self.assertIn(arg, self.ALLOWED_LIVE, call)

    def test_down_removes_only_the_verify_project(self):
        box = Sandbox(self)
        box.exports.mkdir()
        (box.exports / 'pkg.tar.gz').write_text('x')
        result = box.run(['bash', str(STACK), 'down'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = box.calls()
        docker = [c[1:] for c in calls if c[0] == 'docker']
        self.assertEqual(docker[0], ['compose', '-p', 'rag-verify', 'down', '-v', '--remove-orphans'])
        label = 'label=com.docker.compose.project=rag-verify'
        for call in docker[1:]:
            # #154: images are listed by the project label and removed by name.
            listing = call[:2] in (['ps', '-aq'], ['network', 'ls'], ['volume', 'ls'], ['image', 'ls'])
            removal = call in (['rm', '-f', 'c1'], ['network', 'rm', 'n1'], ['volume', 'rm', 'v1'],
                               ['image', 'rm', 'rag-verify-api:latest'])
            self.assertTrue(listing or removal, call)
            if listing:
                self.assertIn(label, call)
        self.assertIn(['image', 'rm', 'rag-verify-api:latest'], docker)
        self.assertNotIn(['volume', 'rm', 'rag-verify-ollama-models'], docker)
        self.assertFalse(any('rag-verify-ollama-models' in c and c[:2] == ['volume', 'rm'] for c in docker))
        self.assertFalse(any('--rmi' in c for c in docker))
        self.assertFalse(box.exports.exists())
        self.assert_no_live_object(calls)

    def test_up_tears_down_first_seeds_read_only_and_retries_once(self):
        box = Sandbox(self)
        (box.root / 'config.json').write_text(json.dumps(passing_config(ROOT, box.exports)))
        result = box.run(['bash', str(STACK), 'up'])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        docker = [c[1:] for c in box.calls() if c[0] == 'docker']
        self.assertEqual(docker[0], ['compose', '-p', 'rag-verify', 'down', '-v', '--remove-orphans'])
        runs = [c for c in docker if c[:1] == ['run']]
        self.assertEqual(len(runs), 1)
        seed = runs[0]
        self.assertIn('--rm', seed)
        self.assertIn('none', seed[seed.index('--network') + 1:seed.index('--network') + 2])
        self.assertIn('rag-docker_ollama_models:/live:ro', seed)
        self.assertIn('rag-verify-ollama-models:/copy', seed)
        compose = [c for c in docker if c[:1] == ['compose']]
        for call in compose:
            self.assertEqual(call[1:3], ['-p', 'rag-verify'], call)
        ups = [c for c in compose if 'up' in c]
        self.assertEqual(len(ups), 2)
        for call in ups:
            self.assertEqual(call[3:], ['up', '-d', '--wait', '--wait-timeout', '900'])
        order = [next(i for i, c in enumerate(docker) if c[:1] == ['run']),
                 next(i for i, c in enumerate(docker) if 'config' in c),
                 next(i for i, c in enumerate(docker) if 'build' in c),
                 next(i for i, c in enumerate(docker) if 'up' in c)]
        self.assertEqual(order, sorted(order), 'seed, guard, build, up must run in that order')
        self.assertIn('logs', [a for c in compose for a in c])
        self.assert_no_live_object(box.calls())

    def test_guard_refusal_stops_before_build(self):
        box = Sandbox(self)
        bad = passing_config(ROOT, box.exports)
        bad['services']['api']['image'] = 'rag-docker-api:latest'
        (box.root / 'config.json').write_text(json.dumps(bad))
        result = box.run(['bash', str(STACK), 'up'])
        self.assertEqual(result.returncode, 2)
        docker = [c[1:] for c in box.calls() if c[0] == 'docker']
        self.assertFalse(any('build' in c or 'up' in c for c in docker if c[:1] == ['compose']))

    def test_down_removes_every_labelled_verify_image(self):
        # #154 (S9): every image the verify project built goes, by name,
        # whatever its service; untagged entries are left to Docker.
        box = Sandbox(self)
        result = box.run(['bash', str(STACK), 'down'],
                         STUB_IMAGES='rag-verify-api:latest\nrag-verify-proxy:latest\n<none>:<none>')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        docker = [c[1:] for c in box.calls() if c[0] == 'docker']
        removed = [a for c in docker if c[:2] == ['image', 'rm'] for a in c[2:]]
        self.assertEqual(sorted(removed), ['rag-verify-api:latest', 'rag-verify-proxy:latest'])
        self.assertFalse(any(c[:2] == ['image', 'rm'] and ('-f' in c or '--force' in c) for c in docker))
        self.assertIn('verify project rag-verify removed', result.stdout)

    def test_down_fails_on_a_labelled_image_with_another_name(self):
        # #154 (S10): a verify-built image under another name (a live tag,
        # say) is reported and left alone, and down doesn't claim success.
        box = Sandbox(self)
        result = box.run(['bash', str(STACK), 'down'],
                         STUB_IMAGES='rag-verify-api:latest\nnginx:1.29-alpine')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('nginx:1.29-alpine', result.stderr)
        self.assertNotIn('verify project rag-verify removed', result.stdout)
        docker = [c[1:] for c in box.calls() if c[0] == 'docker']
        self.assertFalse(any(c[:2] == ['image', 'rm'] and 'nginx:1.29-alpine' in c for c in docker))

    def test_down_fails_when_an_image_is_not_removed(self):
        box = Sandbox(self)
        result = box.run(['bash', str(STACK), 'down'],
                         STUB_IMAGES='rag-verify-api:latest', STUB_STUCK='rag-verify-api:latest')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('rag-verify-api:latest', result.stderr)
        self.assertNotIn('verify project rag-verify removed', result.stdout)

    @unittest.skipIf(os.geteuid() == 0, 'root can remove the locked folder')
    def test_down_fails_when_exports_can_not_be_removed(self):
        # #154 (S10, B2): a failed removal of the exports folder is reported.
        box = Sandbox(self)
        locked = box.exports / 'locked'
        locked.mkdir(parents=True)
        (locked / 'pkg.tar.gz').write_text('x')
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        result = box.run(['bash', str(STACK), 'down'])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(str(box.exports), result.stderr)
        self.assertNotIn('verify project rag-verify removed', result.stdout)

    def test_private_folder_symlink_or_file_is_refused(self):
        # #154 (S12): the folder holding the exports folder must be a real
        # folder of the user's, checked before any Docker command.
        for kind in ('symlink', 'file'):
            for cmd in ('up', 'down'):
                with self.subTest(kind=kind, cmd=cmd):
                    box = Sandbox(self)
                    box.private.rmdir()
                    if kind == 'symlink':
                        target = box.root / 'elsewhere'
                        target.mkdir()
                        box.private.symlink_to(target)
                    else:
                        box.private.write_text('x')
                    result = box.run(['bash', str(STACK), cmd])
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(str(box.private), result.stderr)
                    self.assertEqual(box.calls(), [])

    def test_up_creates_the_private_folder_mode_700(self):
        # #154 (S12): created with mode 700, or tightened to it.
        for state in ('missing', 'loose'):
            with self.subTest(state=state):
                box = Sandbox(self)
                if state == 'missing':
                    box.private.rmdir()
                else:
                    box.private.chmod(0o755)
                (box.root / 'config.json').write_text(json.dumps(passing_config(ROOT, box.exports)))
                box.run(['bash', str(STACK), 'up'])
                self.assertTrue(box.private.is_dir() and not box.private.is_symlink())
                self.assertEqual(box.private.stat().st_mode & 0o777, 0o700)

    def test_run_failed_start_says_removed(self):
        # #154 (S14): `run` tears down after a failed start, and says so.
        box = Sandbox(self)
        (box.root / 'config.json').write_text(json.dumps(passing_config(ROOT, box.exports)))
        result = box.run(['bash', str(STACK), 'run'])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn('left running', result.stderr)
        self.assertIn('removes it now', result.stderr)
        docker = [c[1:] for c in box.calls() if c[0] == 'docker']
        last_up = max(i for i, c in enumerate(docker) if c[:1] == ['compose'] and 'up' in c)
        self.assertTrue(any(c[:4] == ['compose', '-p', 'rag-verify', 'down'] for c in docker[last_up:]))

    def test_up_failed_start_is_left_running(self):
        box = Sandbox(self)
        (box.root / 'config.json').write_text(json.dumps(passing_config(ROOT, box.exports)))
        result = box.run(['bash', str(STACK), 'up'])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('left running for inspection', result.stderr)

    def test_down_refuses_a_symlinked_exports_folder(self):
        # Written by the #153 testing reviewer: do_down must not follow a
        # symlink at the exports path and delete what it points to.
        box = Sandbox(self)
        target = box.root / 'elsewhere'
        target.mkdir()
        (target / 'keep.txt').write_text('x')
        box.exports.symlink_to(target)
        result = box.run(['bash', str(STACK), 'down'])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('symlink', result.stderr)
        self.assertTrue((target / 'keep.txt').exists())
        self.assertTrue(box.exports.is_symlink())

    def test_seed_is_skipped_without_the_live_model_volume(self):
        # Written by the #153 testing reviewer.
        box = Sandbox(self)
        (box.root / 'config.json').write_text(json.dumps(passing_config(ROOT, box.exports)))
        result = box.run(['bash', str(STACK), 'up'], STUB_NO_LIVE='1')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('there is no rag-docker_ollama_models volume', result.stdout)
        docker = [c[1:] for c in box.calls() if c[0] == 'docker']
        self.assertFalse(any(c[:1] == ['run'] for c in docker), docker)
        self.assertTrue(any(c[:1] == ['compose'] and 'build' in c for c in docker),
                        'the build still runs: the entrypoint pulls the models')
        self.assert_no_live_object(box.calls())


class StackLockTests(unittest.TestCase):
    """#154 (S18, S30): the build check holds the verify lock in its own
    shell; stack.sh inherits it, doesn't release it, and a second run waits."""

    def test_init_or_zero_cannot_authorize_an_inherited_lock(self):
        for owner in ('0', '1'):
            with self.subTest(owner=owner):
                box = Sandbox(self)
                lock = Path(box.env['RAG_VERIFY_LOCK'])
                lock.mkdir()
                (lock / 'pid').write_text(owner)
                result = box.run(['bash', '-c', f'. "{VERIFY}/lock.sh"; echo forbidden'],
                                 RAG_VERIFY_LOCK_HELD='1')
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertNotIn('forbidden', result.stdout)
                self.assertEqual((lock / 'pid').read_text(), owner)

    def test_held_lock_is_inherited_not_released(self):
        box = Sandbox(self)
        lock = box.env['RAG_VERIFY_LOCK']
        script = textwrap.dedent(f"""
            . "{VERIFY}/lock.sh"
            bash "{STACK}" down >/dev/null 2>&1
            echo "down=$?"
            [ "$(cat "{lock}/pid" 2>/dev/null)" = "$$" ] && echo held-after-down
            before=$(wc -l < "{box.log}")
            env -u RAG_VERIFY_LOCK_HELD bash "{STACK}" up >/dev/null 2>&1
            echo "second=$?"
            after=$(wc -l < "{box.log}")
            [ "$before" = "$after" ] && echo no-docker-call
        """)
        result = box.run(['bash', '-c', script])
        self.assertIn('down=0', result.stdout, result.stdout + result.stderr)
        self.assertIn('held-after-down', result.stdout)
        self.assertIn('second=3', result.stdout)
        self.assertIn('no-docker-call', result.stdout)
        self.assertFalse(Path(lock).exists(), 'released when the holding shell exits')


class StackRunTeardownTests(unittest.TestCase):
    """Written by the #153 testing reviewer: `run` always tears the project
    down, after a failing all.sh and after an interrupt, and keeps all.sh's
    exit status."""

    def checkout(self, box, body):
        checkout = box.root / 'checkout'
        (checkout / 'scripts/verify').mkdir(parents=True)
        (checkout / 'docker-compose.yml').write_text('services: {}\n')
        (checkout / 'scripts/verify/all.sh').write_text(body)
        (box.root / 'config.json').write_text(json.dumps(passing_config(checkout, box.exports)))
        return checkout

    def assert_torn_down_after_up(self, box):
        docker = [c[1:] for c in box.calls() if c[0] == 'docker']
        up = max(i for i, c in enumerate(docker) if c[:1] == ['compose'] and 'up' in c)
        downs = [i for i, c in enumerate(docker)
                 if c == ['compose', '-p', 'rag-verify', 'down', '-v', '--remove-orphans']]
        self.assertTrue(any(i > up for i in downs), 'no teardown after the project came up')
        self.assertFalse(box.exports.exists())

    def test_failing_suite_is_torn_down_and_keeps_its_status(self):
        box = Sandbox(self)
        checkout = self.checkout(box, 'echo "all.sh project=$COMPOSE_PROJECT_NAME api=$RAG_API exports=$RAG_EXPORTS_DIR"\nexit 7\n')
        result = box.run(['bash', str(STACK), 'run', '--checkout', str(checkout)],
                         STUB_UP_OK='1', STUB_CURL_CODE='200')
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn('all.sh project=rag-verify api=http://localhost:8081/api', result.stdout)
        self.assertIn(f'exports={box.exports}', result.stdout)
        self.assertIn('verify project rag-verify removed', result.stdout)
        self.assert_torn_down_after_up(box)

    def test_interrupt_tears_down(self):
        box = Sandbox(self)
        marker = box.root / 'all-started'
        checkout = self.checkout(box, f'touch "{marker}"\nsleep 30\nexit 0\n')
        env = {k: v for k, v in {**box.env, 'STUB_UP_OK': '1', 'STUB_CURL_CODE': '200'}.items()}
        proc = subprocess.Popen(['bash', str(STACK), 'run', '--checkout', str(checkout)], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            for _ in range(200):
                if marker.exists():
                    break
                import time; time.sleep(0.05)
            self.assertTrue(marker.exists(), 'all.sh never started')
            os.killpg(proc.pid, signal.SIGINT)
            out, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        self.assertEqual(proc.returncode, 130, out + err)
        self.assertIn('verify project rag-verify removed', out)
        self.assert_torn_down_after_up(box)
        self.assertFalse(Path(box.env['RAG_VERIFY_LOCK']).exists(), 'the verify lock is released')


def alive(pid):
    """True while pid runs (a zombie, already dead, counts as gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith('Z')


class StackRunSuiteGroupTests(unittest.TestCase):
    """#184: `run` stops the suite's whole process group before it tears the
    project down, so no suite outlives the run that started it."""

    checkout = StackRunTeardownTests.checkout
    assert_torn_down_after_up = StackRunTeardownTests.assert_torn_down_after_up

    def wait_for(self, path):
        import time
        for _ in range(200):
            if path.exists() and path.read_text().strip():
                return int(path.read_text().split()[0])
            time.sleep(0.05)
        self.fail(f'{path} never appeared')

    def assert_gone(self, *pids):
        import time
        for _ in range(100):
            if not any(alive(p) for p in pids):
                return
            time.sleep(0.05)
        self.fail(f'still running: {[p for p in pids if alive(p)]}')

    def start(self, box, checkout):
        env = {**box.env, 'STUB_UP_OK': '1', 'STUB_CURL_CODE': '200'}
        proc = subprocess.Popen(['bash', str(STACK), 'run', '--checkout', str(checkout)], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and (os.killpg(proc.pid, signal.SIGKILL), proc.wait()))
        return proc

    def test_a_background_child_left_by_the_suite_is_stopped(self):
        box = Sandbox(self)
        child = box.root / 'child.pid'
        checkout = self.checkout(box, f'sleep 300 &\necho $! > "{child}"\nexit 0\n')
        # Registered before the run: when the group isn't stopped, box.run
        # times out on the child's inherited pipe and the child would be left.
        self.addCleanup(lambda: child.exists() and alive(int(child.read_text()))
                        and os.kill(int(child.read_text()), signal.SIGKILL))
        result = box.run(['bash', str(STACK), 'run', '--checkout', str(checkout)],
                         STUB_UP_OK='1', STUB_CURL_CODE='200')
        pid = int(child.read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_gone(pid)
        self.assert_torn_down_after_up(box)

    def test_term_to_stack_alone_stops_the_suite_at_once(self):
        box = Sandbox(self)
        pids = box.root / 'pids'
        checkout = self.checkout(box, f'sleep 300 &\necho "$$ $!" > "{pids}"\nsleep 30\nexit 0\n')
        proc = self.start(box, checkout)
        self.wait_for(pids)
        suite, child = map(int, pids.read_text().split())
        self.addCleanup(lambda: [alive(p) and os.kill(p, signal.SIGKILL) for p in (suite, child)])
        os.kill(proc.pid, signal.SIGTERM)      # stack.sh only, not its group
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 143, out + err)
        self.assert_gone(suite, child)
        self.assertIn('verify project rag-verify removed', out)
        self.assert_torn_down_after_up(box)
        self.assertFalse(Path(box.env['RAG_VERIFY_LOCK']).exists(), 'the verify lock is released')

    def test_term_between_fork_and_pid_registration_stops_the_suite(self):
        box = Sandbox(self)
        child = box.root / 'child.pid'
        checkout = self.checkout(box, f'echo $$ > "{child}"\nsleep 300\n')
        instrumented = box.root / 'stack.sh'
        source = STACK.read_text()
        source = re.sub(r'^HARNESS=.*$', f'HARNESS="{ROOT}"', source, count=1, flags=re.M)
        source = source.replace('    SUITE=$!', '    kill -TERM $$\n    SUITE=$!')
        instrumented.write_text(source)
        self.addCleanup(lambda: child.exists() and alive(int(child.read_text()))
                        and os.kill(int(child.read_text()), signal.SIGKILL))
        result = box.run(['bash', str(instrumented), 'run', '--checkout', str(checkout)],
                         STUB_UP_OK='1', STUB_CURL_CODE='200')
        self.assertEqual(result.returncode, 143, result.stdout + result.stderr)
        if child.exists(): self.assert_gone(int(child.read_text()))
        self.assert_torn_down_after_up(box)

    def test_background_output_signal_cannot_stop_suite(self):
        box = Sandbox(self)
        checkout = self.checkout(box, 'kill -TTOU $$\necho output-survived\n')
        proc = self.start(box, checkout)
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertIn('output-survived', out)
        self.assert_torn_down_after_up(box)

    def test_the_suite_reads_no_terminal(self):
        # With job control on, a background job keeps the caller's stdin. From
        # an interactive terminal the suite's first read (docker compose exec
        # -T) is then stopped by SIGTTIN and the run hangs, so stack.sh gives
        # the suite /dev/null. Data on stack.sh's stdin must not reach it.
        box = Sandbox(self)
        seen = box.root / 'stdin'
        checkout = self.checkout(box, f'if read -r line; then echo "read:$line"; else echo eof; fi > "{seen}"\n')
        env = {k: v for k, v in {**box.env, 'STUB_UP_OK': '1', 'STUB_CURL_CODE': '200'}.items() if v is not None}
        result = subprocess.run(['bash', str(STACK), 'run', '--checkout', str(checkout)], cwd=ROOT, env=env,
                                input='from-the-terminal\n', capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(seen.read_text().strip(), 'eof')

    def test_the_suite_is_stopped_before_the_teardown(self):
        box = Sandbox(self)
        started = box.root / 'started'
        checkout = self.checkout(box, textwrap.dedent(f'''\
            trap 'echo '"'"'["suite", "terminated"]'"'"' >> "$STUB_LOG"; exit 143' TERM
            echo $$ > "{started}"
            sleep 30 & wait
            '''))
        proc = self.start(box, checkout)
        self.wait_for(started)
        os.kill(proc.pid, signal.SIGTERM)
        out, err = proc.communicate(timeout=15)
        calls = box.calls()
        self.assertIn(['suite', 'terminated'], calls, out + err)
        down = ['docker', 'compose', '-p', 'rag-verify', 'down', '-v', '--remove-orphans']
        last_down = max(i for i, c in enumerate(calls) if c == down)
        self.assertLess(calls.index(['suite', 'terminated']), last_down)


class CraftedPackageWriteTests(unittest.TestCase):
    """#184: a package the host writes for the API to import is written under
    a .part name in the same folder and renamed into place, because on Docker
    Desktop the container can read a freshly closed bind-mounted file as empty."""

    def test_every_crafted_package_is_renamed_into_place(self):
        for name, var, count in (('05_transfer.sh', 'part', 6), ('retrieval_settings.py', 'part', 1),
                                 ('legacy_retrieval.py', 'partial', 1)):
            with self.subTest(file=name):
                source = (VERIFY / name).read_text()
                opened = re.findall(r"tarfile\.open\(([^,()]+), ['\"]w:gz['\"]\)", source)
                self.assertEqual(len(opened), count, opened)
                self.assertEqual(set(opened), {var}, 'written straight to the final name')
                self.assertEqual(source.count(var + '.replace('), count)


@unittest.skipUnless(shutil.which('sha256sum'), 'needs sha256sum (it runs in the ollama image)')
class ModelSyncTests(unittest.TestCase):
    """Written by the #153 testing reviewer: the SYNC script that keeps
    rag-verify-ollama-models in step with the live store copies, repairs and
    removes, and never writes to the live side."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(os.path.realpath(temp.name))
        self.live, self.copy = root / 'L', root / 'C'
        self.live.mkdir()
        self.copy.mkdir()
        blocks = re.findall(r"^SYNC='(.*?)'$", STACK.read_text(), re.S | re.M)
        self.assertEqual(len(blocks), 1, 'stack.sh must hold exactly one SYNC block')
        self.script = blocks[0].replace('/live', str(self.live)).replace('/copy', str(self.copy))
        import hashlib
        self.blob_body = b'model weights'
        digest = hashlib.sha256(self.blob_body).hexdigest()
        self.blob = f'models/blobs/sha256-{digest}'
        self.manifest = 'models/manifests/registry/library/m/latest'
        for rel, body in ((self.blob, self.blob_body), (self.manifest, b'{"layers": []}')):
            (self.live / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.live / rel).write_bytes(body)

    def sync(self):
        result = subprocess.run(['bash', '-c', self.script], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def snapshot(self, base):
        return {str(p.relative_to(base)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in sorted(base.rglob('*')) if p.is_file()}

    def assert_same_files(self):
        self.assertEqual({k: v[0] for k, v in self.snapshot(self.live).items()},
                         {k: v[0] for k, v in self.snapshot(self.copy).items()})

    def test_copy_then_unchanged(self):
        before = self.snapshot(self.live)
        self.assertIn('copied=2 repaired=0 removed=0', self.sync().stdout)
        self.assert_same_files()
        self.assertIn('copied=0 repaired=0 removed=0', self.sync().stdout)
        self.assertEqual(self.snapshot(self.live), before, 'the live store was written')

    def test_corrupt_copy_is_repaired(self):
        self.sync()
        (self.copy / self.blob).write_bytes(b'corrupted')
        self.assertIn('copied=0 repaired=1 removed=0', self.sync().stdout)
        self.assert_same_files()

    def test_extra_files_and_empty_folders_are_removed(self):
        self.sync()
        (self.copy / 'models/blobs/sha256-stale').write_bytes(b'old')
        (self.copy / 'models/empty/deeper').mkdir(parents=True)
        self.assertIn('copied=0 repaired=0 removed=1', self.sync().stdout)
        self.assert_same_files()
        self.assertFalse((self.copy / 'models/empty').exists())

    def test_symlink_in_the_copy_is_replaced_not_followed(self):
        outside = self.copy.parent / 'outside.txt'
        outside.write_bytes(b'untouched')
        (self.copy / self.manifest).parent.mkdir(parents=True)
        (self.copy / self.manifest).symlink_to(outside)
        self.assertIn('copied=2', self.sync().stdout)
        self.assertFalse((self.copy / self.manifest).is_symlink())
        self.assertEqual(outside.read_bytes(), b'untouched')
        self.assert_same_files()

    def test_a_live_blob_that_does_not_match_its_name_warns(self):
        bad = self.live / 'models/blobs/sha256-0000'
        bad.write_bytes(b'x')
        self.assertIn('does not match its name', self.sync().stderr)


if __name__ == '__main__':
    unittest.main()
