#!/usr/bin/env python3
"""Hermes pre_tool_call hook: protects publisher skills and sends skill and memory writes to review.

Standard library only: it runs under the Windows Python as well as /usr/bin/python3 in WSL or on Linux.
"""
import glob
import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

WINDOWS = os.name == 'nt'
SERVICE = os.environ.get('SKILLWATCH_URL', 'http://127.0.0.1:8770') + '/api/review'
HTTP_TIMEOUT = 290  # Hermes waits for a hook callback up to plugins.hook_callback_timeout (set it to 300)
REVIEWER_SOURCE = 'tool'  # the reviewer runs with --source tool; every write is forbidden in such sessions
WRITE_TOOLS = ('skill_manage', 'memory', 'write_file', 'patch')
PROTECTED = ("This skill belongs to an external publisher: Hermes does not edit it. "
             "It is updated only by its installer, when the user asks.")


def host():
    if WINDOWS:
        return 'windows'
    try:
        return 'wsl' if 'microsoft' in Path('/proc/version').read_text().lower() else 'linux'
    except OSError:
        return 'linux'


def hermes_home():
    home = os.environ.get('HERMES_HOME')
    if home:
        return Path(home)
    return Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes' if WINDOWS else Path.home() / '.hermes'


def publisher_roots():
    """Directories of publisher skills (`npx skills` installs them to ~/.agents/skills). Under WSL the Windows
    users' directories count too: a Hermes in WSL may load them through skills.external_dirs."""
    if os.environ.get('SKILLWATCH_PUBLISHER_DIRS'):
        return [Path(p) for p in os.environ['SKILLWATCH_PUBLISHER_DIRS'].split(os.pathsep) if p]
    roots = [Path.home() / '.agents' / 'skills']
    if host() == 'wsl':
        roots += [Path(p) for p in glob.glob('/mnt/c/Users/*/.agents/skills')]
    return [r for r in roots if r.is_dir()]


def is_reviewer_session(session_id):
    """The session runs with --source tool (the service starts the reviewer that way; the gateway runs the turn,
    so the environment cannot carry a marker)."""
    db_path = hermes_home() / 'state.db'
    if not session_id or not db_path.is_file():
        return False
    try:
        db = sqlite3.connect(f'file:{db_path.as_posix()}?mode=ro', uri=True, timeout=5)
        try:
            row = db.execute('select source from sessions where id=?', (session_id,)).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == REVIEWER_SOURCE


def publisher_names(roots):
    names = set()
    for root in roots:
        lock = root.parent / '.skill-lock.json'
        if lock.is_file():
            names |= set(json.loads(lock.read_text(encoding='utf-8')).get('skills', {}))
        names |= {p.name for p in root.iterdir() if (p / 'SKILL.md').is_file()}
    return {n.casefold() for n in names}


def under(path, cwd, roots):
    if not isinstance(path, str) or not path:
        return False
    p = Path(os.path.expanduser(path.replace('\\', '/')))
    if not p.is_absolute():
        p = Path(cwd) / p
    try:
        p = p.resolve(strict=False)
        return any(p.is_relative_to(r.resolve(strict=False)) for r in roots)
    except (OSError, ValueError):
        return False


def ops(args):
    o = args.get('operations')
    return [x for x in o if isinstance(x, dict)] if isinstance(o, list) else [args]


def guard(tool, args, cwd, session_id=None):
    """A block message, or None."""
    if tool in WRITE_TOOLS and is_reviewer_session(session_id):
        return 'The edit reviewer only reads: writes are forbidden.'
    if tool == 'skill_manage':
        names = publisher_names(publisher_roots())
        denied = sorted({str(op.get('name') or args.get('name')) for op in ops(args)
                         if str(op.get('name') or args.get('name') or '').replace('\\', '/').rsplit('/', 1)[-1].casefold() in names})
        if denied:
            return f"{PROTECTED} Rejected as a whole: {', '.join(denied)}."
    if tool in ('write_file', 'patch'):
        paths = [args.get('path')] + [line.split(': ', 1)[1] for line in str(args.get('patch') or '').splitlines()
                                      if re.match(r'\*\*\* (Update|Add|Delete) File: |\*\*\* Move to: ', line)]
        roots = publisher_roots()
        if any(under(p, cwd, roots) for p in paths):
            return PROTECTED
    return None


def review(payload):
    """The service's verdict; whenever the service is unavailable the write goes through (fail-open, marked unreviewed)."""
    body = json.dumps({**payload, 'host': host()}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(SERVICE, body, {'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception:
        return {'action': 'continue'}


def main():
    payload = json.load(sys.stdin)
    tool, args = payload.get('tool_name', ''), payload.get('tool_input') or {}
    message = guard(tool, args, payload.get('cwd') or os.getcwd(), payload.get('session_id'))
    if message:
        result = {'action': 'block', 'message': message}
    elif tool in ('skill_manage', 'memory'):
        result = review(payload)
    else:
        result = {'action': 'continue'}
    sys.stdout.write(json.dumps(result))  # ASCII-escaped: stdout encoding is not guaranteed on Windows


if __name__ == '__main__':
    main()
