"""Batch runs: summaries for older ledger history and reviewer calibration."""
from __future__ import annotations

import difflib
import json
import os
import random
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import db, events, reviewer, sources

BATCH = 12
DIFF_CHARS = 4000


def _diff_text(prof: sources.Profile, entry: dict) -> str:
    out = []
    for d in events.ledger_diffs(prof, entry):
        out += difflib.unified_diff(d['before'].splitlines(), d['after'].splitlines(), f"a/{d['file']}", f"b/{d['file']}", lineterm='', n=2)
    text = '\n'.join(out)
    return text[:DIFF_CHARS] + ('\n…[truncated]' if len(text) > DIFF_CHARS else '')


def _summarize(prof: sources.Profile, batch: list[dict]) -> dict:
    blocks = '\n\n'.join(f"### id={e['id']} skill={e['skill']} action={e.get('action')}\n```diff\n{_diff_text(prof, e)}\n```" for e in batch)
    prompt = f"""Below are skill edits of the Hermes agent from its ledger. Write a summary for each.
Answer with a single JSON object and no text around it: {{"<id>": {{"what": "...", "where": "..."}}, ...}} — for all {len(batch)} ids.
- what: one sentence in {reviewer.language()}, up to 110 characters, past tense: what exactly was added, removed or changed, and about what. No generic phrases like "updated the skill".
- where: the file and the section inside it, e.g. "references/preview.md › Startup".

{blocks}"""
    cfg = reviewer.model_config()
    with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, encoding='utf-8') as f:
        f.write(prompt)
    try:
        proc = subprocess.run([reviewer.hermes_exe(), 'chat', '--query-file', f.name, *reviewer.HERMES_ARGS, '--provider', cfg['provider'],
                               '-m', cfg['model'], '--reasoning', cfg['reasoning'], '-t', 'todo', '--max-turns', '2'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    finally:
        os.unlink(f.name)
    text = proc.stdout
    dec = json.JSONDecoder()
    start = text.find('{')
    while start != -1:
        try:
            data, _ = dec.raw_decode(text, start)
            if isinstance(data, dict) and all(isinstance(v, dict) for v in data.values()):
                return data
        except json.JSONDecodeError:
            pass
        start = text.find('{', start + 1)
    raise reviewer.ReviewError(f'no JSON in the answer: {text[-300:]!r}')


def backfill(limit_batches: int) -> int:
    """What/where summaries for ledger entries without one (history before the reviewer, edits while the service was down); no verdict."""
    # Events with a review — live (matched) or summarized earlier — already have a summary.
    done = {tuple(e['key'].split('|', 1)) for e in events.events() if e['what'] and '|' in e['key']}
    fresh = datetime.now(timezone.utc) - timedelta(minutes=15)  # a live review may not be matched yet
    todo = []
    for prof in sources.profiles():
        todo += [(prof, e) for e in sources.ledger(prof) if e.get('actor') in events.ACTORS
                 and (prof.key, e['id']) not in done and events._ts(e['ts']) < fresh]
    todo.sort(key=lambda pe: pe[1]['ts'], reverse=True)  # newest first
    batches = []
    for prof in {p.key: p for p, _ in todo}.values():
        mine = [e for p, e in todo if p.key == prof.key]
        batches += [(prof, mine[i:i + BATCH]) for i in range(0, len(mine), BATCH)]
    batches = batches[:limit_batches]
    print(f'without summary: {len(todo)}, batches in this run: {len(batches)}', flush=True)

    def one(job):
        prof, batch = job
        try:
            data = _summarize(prof, batch)
        except Exception as e:
            print(f'{prof.key}: batch failed: {e}', flush=True)
            return 0
        n = 0
        for e in batch:
            s = data.get(e['id'])
            if s and s.get('what'):
                db.insert_review(mode='summary', profile_key=prof.key, session_id=(e.get('evidence') or {}).get('session_id'),
                                 tool='skill_manage', op={}, skill=e['skill'], ledger_id=e['id'], status='done',
                                 what=str(s['what']), where_=str(s.get('where') or ''))
                n += 1
        print(f'{prof.key}: +{n}/{len(batch)}', flush=True)
        return n

    with ThreadPoolExecutor(3) as pool:
        total = sum(pool.map(one, batches))
    print(f'done: {total}', flush=True)
    return total


def calibrate(**override) -> None:
    """Run the reviewer over the calibration set: the queue plus everything the user rated.
    Model from reviewer.yaml or flags. Decisions done on this version are skipped, failed ones rerun."""
    cfg = reviewer.model_config(**override)
    version = db.prompt_version(*reviewer.version_key(cfg))
    db.execute("delete from reviews where mode='calibration' and prompt_version=? and status!='done'", (version,))
    keys = dict.fromkeys(r['event_key'] for r in db.rows('select event_key from queue union select event_key from labels'))
    done = {f"{r['profile_key']}|{r['ledger_id']}" for r in db.reviews("mode='calibration' and prompt_version=?", (version,))}
    profs = {p.key: p for p in sources.profiles()}
    entries = {f"{pk}|{e['id']}": (prof, e) for pk, prof in profs.items() for e in sources.ledger(prof)}
    jobs = [(k, *entries[k]) for k in keys if k in entries and k not in done]
    print(f"reviewer v{version} ({cfg['provider']}/{cfg['model']} {cfg['reasoning']}): set {len(keys)}, to run {len(jobs)}", flush=True)
    from .app import review_ledger_entry

    def one(job):
        key, prof, entry = job
        try:
            got = review_ledger_entry(prof, entry, 'calibration', prompt_version=version, cfg=cfg)
            print(f"{key} {got['verdict']}", flush=True)
        except Exception as e:
            print(f'{key} error: {e}', flush=True)

    with ThreadPoolExecutor(3) as pool:
        list(pool.map(one, jobs))
    s = next(x for x in events.calibration_stats() if x['version'] == version)
    print(f"\nv{version}: decisions {s['total']}, pass {s['pass']}, reject {s['reject']}, errors {s['errors']}, "
          f"median {s['median_s']} s, p90 {s['p90_s']} s, agrees with reference {s['agree_ref']}/{s['ref_total']}")


def build_queue(size: int, seed: int = 1) -> int:
    """Calibration set: ledger edits spread across skills, profiles and actor (background/chat).
    Publisher skills are skipped: the hook blocks their edits without the reviewer."""
    publisher = set(sources.publisher_owners())
    have = {r['event_key'] for r in db.rows('select event_key from queue')}
    pool = [e for e in events.events() if e['kind'] == 'skill' and not e['key'].startswith('review|')
            and (e['skill'] or '').rsplit('/', 1)[-1] not in publisher and e['key'] not in have]
    rnd = random.Random(seed)
    groups: dict[tuple, list] = {}
    for e in pool:
        groups.setdefault((e['skill'], e['actor'], e['profile']), []).append(e)
    for g in groups.values():
        rnd.shuffle(g)
    order = list(groups)
    rnd.shuffle(order)
    picked = []
    while len(picked) < size and any(groups.values()):  # round robin: one edit per group per pass
        for k in order:
            if groups[k] and len(picked) < size:
                picked.append(groups[k].pop())
    for e in picked:
        db.execute('insert or ignore into queue values (?, ?)', (e['key'], db.now()))
    print(f'added to the calibration set: {len(picked)}', flush=True)
    return len(picked)
