# claude-queue（多引擎改造版）使用说明

本仓库基于开源项目 [`vasiliyk/claude-queue`](https://github.com/vasiliyk/claude-queue) 二次开发。

**改造目标**：把底层执行器从 Claude Code CLI 替换为 **Antigravity CLI（`agy`）**、
**Google Gemini CLI（`gemini`）** 与 **OpenAI Codex CLI（`codex`）** 多引擎，并为其注入
**外部硬核验收（Verifier）**、**Git 守护（Git Guard）** 与 **睡醒早报（MORNING_REPORT.md）**，
使其可以真正无人值守地跑完一整本任务书。

---

## 一、改造内容速览

| 维度 | 原始项目 | 改造后 |
| --- | --- | --- |
| 执行器 | 硬编码 `claude -p <prompt>` | 多引擎可配置：`agy` / `gemini` / `codex` |
| 默认引擎 | — | 主/备均为 `codex`（本机 `agy`、`gemini` 不在 PATH，避免白耗 attempt 1） |
| 失败处理 | 同引擎重试 | **第 2 次起自动切换到备用引擎**（故障轮替） |
| 成功判定 | 仅看 CLI 退出码 | CLI 退出码 **+** `verify_commands` 全部返回 0 |
| 验收失败 | 无 | 捕获报错尾部拼进下一轮 prompt，让 AI 自我修复 |
| 成功归档 | 无 | `git add .` + `git commit -m "AutoTask: [任务ID] 完成"` |
| 失败熔断 | 无 | `git reset --hard` + `git clean -fd` 回滚，写 `STUCK.md`，标记 `SKIPPED` |
| 退出产物 | 无 | 自动生成 `MORNING_REPORT.md`（耗时 / 状态 / 引擎 / commit hash） |
| Claude 依赖 | `requests` + claude.ai API | **已彻底移除**（无 `usage` 子命令、无 `requests` 依赖） |
| 跨平台 | 仅 Linux/macOS（依赖 `fcntl`） | Windows / Linux / macOS 均可运行 |

> 注：类名 `ClaudeWorker` 为保持上游测试兼容而保留，实现已不再 shell 调用 `claude`。

---

## 二、前置准备

```powershell
# 依赖（Python >= 3.10）
python -m pip install pyyaml
```

### 1. 配置 Codex CLI（默认引擎）

确保 `codex` 在 PATH 中：

```powershell
where.exe codex
# 例如：C:\Users\<you>\AppData\Local\OpenAI\Codex\bin\codex.exe
```

### 2. 可选：配置 Antigravity CLI（`agy`）

命令格式：

```text
agy -p "<prompt>" --dangerously-skip-permissions
```

若 `agy` 未注册到 PATH（Antigravity 桌面版常见情况：只有 IDE 内的
`resources\bin\language_server.exe agentapi`），请显式指定可执行文件路径：

```powershell
$env:AGY_BIN = "C:\path\to\agy.exe"      # 指向真实的 agy 可执行文件/包装脚本
setx AGY_BIN "C:\path\to\agy.exe"        # 永久生效
```

### 3. 可选：配置 Google Gemini CLI（`gemini`）

命令格式（`--yolo` 等价于 agy 的跳过权限确认）：

```text
gemini -p "<prompt>" --yolo
```

```powershell
setx GEMINI_BIN "C:\path\to\gemini.cmd"  # 若不在 PATH，可显式指定
```

### 4. 引擎解析顺序

引擎可执行文件的解析顺序（见 [resolve_engine_binary](claude-queue.py)）：

1. 环境变量 `AGY_BIN` / `GEMINI_BIN` / `CODEX_BIN`（支持完整路径）
2. `shutil.which()` 查找（Windows 下自动识别 `.exe` / `.cmd` / `.bat`）

若选定的引擎解析不到，worker 会先尝试另一个引擎；仍失败则打印告警并直接执行，
失败会走正常重试/熔断流程。

### 5. 引擎轮替规则

- **第 1 次尝试**：`--primary-engine`（默认 `codex`）
- **第 2 次及以后**：`--fallback-engine`（默认 `codex`）

本机既无 `agy` 也无 `gemini`，因此默认两者都是 `codex`，避免第 1 次尝试白白消耗。
若你已装好 `agy`/`gemini`，可显式启用轮替：

```powershell
--primary-engine agy --fallback-engine codex
--primary-engine gemini --fallback-engine codex
```

---

## 三、任务书（YAML）字段说明

```yaml
tasks:
  - prompt: |
      让 AI 做的事（自然语言）
    session: "my-task"          # 任务唯一标识，供 depends_on 引用
    priority: 10                # 越大越先执行
    max_attempts: 3             # 最大尝试次数（第 2 次起切换到备用引擎）
    on_fail: skip               # fail | skip
    working_dir: "D:\\work\\proj"  # 目标工作区（git 仓库根目录或子目录）
    timeout: 1800               # 单次执行超时（秒）
    depends_on: ["other-task"]  # 前置任务 session 列表
    verify_commands:            # 外部硬核验收：全部返回 0 才 COMPLETED
      - "pytest"
      - "mvn -q test"
```

### 验收闭环（Verifier）

1. CLI 退出码为 0 后，按顺序在 `working_dir` 执行 `verify_commands`（受 `--verify-timeout` 超时控制）。
2. **所有命令返回码均为 0** → 判定 `COMPLETED`，并触发 git 自动提交。
3. 任一命令非 0 → 截取该命令 `stdout + stderr` 的**最后 2000 字符**，拼接到下一轮 prompt 末尾：

   ```text
   上次修改未通过验收，报错如下，请修复：
   <报错尾部>
   ```

   然后进入原有 retry 机制（第 2 次起切换到 `--fallback-engine`，默认 `codex`）。

### Git 守护（Git Guard）

| 时机 | 动作 |
| --- | --- |
| 验收全部通过 | `git add .` → `git commit -m "AutoTask: [任务ID] 完成"` |
| 重试耗尽且 `on_fail: skip` | `git reset --hard` + `git clean -fd` → 写 `STUCK.md` → 状态置 `SKIPPED` |

说明：

- 仅当 `working_dir` 或其上级存在 `.git` 时才执行 git 操作（否则静默跳过）。
- `STUCK.md` 在回滚**之后**写入，因此它会保留在工作区，便于人工排查。
- 被 `SKIPPED` 的任务，其下游 `depends_on` 任务不会被调度——队列会继续执行**不依赖它**的其它任务。
- 用 `--no-git-guard` 可整体关闭提交与回滚。

### 任务状态

`queued` / `running` / `completed` / `failed` / `rate_limited` / **`skipped`**

---

## 四、启动无人值守队列

### 1. 导入任务书

```powershell
python claude-queue.py batch task_book_demo.yaml            # 正式入队
python claude-queue.py batch task_book_demo.yaml --dry-run  # 仅预览
```

### 2. 前台运行（调试用）

```powershell
python claude-queue.py worker --idle 30
# --idle 30 表示队列空时每 30 秒轮询一次，进程常驻不退出
# 默认 --primary-engine codex --fallback-engine codex
```

### 3. 后台常驻（Windows）

```powershell
# 后台启动 worker，日志落盘
Start-Process -WindowStyle Hidden python `
  -ArgumentList "claude-queue.py worker --idle 30" `
  -RedirectStandardOutput "worker.log" -RedirectStandardError "worker.err.log"

# 查看进度
python claude-queue.py status
python claude-queue.py list
```

> 若需开机自启，可把上述命令包装成 `.ps1`，用「任务计划程序」设置触发器即可。

### 4. 常用参数

```powershell
python claude-queue.py worker `
  --idle 30 `                  # 空队列轮询间隔
  --timeout 3600 `             # 单任务默认超时
  --verify-timeout 600 `       # 单条验收命令超时
  --primary-engine codex `     # 也可选 agy / gemini
  --fallback-engine codex `
  --report-file .\MORNING_REPORT.md `  # 早报输出路径（默认 ./MORNING_REPORT.md）
  --save-output `              # 保存 CLI 输出到 ~/.claude-queue/outputs/
  --no-git-guard               # 关闭 git 自动提交/回滚
```

### 5. 睡醒早报（MORNING_REPORT.md）

队列跑完（或 Ctrl+C 终止）退出时，worker 会自动在工作区（队列运行目录）生成
`MORNING_REPORT.md`，内容包含：

- 开始时间 / 生成时间 / **总耗时** / 退出原因 / 引擎配置
- 每个任务的 **ID、任务名称、最终状态、实际尝试次数、使用引擎**
- 成功任务的 **Git Commit Hash**；失败任务的**失败摘要**（截断至 200 字符）

示例片段：

```markdown
- 总耗时: 0h 42m 13s（2533s）
- 引擎配置: primary=codex, fallback=codex
- 任务统计: 共 2 个（completed=1, skipped=1, failed=0, ...）

| 任务 ID | 任务名称 | 最终状态 | 尝试次数 | 引擎 | Git Commit / 失败摘要 |
| --- | --- | --- | --- | --- | --- |
| `task-a1b2c3d4e5f6` | demo-create-hello | completed | 1/3 | codex | `9f3c1ab` |
| `task-0f9e8d7c6b5a` | demo-update-world | skipped | 3/3 | codex | Verification failed: ... |
```

---

## 五、单任务添加示例

```powershell
python claude-queue.py add "修复登录接口的空指针异常" `
  --session fix-login-npe `
  --working-dir "D:\work\my-api" `
  --verify-command "pytest" `
  --verify-command "mvn -q test" `
  --on-fail skip `
  --max-attempts 3
```

`--verify-command` 可重复传入，形成验收命令列表。

---

## 六、自测与测试

### 1. 端到端自测（临时目录，非交付目录）

```text
%TEMP%\cq_selftest2\verify_report.py
```

覆盖三条关键链路 + 早报与默认引擎：

| 场景 | 期望结果 |
| --- | --- |
| 默认引擎 | `primary=fallback=codex`，`gemini` 构造器可用 |
| A. 验收通过 | `completed`，`attempts=1`，`engine=codex`，报含 commit hash |
| B. 连续验收失败 | `skipped`，脏代码回滚，`STUCK.md` 生成，报含失败摘要 |

```text
15/15 checks passed
```

### 2. 上游单元测试

```powershell
python -m pytest tests -q
# 93 passed
```

覆盖队列状态机、批量加载、worker 生命周期、验收/熔断/早报逻辑。
Windows 下需先安装 `pytest`、`pytest-mock`、`pyyaml`。

---

## 七、示例任务书

见 [`task_book_demo.yaml`](task_book_demo.yaml)：包含 2 个带 `depends_on` 与
`verify_commands` 的简单测试任务，演示「验收 + 自动提交 + 依赖链」完整闭环。

> 示例使用 `working_dir: "."`（即 worker 当前目录）便于本地试跑；
> **生产环境请改为真实项目路径**，避免污染当前目录。

---

## 八、故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| `⚠ Engine 'agy' not found on PATH` | 未配置 `agy`；用 `AGY_BIN` 指定路径，或改用默认 `codex` |
| 任务一直 `queued` 不执行 | 检查 `depends_on` 前置任务是否为 `completed`（`skipped` 会阻塞下游） |
| 验收老是失败但 CLI 成功 | 验收命令的 cwd 是 `working_dir`；确认命令在该目录下可独立通过 |
| git 未提交 | `working_dir` 不在 git 仓库内，或没有实际改动（commit 会被跳过，属正常） |
| 任务被 `SKIPPED` | 见工作区 `STUCK.md`，内含报错尾部与任务信息 |
| 想看整晚跑了什么 | 看工作区 `MORNING_REPORT.md`（或 `--report-file` 指定路径） |

---

## 九、兼容性说明

- 本项目已移除对 `fcntl` 的强依赖（Windows 上用 `msvcrt` 降级），可在 Windows 原生运行。
- **已彻底移除 Claude 依赖**：不存在 `ClaudeUsageChecker` 类、`usage` 子命令，
  `pyproject.toml` 不再依赖 `requests`；worker 启动无需任何 session key。
- 队列文件默认位于 `~/.claude-queue/tasks.json`（沿用上游路径），可用 `--queue-file` 指定。
- 原始项目的单元测试（`tests/`）保持兼容并在 Windows 下 100% 通过；
  需先安装 `pytest`、`pytest-mock`、`pyyaml`。
