"""The feed: edit ledger entries of every profile, joined with review results."""
from __future__ import annotations

import difflib
import json
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from . import db, sources

ACTORS = ('agent', 'curator')  # the user's own edits (actor=user) stay out of the feed
MATCH_WINDOW = timedelta(minutes=10)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def ops_of(tool_input: dict) -> list[dict]:
    ops = tool_input.get('operations')
    return [o for o in ops if isinstance(o, dict)] if isinstance(ops, list) else [tool_input]


def _read(path) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return ''


def op_diffs(review: dict) -> list[dict]:
    """Before/after from the call arguments — for writes that never reached the ledger (rejected)."""
    op_all = review['op']
    if review['tool'] == 'memory':
        name = 'USER.md' if op_all.get('target') == 'user' else 'MEMORY.md'
        out = []
        for op in ops_of(op_all):
            new = op.get('content') or op.get('new_text') or ''
            old = op.get('old_text') or ''
            act = op.get('action')
            out.append({'file': name, 'before': '' if act == 'add' else old, 'after': '' if act == 'remove' else new})
        return out
    prof = sources.profile(review['profile_key'])
    out = []
    for op in ops_of(op_all):
        name = op.get('name') or op_all.get('name') or review['skill']
        sk = next((s for s in sources.skills() if name in (s['name'], s['dir'])), None) if prof else None
        base = Path(sk['path']) if sk else None
        act = op.get('action') or op_all.get('action')
        rel = op.get('file_path') or 'SKILL.md'
        current = _read(base / rel) if base else ''
        if act == 'patch' and op.get('old_string') is not None:
            out.append({'file': rel, 'before': op.get('old_string') or '', 'after': op.get('new_string') or ''})
        elif act in ('patch', 'edit', 'create'):
            out.append({'file': 'SKILL.md', 'before': current if act != 'create' else '', 'after': op.get('content') or ''})
        elif act == 'write_file':
            out.append({'file': rel, 'before': current, 'after': op.get('file_content') or ''})
        elif act in ('remove_file', 'delete'):
            out.append({'file': rel if act == 'remove_file' else 'SKILL.md', 'before': current, 'after': ''})
    return out


def ledger_diffs(prof: sources.Profile, entry: dict) -> list[dict]:
    before = {x['path']: x.get('sha256') for x in entry.get('before') or []}
    after = {x['path']: x.get('sha256') for x in entry.get('after') or []}
    skill_dir = '/' + entry['skill'].replace('\\', '/').rsplit('/', 1)[-1] + '/'
    out = []
    for path in sorted(set(before) | set(after)):
        b, a = sources.blob(prof, before.get(path)), sources.blob(prof, after.get(path))
        p = path.replace('\\', '/')
        rel = p.rsplit(skill_dir, 1)[1] if skill_dir in p else sources.short_path(path)  # path inside the skill
        out.append({'file': rel, 'before': (b or '').replace('\r\n', '\n'),
                    'after': (a or '').replace('\r\n', '\n'), 'missing': b is None or a is None})
    return out


@lru_cache(maxsize=4096)
def _stats(key: str) -> tuple[int, int, tuple[str, ...]]:
    add = rem = 0
    ds = diffs(key)
    for d in ds:
        for line in difflib.unified_diff(d['before'].splitlines(), d['after'].splitlines(), lineterm='', n=0):
            if line.startswith('+') and not line.startswith('+++'):
                add += 1
            elif line.startswith('-') and not line.startswith('---'):
                rem += 1
    return add, rem, tuple(sorted({d['file'] for d in ds}))


def _review_fields(r: dict | None) -> dict:
    if not r:
        return {'status': 'unreviewed', 'verdict': None, 'what': None, 'where': None, 'note': None, 'rule': None, 'review_id': None}
    return {'status': r['status'], 'verdict': r['verdict'], 'what': r['what'], 'where': r['where_'],
            'note': r['note'] or r['error'], 'rule': r['rule'], 'review_id': r['id']}


def _names(r: dict) -> list[str]:
    """Skills a skill_manage call touches: a batch may have several."""
    names = [op.get('name') or r['op'].get('name') for op in ops_of(r['op'])] if r['op'] else []
    return [n.replace('\\', '/').rsplit('/', 1)[-1] for n in names if n] or [r['skill'] or '']


def events() -> list[dict]:
    """Read-only: live reviews are matched to ledger entries on every call, nothing is written back."""
    profs = {p.key: p for p in sources.profiles()}
    revs = db.reviews("mode in ('live', 'retro', 'summary')")
    # A real review wins over a verdict-less summary, even when the summary was written later.
    by_ledger: dict = {}
    for r in revs:
        k = (r['profile_key'], r['ledger_id'])
        if r['ledger_id'] and (k not in by_ledger or r['mode'] != 'summary' or by_ledger[k]['mode'] == 'summary'):
            by_ledger[k] = r
    # Unused (review, skill) pairs: each ledger entry takes one.
    pending = [(r, n) for r in revs if r['mode'] == 'live' and not r['ledger_id'] and r['tool'] == 'skill_manage'
               and r['verdict'] != 'reject' for n in _names(r)]
    matched: set[int] = set()
    out = []
    for prof in profs.values():
        for e in sources.ledger(prof):
            if e.get('actor') not in ACTORS:
                continue
            sid = (e.get('evidence') or {}).get('session_id')
            ets, skill = _ts(e['ts']), e['skill'].replace('\\', '/').rsplit('/', 1)[-1]
            pair = next((p for p in pending if p[0]['profile_key'] == prof.key and p[0]['session_id'] == sid and p[1] == skill
                         and timedelta(0) <= ets - _ts(p[0]['ts']) <= MATCH_WINDOW), None)
            if pair:
                pending.remove(pair)
                matched.add(pair[0]['id'])
            r = pair[0] if pair else by_ledger.get((prof.key, e['id']))
            out.append({'key': f"{prof.key}|{e['id']}", 'ts': e['ts'], 'kind': 'skill', 'skill': e['skill'],
                        'profile': prof.key, 'profile_label': prof.label, 'actor': e['actor'], 'action': e.get('action'),
                        'session_id': sid, 'session_title': None, 'applied': True, **_review_fields(r)})
    now = datetime.now().astimezone()
    for r in revs:
        if r['ledger_id'] or r['mode'] != 'live' or r['id'] in matched:
            continue
        if r['tool'] == 'skill_manage' and r['verdict'] != 'reject' and now - _ts(r['ts']) <= MATCH_WINDOW:
            continue  # still waiting for its ledger entry
        prof = profs.get(r['profile_key'])
        # skill_manage without a ledger entry: rejected by the reviewer, or never written after the review.
        applied = r['verdict'] != 'reject' and r['tool'] == 'memory'
        out.append({'key': f"review|{r['id']}", 'ts': r['ts'], 'kind': 'memory' if r['tool'] == 'memory' else 'skill',
                    'skill': r['skill'], 'memory_target': r['target'], 'profile': r['profile_key'],
                    'profile_label': prof.label if prof else r['profile_key'], 'actor': None, 'action': None,
                    'session_id': r['session_id'], 'session_title': None,
                    'applied': applied, **_review_fields(r)})
    out.sort(key=lambda x: x['ts'], reverse=True)
    for key, prof in profs.items():
        mine = [e for e in out if e['profile'] == key]
        titles = sources.session_titles(prof, {e['session_id'] for e in mine if e['session_id']})
        for e in mine:
            e['session_title'] = titles.get(e['session_id'])
    return out


def diffs(key: str) -> list[dict]:
    left, right = key.split('|', 1)
    if left == 'review':
        rows = db.reviews('id=?', (int(right),))
        return op_diffs(rows[0]) if rows else []
    prof = sources.profile(left)
    entry = next((e for e in sources.ledger(prof) if e['id'] == right), None) if prof else None
    return ledger_diffs(prof, entry) if prof and entry else []


def with_stats(evs: list[dict]) -> list[dict]:
    for e in evs:
        e['add'], e['rem'], files = _stats(e['key'])
        e['files'] = list(files)
    return annotate(evs)


def latest_version() -> int | None:
    row = db.rows('select max(id) as v from prompt_versions')
    return row[0]['v'] if row else None


def annotate(evs: list[dict]) -> list[dict]:
    """The user's rating of an edit, and the reviewer decision of the latest calibration run with its rating."""
    labels = {r['event_key']: r for r in db.rows('select * from labels')}
    version = latest_version()
    calib = {f"{r['profile_key']}|{r['ledger_id']}": r for r in
             db.reviews("mode='calibration' and prompt_version=?", (version,))} if version else {}
    feedback = {r['review_id']: r for r in db.rows('select * from review_feedback')}
    for e in evs:
        lb = labels.get(e['key'])
        e['label'] = {'verdict': lb['verdict'], 'comment': lb['comment']} if lb else None
        c = calib.get(e['key'])
        fb = feedback.get(c['id']) if c else None
        e['calib'] = {'review_id': c['id'], 'version': version, 'status': c['status'], 'verdict': c['verdict'],
                      'what': c['what'], 'note': c['note'] or c['error'], 'rule': c['rule'],
                      'feedback': {'verdict': fb['verdict'], 'comment': fb['comment']} if fb else None} if c else None
    return evs


def calibration_stats() -> list[dict]:
    """Per reviewer version: decisions, timing, agreement with the reference version and the user's ratings of decisions."""
    from . import reviewer
    feedback = {r['review_id']: r['verdict'] for r in db.rows('select * from review_feedback')}
    ref_id = (reviewer.config().get('calibration') or {}).get('reference_version')
    ref = {r['ledger_id']: r['verdict'] for r in db.reviews("mode='calibration' and status='done' and prompt_version=?", (ref_id,))}
    out = []
    for v in db.rows('select id, ts, config from prompt_versions order by id desc'):
        runs = db.reviews("mode='calibration' and prompt_version=?", (v['id'],))
        done = [r for r in runs if r['status'] == 'done']
        durs = sorted(r['duration'] for r in done if r['duration'])
        shared = [r for r in done if r['ledger_id'] in ref]
        out.append({'version': v['id'], 'ts': v['ts'], 'config': json.loads(v['config'] or '{}'), 'reference': v['id'] == ref_id,
                    'total': len(runs), 'pass': sum(r['verdict'] == 'pass' for r in runs),
                    'reject': sum(r['verdict'] == 'reject' for r in runs), 'errors': sum(r['status'] == 'error' for r in runs),
                    'median_s': round(durs[len(durs) // 2]) if durs else None,
                    'p90_s': round(durs[min(len(durs) - 1, int(len(durs) * .9))]) if durs else None,
                    'agree_ref': sum(r['verdict'] == ref[r['ledger_id']] for r in shared), 'ref_total': len(shared),
                    'right': sum(feedback.get(r['id']) == 'right' for r in runs),
                    'wrong': sum(feedback.get(r['id']) == 'wrong' for r in runs)})
    return out
