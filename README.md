# skillwatch

Watch what the [Hermes](https://github.com/NousResearch/hermes-agent) agent writes to its skills and memory, and have
an agent reviewer check every such write before it lands.

Hermes learns as it works: background self-learning and agents in chat keep patching skills and memory. Left alone,
that turns into noise — one-off details, incident stories, lessons filed in the wrong skill, rewrites of skills that
belong to someone else. skillwatch gives you:

- **A window** (`http://127.0.0.1:8770/`) with a live feed of every skill and memory edit across all your Hermes profiles:
  what changed, where and who changed it, with a Monaco diff and the reviewer's note. Skill pages show the history first,
  then the current files.
- **A reviewer**: every `skill_manage` and `memory` write goes to a one-shot Hermes run that only reads and answers
  `pass` or `reject` by the rules in [`rules.md`](rules.md). A rejection reaches the writing agent as the tool error, with a
  quote, the rule and a minimal alternative.
- **Publisher protection**: skills installed with [`npx skills`](https://github.com/vercel-labs/skills) into
  `~/.agents/skills` cannot be edited by Hermes at all.
- **Calibration**: run the reviewer over a fixed set of real edits, rate its decisions in the window, tune `rules.md`,
  compare versions and models.

What it leaves to you: the window does not edit skills. You read, then tell an agent in chat what to fix.

## How it works

```
Hermes tool call ──► pre_tool_call hook (hook/skillwatch-guard.py)
                      ├─ publisher skill or file?          ──► block
                      ├─ the reviewer's own session?       ──► block (the reviewer only reads)
                      └─ skill_manage / memory             ──► POST /api/review ──► one-shot `hermes chat` reviewer
                                                                                    └─ pass / reject + what, where, note
skillwatch service (FastAPI, 127.0.0.1:8770) ──► reads Hermes: profile configs, skills/.curator_ledger.jsonl and its blobs,
                                                state.db, memories/ — and serves the window (web/, React + Monaco)
```

The service never writes to Hermes. Its own state is `data/skillwatch.db` (SQLite): reviews, your ratings, reviewer
versions. If the service is down, the hook lets writes through and the feed marks them "unreviewed".

On Windows it also reads the Hermes profiles inside the default WSL distro, so one window covers both.

## Requirements

- Hermes Agent with skill ledgers (`skills/.curator_ledger.jsonl`).
- Python 3.12+ and [uv](https://docs.astral.sh/uv/); Node.js 20+ to build the window.

## Install

```bash
git clone https://github.com/hakastein/skillwatch
cd skillwatch
uv sync
(cd web && npm install && npm run build)
uv run python -m skillwatch serve          # http://127.0.0.1:8770/
```

### Connect the hook

In the `config.yaml` of every Hermes profile you want covered:

```yaml
plugins:
  hook_callback_timeout: 300     # a review takes 15–60 s; the 30 s default makes Hermes drop the hook
hooks:
  pre_tool_call:
    - command: python3 /path/to/skillwatch/hook/skillwatch-guard.py
      matcher: ^(skill_manage|memory|write_file|patch)$
      timeout: 300
      fail_closed: true
```

The hook uses only the standard library, so any Python 3.12+ works, including the one inside WSL. Under WSL it also
protects Windows users' `~/.agents/skills` directories; set `SKILLWATCH_PUBLISHER_DIRS` (path-separated) to override the
list, and `SKILLWATCH_URL` if the service is not on `http://127.0.0.1:8770`.

Without `hook_callback_timeout`, Hermes cuts the hook at 30 s and then suppresses it gateway-wide for a minute, failing
tool calls with "pre_tool_call plugin callback timed out or is still running".

### Choose the reviewer model

Copy [`reviewer.example.yaml`](reviewer.example.yaml) to `reviewer.yaml` (git-ignored) and set any Hermes provider and
model. Medium reasoning is enough: a review then takes about 20 s.

The reviewer prompt is yours to change:

- `rules` — the rules file. Copy `rules.md` to `rules.local.md` (git-ignored), set `rules: rules.local.md` and edit it;
  `git pull` will not touch your copy.
- `language` — the language of what you read: change summaries and reviewer notes. The agent whose write was rejected
  still gets the note in English, so agents keep talking to each other in English.

Either change makes a new reviewer version: run `calibrate` to see how decisions moved.

## Use

| Link | Shows |
|---|---|
| `#/feed` | all edits, newest first, refreshed every 5 s |
| `#/skill/<name>` | a skill's history with reviewer notes, then its current files and which profiles load it |
| `#/memory/<host>:<profile>` | `MEMORY.md` / `USER.md` of a profile and their history |
| `#/rules` | the reviewer rules and verdict counts |
| `#/calibration` | reviewer decisions of the latest calibration run, to rate |

These links are stable, so an agent can hand them to you. To point Hermes at them, add a short skill of your own that
lists the link formats.

### Commands

| Command | Does |
|---|---|
| `uv run python -m skillwatch serve [--no-review]` | the service; `--no-review` records writes as unreviewed |
| `uv run python -m skillwatch backfill --limit N` | what/where summaries for N batches of older ledger entries |
| `uv run python -m skillwatch queue --size N` | add N real edits to the calibration set, spread across skills, profiles and actors |
| `uv run python -m skillwatch calibrate [--provider P --model M --reasoning R]` | run a reviewer version over the calibration set |

### Tuning the reviewer

1. `queue` builds a calibration set from your own history.
2. `calibrate` runs the current rules and model over it. A version is the rules text plus model plus context layout, so
   every change gets its own row.
3. In `#/calibration` rate each decision "right" or "wrong" and say what is off.
4. Have an agent rewrite `rules.md` from your ratings, run `calibrate` again and compare: decisions, timing, agreement
   with your `reference_version`.

## Development

```bash
uv run python -m unittest discover -s tests
(cd web && npm run build)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
