"""SQLite — the service's only state of its own: reviews, the user's ratings, the rating queue and reviewer versions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / 'data' / 'skillwatch.db'
_lock = threading.Lock()

SCHEMA = """
create table if not exists reviews (
  id integer primary key,
  ts text not null,              -- UTC ISO, review start
  mode text not null,            -- live | retro | summary | calibration
  profile_key text not null,     -- windows:default, wsl:work ...
  session_id text,
  tool text not null,            -- skill_manage | memory
  op text not null,              -- JSON of the call arguments (live) or {} (from the ledger)
  skill text,                    -- skill name; null for memory
  target text,                   -- memory | user for memory writes
  ledger_id text,                -- ledger entry, when the review was made from one
  status text not null,          -- running | done | error | unreviewed
  verdict text,                  -- pass | reject
  what text, where_ text, note text, rule text,
  error text, duration real,
  prompt_version integer         -- calibration: reviewer version
);
create index if not exists reviews_ledger on reviews(profile_key, ledger_id);
create index if not exists reviews_skill on reviews(skill);

-- The user's ratings of edits.
create table if not exists labels (
  event_key text primary key,    -- <profile>|<ledger id> or review|<id>
  verdict text,                  -- ok | bad | null (comment only)
  comment text,
  ts text not null
);
-- The user's rating of a reviewer decision in a calibration run.
create table if not exists review_feedback (
  review_id integer primary key,
  verdict text,                  -- right | wrong | null (comment only)
  comment text,
  ts text not null
);
-- The calibration set.
create table if not exists queue (
  event_key text primary key,
  added text not null
);
-- Reviewer versions (rules + model + context layout) that calibration runs used.
create table if not exists prompt_versions (
  id integer primary key,
  ts text not null,
  sha text not null unique,
  rules text not null,           -- the full version key
  config text                    -- JSON: provider, model, reasoning, context
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


_db = None


def db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = connect()
    return _db


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_review(**fields) -> int:
    fields.setdefault('ts', now())
    if not isinstance(fields.get('op'), str):
        fields['op'] = json.dumps(fields.get('op') or {}, ensure_ascii=False)
    cols = ', '.join(fields)
    with _lock, db():
        cur = db().execute(f'insert into reviews ({cols}) values ({", ".join("?" * len(fields))})', tuple(fields.values()))
        return cur.lastrowid or 0


def update_review(review_id: int, **fields) -> None:
    sets = ', '.join(f'{k}=?' for k in fields)
    with _lock, db():
        db().execute(f'update reviews set {sets} where id=?', (*fields.values(), review_id))


def reviews(where: str = '1=1', args: tuple = ()) -> list[dict]:
    with _lock:
        rows = db().execute(f'select * from reviews where {where} order by ts', args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d['op'] = json.loads(d['op'] or '{}')
        out.append(d)
    return out


def rows(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in db().execute(sql, args).fetchall()]


def execute(sql: str, args: tuple = ()) -> None:
    with _lock, db():
        db().execute(sql, args)


def set_label(key: str, verdict: str | None, comment: str | None) -> None:
    if verdict is None and not comment:
        execute('delete from labels where event_key=?', (key,))
    else:
        execute('insert into labels values (?, ?, ?, ?) on conflict(event_key) do update set '
                'verdict=excluded.verdict, comment=excluded.comment, ts=excluded.ts', (key, verdict, comment or None, now()))


def set_feedback(review_id: int, verdict: str | None, comment: str | None) -> None:
    if verdict is None and not comment:
        execute('delete from review_feedback where review_id=?', (review_id,))
    else:
        execute('insert into review_feedback values (?, ?, ?, ?) on conflict(review_id) do update set '
                'verdict=excluded.verdict, comment=excluded.comment, ts=excluded.ts', (review_id, verdict, comment or None, now()))


def prompt_version(key: str, config: dict | None = None) -> int:
    """Reviewer version number (rules + model + context layout); a new one is created on its first run."""
    sha = hashlib.sha256(key.encode('utf-8')).hexdigest()
    with _lock, db():
        db().execute('insert or ignore into prompt_versions (ts, sha, rules, config) values (?, ?, ?, ?)',
                     (now(), sha, key, json.dumps(config or {}, ensure_ascii=False)))
        return db().execute('select id from prompt_versions where sha=?', (sha,)).fetchone()[0]
