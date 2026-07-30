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
stack lives in [`repopilot/adapters/`](repopilot/adapters/) — one file per stack.
The core reads `kind` (which it *logs*, never branches on) and `test_cmd` (an
opaque string to run); everything else it gets by calling the adapter.

The contract is [`adapters/base.py`](repopilot/adapters/base.py). Only three
members are mandatory; the rest degrade to honest defaults:

| Member | Required | Default if you skip it |
|--------|----------|------------------------|
| `kind` | ✅ | — |
| `detect(root)` | ✅ | — |
| `test_command()` | ✅ | — |
| `parse_test_output(out, code)` | | exit code only, `parsed=False` |
| `validation_steps()` | | just the test step |
| `services()` | | nothing startable |
| `e2e_command()` | | none |
| `env_requirements()` | | nothing checked |
| `symbols(path)` | | dispatches on file suffix via `symbols.py` |

Adding a stack:

1. Create `repopilot/adapters/<stack>_stack.py` with a `RepoAdapter` subclass.
   `detect()` must be **zero-cost** — check for marker files only. No reading
   source, no model calls, no subprocesses: monorepo scanning runs detection
   against every directory in the tree.
2. Register the class in `registry.ADAPTERS`. **Order is priority** —
   language-specific markers before generic fallbacks like `Makefile`, which must
   stay last (plenty of Python and Go repos also ship a Makefile, and there we'd
   rather use the language's own knowledge).
3. Implement `parse_test_output` if at all possible. Without it the verifier can
   only see an exit code — it cannot tell whether the failure count went down or
   whether a *new* test broke. Try structured output (JSON/XML) first and fall back
   to text, so that upgrading the command later improves accuracy for free.
4. Set `parsed=False` whenever you could not read the numbers. Never return zeros
   you didn't actually parse — "zero failures" and "I don't understand this output"
   must stay distinguishable.
5. Add tests: detection in [`tests/test_adapters.py`](tests/test_adapters.py), and
   output parsing in [`tests/test_adapters_parse.py`](tests/test_adapters_parse.py)
   using a real output sample. Include the ugly cases — a compile/collection error
   (where *no* test ran, which is worse than a failure, yet looks like zero
   failures) and an output your parser cannot read.

If your adapter required touching `orchestrator.py`, `executor.py`, `verifier.py`
or `reviewer.py`, something is wrong — that would break the framework-agnostic
guarantee. `grep -rn 'kind == "' repopilot/orchestrator.py` should stay empty.

## Safety constraints are hard constraints

Path jailing, the dangerous-command blocklist, the modified-files cap, and the
`git commit`/`push` block in [`policy.py`](repopilot/policy.py) exist on purpose.
If you change one, add a test that pins the new behavior — safety rules are
enforced by code and tests, never by prompt wording alone.
