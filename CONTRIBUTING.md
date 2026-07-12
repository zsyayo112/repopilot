# Contributing to RepoPilot

Thanks for your interest. RepoPilot is a hand-written coding agent with a
deliberate design stance — **no agent framework** (no LangChain, no LangGraph).
The tool-calling loop, permission gate, structured output, and state machine are
all plain, readable Python. Please keep it that way: contributions that add a
heavyweight framework dependency to the core will not be accepted.

## Development setup

```bash
pip install -e ".[dev]"     # installs pytest + ruff
cp .env.example .env        # only needed to actually run the agent, not for tests
```

Tests and linting run **offline** — no API key required (the model client is
lazily constructed, so importing the package never touches your key).

```bash
pytest -q          # run the test suite
ruff check .       # lint
ruff check --fix . # auto-fix import order and simple issues
```

## The main extension point: adding a Repository Adapter

The core agent is framework-agnostic. Everything it knows about a specific tech
stack lives in one file: [`repopilot/adapters.py`](repopilot/adapters.py). The
core only ever reads two things off a `RepoProfile`: `kind` (which it *logs*,
never branches on) and `test_cmd` (which it treats as an opaque string to run).

So supporting a new language/framework means adding one detector — nothing else
changes:

1. Write a `_detect_<stack>(root: Path) -> RepoProfile | None` function. Return a
   `RepoProfile` if you recognize the repo (by a marker file such as `Cargo.toml`,
   `go.mod`, `pom.xml`, …), otherwise `None`.
2. Register it in the `_DETECTORS` list (order = priority; language-specific
   markers before generic fallbacks like `Makefile`).
3. Add a test in [`tests/test_adapters.py`](tests/test_adapters.py) following the
   existing one-per-stack pattern.

If your detector required touching `orchestrator.py`, `executor.py`, or
`verifier.py`, something is wrong — that would break the framework-agnostic
guarantee. Those files should never need to know your stack exists.

## Safety constraints are hard constraints

Path jailing, the dangerous-command blocklist, the modified-files cap, and the
`git commit`/`push` block in [`policy.py`](repopilot/policy.py) exist on purpose.
If you change one, add a test that pins the new behavior — safety rules are
enforced by code and tests, never by prompt wording alone.
