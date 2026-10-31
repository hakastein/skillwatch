"""The reviewer agent: a one-shot Hermes run that gets the whole context of a write at once and returns a verdict."""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import yaml

from . import sources

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / 'rules.md'
CONFIG = ROOT / 'reviewer.yaml'
EXAMPLE_CONFIG = ROOT / 'reviewer.example.yaml'
# The Hermes gateway runs the reviewer's turn, so a marker cannot travel through the environment: the reviewer runs with
# --source tool, and the hook forbids every write in such sessions (they also stay out of the session list).
HERMES_ARGS = ['--oneshot', '-Q', '--ignore-rules', '--source', 'tool']
CONTEXT_REV = 3  # bump when what the reviewer receives changes: that is a new version for calibration
FILE_CHARS = 40_000
RELATED_CHARS = 15_000
RELATED_MAX = 5
_slots = threading.Semaphore(3)


class ReviewError(Exception):
    pass


def config() -> dict:
    return yaml.safe_load((CONFIG if CONFIG.is_file() else EXAMPLE_CONFIG).read_text(encoding='utf-8'))


def model_config(**override) -> dict:
    cfg = config()['reviewer']
    return {k: override.get(k) or cfg[k] for k in ('provider', 'model', 'reasoning', 'timeout')}


def language() -> str:
    return config()['reviewer'].get('language') or 'English'


# ---- context

def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f'\n…[truncated, {len(text)} characters in total]'


def _profile_skills(prof: sources.Profile) -> list[dict]:
    return [s for s in sources.skills() if any(p['key'] == prof.key and p['state'] != 'absent' for p in s['profiles'])]


def skill_list(prof: sources.Profile) -> str:
    return '\n'.join(f"- {s['name']} [{s['kind']}: {s['group']}] ({s['path']}): {s['description'][:200]}" for s in _profile_skills(prof))


def session_block(prof: sources.Profile, session_id: str | None) -> str:
    info = sources.session_info(prof, session_id or '')
    msgs = '\n'.join(f'{i}. {m}' for i, m in enumerate(info['user_messages'], 1)) or '(no data)'
    return f"Title: {info['title'] or '(none)'}\nThe user's latest messages, oldest first:\n{msgs}"


def find_skill(name: str | None, prof: sources.Profile) -> dict | None:
    if not name:
        return None
    rows = [s for s in sources.skills() if name in (s['name'], s['dir'])]
    mine = [s for s in rows if any(p['key'] == prof.key and p['state'] != 'absent' for p in s['profiles'])]
    return (mine or rows or [None])[0]


def apply_op(current: str | None, op: dict) -> str | None:
    """The file after a skill_manage operation; None when it cannot be computed (Hermes matches old_string fuzzily)."""
    act = op.get('action')
    if act in ('create', 'edit'):
        return op.get('content') or ''
    if act == 'write_file':
        return op.get('file_content') or ''
    if act in ('delete', 'remove_file'):
        return ''
    if act == 'patch' and current is not None and op.get('old_string') and op['old_string'] in current:
        return current.replace(op['old_string'], op.get('new_string') or '', -1 if op.get('replace_all') else 1)
    return None


def live_files(op_all: dict, prof: sources.Profile) -> dict[str, list[dict]]:
    """Before/after file states of a live skill_manage call, per skill; a batch is applied in order."""
    from .events import ops_of
    out: dict[str, list[dict]] = {}
    state: dict[tuple[str, str], dict] = {}
    for op in ops_of(op_all):
        name = op.get('name') or op_all.get('name')
        if not name:
            continue
        rel = 'SKILL.md' if op.get('action') in ('create', 'edit', 'delete') else op.get('file_path') or 'SKILL.md'
        if (name, rel) not in state:
            sk = find_skill(name, prof)
            path = Path(sk['path']) / rel if sk else None
            before = path.read_text(encoding='utf-8', errors='replace') if path and path.is_file() else None
            state[(name, rel)] = {'file': rel, 'before': before or '', 'after': before}
            out.setdefault(name, []).append(state[(name, rel)])
        f = state[(name, rel)]
        f['after'] = apply_op(f['after'], op)
        if f['after'] is None:
            f['op'] = op
    return out


def _related(skill: dict | None, text: str, prof: sources.Profile) -> list[dict]:
    """Skills the target skill points to (related_skills) or the edit mentions — to judge home and duplicates."""
    names: list[str] = []
    if skill:
        fm = sources._frontmatter((Path(skill['path']) / 'SKILL.md').read_text(encoding='utf-8', errors='replace'))
        meta = fm.get('metadata') if isinstance(fm.get('metadata'), dict) else {}
        hermes = meta.get('hermes') if isinstance(meta.get('hermes'), dict) else {}
        names += [str(n) for n in (fm.get('related_skills') or hermes.get('related_skills') or [])]
    names += [s['name'] for s in _profile_skills(prof)
              if len(s['name']) >= 4 and re.search(rf'(?<![\w-]){re.escape(s["name"])}(?![\w-])', text)]
    out = []
    for n in dict.fromkeys(names):
        s = find_skill(n, prof)
        if s and (not skill or s['path'] != skill['path']):
            out.append(s)
    return out[:RELATED_MAX]


def skill_context(files_by_skill: dict[str, list[dict]], prof: sources.Profile, retro: bool) -> str:
    """The diff, touched files in full after the edit, SKILL.md, a list of other files and related skills."""
    parts = ['The files below are the state right after the edit, from the ledger; they may have changed on disk since.'] if retro else []
    for name, files in files_by_skill.items():
        sk = find_skill(name, prof)
        touched = {f['file'] for f in files}
        parts.append(f"## Skill {name} — {sk['kind']}, group {sk['group']}, directory {sk['path']}\nDescription: {sk['description']}"
                     if sk else f'## Skill {name} — not on disk now (being created, or removed later)')
        added = []
        for f in files:
            if f.get('op'):
                parts.append(f"### Operation on {f['file']} (result not computed: old_string matches only fuzzily)\n"
                             f"```json\n{json.dumps(f['op'], ensure_ascii=False, indent=1)}\n```")
                added.append(str(f['op'].get('new_string') or ''))
                continue
            diff = '\n'.join(difflib.unified_diff(f['before'].splitlines(), f['after'].splitlines(),
                                                  f"a/{f['file']}", f"b/{f['file']}", lineterm='', n=3))
            parts.append(f"### Diff of {f['file']}\n```diff\n{_cut(diff, FILE_CHARS) or '(text unchanged)'}\n```")
            added += [ln[1:] for ln in diff.splitlines() if ln.startswith('+') and not ln.startswith('+++')]
            if f['after']:
                parts.append(f"### {f['file']} in full after the edit\n````\n{_cut(f['after'], FILE_CHARS)}\n````")
        if not sk:
            continue
        if 'SKILL.md' not in touched:
            md = (Path(sk['path']) / 'SKILL.md').read_text(encoding='utf-8', errors='replace')
            parts.append(f"### SKILL.md (not touched by the edit)\n````\n{_cut(md, FILE_CHARS)}\n````")
        others = [f"- {p['path']} ({len(p['text'] or '')} chars)" for p in sources.skill_files(Path(sk['path']))
                  if p['path'] not in touched | {'SKILL.md'}]
        if others:
            parts.append('### Other files of the skill (read them if needed)\n' + '\n'.join(others))
        for r in _related(sk, '\n'.join(added), prof):
            md = (Path(r['path']) / 'SKILL.md').read_text(encoding='utf-8', errors='replace')
            parts.append(f"### Related skill {r['name']} ({r['path']})\n````\n{_cut(md, RELATED_CHARS)}\n````")
    return '\n\n'.join(parts)


def memory_context(op: dict, prof: sources.Profile) -> str:
    name = 'USER.md' if op.get('target') == 'user' else 'MEMORY.md'
    current = sources.memory_files(prof).get(name, '')
    return (f"Arguments of the memory call:\n```json\n{json.dumps(op, ensure_ascii=False, indent=1)}\n```\n\n"
            f"Memory file {name} (entries separated by a line with \"§\"), current content:\n```\n{current}\n```")


def build_prompt(*, tool: str, context: str, prof: sources.Profile, session_id: str | None, retro: bool) -> str:
    intro = ('The edit is already applied; it comes from the ledger. Judge it as if it were only being proposed.' if retro else
             'The agent is about to make the write below. Decide whether to let it through or reject it.')
    lang = language()
    return f"""You review edits to the skills and memory of the Hermes agent. {intro}
Everything needed for the decision is collected below. The read tools (read_file, search_files, skill_view) are for what is missing here: another file of the skill or a skill not included in the context. You only read.

# Rules
{RULES.read_text(encoding='utf-8')}

# The write
Tool: {tool}. Profile: {prof.label}.

{context}

# The session that made the write
{session_block(prof, session_id)}

# Skills of the profile (name [kind: group or publisher], directory, description)
{skill_list(prof)}

# Answer
Answer with a single JSON object and no text around it:
{{"verdict": "pass" or "reject", "what": "...", "where": "...", "note": "...", "rule": "..."}}
- what: one sentence in {lang}, up to 110 characters, past tense: what exactly was added, removed or changed, and about what. No generic phrases like "updated the skill".
- where: the file and the section inside it, e.g. "references/preview.md › Startup"; for memory — "MEMORY.md" or "USER.md".
- note: in {lang}, as the "Note" section of the rules says.
- rule: on reject — the heading of the violated rules section; on pass — an empty string.
"""


def version_key(cfg: dict) -> tuple[str, dict]:
    """Everything decisions depend on: rules, model, output language and context layout. Same key, same version."""
    model = {k: cfg[k] for k in ('provider', 'model', 'reasoning')}
    meta = {**model, 'language': language(), 'context': CONTEXT_REV}
    return json.dumps({**meta, 'rules': RULES.read_text(encoding='utf-8')}, ensure_ascii=False, sort_keys=True), meta


# ---- running

def parse_verdict(text: str) -> dict:
    """The last JSON object with a verdict in the Hermes output."""
    dec = json.JSONDecoder()
    for i in reversed([m.start() for m in re.finditer(r'\{', text)]):
        try:
            data, _ = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get('verdict') in ('pass', 'reject') and data.get('what'):
            return {k: str(data.get(k) or '') for k in ('verdict', 'what', 'where', 'note', 'rule')}
    raise ReviewError(f'the reviewer returned no verdict: {text[-400:]!r}')


def hermes_exe() -> str:
    exe = shutil.which('hermes') or shutil.which(str(Path.home() / '.local' / 'bin' / 'hermes'))
    if not exe:
        raise ReviewError('hermes is not on PATH')
    return exe


def run(prompt: str, cfg: dict | None = None) -> tuple[dict, float]:
    """Run the reviewer within one overall timeout, waiting for a free slot included."""
    cfg = cfg or model_config()
    timeout = cfg['timeout']
    deadline = time.monotonic() + timeout
    with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, encoding='utf-8') as f:
        f.write(prompt)
        path = f.name
    try:
        if not _slots.acquire(timeout=timeout / 2):
            raise ReviewError('all reviewer slots are busy')
        try:
            start = time.monotonic()
            proc = subprocess.run(
                [hermes_exe(), 'chat', '--query-file', path, *HERMES_ARGS, '--provider', cfg['provider'], '-m', cfg['model'],
                 '--reasoning', cfg['reasoning'], '-t', 'file,skills', '--max-turns', '30'],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=max(30, deadline - start),
                cwd=ROOT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            took = time.monotonic() - start
        finally:
            _slots.release()
    except subprocess.TimeoutExpired as e:
        raise ReviewError(f'the reviewer did not finish within {timeout} s') from e
    finally:
        os.unlink(path)
    if proc.returncode != 0:
        raise ReviewError(f'hermes exited with code {proc.returncode}: {(proc.stderr or proc.stdout)[-400:]}')
    return parse_verdict(proc.stdout), took
