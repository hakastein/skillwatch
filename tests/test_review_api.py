"""The service's contract with the hook: reviewer verdict -> hook answer and a database record."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from skillwatch import app as app_mod, db, reviewer, sources

PROFILE = sources.Profile('windows', 'default', Path(tempfile.gettempdir()), {})
CALL = {'host': 'windows', 'profile': 'default', 'session_id': 's1', 'tool_name': 'skill_manage',
        'tool_input': {'action': 'patch', 'name': 'my-skill', 'old_string': 'a', 'new_string': 'b'}}


class ReviewApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / 'test.db'
        db._db = None
        self.client = TestClient(app_mod.app)
        patches = [mock.patch.object(sources, 'profile', return_value=PROFILE),
                   mock.patch.object(reviewer, 'find_skill', return_value=None),
                   mock.patch.object(reviewer, 'build_prompt', return_value='prompt')]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        db._db.close()
        db._db = None
        self.tmp.cleanup()

    def verdict(self, v):
        return mock.patch.object(reviewer, 'run', return_value=({'verdict': v, 'what': 'Added X', 'where': 'SKILL.md',
                                                                 'note': 'Wrong home', 'rule': 'Home' if v == 'reject' else ''}, 1.0))

    def test_reject_blocks_with_note_and_is_recorded(self):
        with self.verdict('reject'):
            r = self.client.post('/api/review', json=CALL).json()
        self.assertEqual(r['action'], 'block')
        self.assertIn('Wrong home', r['message'])
        row = db.reviews()[0]
        self.assertEqual((row['status'], row['verdict'], row['skill'], row['what']), ('done', 'reject', 'my-skill', 'Added X'))

    def test_pass_continues(self):
        with self.verdict('pass'):
            self.assertEqual(self.client.post('/api/review', json=CALL).json(), {'action': 'continue'})
        self.assertEqual(db.reviews()[0]['verdict'], 'pass')

    def test_reviewer_failure_fails_open_and_is_visible(self):
        with mock.patch.object(reviewer, 'run', side_effect=reviewer.ReviewError('timeout')):
            self.assertEqual(self.client.post('/api/review', json=CALL).json(), {'action': 'continue'})
        row = db.reviews()[0]
        self.assertEqual((row['status'], row['error']), ('error', 'timeout'))

    def test_review_off_records_unreviewed(self):
        with mock.patch.object(app_mod, 'REVIEW_ON', False), mock.patch.object(reviewer, 'run') as run:
            self.assertEqual(self.client.post('/api/review', json=CALL).json(), {'action': 'continue'})
        run.assert_not_called()
        self.assertEqual(db.reviews()[0]['status'], 'unreviewed')


class ParseVerdictTest(unittest.TestCase):
    def test_takes_last_verdict_object_from_noisy_output(self):
        out = 'thinking {"x": 1}\n```json\n{"verdict": "reject", "what": "w", "where": "", "note": "n {braces}", "rule": "Home"}\n```\nsession_id: s'
        self.assertEqual(reviewer.parse_verdict(out)['verdict'], 'reject')

    def test_no_verdict_is_an_error(self):
        with self.assertRaises(reviewer.ReviewError):
            reviewer.parse_verdict('{"verdict": "maybe", "what": "w"}')


if __name__ == '__main__':
    unittest.main()
