"""The hook at its real boundary: JSON on stdin, a verdict on stdout, the service is a fake HTTP server."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / 'hook' / 'skillwatch-guard.py'


class FakeService(BaseHTTPRequestHandler):
    answer = {'action': 'continue'}
    seen = []

    def do_POST(self):
        FakeService.seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        body = json.dumps(FakeService.answer).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class GuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), FakeService)
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.publisher = Path(cls.tmp.name) / 'skills'
        (cls.publisher / 'grilling').mkdir(parents=True)
        (cls.publisher / 'grilling' / 'SKILL.md').write_text('---\nname: grilling\n---\n', encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        FakeService.seen.clear()
        FakeService.answer = {'action': 'continue'}

    def run_hook(self, tool, args, env=None, url='http://127.0.0.1:9', session='s1'):
        payload = {'hook_event_name': 'pre_tool_call', 'tool_name': tool, 'tool_input': args,
                   'session_id': session, 'cwd': str(Path.cwd()), 'profile': 'default'}
        env = {**os.environ, 'HERMES_HOME': os.devnull, 'SKILLWATCH_URL': url,
               'SKILLWATCH_PUBLISHER_DIRS': str(self.publisher), **(env or {})}
        proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                              env=env, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def test_publisher_skill_blocked_without_review(self):
        r = self.run_hook('skill_manage', {'operations': [{'action': 'patch', 'name': 'my-own'}, {'action': 'patch', 'name': 'grilling'}]}, url=self.url)
        self.assertEqual(r['action'], 'block')
        self.assertIn('grilling', r['message'])
        self.assertEqual(FakeService.seen, [])

    def test_publisher_file_write_blocked(self):
        target = self.publisher / 'grilling' / 'SKILL.md'
        self.assertEqual(self.run_hook('write_file', {'path': str(target)})['action'], 'block')
        patch = f'*** Begin Patch\n*** Update File: {target}\n@@\n-a\n+b\n*** End Patch'
        self.assertEqual(self.run_hook('patch', {'mode': 'patch', 'patch': patch})['action'], 'block')

    def test_ordinary_file_write_passes_without_review(self):
        other = str(Path(self.tmp.name) / 'elsewhere' / 'x.txt')
        self.assertEqual(self.run_hook('write_file', {'path': other}, url=self.url)['action'], 'continue')
        self.assertEqual(FakeService.seen, [])

    def test_reviewer_session_cannot_write(self):
        with tempfile.TemporaryDirectory() as home:
            db = sqlite3.connect(Path(home) / 'state.db')
            db.execute('create table sessions (id text primary key, source text)')
            db.executemany('insert into sessions values (?, ?)', [('s1', 'tool'), ('s2', 'desktop')])
            db.commit()
            db.close()
            env = {'HERMES_HOME': home}
            for tool in ('skill_manage', 'memory', 'write_file', 'patch'):
                self.assertEqual(self.run_hook(tool, {'name': 'x'}, env=env, url=self.url)['action'], 'block', tool)
            self.assertEqual(self.run_hook('read_file', {'path': 'x'}, env=env)['action'], 'continue')
            self.assertEqual(FakeService.seen, [])
            other = str(Path(home) / 'x.txt')
            self.assertEqual(self.run_hook('write_file', {'path': other}, env=env, session='s2')['action'], 'continue')

    def test_service_verdict_is_returned(self):
        FakeService.answer = {'action': 'block', 'message': 'no'}
        r = self.run_hook('memory', {'action': 'add', 'target': 'user', 'content': 'x'}, url=self.url)
        self.assertEqual(r, {'action': 'block', 'message': 'no'})
        self.assertEqual(FakeService.seen[0]['tool_input']['content'], 'x')
        self.assertIn(FakeService.seen[0]['host'], ('windows', 'wsl', 'linux'))

    def test_service_down_fails_open(self):
        self.assertEqual(self.run_hook('skill_manage', {'action': 'patch', 'name': 'my-own'})['action'], 'continue')


if __name__ == '__main__':
    unittest.main()
