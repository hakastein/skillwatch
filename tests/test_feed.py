"""The feed: matching live reviews to Hermes ledger entries."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from skillwatch import db, events, sources

PROF = sources.Profile('windows', 'default', Path(tempfile.gettempdir()), {})
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def at(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


def entry(id_, skill, minutes, session='s1'):
    return {'id': id_, 'ts': at(minutes), 'actor': 'curator', 'action': 'patch', 'skill': skill,
            'evidence': {'session_id': session}, 'before': [], 'after': []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / 'test.db'
        db._db = None
        self.ledger = []
        for p in (mock.patch.object(sources, 'profiles', return_value=[PROF]),
                  mock.patch.object(sources, 'ledger', side_effect=lambda prof: self.ledger),
                  mock.patch.object(sources, 'session_titles', return_value={})):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        db._db.close()
        db._db = None
        self.tmp.cleanup()

    def live(self, minutes, op, verdict='pass', tool='skill_manage', what='w'):
        return db.insert_review(ts=at(minutes), mode='live', profile_key=PROF.key, session_id='s1', tool=tool, op=op,
                                skill=op.get('name'), status='done', verdict=verdict, what=what)

    def by_key(self):
        return {e['key']: e for e in events.events()}


class FeedMatchingTest(Base):
    def test_batch_review_covers_every_skill_it_wrote(self):
        rid = self.live(0, {'operations': [{'action': 'patch', 'name': 'a'}, {'action': 'patch', 'name': 'b'}]})
        self.ledger = [entry('L1', 'a', 1), entry('L2', 'b', 1)]
        evs = self.by_key()
        self.assertEqual(evs['windows:default|L1']['review_id'], rid)
        self.assertEqual(evs['windows:default|L2']['review_id'], rid)

    def test_one_review_is_not_reused_for_a_later_write_of_the_same_skill(self):
        first = self.live(0, {'action': 'patch', 'name': 'a'})
        second = self.live(2, {'action': 'patch', 'name': 'a'})
        self.ledger = [entry('L1', 'a', 1), entry('L2', 'a', 3)]
        evs = self.by_key()
        self.assertEqual((evs['windows:default|L1']['review_id'], evs['windows:default|L2']['review_id']), (first, second))

    def test_later_summary_does_not_hide_the_live_verdict(self):
        rid = self.live(0, {'action': 'patch', 'name': 'a'}, verdict='reject', what='rejected')
        db.update_review(rid, ledger_id='L1')
        db.insert_review(ts=at(30), mode='summary', profile_key=PROF.key, session_id='s1', tool='skill_manage', op={},
                         skill='a', ledger_id='L1', status='done', what='summary')
        self.ledger = [entry('L1', 'a', 1)]
        self.assertEqual(self.by_key()['windows:default|L1']['verdict'], 'reject')

    def test_passed_write_that_never_reached_the_ledger_is_shown_as_not_applied(self):
        rid = self.live(0, {'action': 'patch', 'name': 'a'})
        e = self.by_key()[f'review|{rid}']
        self.assertEqual((e['verdict'], e['applied']), ('pass', False))

    def test_rejected_attempt_is_in_the_feed(self):
        rid = self.live(0, {'action': 'add', 'target': 'user', 'content': 'x'}, verdict='reject', tool='memory')
        e = self.by_key()[f'review|{rid}']
        self.assertEqual((e['kind'], e['verdict'], e['applied']), ('memory', 'reject', False))


class CalibrationTest(Base):
    """The user's ratings -> comparison with the reviewer per version."""

    def calib(self, lid, verdict, version, status='done'):
        return db.insert_review(mode='calibration', profile_key=PROF.key, session_id='s1', tool='skill_manage', op={},
                                skill='a', ledger_id=lid, status=status, verdict=verdict, prompt_version=version)

    def test_stats_count_decisions_and_user_verdicts_per_version(self):
        v1 = db.prompt_version('rules v1')
        ids = [self.calib(f'L{i}', got, v1, status='done' if got else 'error') for i, got in enumerate(['pass', 'reject', 'reject', None])]
        db.set_feedback(ids[0], 'right', None)
        db.set_feedback(ids[1], 'wrong', 'a lesson, not a duplicate')
        db.set_feedback(ids[2], None, 'comment only')
        v2 = db.prompt_version('rules v2')
        self.calib('L0', 'pass', v2)
        stats = {s['version']: s for s in events.calibration_stats()}
        self.assertEqual({k: stats[v1][k] for k in ('total', 'pass', 'reject', 'errors', 'right', 'wrong')},
                         {'total': 4, 'pass': 1, 'reject': 2, 'errors': 1, 'right': 1, 'wrong': 1})
        self.assertEqual((stats[v2]['total'], stats[v2]['right']), (1, 0))

    def test_same_rules_text_is_the_same_version(self):
        self.assertEqual(db.prompt_version('x'), db.prompt_version('x'))
        self.assertNotEqual(db.prompt_version('x'), db.prompt_version('y'))

    def test_event_carries_latest_run_with_its_feedback(self):
        self.ledger = [entry('L1', 'a', 1)]
        self.calib('L1', 'pass', db.prompt_version('v1'))
        rid = self.calib('L1', 'reject', db.prompt_version('v2'))
        db.set_feedback(rid, 'wrong', 'no-op, but for another reason')
        e = events.annotate([self.by_key()['windows:default|L1']])[0]
        self.assertEqual((e['calib']['verdict'], e['calib']['feedback']), ('reject', {'verdict': 'wrong', 'comment': 'no-op, but for another reason'}))

    def test_calibration_tabs_split_by_user_verdict(self):
        from fastapi.testclient import TestClient
        from skillwatch import app as app_mod
        self.ledger = [entry('L1', 'a', 1), entry('L2', 'a', 2)]
        v = db.prompt_version('v1')
        r1, _ = self.calib('L1', 'pass', v), self.calib('L2', 'reject', v)
        client = TestClient(app_mod.app)
        self.assertEqual(client.put(f'/api/feedback/{r1}', json={'verdict': 'wrong', 'comment': 'x'}).status_code, 200)
        self.assertEqual(client.put('/api/feedback/999', json={'verdict': 'right'}).status_code, 404)
        keys = lambda show: [e['key'] for e in client.get(f'/api/calibration?show={show}').json()['events']]  # noqa: E731
        self.assertEqual((keys('todo'), keys('wrong'), keys('rated')),
                         (['windows:default|L2'], ['windows:default|L1'], ['windows:default|L1']))

    def test_clearing_verdict_and_comment_removes_the_label(self):
        db.set_label('k', 'ok', 'c')
        db.set_label('k', None, '')
        self.assertEqual(db.rows('select * from labels'), [])

    def test_label_api_rejects_unknown_event_and_bad_verdict(self):
        from fastapi.testclient import TestClient
        from skillwatch import app as app_mod
        self.ledger = [entry('L1', 'a', 1)]
        client = TestClient(app_mod.app)
        self.assertEqual(client.put('/api/label/windows:default|nope', json={'verdict': 'ok'}).status_code, 404)
        self.assertEqual(client.put('/api/label/windows:default|L1', json={'verdict': 'maybe'}).status_code, 422)
        self.assertEqual(client.put('/api/label/windows:default|L1', json={'verdict': 'bad', 'comment': 'x'}).status_code, 200)
        self.assertEqual(db.rows('select verdict, comment from labels'), [{'verdict': 'bad', 'comment': 'x'}])


if __name__ == '__main__':
    unittest.main()
