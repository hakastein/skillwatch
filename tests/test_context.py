"""Reviewer context: the file after a live edit is computed the way Hermes will write it."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skillwatch import reviewer, sources

PROF = sources.Profile('windows', 'default', Path(tempfile.gettempdir()), {})


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, text in {'a': '---\nname: a\nmetadata:\n  hermes:\n    related_skills: [b]\n---\n# A\nold line\n', 'b': '# B body\n'}.items():
            (root / name).mkdir()
            (root / name / 'SKILL.md').write_text(text, encoding='utf-8')
        skills = [{'name': n, 'dir': n, 'description': n, 'kind': 'shared', 'group': 'g', 'path': str(root / n),
                   'profiles': [{'key': PROF.key, 'state': 'on'}]} for n in ('a', 'b')]
        p = mock.patch.object(sources, 'skills', return_value=skills)
        p.start()
        self.addCleanup(p.stop)

    def test_batch_patches_apply_in_order_to_the_same_file(self):
        files = reviewer.live_files({'operations': [
            {'action': 'patch', 'name': 'a', 'old_string': 'old line', 'new_string': 'mid line'},
            {'action': 'patch', 'name': 'a', 'old_string': 'mid line', 'new_string': 'new line'}]}, PROF)
        f = files['a'][0]
        self.assertIn('old line', f['before'])
        self.assertIn('new line', f['after'])
        self.assertNotIn('op', f)

    def test_unmatched_patch_falls_back_to_the_raw_operation(self):
        op = {'action': 'patch', 'name': 'a', 'old_string': 'no such text', 'new_string': 'x'}
        f = reviewer.live_files(op, PROF)['a'][0]
        self.assertEqual((f['after'], f['op']), (None, op))
        self.assertIn('matches only fuzzily', reviewer.skill_context({'a': [f]}, PROF, retro=False))

    def test_context_carries_full_file_and_related_skill(self):
        files = reviewer.live_files({'action': 'patch', 'name': 'a', 'old_string': 'old line', 'new_string': 'new line'}, PROF)
        text = reviewer.skill_context(files, PROF, retro=False)
        self.assertIn('+new line', text)               # the diff
        self.assertIn('# A\nnew line', text)           # the whole file after the edit
        self.assertIn('# B body', text)                # a related skill from related_skills

    def test_version_depends_on_model(self):
        cfg = {'provider': 'p', 'model': 'm1', 'reasoning': 'medium', 'timeout': 1}
        self.assertNotEqual(reviewer.version_key(cfg)[0], reviewer.version_key({**cfg, 'model': 'm2'})[0])


if __name__ == '__main__':
    unittest.main()
