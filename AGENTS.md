# Working on skillwatch

- Start with `README.md`. The service only reads Hermes data; its own state is `data/skillwatch.db`.
- The window does not edit skills, memory or `rules.md`; those changes go through an agent in chat. The only writes from
  the window are the user's ratings, stored in the service's own database.
- Publisher skills (`~/.agents/skills`) are never edited; they are updated with their installer when the user asks.
- `hook/skillwatch-guard.py` uses the standard library only and must run under any Python 3.12+, including WSL's
  `/usr/bin/python3`. Run `tests/test_guard.py` after changing it.
- Only the reviewer agent judges the text of a write, by `rules.md`; do not add text checks in code.
- Changing what the reviewer receives (`skillwatch/reviewer.py`) means bumping `CONTEXT_REV`: it is a new version for
  calibration.
- `data/` holds summaries of private sessions and is never committed. Every tag gets a section in `CHANGELOG.md`.
