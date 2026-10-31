"""HTTP service: write reviews for the hook, the window's API and the user's ratings for tuning the reviewer."""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, events, reviewer, sources

SUMMARY_EVERY = 600  # s: summarize edits that bypassed the reviewer


def _keep_summaries() -> None:
    from .batch import backfill
    while True:
        time.sleep(SUMMARY_EVERY)
        try:
            backfill(3)
        except Exception:
            logging.exception('summary backfill failed')


@asynccontextmanager
async def lifespan(_app):
    # Reviews cut off by a service stop would otherwise stay "running" forever.
    for r in db.reviews("status='running' and mode='live'"):
        db.update_review(r['id'], status='error', error='the service restarted during the review')
    threading.Thread(target=_keep_summaries, daemon=True, name='summaries').start()
    yield


app = FastAPI(title='skillwatch', lifespan=lifespan)
DIST = Path(__file__).resolve().parent.parent / 'web' / 'dist'
REVIEW_ON = os.environ.get('SKILLWATCH_REVIEW', 'on') != 'off'  # serve --no-review sets it off
REJECT_HINT = 'Do not resend this write unchanged: fix it as the note says, or drop it.'


# ---- review (called by the hook)

def _skill_names(tool_input: dict) -> list[str]:
    names = [op.get('name') or tool_input.get('name') for op in events.ops_of(tool_input)]
    return list(dict.fromkeys(n for n in names if n))


@app.post('/api/review')
def review(payload: dict) -> dict:
    host, tool = payload.get('host', sources.LOCAL_HOST), payload.get('tool_name')
    op = payload.get('tool_input') or {}
    key = f"{host}:{payload.get('profile') or 'default'}"
    names = _skill_names(op) if tool == 'skill_manage' else []
    row = dict(mode='live', profile_key=key, session_id=payload.get('session_id'), tool=tool, op=op,
               skill=names[0] if names else None, target=op.get('target') if tool == 'memory' else None)
    prof = sources.profile(key)
    if not REVIEW_ON or not prof or tool not in ('skill_manage', 'memory'):
        db.insert_review(**row, status='unreviewed', error=None if prof else f'profile {key} not found')
        return {'action': 'continue'}
    rid = db.insert_review(**row, status='running')
    try:
        context = (reviewer.memory_context(op, prof) if tool == 'memory'
                   else reviewer.skill_context(reviewer.live_files(op, prof), prof, retro=False))
        prompt = reviewer.build_prompt(tool=tool, context=context, prof=prof, session_id=row['session_id'], retro=False)
        verdict, took = reviewer.run(prompt)
    except Exception as e:  # reviewer unavailable: the write goes through unreviewed (fail-open)
        db.update_review(rid, status='error', error=str(e)[:1000])
        return {'action': 'continue'}
    db.update_review(rid, status='done', verdict=verdict['verdict'], what=verdict['what'], where_=verdict['where'],
                     note=verdict['note'], rule=verdict['rule'], duration=round(took, 1))
    if verdict['verdict'] == 'reject':
        return {'action': 'block', 'message': f"The edit reviewer rejected this write. {verdict['note']}\n"
                                              f"Rule: {verdict['rule'] or '—'}. {REJECT_HINT}"}
    return {'action': 'continue'}


def review_ledger_entry(prof: sources.Profile, entry: dict, mode: str, prompt_version: int | None = None,
                        cfg: dict | None = None) -> dict:
    """Review an already applied edit from the ledger (history and calibration)."""
    name = entry['skill'].replace('\\', '/').rsplit('/', 1)[-1]
    context = reviewer.skill_context({name: events.ledger_diffs(prof, entry)}, prof, retro=True)
    session_id = (entry.get('evidence') or {}).get('session_id')
    prompt = reviewer.build_prompt(tool='skill_manage', context=context, prof=prof, session_id=session_id, retro=True)
    rid = db.insert_review(mode=mode, profile_key=prof.key, session_id=session_id,
                           tool='skill_manage', op={}, skill=entry['skill'], ledger_id=entry['id'], status='running',
                           prompt_version=prompt_version)
    try:
        verdict, took = reviewer.run(prompt, cfg)
    except reviewer.ReviewError as e:
        db.update_review(rid, status='error', error=str(e)[:1000])
        raise
    db.update_review(rid, status='done', verdict=verdict['verdict'], what=verdict['what'], where_=verdict['where'],
                     note=verdict['note'], rule=verdict['rule'], duration=round(took, 1))
    return verdict


# ---- window API

@app.get('/api/tree')
def tree() -> dict:
    evs = events.events()
    count, rejected = Counter(), Counter()
    for e in evs:
        if e['kind'] == 'skill':
            count[e['skill']] += 1
            rejected[e['skill']] += e['verdict'] == 'reject'
    skills = [dict(s) for s in sources.skills()]
    for s in skills:
        s['changes'] = count[s['name']] + (count[s['dir']] if s['dir'] != s['name'] else 0)
        s['rejected'] = rejected[s['name']]
    profs = [{'key': p.key, 'label': p.label, 'has_memory': bool(sources.memory_files(p))} for p in sources.profiles()]
    runs, fb = _latest_runs()
    return {'skills': skills, 'profiles': profs, 'feed_total': len(evs), 'calib_todo': sum(fb.get(r['id']) is None for r in runs)}


@app.get('/api/feed')
def feed(offset: int = 0, limit: int = 60) -> dict:
    evs = events.events()
    page = events.with_stats(evs[offset:offset + limit])
    return {'total': len(evs), 'events': page,
            'summary': {'pass': sum(e['verdict'] == 'pass' for e in evs), 'reject': sum(e['verdict'] == 'reject' for e in evs),
                        'unreviewed': sum(e['status'] in ('unreviewed', 'error') for e in evs)}}


@app.get('/api/change/{key:path}')
def change(key: str) -> list[dict]:
    return events.diffs(key)


@app.get('/api/skill/{name}')
def skill(name: str) -> dict:
    copies = [s for s in sources.skills() if name in (s['name'], s['dir'])]
    history = [e for e in events.events() if e['kind'] == 'skill' and (e['skill'] or '').rsplit('/', 1)[-1] in
               {name, *(c['dir'] for c in copies), *(c['name'] for c in copies)}]
    if not copies and not history:
        raise HTTPException(404, 'skill not found')
    files = sources.skill_files(Path(copies[0]['path'])) if copies else []
    return {'name': name, 'copies': copies, 'files': files, 'history': events.with_stats(history)}


@app.get('/api/memory/{key}')
def memory(key: str) -> dict:
    prof = sources.profile(key)
    if not prof:
        raise HTTPException(404, 'profile not found')
    history = [e for e in events.events() if e['kind'] == 'memory' and e['profile'] == key]
    return {'key': key, 'label': prof.label, 'files': sources.memory_files(prof), 'limits': sources.memory_limits(prof),
            'history': events.with_stats(history)}


@app.get('/api/rules')
def rules() -> dict:
    revs = db.reviews("mode in ('live', 'retro') and status='done'")
    return {'text': reviewer.RULES.read_text(encoding='utf-8'), 'review_on': REVIEW_ON,
            'stats': {'pass': sum(r['verdict'] == 'pass' for r in revs), 'reject': sum(r['verdict'] == 'reject' for r in revs),
                      'by_rule': Counter(r['rule'] for r in revs if r['verdict'] == 'reject' and r['rule'])}}


# ---- the user's ratings (ground truth for tuning the reviewer; never touch Hermes or skills)

@app.get('/api/calibration')
def calibration(show: str = 'todo') -> dict:
    """Reviewer decisions of the latest run. show: todo — not rated yet; rated — rated; wrong — rated wrong."""
    runs, fb = _latest_runs()
    want = {'wrong': lambda v: v == 'wrong', 'rated': lambda v: v is not None}.get(show, lambda v: v is None)
    keys = {f"{r['profile_key']}|{r['ledger_id']}" for r in runs if want(fb.get(r['id']))}
    page = [e for e in events.events() if e['key'] in keys]
    return {'events': events.with_stats(page), 'todo': sum(fb.get(r['id']) is None for r in runs),
            'versions': events.calibration_stats()}


def _latest_runs() -> tuple[list[dict], dict[int, str | None]]:
    version = events.latest_version()
    runs = db.reviews("mode='calibration' and prompt_version=?", (version,)) if version else []
    return runs, {r['review_id']: r['verdict'] for r in db.rows('select review_id, verdict from review_feedback')}


class Label(BaseModel):
    verdict: Literal['ok', 'bad'] | None = None
    comment: str | None = Field(None, max_length=20000)


class Feedback(BaseModel):
    verdict: Literal['right', 'wrong'] | None = None
    comment: str | None = Field(None, max_length=20000)


@app.put('/api/label/{key:path}')
def put_label(key: str, body: Label) -> dict:
    if not any(e['key'] == key for e in events.events()):
        raise HTTPException(404, 'event not found')
    db.set_label(key, body.verdict, body.comment)
    return {'ok': True}


@app.put('/api/feedback/{review_id}')
def put_feedback(review_id: int, body: Feedback) -> dict:
    if not db.reviews("id=? and mode='calibration'", (review_id,)):
        raise HTTPException(404, 'calibration review not found')
    db.set_feedback(review_id, body.verdict, body.comment)
    return {'ok': True}


if DIST.is_dir():
    app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')

    @app.get('/{path:path}')
    def spa(path: str) -> FileResponse:
        return FileResponse(DIST / 'index.html')
