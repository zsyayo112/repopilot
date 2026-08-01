English | [中文](README.zh-CN.md)

# RepoPilot: A Repository-Aware Issue Resolution Agent

[![CI](https://github.com/zsyayo112/repopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/zsyayo112/repopilot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org)

Give it a git repository and an issue. It runs the full software engineering
loop end to end:

```
Issue → Baseline Tests → [Start App → Reproduce in Browser] → Plan → Locate & Edit Code
      → Verify (tests + lint/typecheck/build + the same reproduction scenario)
      → Retry on Failure → Independent Review → Diff / Report → Cleanup
```

The bracketed steps are opt-in (`--with-runtime` / `--scenario`) and exist for the
class of bugs unit tests structurally cannot see: a button that renders but can't
be clicked, a navigation that goes to the wrong route, a request that succeeds
while the page never updates.

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

repo-pilot detect --repo ../tinydb-demo    # free: adapter detection + monorepo units
repo-pilot doctor --repo ../tinydb-demo    # free: is the environment actually ready?
repo-pilot solve  --repo ../tinydb-demo --issue-file examples/tinydb_issue.md --plan-only  # one call: plan only
repo-pilot solve  --repo ../tinydb-demo --issue-file examples/tinydb_issue.md              # full loop
```

For issues that only reproduce once the app is rendering, add the runtime layer
(Playwright is an **optional** dependency — everything above works without it):

```bash
pip install -e ".[browser]" && playwright install chromium

repo-pilot solve --repo ../my-next-app --issue-file issue.md --with-runtime
repo-pilot solve --repo ../my-next-app --issue-file issue.md \
                 --scenario examples/scenario_mobile_booking.json
```

`examples/tinydb_issue.md` describes a real, reproducible boundary-value bug (a
`<=` query silently drops records exactly at the boundary). To watch the
Verifier's baseline-comparison logic actually catch something, break
`tinydb/queries.py`'s `__le__` method by hand first (change `<=` to `<`) in
`../tinydb-demo`, then run the command above.

## Architecture: Modules Map Directly to Files

```
Agent Core             orchestrator.py   State machine (10 states, 3 conditional)
                        planner.py        issue → structured plan (JSON)
                        executor.py       Tool-calling main loop (streaming)
                        explorer.py       Read-only exploration sub-agent — context isolation to save budget
                        reviewer.py       Independent-context review — the same isolation, used to remove bias
Repository Intel        workspace.py      Git workspace: clean-tree gate / diff / rollback / file listing
                        adapters/         The only place that knows about a specific tech stack
                          base.py           The contract + shared vocabulary (TestReport, ServiceSpec, …)
                          registry.py       Detection registry, RepoProfile, monorepo scanning
                          symbols.py        Multi-language symbol outline (Python AST; regex elsewhere)
                          *_stack.py        One file per stack — all of its knowledge
Tool Runtime            tools.py          28 tools in 3 capability groups (13 exposed by default)
                        runtime.py        Runtime Manager: start services, health-check, logs, process groups
                        browser.py        Browser Session (Playwright, optional) — ref-tagged page snapshots
                        evidence.py       Evidence Collector: console / network / screenshots, redacted on write
                        scenario.py       Structured reproduction: identical steps + assertions, before and after
Verification            verifier.py       Baseline comparison + validation pipeline (lint→typecheck→test→build)
                        doctor.py         Environment triage: "broken code" vs "unprepared machine"
Safety                  policy.py         Path jailing, command blocklist, irreversible-intent guard
                        permissions.py    Human visibility and veto over every mutating action
Observability           trace.py          run.jsonl execution trace (raw data for evaluation)
GitHub (optional shell) github.py         Fetch issues via gh CLI; PR creation is Phase 4
```

## Supported Stacks

The core agent is framework-agnostic; all tech-stack knowledge lives in
[`adapters/`](repopilot/adapters/). Adding a stack is one `*_stack.py` plus one
line in the registry, with **zero changes to the core**. Anything unrecognized
still works via `--test-cmd`.

Each adapter answers seven questions, not one: what kind of project is this, how
do I test it, **what does that output mean**, what else must be validated, how do
I run it, what does it need installed, and what symbols are in this file.

| Stack | Detected by | Test command | Output parsed as | Can start |
|-------|-------------|--------------|------------------|-----------|
| Python | `pyproject.toml` / `setup.py` / `pytest.ini` / … | `pytest` | pytest text | Django / Flask / FastAPI |
| Node | `package.json` | `npm`/`pnpm`/`yarn`/`bun test` | jest / vitest / mocha, or `--json` | Vite |
| NestJS | `package.json` with `@nestjs/core` | ditto | ditto | `nest start --watch` |
| Next.js / Nuxt / Angular | `next` / `nuxt` / `@angular/core` | ditto | ditto | dev server |
| Go | `go.mod` | `go test ./...` | `--- FAIL:` text **or** `-json` | — |
| Rust | `Cargo.toml` | `cargo test` | cargo text (all targets summed) | — |
| Java (Maven) | `pom.xml` | `mvn -q -B test` | **JUnit XML** → Surefire text | Spring Boot |
| Java (Gradle) | `build.gradle[.kts]` | `./gradlew test` | **JUnit XML** → Gradle text | Spring Boot |
| Ruby | `.rspec` / `spec/` / `Gemfile` | `bundle exec rspec` | rspec / minitest | Rails |
| _(fallback)_ | `Makefile` with a `test:` target | `make test` | exit code only | — |
| _anything_ | `--test-cmd "<cmd>"` | your command | **keeps the detected parser** | ditto |

Parsers try structured output first (JSON / XML) and fall back to text. That means
upgrading a command — `--test-cmd "go test -json ./..."` — makes parsing *more*
accurate with no code change.

### Monorepos

A repository is no longer assumed to be one language. `scan()` finds every
project unit, and changes are verified against the units they actually touch:

```
$ repo-pilot detect --repo ../shop
识别到 5 个项目单元（monorepo）
  .              node       pnpm test
  apps/api       nestjs     pnpm test
  apps/search    go         go test ./...
  apps/web       nextjs     pnpm test
  apps/worker    python     pytest
```

Each unit gets **its own baseline** — you can only compare what you measured —
and its own parser, since the workspace root often understands nothing while
`apps/web` speaks vitest.

## Runtime Verification (opt-in)

For bugs that only exist once the app renders, three components sit behind the
tools, each with its own lifecycle:

- **Runtime Manager** starts services in dependency order and waits until they are
  *actually usable*. `npm run dev` returns in a second; Next.js needs fifteen more
  to compile. Only checking "is the process alive" makes the agent open a page,
  get a connection refused, and start fixing a bug that doesn't exist. Readiness
  needs real evidence: an HTTP health check, or a known ready-line in the logs.
- **Browser Session** hands the model a numbered table of interactive elements
  rather than HTML (which blows the context) or CSS selectors (which break on any
  restyle). Each snapshot tags the DOM with stable refs, so `click(ref="e12")`
  means exactly one element — and flags anything **covered or disabled**, which is
  how "the button renders fine but does nothing" gets caught.
- **Evidence Collector** records console errors, uncaught exceptions and failed
  requests from the first millisecond of page load — errors happen during first
  paint, long before an agent could react. Credentials are redacted **before**
  anything is written to disk; a secret already on disk has already leaked.

Reproduction steps are frozen into a [JSON scenario](examples/) so the run before
the fix and the run after it are provably the same actions and the same
assertions. If the scenario passes *before* any change, RepoPilot stops and
reports that the issue does not hold on the current code — that is a legitimate
result, not a failure.

## Core Design Decisions

- **Verification is the whole point.** Run the test suite before touching anything to
  record a baseline, then run it again after. "Fixed" is a measured fact, not a
  claim the model makes about itself.
- **Uncertainty is explicit, never disguised as certainty.** Every `TestReport`
  carries a `parsed` flag. The old code scraped `(\d+) passed` and returned 0 when
  the regex missed — so "zero failures" and "I don't understand this output" were
  indistinguishable, and the verifier confidently compared fabricated zeros for
  every non-pytest stack. Now it says `confidence: low` and the Reviewer is told
  not to treat "no new failures" as evidence.
- **The Reviewer is context-isolated.** It only sees the issue, the plan, the diff, and
  the evidence — never the executor's own "I fixed it" narrative. An agent
  reviewing its own conversation history will always approve; isolation is what
  makes the review real. `explorer.py` reuses the same mechanism for a different
  payoff: the ten files it reads to locate a bug never enter the main context.
- **Adapter pattern.** The core never branches on `kind`. Supporting a new stack
  means adding one file and one registry line — the state machine, executor and
  reviewer don't change.
- **Hard constraints over soft ones.** Paths are jailed to the repo root, dangerous
  commands are blocklisted, browser actions that look irreversible (pay, delete,
  place order) bounce back to a human, API keys are stripped from every child
  process environment, the browser can only reach localhost, and there's a cap on
  files modified and loop turns. `git commit`/`git push` are blocked outright — the
  final commit is always a human decision.
- **Progressive tool disclosure.** 28 tools exist; 13 are exposed by default. Tool
  lists are re-sent every turn, and more options means more chances to pick wrong —
  hand a pure-Python library task nine browser tools and it will try to open a
  browser.
- **Whoever starts something owns closing it.** Services and browsers are torn down
  by a `CLEANUP` state *and* a `finally` block: the state is observable, the
  `finally` is reliable. `stop()` also waits for the port to actually be released —
  the process exiting is not the same event as the kernel freeing the socket.
- **Fully observable.** Every state transition and tool call is appended to
  `runs/<timestamp>/run.jsonl`. Evaluation is just aggregating over these files.

## Roadmap

- [x] **Phase 0–3 (MVP, current)** Full loop: Plan / Execute / Verify (baseline
      comparison + retry) / Review (isolated context) / Trace / multi-stack
      adapters / safety policy / CI + lint
- [x] **Multi-language depth** Adapters answer seven questions instead of two:
      per-framework test-output parsing (pytest / jest / vitest / mocha / go / cargo /
      JUnit XML / rspec / minitest, structured-first), validation pipelines
      (lint → typecheck → test → build), multi-language symbol outlines, environment
      requirements, and monorepo scanning with per-unit baselines
- [x] **Runtime verification** Runtime Manager (dependency-ordered startup, real
      health checks, log capture, process-group teardown), Browser Session
      (ref-tagged snapshots, covered/disabled detection, localhost-only), Evidence
      Collector (console / network / screenshots, redacted before write), and
      structured reproduction scenarios replayed identically before and after
- [ ] **Phase 4 (shell)** Direct GitHub issue fetching (prototype exists), automatic
      branch + draft PR creation
- [x] **Evaluation harness v2 — built to survive being questioned**
      ([`eval/`](eval/), [methodology](eval/README.md)). The v1 Lite script was
      retired because it had a confirmed leak: it scoped the agent's test command
      to paths extracted from the official `test_patch`, i.e. it told the agent
      which file the hidden tests lived in. Not malice — convenience. Convenience
      is inevitable, so the boundary is now structural rather than disciplinary:
      - **Inference and grading are separate processes.** `PublicInstance` carries
        exactly five fields; `test_patch` / `FAIL_TO_PASS` / `PASS_TO_PASS` / gold
        exist only in `grader.py`. An AST check in CI fails the build if an
        inference-side module so much as names them. The test command now comes
        only from adapter detection plus the repo's own test directories.
      - **SWE-bench Verified with a pre-declared sampling frame**, split into
        frozen `dev` (15) / `test` (30) / `holdout` (10) lists committed to git
        with checksums. The denominator is the frozen list — instances that fail
        to build stay in it and are reported separately, never dropped.
      - **Immutable manifest per run**: agent commit, prompt hash, tool-schema
        hash, budget fingerprint, instance checksum, harness version.
      - **One budget object** shared by every variant, enforced in the loop rather
        than requested in the prompt; cost reported alongside every effect number,
        priced with cache-hit rates counted separately.
      - **Three baselines** (single-shot / plain ReAct / full) compared **pairwise
        on the same instances**, because a net percentage hides the column where
        the extra machinery made things worse.
      - **Patch auditing** (test tampering, added skips, removed assertions,
        dependency pinning, debug leftovers) and **single-cause failure
        classification**, so a failure count turns into a decision about what to
        fix next.
      - Environment artifacts are suppressed by two mechanical rules — interpreter
        chosen from the repo's own `requires-python`, dependencies installed as of
        the base commit's date — replacing v1's hand-tuned version pins. A table of
        magic numbers you can tune is a numerator you can tune.
      First real sweep (dev split, 15 frozen / 9 gold-certifiable), and the
      uncomfortable part is the point of having built it: **full RepoPilot
      resolved 4/9 — the same four instances a plain ReAct loop resolved, at
      twice the tokens** (545k vs 270k, 6/9 exhausting the budget against 1/9).
      Single-shot patching scored 0/9, so nothing here is guessable from the
      issue text alone. Whether the Reviewer helps is **not measurable at this
      scale**: a negative control (a variant differing only by a tool that was
      never invoked once) flipped 2 of 9 instances, so a 1-instance effect is
      below the noise floor — temperature 0 is not determinism on an MoE model,
      and pairing removes instance difficulty but not run-to-run variance.
      Traces say why: 6 of 13 instances never called `edit_file` at all, burning
      the budget still reading. The bottleneck is the absence of context
      compaction, and every mechanism added here sits downstream of it.
      dev is now a development sample — its failure traces were read and acted
      on — so these are not headline numbers. See [`eval/README.md`](eval/README.md).
- [ ] **Phase 5 (deep end, one at a time)** Docker sandbox in place of the
      blocklist (a container boundary is kernel-level; a substring blocklist is not) /
      tree-sitter or LSP in place of regex symbol extraction / context compaction /
      dependency-graph retrieval / the official Docker harness as the environment
      layer, which is also what would let the sampling frame grow past seven repos

### Known gaps (stated, not hidden)

- **No context compaction.** History only grows; the evaluation peak hit 42.6k
  against a 64k window. `explore` reduces the pressure by keeping localisation
  spills out of the main context, but it is not a substitute for compaction.
- **Symbol extraction outside Python is regex-based** — it misses and it
  over-matches. It buys 80% of the value ("roughly what's here, at roughly which
  line") for zero new dependencies; the real fix is tree-sitter or LSP.
- **The blocklist and the irreversible-intent guard are substring matches** — weak
  defences by construction. A payment button labelled "Continue" walks straight
  through. The structural answer is that irreversible actions belong to humans and
  agents belong on test data.
- **No semantic relevance check.** The guardrails on scope are quantitative (file
  count, unique-match edits). An agent that changes the wrong thing *within* the
  allowance isn't caught.
- **No persistence.** A crashed run restarts from scratch.
- **The evaluation's environment layer is not the official Docker harness.** Two
  mechanical rules (era-appropriate interpreter, date-bounded dependencies) push
  the artifact rate down a long way, but per-instance official images would push it
  further — and are what the sampling frame would need to grow past seven repos.
- **Reviewer false kills can only be reported as "suspected."** Establishing a real
  one means grading the rejected revision on its own, which isn't done yet.
- **No published headline number for the v2 harness.** It is verified end to end on
  a real SWE-bench Verified instance; the frozen splits have not been swept. The v1
  Lite numbers are retracted (leak — see the roadmap), and quoting them anywhere
  would be quoting a measurement taken through a hole in the wall.

## Development

```bash
pip install -e ".[dev]"   # installs pytest + ruff
pytest -q                 # 106 tests, offline — no API key needed
ruff check .              # lint
```

The runtime tests are **not** mocked: they start a real `http.server`, assert that
startup waits for a real health check, and assert that stopping actually frees the
port (including through a shell → child → grandchild process chain, which is what
`pnpm dev` really looks like). Mocking that away would test nothing.

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
