# Changelog

## Unreleased

## 0.1.0

First public version.

- Window with a live change feed across all Hermes profiles (Windows, Linux, WSL), skill pages with history and
  current files, memory pages, reviewer rules.
- Reviewer: a one-shot Hermes run per `skill_manage` / `memory` write, verdict `pass` / `reject` with a summary and a
  note; configurable provider, model, reasoning and output language.
- Hook blocking edits of publisher skills in `~/.agents/skills` and any write from the reviewer's own session.
- Calibration: a set of real edits, versioned reviewer runs, rating of decisions in the window.
