# Agent Automation Test Design

## Objective

Add four complementary automation layers to the local-agent demo without making the default test suite depend on Ollama, Node.js, a browser, network access, or real user data:

1. a strict session-bundle validator and CLI;
2. optional real-Ollama smoke tests;
3. deterministic Streamlit AppTest coverage;
4. a real-browser Playwright smoke workflow backed by a deterministic fake runtime.

The existing 114-test suite remains the fast default gate. No production capability, workspace, corpus, tool budget, or report schema changes are in scope.

## Architecture

### Session bundle validator

`agent_runtime/session_validator.py` is a pure reader layered on the existing replay/session models. Its public interface is:

```python
validate_session_bundle(session_dir: Path | str) -> ValidationReport
```

`ValidationReport` is immutable and contains the session ID, workspace, terminal status, checked artifact paths, errors, and warnings. Validation never repairs or mutates a bundle.

Errors cover malformed files, illegal or over-budget counters, artifact path escape, missing required completed artifacts, completed reports with dangling evidence IDs, incomplete patent sessions with a completed report, and unmasked meeting phone/email values. Warnings cover non-terminal bundles and optional artifacts that no longer exist.

`scripts/validate_agent_session.py` exposes the validator. A valid bundle exits `0`; validation errors exit `2`; malformed CLI input also exits nonzero. Output is human-readable by default and supports deterministic JSON for automation.

### Optional Ollama smoke tests

`tests/test_ollama_smoke.py` is marked `ollama`. Tests are skipped unless `RUN_OLLAMA_SMOKE=1` is set. When enabled, the module checks that Ollama is connected and that `qwen3.5:9b` and `nomic-embed-text` are present; missing prerequisites produce an explicit skip rather than downloads or service startup.

The meeting and patent golden paths use the existing production runtime. Assertions are structural: terminal state, budget counters, expected artifacts, and a successful session-validator report. Model prose is not snapshot-tested. Generated session and benchmark data remain under ignored local directories.

### Streamlit test runtime

The production UI receives a narrow dependency-injection boundary rather than a parallel test-only application. Runtime construction and session-root selection are passed into focused rendering helpers. An explicit `AGENT_DEMO_TEST_MODE=1` environment variable selects a deterministic fake runtime only in automated browser processes. Normal execution ignores the fake implementation.

The fake runtime uses the real `AgentRuntime`, `SessionStore`, playbooks, tools, evidence persistence, clarification states, and artifact writers. Only model/search adapters are deterministic. This keeps UI tests representative without Ollama.

`tests/test_agent_streamlit_app.py` uses `streamlit.testing.v1.AppTest` to cover navigation order, workspace switching, plan visibility, approval, clarification, cancellation plumbing, Replay distinction, and access to Classic Audit. Tests use temporary session roots and do not edit rules or start audio/model services.

### Playwright browser smoke

`scripts/playwright_agent_smoke.sh` starts Streamlit on an available local port with `AGENT_DEMO_TEST_MODE=1` and an isolated temporary session root. It uses the repository's existing Playwright CLI wrapper through `npx`, never installs global packages, and exits clearly when `npx` is absent.

The browser flow verifies Agent Demo is first, runs a deterministic meeting session through approval and clarification, opens Replay for the resulting bundle, and switches to Classic Audit. Snapshots and screenshots go only to ignored `output/playwright/`. The script owns and cleans up its Streamlit process and temporary files on every exit.

The browser workflow is opt-in and is not executed by default `pytest -q`, because it requires Node.js, a browser download/cache, a local port, and subprocess management.

## Commands

Default deterministic gate:

```bash
uv run pytest -q
```

Optional real-model smoke:

```bash
RUN_OLLAMA_SMOKE=1 uv run pytest -m ollama -q
```

Session validation:

```bash
uv run python scripts/validate_agent_session.py sessions/<session-id>
```

Browser smoke:

```bash
bash scripts/playwright_agent_smoke.sh
```

## Error handling and safety

- No test starts Ollama, downloads a model, installs a dependency, or mutates a knowledge base.
- Default tests do not require network access.
- Fake mode requires an explicit environment variable and is documented as test-only.
- Session validation never follows artifact paths outside the session directory.
- Test session directories are temporary or ignored.
- Browser subprocesses are terminated through a shell trap.
- No raw meeting material, `.env`, model data, vector database, browser profile, session bundle, or screenshot is committed.

## Acceptance criteria

- Existing default tests remain green without Ollama or Node.js.
- Validator tests demonstrate valid, malformed, dangling-evidence, escaped-path, over-budget, sensitive-data, and false-completion cases.
- `pytest -m ollama` selects only the optional real-model smoke tests; without opt-in they skip cleanly.
- Streamlit AppTest exercises both top-level views and the agent state transitions without Ollama.
- Playwright controls a real browser against the fake-backed Streamlit server and cleans up all processes.
- Full compile, whitespace, ignored-file, and secret-risk checks pass.
- Final handoff lists only genuinely visual, qualitative, and hardware-experience checks as remaining manual work.

## Out of scope

- CI service configuration, GitHub Actions, container images, or remote browser grids.
- Exact LLM prose assertions or deterministic latency targets.
- Automatic model installation, Ollama startup, or browser installation.
- Visual regression baselines or pixel-perfect screenshot comparison.
- Changes to the two agent capabilities, their budgets, or the Classic Audit product behavior.
