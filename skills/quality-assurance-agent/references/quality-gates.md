# Quality Gates

Quality gates verify whether confirmed business cases can be executed safely and repeatably. They are not business test cases. Do not count `Maven/Vitest/node:test/Playwright install/compile/build/test list` checks as cases in `test-cases.json`; link them to cases as execution evidence.

## G1: Requirement And Test-Case Review

Pass when:

- Requirement facts and assumptions are listed.
- Open questions are recorded.
- `test-cases.json` validates and contains business operation cases, not pure tooling checks.
- Multi-model review findings are synthesized.
- User confirms test cases.

## G2: Unit Tests And Coverage

Pass when:

- Frontend/backend unit tests for confirmed unit cases exist.
- Configured unit commands pass.
- Coverage meets configured thresholds or the report explains why coverage was unavailable.
- Unit commands map back to confirmed business cases or helper-level logic that supports them.

Suggested defaults:

- Line coverage: 80
- Branch coverage: 70
- Function/method coverage: 80
- New-code coverage: 90

## G3: API And Contract Tests

Pass when:

- API tests validate method, path, request schema, response schema, error codes, auth/permission, and idempotency where relevant.
- Contract assumptions are backed by OpenAPI/docs/code.
- Test data is isolated or reset safely.

## G4: E2E User Flow

Pass when:

- Critical user flows are covered by E2E or explicitly justified as not automatable.
- E2E tests assert real API responses for submit/state-changing actions, not only DOM transitions.
- Artifacts such as HTML report, trace, screenshots, or videos are linked when available.
- Playwright Test Agents installed/listed is environment readiness; it only supports G4 and does not pass G4 by itself.

## G0: Environment And Tooling Readiness

Track separately from business cases:

- Required runtimes: Python, Git, Node/npm/npx, Java, Maven, `rg`.
- Local secrets and account env vars: `QA_AGENT_LLM_API_KEY`, configured account username/password env vars, and local-only `.qa-agent/local/.env`.
- Local services: URLs from `.qa-agent/local/services.local.json`; run `doctor --strict --check-services` before real-local E2E.
- Encoding readiness on Windows: `doctor` warns about non-UTF-8 consoles; generated JSON/HTML/Markdown artifacts must still pass `check-mojibake --strict`.
- Test discovery: real test roots exist; globs match files; the E2E runner can list tests when E2E is planned.
- Browser/Playwright agent readiness: installed agents and browser dependencies.
- External dependencies: database, Redis, queue, network service, credentials, or browser server availability.

Default `doctor --strict` policy:

- Missing existing test files are warnings, because the agent may generate or map tests after business cases are confirmed. Use `--require-existing-tests` only when validating a mature regression suite.
- Missing Playwright Test Agents are warnings, because they support E2E automation but are not required for case design or non-E2E execution. Use `--require-e2e-tools` only when E2E tooling must be complete before continuing.
- Required local service reachability remains blocking only when `--check-services` is used and the service is marked required.

G0 failures usually block execution. They should mark affected business cases `blocked`, not `failed`, unless the business assertion actually ran and failed.

When a failure is actionable from local evidence, the agent should repair it before escalating. Escalation is for the case where the **next round cannot produce a new hypothesis** — re-running the same fix or restating the same explanation is not a repair attempt, it is spinning. Run the full loop described in `failure-repair-loop.md` (up to `maxRepairLoops` rounds, default 5) before reporting a blocker, or escalate immediately when the expected result cannot be derived from authoritative sources.

## Completion Gate

`assert-completion` is the final deterministic gate between quality-gate evidence and readiness language. It must run before final reporting whenever `test-spec-tasks.json` exists.

Pass when:

- Every P0/P1 business case is confirmed and has at least the configured minimum spec-task count.
- Every confirmed P0/P1 spec task is implemented or has an allowed blocked/deferred/skipped terminal state.
- Every confirmed P0/P1 spec task has exact mapping, execution evidence, or a documented blocker/deferral with owner and next action.
- Existing test suites are mapped to exact spec tasks instead of being treated as blanket coverage.

Fail when:

- Any P0/P1 case is unconfirmed or under the minimum task count.
- Any P0/P1 task is still `pending`, `not_started`, or `not-run`.
- Any passed/failed P0/P1 task lacks exact mapping or execution evidence.
- Any P0/P1 task is `skipped`, `blocked`, or `deferred` without explicit allowance and evidence.
- A report claims `Ready` or full QA completion while completion-check status is failed.

## Code Review Gate

Run `assert-code-review` after code review writes `.qa-agent/current/code-review.json`.

Pass when:

- `code-review.json` exists and has `status`.
- Status is not failed/blocking/not-ready.
- No unresolved P0/P1/critical/high finding remains.

Fail when:

- Code review was not executed or the artifact is missing.
- The artifact has blocking status.
- Any unresolved blocking review finding exists.

## Readiness Gate

Run `assert-readiness` before using final readiness language. It combines completion gate, code review, and HTML report evidence.

Pass when:

- `completion-check.json` passed.
- `code-review.json` passes `assert-code-review`.
- The HTML report exists.

Fail when any required artifact is missing or not passed. Do not say `Ready` without `readiness-check.json`.

Readiness wording:

- `Ready`: completion gate passed, all required business assertions passed, code review has no blocking finding, and readiness gate passed.
- `Conditionally Ready`: completion gate passed with allowed blockers/deferred work or non-blocking risks accepted.
- `Not Ready`: required business assertions failed or blocking environment/product defects remain.
- `Incomplete`: required spec tasks, code review, report, or readiness evidence is missing/not implemented/not executed.

## Code Review

Pass when:

- Independent review finds no unresolved blocking architecture, security, regression, or missing-test issue.
- Findings are documented with severity and file references.
- Remaining non-blocking risks are listed.
