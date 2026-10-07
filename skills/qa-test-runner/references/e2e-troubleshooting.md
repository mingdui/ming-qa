# E2E 故障排除参考

## E2E 常见障碍处理（必须先修复，不跳过）

| 障碍 | 症状 | 修复方式 |
|------|------|----------|
| 促销弹窗遮挡 | 点击被 `<span>Claim My Free Box</span> intercepts pointer events` | 在 fixture 中主动查找弹窗按钮关闭（`dismissClaimPopup` 模式），不依赖 Escape 键 |
| 登录态时序 | 登录后页面重定向到 `/` 而非目标页 | 加大 waitForTimeout 或在 reload 后等 AuthContext 初始化完毕 |
| MCP selector 解析错误 | `Unexpected token while parsing css selector` | 降级到 `browser_run_code_unsafe` 写裸 Playwright 代码（`page.locator()` / `page.getByRole()`） |
| auth middleware 重定向 | 已设 token 但导航到 `/inventory` 后跳回 `/` | 确认 `localStorage.setItem` 在 `page.goto` 之前完成，token 字段名正确 |
| 移动端设备未授权 | `adb devices` 显示 `unauthorized` | 在手机屏幕上点「允许 USB 调试」；弹窗不出现时执行 `adb kill-server && adb devices` 重新触发 |
| 移动端设备歧义 | 多台设备在线，操作对象不确定 | 显式指定 `--device <serial>`，不允许静默选取第一台 |

## E2E 执行路径选择

E2E task 有三种可执行形态，按优先级选择：

1. **独立 bash 脚本**（`tests/api/<module>/tc-p*-*.sh`）：直接 `run-with-env` 执行，不依赖浏览器。
   适用于以 API 调用为主的 E2E task（如"取消弹窗不产生 API 请求"可通过 network request 拦截验证）
2. **Playwright MCP 逐条手写**（无独立脚本文件时）：使用 `browser_run_code_unsafe` 逐步骤操作浏览器。
   适用于需要真实 UI 交互的验证（弹窗、Toast、跳转、余额刷新）
3. **@playwright/test runner**（`tests/e2e/<module>/*.spec.ts`）：把 spec 文件注册到 `playwright.config.ts`
   的 projects 列表后，通过 `npx playwright test` 批量执行。需要：project 配置 + 共享 fixture 就绪

当前默认策略：优先 Playwright MCP（方式 2），因为它能直接读取 localStorage、网络面板、快照，调试效率最高。`@playwright/test` spec 文件作为可追溯的测试蓝图保存，但日常回归不依赖 runner。

## 环境变量传递（Windows 高频问题）

不要在 bash 调用里手动 `source .env + env + sed`。直接用 `python "$QA_AGENT_DIR/scripts/qa_agent.py"run-with-env`，它内部处理 CRLF→LF、注释过滤、env 内联和日志记录。

## MySQL MCP 降级

如果 `read_query` 报 "Can't add new command when connection is in closed state"，说明 MCP 连接已断。通过后端 API（如 `GET /user/profile`）做降级验证，并在 evidence 中标记"MySQL MCP 不可用时的降级验证"。
