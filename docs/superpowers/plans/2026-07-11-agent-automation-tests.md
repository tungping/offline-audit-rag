# Agent Automation Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automate session-bundle integrity, optional real-Ollama smoke coverage, deterministic Streamlit interaction tests, and an opt-in real-browser Playwright flow while keeping the default test suite offline and deterministic.

**Architecture:** Add a pure validator beside Replay, expose it through a small CLI, and reuse it from optional live-model tests. Refactor only the Agent Demo construction boundary so Streamlit AppTest and a test-only environment mode can use the real runtime/state machinery with deterministic adapters. Keep Playwright as an opt-in shell workflow using the existing CLI wrapper and a locally started fake-backed Streamlit server.

**Tech Stack:** Python 3.10+, pytest, Streamlit `AppTest`, existing Ollama/ChromaDB adapters, Bash, Playwright CLI through `npx`.

## Global Constraints

- Do not add or update Python or JavaScript dependencies.
- `uv run pytest -q` must remain independent of Ollama, Node.js, browser binaries, network access, and real user data.
- Real Ollama tests run only when `RUN_OLLAMA_SMOKE=1`; they never start Ollama or download models.
- Fake browser mode activates only with `AGENT_DEMO_TEST_MODE=1` and writes sessions beneath `AGENT_DEMO_SESSION_ROOT`.
- Playwright output belongs under ignored `output/playwright/`; no screenshots, traces, browser profiles, sessions, vector stores, raw meeting data, or `.env` files are committed.
- Do not change capability tool allowlists, model/tool/query budgets, corpus content, output schemas, or Classic Audit behavior.
- All new production behavior follows a red-green-refactor cycle and every task ends with a focused commit.

---

### Task 1: Add strict read-only session validation and CLI

**Files:**
- Create: `agent_runtime/session_validator.py`
- Create: `scripts/validate_agent_session.py`
- Create: `tests/test_agent_session_validator.py`
- Modify: `agent_runtime/__init__.py`

**Interfaces:**
- Consumes: `load_replay(session_dir)`, `AgentSession.from_dict`, `ResourceBudget`, the session `artifacts/` directory, and workspace-specific artifact names.
- Produces: `ValidationIssue`, `ValidationReport`, `validate_session_bundle(session_dir) -> ValidationReport`, and CLI flags `session_dir` plus `--json`.

- [ ] **Step 1: Write failing validator tests**

Create bundles through `SessionStore` and assert this public behavior:

```python
report = validate_session_bundle(session_dir)
assert report.valid is True
assert report.errors == ()

report = validate_session_bundle(bundle_with_dangling_evidence)
assert report.valid is False
assert any(issue.code == "dangling_evidence" for issue in report.errors)
```

Add separate tests for malformed JSON, `model_calls` above budget, an artifact path outside the session, a completed meeting bundle missing one required artifact, an incomplete patent bundle containing `patent_research_report.md`, raw `13812345678` in persisted meeting data, and CLI JSON/exit codes.

- [ ] **Step 2: Run validator tests and verify RED**

Run:

```bash
uv run pytest tests/test_agent_session_validator.py -q
```

Expected: import failure for `agent_runtime.session_validator`.

- [ ] **Step 3: Implement immutable validation results**

Use these exact result types:

```python
@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str

@dataclass(frozen=True)
class ValidationReport:
    session_id: str
    workspace: str
    status: str
    checked_artifacts: tuple[str, ...]
    errors: tuple[ValidationIssue, ...]
    warnings: tuple[ValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.errors
```

Load files without mutation. Resolve every artifact path and require it to be under `<session_dir>/artifacts`. Scan report/CSV text for 32-character evidence IDs and require each reference to exist in `evidence.json`. Enforce workspace required files only for `COMPLETED`; forbid a completed patent report for `INCOMPLETE`. Check counters against the serialized budget and scan all persisted meeting files for unmasked mobile/email patterns.

- [ ] **Step 4: Implement the CLI**

`scripts/validate_agent_session.py` must insert the repository root when executed directly, call `validate_session_bundle`, print either JSON or concise issues, exit `0` when valid, and exit `2` for validation or input errors. It must never repair files.

- [ ] **Step 5: Run focused and regression tests**

```bash
uv run pytest tests/test_agent_session_validator.py tests/test_agent_replay.py tests/test_agent_security.py -q
uv run python scripts/validate_agent_session.py --help
```

Expected: all pass; help lists `--json`.

- [ ] **Step 6: Commit validator**

```bash
git add agent_runtime scripts/validate_agent_session.py tests/test_agent_session_validator.py
git commit -m "feat: validate agent session bundles [Codex (GPT-5)]"
```

### Task 2: Add opt-in real Ollama smoke tests

**Files:**
- Create: `tests/test_ollama_smoke.py`
- Modify: `pyproject.toml`
- Modify: `README.md`

**Interfaces:**
- Consumes: `build_live_runtime`, both demo input files, `check_ollama_status`, and `validate_session_bundle`.
- Produces: pytest marker `ollama` and two explicitly opted-in live smoke cases.

- [ ] **Step 1: Write the selected-but-skipped test contract**

At module import, define:

```python
pytestmark = pytest.mark.ollama
RUN_LIVE = os.getenv("RUN_OLLAMA_SMOKE") == "1"
```

Each test calls a shared prerequisite function. Without `RUN_OLLAMA_SMOKE=1`, it uses `pytest.skip("set RUN_OLLAMA_SMOKE=1 ...")`. With opt-in but missing service/models, skip with the exact missing prerequisite. Tests must never call `ollama.pull` or start a process.

- [ ] **Step 2: Run marker selection and verify RED**

```bash
uv run pytest -m ollama -q
```

Expected before the file/marker exists: no selected Ollama smoke tests and a marker warning or missing expected cases.

- [ ] **Step 3: Implement two live structural cases**

For each workspace, build the production runtime, create and approve one session, run serially, skip a meeting clarification when requested, and assert:

```python
assert session.status in {
    SessionStatus.COMPLETED,
    SessionStatus.INCOMPLETE,
    SessionStatus.FAILED,
}
assert session.model_calls <= session.budget.max_model_calls
assert session.tool_calls <= session.budget.max_tool_calls
assert session.query_rounds <= session.budget.max_query_rounds
```

When status is `COMPLETED`, require `validate_session_bundle(...).valid` and workspace artifact names. Use `tmp_path / "sessions"` by replacing the runtime store; do not write repository `sessions/`.

- [ ] **Step 4: Register marker and document commands**

Add this under `[tool.pytest.ini_options]`:

```toml
markers = [
    "ollama: opt-in tests that require a running local Ollama and installed models",
]
```

Document both default and opt-in commands, prerequisites, skip semantics, and the absence of automatic downloads.

- [ ] **Step 5: Verify offline default and opt-in selection**

```bash
uv run pytest tests/test_ollama_smoke.py -q
uv run pytest -m ollama -q
uv run pytest -m "not ollama" -q
```

Expected in the current offline environment: two explicit skips for the first two commands; all deterministic tests pass for the third.

- [ ] **Step 6: Commit Ollama smoke layer**

```bash
git add tests/test_ollama_smoke.py pyproject.toml README.md
git commit -m "test: add optional Ollama agent smoke tests [Codex (GPT-5)]"
```

### Task 3: Add deterministic Streamlit AppTest coverage

**Files:**
- Create: `agent_runtime/demo_factory.py`
- Create: `tests/test_agent_streamlit_app.py`
- Modify: `agent_webui.py`
- Modify: `webui.py`
- Modify: `tests/test_agent_webui.py`

**Interfaces:**
- Consumes: real `AgentRuntime`, `SessionStore`, meeting/patent playbooks and tools, existing synthetic corpus, and Streamlit `AppTest`.
- Produces: `build_demo_runtime(workspace, source_text, source_name, session_root)`, `runtime_factory_from_environment()`, and render functions with injectable runtime/session roots.

- [ ] **Step 1: Write failing fake-runtime and AppTest tests**

First test `build_demo_runtime` directly: a meeting session reaches `WAITING_FOR_CLARIFICATION`, can skip, completes, and validates. Then use `AppTest.from_file("webui.py")` with `AGENT_DEMO_TEST_MODE=1` and `AGENT_DEMO_SESSION_ROOT=<tmp>` to assert:

```python
assert [option for option in at.radio[0].options] == ["Agent Demo", "Classic Audit"]
assert "Technical Project Meeting Audit" in at.selectbox[0].options
assert at.button(key="agent_approve").label == "Approve Plan & Run"
```

Cover patent workspace selection, required-input warning, plan visibility, one clarification submission/skip, distinct Replay badge, and switching to Classic Audit without invoking Ollama.

- [ ] **Step 2: Run AppTest tests and verify RED**

```bash
uv run pytest tests/test_agent_streamlit_app.py -q
```

Expected: import failure for `agent_runtime.demo_factory` or Ollama construction reached from the UI.

- [ ] **Step 3: Implement deterministic adapter factory**

Create real runtimes with deterministic adapters:

- meeting model returns the same structured task/decision fixture used by golden tests and exact quotes from `meeting_with_gaps.txt`;
- meeting rule search returns one fixed QA rule;
- patent feature model extracts the fixed bottom-shield feature from the demo brief;
- patent keyword search uses production `keyword_search`;
- patent semantic search returns `SYN-SIC-009` and `SYN-SIC-001` as typed `PatentHit` values.

All other runtime, state, evidence, tools, budgets, playbooks, and artifact writers remain production implementations.

- [ ] **Step 4: Add the narrow UI construction boundary**

Add stable widget keys (`agent_mode_selector`, `agent_workspace_selector`, `agent_goal`, `agent_material`, `agent_approve`, `agent_cancel`, `agent_submit_clarification`, `agent_skip_clarification`, `agent_replay_path`) and replace the direct `build_live_runtime` call with an injected factory chosen as follows:

```python
if os.getenv("AGENT_DEMO_TEST_MODE") == "1":
    return build_demo_runtime(..., session_root=Path(os.environ["AGENT_DEMO_SESSION_ROOT"]))
return build_live_runtime(...)
```

The environment variable is ignored unless its value is exactly `1`. Classic rendering remains unchanged.

- [ ] **Step 5: Run AppTest and full UI regressions**

```bash
uv run pytest tests/test_agent_streamlit_app.py tests/test_agent_webui.py tests/test_app.py -q
uv run python -m py_compile webui.py agent_webui.py agent_runtime/demo_factory.py
```

Expected: all pass without Ollama.

- [ ] **Step 6: Commit deterministic UI automation**

```bash
git add agent_runtime/demo_factory.py agent_webui.py webui.py tests/test_agent_streamlit_app.py tests/test_agent_webui.py
git commit -m "test: automate Streamlit agent flows [Codex (GPT-5)]"
```

### Task 4: Add opt-in Playwright real-browser smoke and final gate

**Files:**
- Create: `scripts/playwright_agent_smoke.sh`
- Modify: `README.md`
- Modify: `.gitignore` only if `output/playwright/` is not already covered by `output/`

**Interfaces:**
- Consumes: `npx`, `/Users/tenan/.codex/skills/playwright/scripts/playwright_cli.sh`, `uv run streamlit`, the explicit fake-mode environment, and stable widget labels/keys from Task 3.
- Produces: one self-cleaning opt-in browser workflow and ignored screenshots/snapshots under `output/playwright/`.

- [ ] **Step 1: Write a failing shell contract test**

Add a Python test in `tests/test_agent_streamlit_app.py` that invokes:

```bash
bash scripts/playwright_agent_smoke.sh --check
```

It must assert exit `0`, no server start, and output containing checks for `npx`, the wrapper, Streamlit, fake mode, output path, and cleanup trap. Expected initially: file-not-found failure.

- [ ] **Step 2: Run the contract test and verify RED**

```bash
uv run pytest tests/test_agent_streamlit_app.py -k playwright_script_contract -q
```

Expected: nonzero because the script is absent.

- [ ] **Step 3: Implement the self-cleaning browser script**

The script must:

1. use `set -euo pipefail`;
2. verify `npx`, the Playwright wrapper, and `.venv/bin/streamlit`/`uv` availability;
3. create temporary session/log paths and `output/playwright/`;
4. choose a free localhost port with Python;
5. export `AGENT_DEMO_TEST_MODE=1` and `AGENT_DEMO_SESSION_ROOT`;
6. start Streamlit in the background and poll its health endpoint with a bounded timeout;
7. use the wrapper to open the page, snapshot before each ref-based interaction, select/fill/click the deterministic meeting flow, submit or skip clarification, switch to Replay using the generated session path, then switch to Classic Audit;
8. save a final screenshot under `output/playwright/`;
9. close Playwright and terminate Streamlit/temp files in an `EXIT` trap.

`--check` performs prerequisite and static-contract checks only. It must not start Streamlit or Playwright.

- [ ] **Step 4: Run prerequisite and browser smoke**

```bash
bash scripts/playwright_agent_smoke.sh --check
bash scripts/playwright_agent_smoke.sh
```

Expected: `--check` passes. Full smoke passes when the Playwright browser is available; if the wrapper reports a missing browser installation or sandbox restriction, record the exact environmental failure without installing anything.

- [ ] **Step 5: Run the complete final gate and inspect artifacts**

```bash
uv run pytest -q
uv run python -m py_compile \
  app.py agent_cli.py webui.py agent_webui.py \
  audit_core/*.py agent_runtime/*.py \
  capabilities/meeting_audit/*.py capabilities/patent_research/*.py \
  scripts/*.py
git diff --check
git status --short --ignored
git diff --stat main...HEAD
git diff main...HEAD
```

Expected: deterministic tests pass; two Ollama tests are skipped unless opted in; browser outputs and sessions are ignored; no raw input, `.env`, vector store, model, credential, or browser profile is tracked.

- [ ] **Step 6: Update README and commit browser automation**

Document all four automation commands, prerequisites, default/optional boundaries, generated local paths, and the remaining manual visual/hardware checks. Then commit:

```bash
git add scripts/playwright_agent_smoke.sh README.md tests/test_agent_streamlit_app.py
git commit -m "test: add Playwright agent smoke flow [Codex (GPT-5)]"
```

## Completion gate

Before integration, report separately:

- deterministic pytest count and skips;
- validator CLI result against a generated valid bundle;
- Ollama smoke status and exact skip/failure reason;
- Playwright `--check` and full-browser result;
- ignored local artifacts created;
- remaining manual tests limited to visual clarity, qualitative model output, and M1 Pro resource experience.
