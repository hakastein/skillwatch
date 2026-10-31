# Contributing

Issues and pull requests are welcome on GitHub.

## Scope

skillwatch observes and reviews what Hermes writes to its skills and memory. It reads Hermes data and never writes to
it; the window does not edit skills. Features that turn it into a skill editor or installer are out of scope.

## Checks

```bash
uv sync
uv run python -m unittest discover -s tests
(cd web && npm install && npm run build)
```

Both must pass before a PR. Tests check behaviour at a real boundary (the hook's stdin/stdout, the HTTP API, the
feed built from a ledger); a test that would still pass with the feature broken is not worth adding.

## Rules

- The hook stays standard-library only and runs under Python 3.12+ on Windows, Linux and WSL.
- The reviewer judges text; code does not. Change the rules in `rules.md`, and back a change with a `calibrate` run
  compared against the previous version.
- If you change what the reviewer receives, bump `CONTEXT_REV` in `skillwatch/reviewer.py`.
- Keep commits focused and describe the user-visible change in the PR.

## Releases

The maintainer tags releases. Every tag gets a section in [CHANGELOG.md](CHANGELOG.md).
