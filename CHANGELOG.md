# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed — evaluation methodology rebuilt (v2)

The v1 SWE-bench Lite script (`eval/swebench_eval.py`) is **removed**, and its
results (`eval/RESULTS-v1-lite-DEPRECATED.md`) are **retracted**. It scoped the
agent's test command to file paths extracted from the official `test_patch` —
telling the agent which file the hidden tests lived in. The numbers it produced
cannot be used. Everything below is what replaced it.

- **Inference and grading are separate processes.** `PublicInstance` carries five
  fields and nothing else; `test_patch` / `FAIL_TO_PASS` / `PASS_TO_PASS` / gold
  patch live only behind `grader.py`. Enforced three ways: an AST check in CI that
  fails if an inference-side module *names* a grading symbol, a runtime assertion
  on the exact payload handed to the agent, and a check that rejects any test
  command containing a case-level `::` selector (directory paths are public; case
  IDs can only have come from `FAIL_TO_PASS`). Test scope now derives solely from
  adapter detection plus the repo's own test directories.
- **SWE-bench Verified with a pre-declared sampling frame.** The frame is a
  mechanical rule about what this machine can build (pure-Python, pip without
  compilation, pytest, top-level test dir) applied whole-repo, declared before
  sampling, and stored inside the split files alongside the instances it produced.
  Deterministic stratified sampling into frozen `dev`/`test`/`holdout` lists,
  committed to git with checksums that `load_split()` verifies on every read.
- **The denominator is the frozen list.** Instances that fail to build stay in it
  and are reported on their own line. Both readings (full and gold-certifiable)
  are always printed, so neither can be cherry-picked.
- **Immutable manifest** per run — agent commit (`-dirty` when the tree isn't
  clean), prompt hash over every string that reaches the model, tool-schema hash,
  budget fingerprint, instance checksum, harness version. Re-writing one with a
  different configuration is an error.
- **Gold calibration runs before inference**, not after — afterwards leaves room
  to call whichever instance went badly an "environment artifact".
- **Environment artifacts suppressed by two mechanical rules**: the interpreter is
  chosen from the repo's own `requires-python`, and dependencies install as of the
  base commit's date (`uv --exclude-newer`). This replaces v1's hand-maintained
  version-pin table — every entry of which was a number tunable until an instance
  passed. Measured on `pallets__flask-5014`: Python 3.12 + today's dependencies
  make even the official gold patch fail; 3.9 + 2023-03-11 dependencies make it
  pass. Falling back (no `uv` installed) is recorded in the report, not silent.
- **Three baselines** — single-shot, plain ReAct, full — under one budget object,
  compared **pairwise on the same instances**. The four-cell table exposes the
  column a net percentage hides: instances the extra machinery lost.
- **Cost reported next to every effect number**, priced with cache-hit tokens
  billed separately, and `(no price table)` rather than `$0.0000` for unknown models.
- **Failure classification** into a single primary cause from observable fields
  only, aggregated into a tree that says what to fix next.

### Added — agent changes the evaluation required

- **`budget.py`** — `Budget` (frozen, hashable, derivable via `.variant()`) and
  `Ledger` (per-role token accounting, cost, hard `exhausted()` check run at the
  top of every executor turn). Ablation variants differ *only* in capability
  flags; a test asserts every size and token field stays equal across them.
- **`halting.py`** — stopping rules that read observable quantities, never the
  model's own account of itself: identical failure fingerprint twice, three rounds
  on one file without improvement, diff growing while failures don't shrink, 80%
  token pressure (drops `explore` rather than stopping), Reviewer repeating a
  request. Plus a **best-patch checkpoint**: the run submits its best round, not
  its last. Motivated by v1 traces showing 120 tool calls and 901s spent
  micro-adjusting one wrong approach.
- **`patch_audit.py`** — test tampering, added skips/xfails, net-removed
  assertions, touched eval config, changed dependency pins, hard-coded test IDs;
  plus quality metrics (file count, line churn, stray formatting, debug leftovers,
  public API changes). Overlap with gold is reported as a file-set Jaccard only —
  **never text similarity**, which would reward copying the shape of the reference
  answer rather than fixing the bug.
- **Structured Reviewer output** (`decision` / `issue_addressed` /
  `tests_sufficient` / `scope_minimal` / `risks` / `requested_actions`).
  `requested_actions` turns the Reviewer from a terminal into a loop — it feeds
  back into `EXECUTE` — and makes "is it repeating itself?" answerable.
- **Structured test evidence** (`TestFailure`, `TestReport.evidence()`,
  `TestReport.signature()`): test ID, exception type, source location, first line.
  Roughly a fifth of the characters of a raw log, and the location column tells the
  agent which file to open. The signature is what makes "the same failure twice"
  detectable at all — raw logs contain timings and temp paths and never compare equal.
- **`Workspace.apply_diff()`** for checkpoint restore.

### Fixed

- **`pytest -q` summary lines were never parsed.** The regex required the `====`
  decoration, but `-q` — which is what RepoPilot's own test command uses — prints a
  bare `462 passed, 15 skipped in 2.06s`. Every *green* run therefore came back
  `parsed=False` → `confidence: low` → the Reviewer was told "no new failures" was
  unreliable → it rejected correct patches. Observed live on `pallets__flask-5014`:
  the same patch went from `revise` to `accept` once the line parsed. Decoy lines
  in captured output are still ignored — the ` in 1.23s` suffix is what
  distinguishes a summary from a log line.
- **`HaltingPolicy` fired its no-progress rules on passing runs.** A green suite
  twice in a row has an identical (empty) failure fingerprint, which read as "stuck"
  and killed the round after a Reviewer rejection. Passing states are now exempt.

### Added — multi-language depth

- **`adapters.py` → `adapters/` package.** Adapters now answer seven questions
  instead of two. `RepoAdapter` is an explicit contract; each stack lives in its
  own `*_stack.py`; the core still never branches on `kind`.
- **Per-framework test-output parsing.** pytest, jest, vitest, mocha, `go test`
  (text *and* `-json`), cargo (summing every target), JUnit XML (Maven Surefire /
  Gradle, preferred over log scraping), rspec and minitest. Every parser tries
  structured output first, so upgrading a command — `--test-cmd "go test -json
  ./..."` — improves accuracy with no code change.
- **`TestReport.parsed`.** Distinguishes "zero failures" from "I could not read
  this output"; `compare()` surfaces it as `confidence: high|low` and the Reviewer
  is instructed not to treat a low-confidence "no new failures" as evidence.
- **Validation pipelines** (`run_validation`): lint → typecheck → test → build, in
  cost order, fail-fast, with optional steps skipped when the tool isn't installed
  (rather than counted as a failure). Fixes the classic false pass: unit tests
  green, `tsc` red.
- **Monorepo support.** `scan()` finds every project unit; each gets its own
  baseline and its own parser; `unit_for()` maps a changed file to its unit by
  longest prefix, so verification only re-runs the units a change touched.
  Cross-unit service collection (the runnable app is usually *not* the root unit),
  with automatic port de-confliction and `PORT` propagation.
- **Multi-language symbol outlines** (`adapters/symbols.py`): Python via real AST
  (falling back to regex on syntax errors — precisely when an agent mid-edit needs
  the outline most), plus TS/JS/Go/Rust/Java/Kotlin/Ruby via line-anchored regex.
- **Package-manager detection** for Node (npm/pnpm/yarn/bun) from lockfiles,
  searching up to the workspace root — a pnpm workspace's lockfile only exists at
  the repo root, and running `npm test` against a pnpm-installed tree fails in a
  way that looks like a code bug.
- **`repo-pilot doctor`** and the `check_environment` tool: separate "broken code"
  from "unprepared machine". The evaluation showed the cost of conflating them —
  failing instances burned 54–120 tool calls and up to 901s, against 8–25 calls and
  under 64s for the ones that were actually fixable.

### Added — runtime verification (opt-in: `--with-runtime` / `--scenario`)

- **`runtime.py` Runtime Manager.** Dependency-ordered startup, readiness by real
  evidence (HTTP health check or a ready-line in the logs) rather than "the process
  is alive", log capture via a pump thread (an unread pipe blocks the child),
  process-group teardown, and `stop()` waiting for the port to actually be freed.
- **`browser.py` Browser Session** (Playwright, optional dependency). Snapshots
  return a ref-tagged table of interactive elements — not HTML, not CSS selectors —
  and flag elements that are **covered or disabled**, which is how "renders fine,
  does nothing" is caught. Navigation is restricted to localhost by allowlist.
- **`evidence.py` Evidence Collector.** Console errors, uncaught page exceptions
  and failed requests recorded from the first millisecond of page load; secrets
  redacted *before* anything is written to disk; before/after diffing by set
  difference (equal counts can hide "fixed one, broke another").
- **`scenario.py` structured reproduction.** Steps and assertions as data, replayed
  identically before and after the fix. If the scenario passes *before* any change,
  the run stops and reports that the issue does not hold — a legitimate result.
- **`explorer.py` read-only exploration sub-agent.** Context isolation aimed at the
  largest single consumer of context: files read during localisation that turn out
  to be irrelevant but stay in history forever.
- **Three new state-machine states**: `START_RUNTIME`, `REPRODUCE`, `CLEANUP`.
  Conditional pass-throughs when runtime is off. `DONE` is still the only terminal
  state and everything still flows through `REPORT`; cleanup is additionally
  guaranteed by a `finally` block.
- **Tool capability groups.** 28 tools, 13 exposed by default; `runtime` and
  `browser` groups opt in. Tool lists are re-sent every turn and more options mean
  more chances to pick wrong.
- **Irreversible-intent guard** (`policy.looks_irreversible`): browser interaction
  is auto-approved except when the element name reads like pay / delete / place
  order. Unicode escapes are decoded first — models routinely send
  `\\u786e\\u8ba4\\u652f\\u4ed8` instead of 确认支付, which would walk straight
  through a naive substring check.
- **Secrets stripped from every child process environment** (`runtime.child_env`).
  `npm run dev` executes arbitrary code from the target repo's `package.json`;
  our API key has no reason to be in that process.

### Fixed

- **A green→red transition was reported as `no_change`.** Failed-test *names* are
  only parseable for pytest, so for every other stack `new_failures` was empty,
  and a baseline-green suite going red fell through every branch to `no_change` —
  breaking the test suite was reported as "this round accomplished nothing".
  Regressions are now detected by exit-code transition as well as by name.
- **`improved` could be concluded from unparsed numbers**, i.e. from fabricated
  zeros. It now requires both reports to be trustworthy.
- **`EnvRequirement.check()` ignored exit codes**, so a tool that was present but
  broken (half-installed, incompatible version) reported as available.
- **Monorepo unit tests were parsed by the root adapter**, which typically
  understands nothing, yielding "could not parse" for a sub-project whose own
  adapter speaks vitest.
- **`--test-cmd` was silently ignored on monorepos** in favour of per-unit
  commands. An explicit instruction from a human now overrides all detection.
- **pytest counts were scraped from the whole output**, so a "3 passed" printed by
  the code under test could be mistaken for the summary. Only the trailing summary
  line is read now.
- **jest/vitest failure names kept their timing suffix** (`(5 ms)`), so identical
  failures across two runs looked like new ones — a false regression every time.

### Added — earlier in this cycle
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
