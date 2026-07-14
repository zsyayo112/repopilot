English | [中文](README.zh-CN.md)

# RepoPilot: A Repository-Aware Issue Resolution Agent

[![CI](https://github.com/zsyayo112/repopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/zsyayo112/repopilot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org)

Give it a git repository and an issue. It runs the full software engineering
loop end to end:

```
Issue → Baseline Tests → Plan → Locate & Edit Code → Verify → Retry on Failure → Independent Review → Diff / Report
```

**Hand-written, no agent framework.** No LangChain, no LangGraph — the tool-calling
loop, tool dispatch, permission gate, structured output, and state machine are all
implemented from scratch on top of the raw OpenAI-compatible SDK. This is both a
learning choice and a design stance: the core mechanics should be understandable
and debuggable, not hidden behind an abstraction layer.

## Quick Start

```bash
pip install -e .
pip install pytest-cov  # many Python repos' test suites depend on it
cp .env.example .env    # fill in a DeepSeek / OpenAI-compatible API key

# Pick a real repo to try it on — tinydb is used here as a demo, any git repo works
git clone https://github.com/msiemens/tinydb ../tinydb-demo

repo-pilot detect --repo ../tinydb-demo                                    # free: check adapter detection
repo-pilot solve  --repo ../tinydb-demo --issue-file examples/tinydb_issue.md --plan-only  # one call: plan only
repo-pilot solve  --repo ../tinydb-demo --issue-file examples/tinydb_issue.md              # full loop
```

`examples/tinydb_issue.md` describes a real, reproducible boundary-value bug (a
`<=` query silently drops records exactly at the boundary). To watch the
Verifier's baseline-comparison logic actually catch something, break
`tinydb/queries.py`'s `__le__` method by hand first (change `<=` to `<`) in
`../tinydb-demo`, then run the command above.

## Architecture: Modules Map Directly to Files

```
Agent Core             orchestrator.py   State machine: BASELINE→PLAN→EXECUTE→VERIFY→REVIEW→REPORT
                        planner.py        issue → structured plan (JSON)
                        executor.py       Tool-calling main loop (streaming)
                        reviewer.py       Independent-context review (never sees the executor's own narrative)
Repository Intel        workspace.py      Git workspace: clean-tree gate / diff / rollback / file listing
                        adapters.py       The only place that knows about a specific tech stack
                                          (registry of per-stack detectors; see table below)
Tool Runtime            tools.py          read / list / search (ripgrep) / symbols (ast) /
                                          edit (exact-match replace) / write / bash / run_tests / git_diff
Verification            verifier.py       Test baseline comparison: fixed / regressed / improved / no_change
Safety                  policy.py         Path jailing, dangerous-command blocklist, .git write protection
                        permissions.py    Human visibility and veto over every mutating action
Observability           trace.py          run.jsonl execution trace (raw data for evaluation)
GitHub (optional shell) github.py         Fetch issues via gh CLI; PR creation is Phase 4
```

## Supported Stacks

The core agent is framework-agnostic; all tech-stack knowledge lives in
[`adapters.py`](repopilot/adapters.py). Detection is a registry of small
detector functions — adding a stack is one function plus one test, with **zero
changes to the core**. Anything unrecognized still works via `--test-cmd`.

| Stack | Detected by | Test command |
|-------|-------------|--------------|
| Python | `pyproject.toml` / `setup.py` / `pytest.ini` / … | `pytest` |
| Rust | `Cargo.toml` | `cargo test` |
| Go | `go.mod` | `go test ./...` |
| Java (Maven) | `pom.xml` | `mvn -q -B test` |
| Java (Gradle) | `build.gradle[.kts]` | `./gradlew test` (or `gradle test`) |
| Ruby | `.rspec` / `spec/` / `Gemfile` | `bundle exec rspec` / `rake test` |
| Node | `package.json` | `npm test` |
| NestJS | `package.json` with `@nestjs/core` | `npm test` |
| _(fallback)_ | `Makefile` with a `test:` target | `make test` |
| _anything_ | `--test-cmd "<cmd>"` | your command |

## Core Design Decisions

- **Verification is the whole point.** Run the test suite before touching anything to
  record a baseline, then run it again after. "Fixed" is a measured fact, not a
  claim the model makes about itself.
- **The Reviewer is context-isolated.** It only sees the issue, the plan, the diff, and
  the test comparison — never the executor's own "I fixed it" narrative. An agent
  reviewing its own conversation history will always approve; isolation is what
  makes the review real.
- **Adapter pattern.** The core agent only ever asks two questions: what kind of
  project is this, and how do I run its tests? Supporting a new stack means adding
  one detection branch — the core never changes.
- **Hard constraints over soft ones.** Paths are jailed to the repo root, dangerous
  commands are blocklisted, there's a cap on files modified per run and on loop
  turns, and `git commit`/`git push` are blocked outright — the final commit is
  always a human decision.
- **Fully observable.** Every state transition and tool call is appended to
  `runs/<timestamp>/run.jsonl`. Evaluation is just aggregating over these files.

## Roadmap

- [x] **Phase 0–3 (MVP, current)** Full loop: Plan / Execute / Verify (baseline
      comparison + retry) / Review (isolated context) / Trace / multi-stack
      adapters / safety policy / CI + lint
- [ ] **Phase 4 (shell)** Direct GitHub issue fetching (prototype exists), automatic
      branch + draft PR creation, deeper NestJS adapter (Jest/E2E detection)
- [x] **SWE-bench Lite mini-evaluation** — on 8 attempted pure-Python instances
      in a lightweight local setting: **3/3 resolved on instances our environment
      could certify** (gold-patch calibration: the official fix itself must pass
      locally, else the instance is an environment artifact — Python 3.12 kills
      several 2022-era test suites), 3/8 under the conservative reading. Scoring
      re-implements the official protocol (revert test files → apply official
      test patch → all FAIL_TO_PASS + PASS_TO_PASS), including its
      whitespace-truncated test-ID matching. See [`eval/`](eval/) and
      [`eval/RESULTS.md`](eval/RESULTS.md).
- [ ] **Phase 5 (deep end, one at a time)** Docker sandbox in place of the
      blocklist / ts-morph symbol indexing / dependency-graph retrieval /
      full SWE-bench Lite sweep in the official Docker harness

## Development

```bash
pip install -e ".[dev]"   # installs pytest + ruff
pytest -q                 # run tests (offline — no API key needed)
ruff check .              # lint
```

CI runs lint + tests across Python 3.10–3.12 on every push and PR. See
[CONTRIBUTING.md](CONTRIBUTING.md) for how to add a new Repository Adapter.

## Background

This project grew out of a self-directed course on writing a coding agent from
scratch — no frameworks, building up the tool-calling loop, permission gate, and
context isolation mechanics one piece at a time from a raw API. RepoPilot is that
course's capstone project: taking those mechanics and applying them to something
closer to real engineering work — resolving issues in real code repositories.

## License

[MIT](LICENSE)
