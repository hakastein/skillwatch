"""Where Hermes keeps profiles, skills, edit ledgers and memory — locally and, on Windows, inside WSL."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from functools import lru_cache, wraps
from pathlib import Path

import yaml

WINDOWS = os.name == 'nt'
LOCAL_HOST = 'windows' if WINDOWS else 'linux'
LOCAL_HOME = Path(os.environ.get('HERMES_HOME') or (
    Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'hermes' if WINDOWS else Path.home() / '.hermes'))
PUBLISHER_ROOT = Path.home() / '.agents' / 'skills'
PUBLISHER_LOCK = Path.home() / '.agents' / '.skill-lock.json'
HOST_LABELS = {'windows': 'Windows', 'linux': 'Linux', 'wsl': 'WSL'}


def ttl_cache(seconds: float):
    """Argument-less cache for `seconds`: configs and directories over UNC are slow to read and rarely change."""
    def deco(fn):
        state = {'at': 0.0, 'value': None}

        @wraps(fn)
        def wrapper():
            if time.monotonic() - state['at'] > seconds:
                state['value'], state['at'] = fn(), time.monotonic()
            return state['value']
        return wrapper
    return deco


@lru_cache(maxsize=1)
def wsl_home() -> Path | None:
    """UNC path to ~/.hermes of the default WSL distro; None when not on Windows or WSL is absent."""
    if not WINDOWS:
        return None
    try:
        distro = subprocess.run(['wsl.exe', '-l', '-q'], capture_output=True, timeout=15).stdout.decode('utf-16-le').split()[0].strip('\x00')
        home = subprocess.run(['wsl.exe', '-d', distro, '-e', 'sh', '-c', 'printf %s "$HOME"'], capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, IndexError, subprocess.SubprocessError):
        return None
    path = Path(rf'\\wsl.localhost\{distro}' + home.replace('/', '\\')) / '.hermes'
    return path if home and path.exists() else None


def to_local(path: str, host: str) -> Path:
    """A path from a host's config or ledger -> a path readable by this process."""
    m = re.match(r'^/mnt/([a-zA-Z])/(.*)$', path)
    if m and WINDOWS:
        return Path(f'{m[1].upper()}:/{m[2]}')
    if host == 'wsl' and path.startswith('/') and (home := wsl_home()):
        unc = str(home.parent.parent.parent)  # \\wsl.localhost\<distro>
        return Path(unc + path.replace('/', '\\'))
    return Path(os.path.expanduser(path))


@dataclass
class Profile:
    host: str          # windows | linux | wsl
    name: str          # default | <profile>
    home: Path         # the profile's HERMES_HOME, readable by this process
    config: dict = field(repr=False, default_factory=dict)

    @property
    def key(self) -> str:
        return f'{self.host}:{self.name}'

    @property
    def label(self) -> str:
        return f'{HOST_LABELS.get(self.host, self.host)} · {self.name}'

    @property
    def skill_roots(self) -> list[Path]:
        sk = self.config.get('skills') or {}
        return [self.home / 'skills'] + [to_local(str(d), self.host) for d in sk.get('external_dirs') or []]

    @property
    def disabled(self) -> set[str]:
        return set((self.config.get('skills') or {}).get('disabled') or [])

    @property
    def ledger(self) -> Path:
        return self.home / 'skills' / '.curator_ledger.jsonl'

    @property
    def blobs(self) -> Path:
        return self.home / '.curator_backups' / 'blobs'

    @property
    def state_db(self) -> Path:
        return self.home / 'state.db'


def _load_yaml(path: Path) -> dict:
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


@ttl_cache(10)
def profiles() -> list[Profile]:
    out = []
    for host, home in ((LOCAL_HOST, LOCAL_HOME), ('wsl', wsl_home())):
        if not home:
            continue
        candidates = [('default', home)] + sorted((p.name, p) for p in (home / 'profiles').glob('*') if p.is_dir())
        for name, path in candidates:
            if (path / 'config.yaml').is_file():
                out.append(Profile(host, name, path, _load_yaml(path / 'config.yaml')))
    return out


def profile(key: str) -> Profile | None:
    return next((p for p in profiles() if p.key == key), None)


def norm(path: Path | str) -> str:
    return str(path).replace('\\', '/').rstrip('/').lower()


def kind_of(root: Path) -> str:
    r = norm(root)
    if r == norm(PUBLISHER_ROOT):
        return 'publisher'
    if r.endswith('/shared-skills'):
        return 'shared'
    if r.endswith('/hermes/skills') or r.endswith('/.hermes/skills') or '/profiles/' in r:
        return 'local'
    return 'project'


@lru_cache(maxsize=1)
def _owners_cached(mtime: float) -> dict[str, str]:
    lock = json.loads(PUBLISHER_LOCK.read_text(encoding='utf-8'))['skills']
    return {name: v.get('source', '') for name, v in lock.items()}


def publisher_owners() -> dict[str, str]:
    return _owners_cached(PUBLISHER_LOCK.stat().st_mtime) if PUBLISHER_LOCK.exists() else {}


def _frontmatter(text: str) -> dict:
    m = re.match(r'^---\r?\n(.*?)\r?\n---', text, re.S)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m[1])
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


@ttl_cache(10)
def skills() -> list[dict]:
    """Every skill from every source of every profile; one path is one skill."""
    owners = publisher_owners()
    profs = profiles()
    by_path: dict[str, dict] = {}
    for prof in profs:
        for root in prof.skill_roots:
            if not root.is_dir():
                continue
            for md in root.rglob('SKILL.md'):
                rel = md.relative_to(root).parts
                if any(p.startswith('.') for p in rel):
                    continue
                key = norm(md.parent)
                if key in by_path:
                    by_path[key]['_roots'].add(prof.key)
                    continue
                fm = _frontmatter(md.read_text(encoding='utf-8', errors='replace'))
                kind = kind_of(root)
                name = str(fm.get('name') or md.parent.name)
                group = {'publisher': owners.get(md.parent.name, 'unknown source'),
                         'shared': rel[0] if len(rel) > 2 else 'uncategorized',
                         'local': prof.label,
                         'project': project_name(root)}[kind]
                by_path[key] = {'name': name, 'dir': md.parent.name, 'description': str(fm.get('description') or ''),
                                'kind': kind, 'group': group, 'path': str(md.parent), '_roots': {prof.key}}
    out = []
    for s in by_path.values():
        roots = s.pop('_roots')
        s['profiles'] = [{'key': p.key, 'label': p.label,
                          'state': 'absent' if p.key not in roots else 'off' if s['name'] in p.disabled else 'on'} for p in profs]
        out.append(s)
    return sorted(out, key=lambda s: s['name'])


def project_name(root: Path) -> str:
    parts = norm(root).split('/')
    if parts[-2:] == ['.agents', 'skills'] and len(parts) > 2:
        return parts[-3]
    return parts[-2] if len(parts) > 1 else str(root)


def skill_files(path: Path) -> list[dict]:
    files = []
    for p in sorted(path.rglob('*')):
        if p.is_file() and '__pycache__' not in p.parts and p.stat().st_size < 512_000:
            rel = p.relative_to(path).as_posix()
            try:
                text = p.read_text(encoding='utf-8')
            except UnicodeDecodeError:
                text = None
            files.append({'path': rel, 'text': text})
    return sorted(files, key=lambda f: (f['path'] != 'SKILL.md', f['path']))


# ---- edit ledgers

@lru_cache(maxsize=16)
def _ledger_cached(path: str, mtime: float) -> list[dict]:
    out = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def ledger(prof: Profile) -> list[dict]:
    return _ledger_cached(str(prof.ledger), prof.ledger.stat().st_mtime) if prof.ledger.is_file() else []


def blob(prof: Profile, sha: str | None) -> str | None:
    if not sha:
        return ''
    for p in (prof.blobs / sha, prof.blobs / sha[:2] / sha):
        if p.is_file():
            return p.read_bytes().decode('utf-8', 'replace')
    return None


def short_path(path: str) -> str:
    """A file path inside a skill: everything after the skills directory."""
    p = path.replace('\\', '/')
    m = re.search(r'/(?:skills|shared-skills)/(.+)$', p)
    return m[1] if m else p.rsplit('/', 1)[-1]


# ---- sessions

_WSL_QUERY = ("import json,sqlite3,sys;a=json.load(sys.stdin);db=sqlite3.connect('file:'+a[0]+'?mode=ro',uri=True,timeout=5);"
              "print(json.dumps([db.execute(q,p).fetchall() for q,p in a[1]]))")


def state_query(prof: Profile, queries: list[tuple[str, list]]) -> list[list] | None:
    """Queries against a profile's state.db. A WSL database is read from inside WSL: SQLite locking does not work over \\\\wsl.localhost."""
    if prof.host == 'wsl':
        linux = '/' + str(prof.state_db).split('\\', 4)[-1].replace('\\', '/')  # \\wsl.localhost\<distro>\home\... -> /home/...
        try:
            proc = subprocess.run(['wsl.exe', '-e', 'python3', '-c', _WSL_QUERY], input=json.dumps([linux, queries]),
                                  capture_output=True, text=True, encoding='utf-8', timeout=30,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            return json.loads(proc.stdout) if proc.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return None
    if not prof.state_db.is_file():
        return None
    try:
        db = sqlite3.connect(f'file:{prof.state_db}?mode=ro', uri=True, timeout=5)
        try:
            return [[list(r) for r in db.execute(q, p).fetchall()] for q, p in queries]
        finally:
            db.close()
    except sqlite3.Error:
        return None


_titles: dict[str, dict[str, tuple[str | None, float]]] = {}


def session_titles(prof: Profile, ids: set[str]) -> dict[str, str | None]:
    """Session titles by primary key. Found titles are cached for good; missing ones are rechecked every 5 minutes
    (a title appears after the first reply)."""
    cache = _titles.setdefault(prof.key, {})
    now = time.monotonic()
    need = [i for i in ids if i and (i not in cache or (cache[i][0] is None and now - cache[i][1] > 300))]
    chunks = [need[i:i + 500] for i in range(0, len(need), 500)]
    res = state_query(prof, [(f"select id, title from sessions where id in ({','.join('?' * len(c))})", c) for c in chunks]) if chunks else None
    if res is not None:
        found = {r[0]: r[1] for rows in res for r in rows}
        cache.update({i: (found.get(i), now) for i in need})
    return {i: cache.get(i, (None, 0))[0] for i in ids}


def session_info(prof: Profile, session_id: str, limit: int = 15) -> dict:
    """The session title and the user's latest messages; empty when the database is unavailable."""
    res = state_query(prof, [('select title from sessions where id=?', [session_id]),
                             ("select content from messages where session_id=? and role='user' order by rowid desc limit ?",
                              [session_id, limit])]) if session_id else None
    if not res:
        return {'title': None, 'user_messages': []}
    title = res[0][0][0] if res[0] else None
    return {'title': title, 'user_messages': [str(m[0])[:2000] for m in reversed(res[1])]}


def memory_files(prof: Profile) -> dict[str, str]:
    out = {}
    for name in ('MEMORY.md', 'USER.md'):
        p = prof.home / 'memories' / name
        if p.is_file():
            out[name] = p.read_text(encoding='utf-8', errors='replace')
    return out


def memory_limits(prof: Profile) -> dict[str, int]:
    mem = prof.config.get('memory') or {}  # defaults come from hermes_cli/config_defaults.py
    return {'MEMORY.md': mem.get('memory_char_limit') or 2200, 'USER.md': mem.get('user_char_limit') or 1375}
