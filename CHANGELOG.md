# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Repository Adapters expanded from Python/Node to eight stacks: Python, Rust,
  Go, Java (Maven), Java (Gradle), Ruby, Node, NestJS, plus a `Makefile`
  fallback. Detection is now a registry of small `_detect_*` functions.
- `CONTRIBUTING.md` documenting the adapter extension point and the
  no-framework design stance.
- `CHANGELOG.md`.
- GitHub Actions CI: lint (ruff) + tests (pytest) across Python 3.10–3.12.
- Ruff lint config and pytest config in `pyproject.toml`; project metadata
  (authors, URLs, classifiers, keywords).
- Test for the state machine's runaway-change abort path; more adapter tests.

### Changed
- The model client is now lazily constructed, so importing the package (for
  tests, CI, or `repo-pilot detect`) no longer requires an API key.

### Fixed
- Silent terminal state: hitting the modified-files cap set `state="FAILED"` and
  exited without a report. `DONE` is now the single terminal state; every
  outcome flows through `REPORT`, which prints the diff and rollback command and
  leaves the revert decision to the human.
- Path jailing no longer strips a legitimate `<repo-name>/` prefix when the repo
  contains a same-named package directory (e.g. `tinydb/tinydb/`).

## [0.1.0]

### Added
- Initial MVP: full loop of baseline tests → plan → execute (tool-calling agent)
  → verify (test baseline comparison) → retry → independent review → diff/report.
- Tool runtime: read / list / search (ripgrep) / symbols (ast) / edit / write /
  bash / run_tests / git_diff.
- Safety: path jailing, dangerous-command blocklist, `.git` write protection,
  permission gate, modified-files cap, `git commit`/`push` block.
- Execution trace (`run.jsonl`) for every run.
