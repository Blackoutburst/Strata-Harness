from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.request import Request, urlopen

from strata_board_harness.local import providers
from strata_board_harness.local.agents import Agent
from strata_board_harness.local.proxy import RunProxy
from strata_board_harness.local.runner import build_prompt, cold_start_failure, read_events
from strata_board_harness.local.sandbox import engine_env, write_engine_config


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {
            'HARNESS_HOME': str(self.root / 'harness'), 'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_DATA_HOME': str(self.root / 'data'), 'XDG_CACHE_HOME': str(self.root / 'cache')})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))


class ProviderTests(Home):
    def test_jsonc_comments_and_trailing_commas(self):
        self.assertEqual(providers.load_jsonc('{"a": "x//y", /* c */ "b": [1, 2,], // end\n}'),
                         {'a': 'x//y', 'b': [1, 2]})

    def test_user_configured_provider_wins_over_catalogue(self):
        self.write('config/opencode/opencode.jsonc', '''{
          // a private provider OpenCode itself does not list
          "provider": {"acme": {"npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "https://llm.acme.test/v1", "apiKey": "{env:ACME_KEY}"},
            "models": {"coder": {"limit": {"context": 64000, "output": 8000}}}}}}''')
        model = providers.from_opencode('acme/coder')
        self.assertEqual((model['base_url'], model['npm'], model['key_env']),
                         ('https://llm.acme.test/v1', '@ai-sdk/openai-compatible', 'ACME_KEY'))
        self.assertEqual(model['model_settings']['limit'], {'context': 64000, 'output': 8000})

    def test_catalogue_provider_needs_a_base_url(self):
        self.write('cache/opencode/models.json', {'cloud': {'npm': '@ai-sdk/cloud', 'env': ['CLOUD_KEY'],
                                                            'models': {'m': {'name': 'M'}}}})
        with self.assertRaisesRegex(ValueError, 'base-url'):
            providers.from_opencode('cloud/m')

    def test_unknown_model_lists_known_ones(self):
        self.write('cache/opencode/models.json', {'cloud': {'api': 'https://c.test/v1', 'models': {'m': {}}}})
        with self.assertRaisesRegex(ValueError, 'Known: m'):
            providers.from_opencode('cloud/other')

    def test_explicit_endpoint_rules(self):
        self.assertEqual(providers.describe('http://127.0.0.1:8080/v1/', 'qwen')['base_url'], 'http://127.0.0.1:8080/v1')
        for bad in ('http://example.com/v1', 'ftp://x/v1', ''):
            with self.assertRaises(ValueError):
                providers.describe(bad, 'qwen')

    def test_key_sources_are_remembered_not_keys(self):
        self.write('data/opencode/auth.json', {'acme': {'type': 'api', 'key': 'stored-key'}})
        settings = providers.describe('https://llm.acme.test/v1', 'coder', key_env='ACME_KEY', opencode_auth='acme')
        with mock.patch.dict(os.environ, {'ACME_KEY': 'env-key'}), \
                mock.patch.object(providers, 'check_key', side_effect=lambda url, key: key == 'stored-key'):
            settings['key_source'] = providers.choose_key_source(settings)
            self.assertEqual(settings['key_source'], 'opencode-auth:acme')
            self.assertEqual(providers.resolve_key(settings), 'stored-key')
        self.assertNotIn('stored-key', json.dumps(settings))

    def test_keyless_local_server(self):
        settings = providers.describe('http://localhost:11434/v1', 'llama')
        with mock.patch.object(providers, 'check_key', return_value=True):
            settings['key_source'] = providers.choose_key_source(settings)
        self.assertEqual((settings['key_source'], providers.resolve_key(settings)), ('none', ''))


class SandboxTests(Home):
    def agent(self):
        return Agent.create('coder', 'Coder', providers.describe('https://llm.test/v1', 'm'), 'http://127.0.0.1:9140')

    def test_each_agent_has_its_own_opencode_home_and_no_secrets(self):
        agent = self.agent()
        env = engine_env(agent, {'PATH': '/bin', 'HOME': '/home/me', 'MISTRAL_API_KEY': 'k', 'GITHUB_TOKEN': 't',
                                 'XDG_DATA_HOME': '/shared', 'JAVA_HOME': '/jdk'})
        self.assertEqual(env['HOME'], str(agent.dir / 'engine' / 'home'))
        self.assertEqual(env['XDG_DATA_HOME'], str(agent.dir / 'engine' / 'data'))
        self.assertEqual((env['PATH'], env['JAVA_HOME']), ('/bin', '/jdk'))
        self.assertFalse({'MISTRAL_API_KEY', 'GITHUB_TOKEN'} & set(env))
        self.assertEqual(env['GIT_AUTHOR_EMAIL'], 'coder@agents.local')

    def test_engine_config_points_only_at_the_proxy(self):
        agent = self.agent()
        model = write_engine_config(agent, 'http://127.0.0.1:5000/v1', 'throwaway')
        config = json.loads((agent.dir / 'engine/config/opencode/opencode.json').read_text())
        self.assertEqual(model, 'harness/m')
        self.assertEqual(config['enabled_providers'], ['harness'])
        self.assertEqual(config['provider']['harness']['options'], {'baseURL': 'http://127.0.0.1:5000/v1',
                                                                      'apiKey': 'throwaway'})
        self.assertTrue((agent.dir / 'engine/config/opencode/plugins/workspace_guard.ts').is_file())

    def test_agents_do_not_share_directories(self):
        first = self.agent()
        second = Agent.create('other', 'Other', providers.describe('https://llm.test/v1', 'm'), 'http://127.0.0.1:9140')
        self.assertNotEqual(engine_env(first)['XDG_DATA_HOME'], engine_env(second)['XDG_DATA_HOME'])
        with self.assertRaises(ValueError):
            Agent.create('Bad Name', 'x', {}, 'http://127.0.0.1:9140')


class ProxyTests(unittest.TestCase):
    def setUp(self):
        seen = self.seen = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                seen.append((self.path, dict(self.headers), body))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proxy = RunProxy(f'http://127.0.0.1:{self.server.server_port}/v1', 'real-key',
                              Path(self.tmp.name) / 'proxy.jsonl')
        self.url = self.proxy.start()
        self.addCleanup(self.proxy.close)

    def post(self, path, headers):
        request = Request(self.url + path, data=b'{"model": "m"}', headers={'Content-Type': 'application/json', **headers})
        with urlopen(request, timeout=10) as response:
            return response.status, response.read()

    def test_token_is_swapped_in_the_header_the_sdk_used(self):
        self.assertEqual(self.post('/chat/completions', {'Authorization': 'Bearer ' + self.proxy.token}),
                         (200, b'{"ok": true}'))
        self.post('/messages', {'x-api-key': self.proxy.token, 'anthropic-version': '2023-06-01'})
        (path1, headers1, _), (path2, headers2, _) = self.seen
        self.assertEqual((path1, headers1['Authorization']), ('/v1/chat/completions', 'Bearer real-key'))
        self.assertEqual((path2, headers2['X-Api-Key']), ('/v1/messages', 'real-key'))
        log = (Path(self.tmp.name) / 'proxy.jsonl').read_text()
        self.assertNotIn('real-key', log)
        self.assertNotIn(self.proxy.token, log)

    def test_requests_without_the_run_token_are_refused(self):
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as caught:
            self.post('/chat/completions', {'Authorization': 'Bearer guess'})
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(self.seen, [])


class RunnerTests(unittest.TestCase):
    def test_events_give_report_tokens_and_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            path.write_text('\n'.join(json.dumps(e) for e in [
                {'type': 'text', 'part': {'text': 'thinking aloud'}},
                {'type': 'step_finish', 'part': {'tokens': {'input': 10, 'output': 2, 'cache': {'read': 4}}, 'cost': 0.01}},
                {'type': 'text', 'part': {'text': 'RESULT: done\nAdded it.'}},
                {'type': 'step_finish', 'part': {'tokens': {'input': 5, 'output': 1}, 'cost': 0.002}}]) + '\nnot json\n')
            events = read_events(path)
        self.assertEqual(events['report'], 'RESULT: done\nAdded it.')
        self.assertEqual(events['tokens'], {'input': 15, 'output': 3, 'reasoning': 0, 'cache_read': 4})
        self.assertEqual(events['cost_usd'], 0.012)
        self.assertFalse(cold_start_failure(1, False, events))
        self.assertTrue(cold_start_failure(1, False, {'tokens': {'input': 0, 'output': 0}}))

    def test_prompt_carries_task_project_feedback_and_rules(self):
        agent = mock.Mock(name='agent')
        agent.name, agent.config = 'coder', {'display_name': 'Coder', 'instructions': 'Prefer small commits.'}
        prompt = build_prompt(agent, {'name': 'App', 'context': 'Python only'},
                              {'number': 3, 'title': 'Fix bug', 'description': 'Crash on empty input',
                               'acceptance_criteria': 'No crash'}, 'agent/coder/3-fix-bug',
                              ['- Manager: please add a test'])
        for expected in ('# Task #3: Fix bug', 'No crash', 'Python only', 'please add a test',
                         'Prefer small commits.', 'agent/coder/3-fix-bug', 'RESULT: done', 'Never weaken'):
            self.assertIn(expected, prompt)


if __name__ == '__main__':
    unittest.main()
