# Spec Task Planning

Use this reference after business `test-cases.json` is confirmed and before writing test files.

## Principle

Business cases are for user confirmation. Spec tasks are for strict implementation. Do not write tests from vague intent; generate and validate the spec task backlog first.

## Commands

```bash
python "$QA_AGENT_DIR/scripts/qa_agent.py"generate-spec-tasks --cases .qa-agent/current/test-cases.json --repo . --output .qa-agent/current/test-spec-tasks.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"coverage-balance --spec-tasks .qa-agent/current/test-spec-tasks.json --output .qa-agent/current/coverage-balance.json --strict
python "$QA_AGENT_DIR/scripts/qa_agent.py"assert-completion --cases .qa-agent/current/test-cases.json --spec-tasks .qa-agent/current/test-spec-tasks.json --output .qa-agent/current/completion-check.json
```

Useful tuning:

```bash
--ratio unit=0.60,integration=0.20,api=0.15,e2e=0.05
--min-specs-by-priority P0=8,P1=5,P2=3,P3=1
--max-e2e-per-case 2
```

## Spec Task Contract

Each task must include:

- `id`
- `sourceCaseId`
- `priority`
- `layer`: `unit`, `integration`, `api`, or `e2e`
- `platform`（缺省 `web`）：`android-real` / `android-emulator` / `ios-sim` / `ios-real`
- `focus`
- `targetProject`
- `targetFile`（`@agent:mobile-mcp` 等 `@` 前缀表示 agent 直驱、没有脚本文件）
- `testName`
- `businessActor`
- `operationPath`
- `assertions`
- `oracle`: `ui`, `api`, `db`, `sideEffects`, and `negativeAssertions`
- `command`
- `implementationStatus`
- `executionStatus`
- `evidence`

`command` / `targetFile` 可以为空，但只在两种情况下成立：`needsManualCommand: true`（generic
或 mobile-e2e profile，实现阶段必须补齐），或 `targetFile` 以 `@` 开头（agent 直驱，没有脚本文件）。
`assert-completion` 会拦截「声称通过但 command 为空」的假通过。

## Generation Profiles

| profile | 产物 |
|---|---|
| `development`（默认） | 完整金字塔：unit / integration / api / e2e |
| `mobile-e2e` | 只生成 e2e；`platform=android-real`、`targetFile`/`command` 为空、`needsManualCommand=true` |

`mobile-e2e` 用于无源码黑盒（已安装的 APK）。配套跑 `coverage-balance --profile mobile`：
目标配比 e2e 100%、每优先级最少 1 条，并且不再判「E2E 过重」。

## Coverage Rules

- Unit tasks should be the largest group——**有源码时**。`mobile-e2e` profile 例外：没有 unit 层可写，e2e 占 100%，由 `--profile mobile` 放行。
- Integration should be fewer than unit but more than E2E.
- API should cover contracts and authorization without duplicating all E2E flows.
- E2E should only cover high-risk main flows and a small number of critical branches.
- P0/P1 cases must not be under-specified. If `coverage-balance` fails, repair the spec task plan before implementation.
- P0/P1 risks from `.qa-agent/current/risk-analysis.json` must appear in task assertions or `oracle`; otherwise record an explicit blocker/open question.

## Execution Rules

- Implement tasks one by one.
- Update implementation and execution status after each task.
- Attach command output, screenshots, DB evidence, or report paths to non-empty `evidence`; a `passed` or `failed` task without evidence is not complete.
- Keep `targetFile`, `testName`, `command`, and `assertions` populated for every implemented task; existing suites only count when mapped to these exact fields.
- Keep `oracle` populated so execution knows what to assert at UI/API/DB/side-effect/negative levels.
- If a task is blocked, record the blocking dependency and do not mark the source business case as passed.
- Run `assert-completion` before final reporting. If it fails, the run is incomplete; continue with the next task batch instead of reporting full completion.
- Running an existing suite does not automatically satisfy generated tasks. A task is satisfied only when it records the matching existing or newly added test file, command, assertions, execution status, and evidence.
- Use `skipped` only for tests the runner actually skipped. Use `pending`/`not_started` for unimplemented tasks, `blocked` for environment/data/requirement blockers, and `explicitly_deferred` only when the user approved postponement.

## Completion Gate

`assert-completion` enforces P0/P1 by default:

- Fails when a P0/P1 case is still unconfirmed or has fewer than the configured minimum task count.
- Fails when a P0/P1 task is still `pending`, `not_started`, or `not-run`.
- Fails when a `passed`/`failed` task lacks mapping (`targetFile`, `testName`, `command`, `assertions`) or non-empty `evidence`.
- Fails when a P0/P1 task is `skipped`, `blocked`, or `deferred` unless the caller passes the matching `--allow-*` flag and the task includes non-empty `blocker`, `evidence` or `notes`, `owner`, and `nextAction`.
- Allows executed failures by default so the QA run can complete as `Not Ready`; pass `--fail-on-failed` when the target is ship readiness.
