# Benchmark Orchestrator Technical Design

- 状态：`REVISED`
- 修订日期：2026-07-21
- 实现目标：在独立 Benchmark Orchestrator 仓库中提供本地控制面，调用外部 OpenClaw benchmark runtime

## 1. 技术目标

1. 通过结构化 Run Spec 安全构造 canonical benchmark CLI；
2. 监督完整 CLI invocation，而不是重新实现 task runner；
3. 复用外部 runtime 的 progress、per-record、results artifact contract；
4. 提供可恢复的 create/start/cancel/resume 控制状态；
5. 不复制 OpenClaw session/workspace、VGB scorer 或 record checkpoint 状态；
6. 使实现可通过不调用模型的 contract/integration test 验证。

明确不实现：通用 Harness plugin、Gateway backend、SQLite task store、React frontend、WebSocket
stream、分布式 worker 和原地重跑已落盘失败 record。

## 2. 当前实现基线

### 2.1 关键模块

| 所属/路径 | 能力 |
| --- | --- |
| OpenClaw `benchmarking/workflow/cli.py` | canonical parser、selector、groups、waves、aggregate、Resume merge |
| OpenClaw `benchmarking/workflow/runners/single_llm.py` | OpenClaw single-LLM attempts、retry、answer contract |
| OpenClaw `benchmarking/runtime/single_llm_openclaw_wrapper.py` | `openclaw agent --local`、JSON/transcript recovery |
| OpenClaw `benchmarking/runtime/agent_workspace.py` | attempt workspace、audit、archive/quarantine |
| OpenClaw `benchmarking/runtime/session_isolation.py` | session isolation/postflight |
| OpenClaw `benchmarking/runtime/config_pool.py` | run-scoped OpenClaw configs |
| OpenClaw `benchmarking/runtime/cleanroom.py` | atexit、SIGINT/SIGTERM cleanup |
| OpenClaw `benchmarking/scoring/verifier_grounded_runtime.py` | pinned isolated VGB runtime |
| OpenClaw `benchmarking/dashboard/progress.py` | progress event/snapshot writer 与 reconciliation |
| OpenClaw `benchmarking/dashboard/service.py` | 现有 dashboard read model（Orchestrator 不 import） |
| OpenClaw `benchmarking/dashboard/app.py` | 现有 FastAPI/static GUI/annotation routes（可选） |
| Orchestrator `src/benchmark_orchestrator/*` | control API、GUI、runtime adapter、ArtifactReader、registry、supervisor |

### 2.2 已验证命令

本次只读复核的 OpenClaw CLI 为 `OpenClaw 2026.6.9 (c645ec4)`；`openclaw agent --help` 确认
`--local`、`--agent`、`--session-id`、`--message`、`--thinking`、`--timeout` 和 `--json` 均存在。
canonical wrapper 使用这组参数，不使用同一 CLI 也支持的 `--session-key`。

当前 OpenClaw release pin 已同步为 `verifier-grounded-benchmark==0.3.0`：source tag `v0.3.0`、
source commit `89ed5b9d83547bea98f6eeac4a03a131e33e8b90`、wheel SHA256
`b93c18b818e8d19993e817de6439ccea910b36a8f386c551078b7c6b10420381`。隔离 runtime 已验证三个
track 的版本均为 `0.3.0`，prompt 数量仍为 11/18/2。该 release 使用 `linear_goal_v2` 和 package
result schema `2`；OpenClaw 结果 writer 的 schema 仍为 `3`。

列出 VGB dataset：

```bash
cd ~/.openclaw/workspace
uv run --project ~/.openclaw/workspace python -m benchmarking.workflow.cli \
  --groups single_llm_skills_on \
  --datasets verifier_grounded_rdkit \
  --list-datasets
```

无模型调用预览：

```bash
uv run --project ~/.openclaw/workspace python -m benchmarking.workflow.cli \
  --groups single_llm_skills_on \
  --datasets verifier_grounded_rdkit \
  --record-ids rdkit_qed_max_001 \
  --print-selected-records
```

启动 Orchestrator GUI/API（独立仓库）：

```bash
cd /Users/xutao/Benchmark-Orchestrator
uv run python -m benchmark_orchestrator.app --config ~/.benchmark-orchestrator/orchestrator.yaml
```

OpenClaw 原有 dashboard 仍可独立启动，用于兼容性对照：

```bash
cd ~/.openclaw/workspace
uv run --extra web-ui python -m benchmarking.dashboard.app
```

## 3. 技术栈

Orchestrator 使用自己的 `pyproject.toml`/`uv.lock`；被调用的 OpenClaw workspace 继续使用自己的
`pyproject.toml`/`uv.lock`。两套环境不得通过源码 import 或 `PYTHONPATH` 合并：

| 层 | 技术 |
| --- | --- |
| Orchestrator runtime | Python `>=3.12`，`uv` |
| API | FastAPI `0.135.3`，Uvicorn `0.44.0` |
| Schema | Pydantic v2 |
| YAML | PyYAML `6.0.3`，只使用 `safe_load`/`safe_dump` |
| Frontend | Orchestrator 自有 static HTML/JavaScript/CSS |
| Process | `asyncio.create_subprocess_exec`、POSIX signal/process group |
| Execution state | 现有 JSON/JSONL artifacts |
| Control state | 原子写 JSON/YAML sidecar |
| Annotation | Orchestrator 自己的可选 SQLite，仅用于 annotation/metadata；不存 execution state |

不新增 SQLAlchemy、Alembic、TanStack Query 或 plugin framework。

## 4. 项目目录结构

完整 MVP 位于独立仓库 `/Users/xutao/Benchmark-Orchestrator`，OpenClaw workspace 是外部执行依赖：

```text
/Users/xutao/Benchmark-Orchestrator/
├── pyproject.toml                       # Orchestrator 自己的依赖和发布元数据
├── uv.lock
├── src/benchmark_orchestrator/
│   ├── __init__.py
│   ├── app.py                            # standalone FastAPI/static GUI entrypoint
│   ├── api.py                            # control/read routes
│   ├── config.py                         # orchestrator.yaml
│   ├── models.py                         # RunSpec/control/invocation models
│   ├── registry.py                       # atomic control sidecar store
│   ├── runtime_adapter.py                # external canonical CLI argv/capabilities
│   ├── artifacts.py                      # versioned read-only OpenClaw artifact contract
│   ├── service.py                         # create/cancel/resume use cases
│   ├── supervisor.py                      # local process supervision
│   └── static/                            # Orchestrator GUI
├── tests/benchmark_orchestrator/
├── docs/                                  # copied/referenced design docs and runtime contracts
└── README.md

~/.openclaw/workspace/                    # external OpenClaw repository/workspace
├── benchmarking/workflow/cli.py          # canonical execution entrypoint (owned by OpenClaw)
├── benchmarking/runtime/                 # runner, wrapper, isolation, scorer bridge
├── benchmarking/dashboard/               # optional existing read-only dashboard
├── benchmarking/resources/verifier_grounded/release.json
└── state/benchmark-runs/                  # execution artifacts owned by canonical CLI
    ├── formal/<benchmark>/<model>/<run-id>/
    └── temporary/<benchmark>/<model>/<run-id>/
```

OpenClaw 仓库可以增加 `benchmarking` 下的轻量 integration scaffold（例如 artifact fixture、
capability export 或 contract test），但不得放入 Orchestrator 的完整 API、GUI、registry、service
或 supervisor。Orchestrator 的 `control_root` 默认位于独立的
`~/.benchmark-orchestrator/state`，不嵌套到 OpenClaw checkout。

### 4.1 独立项目交付物

- 发布项目名为 `benchmark-orchestrator`，源码和 wheel 只包含 `benchmark_orchestrator` 包及其 static
  GUI，不 vendoring OpenClaw 或 VGB 源码；
- 每个 Orchestrator release 记录兼容的 OpenClaw runtime revision、canonical CLI contract 版本和 VGB
  release identity；这些信息用于启动 preflight 与 Resume drift 检查；
- 安装 Orchestrator 后，用户通过配置提供 `workspace_root`，不要求把 Orchestrator checkout 复制到
  OpenClaw workspace，也不要求向 OpenClaw 的 `pyproject.toml` 增加 Orchestrator 依赖；
- OpenClaw 侧 integration scaffold 可随 OpenClaw 发布，但其版本不等同于 Orchestrator release，跨仓库
  兼容性由 contract test 验证。

## 5. Python 包划分

### 5.1 `benchmark_orchestrator.models`

仅放不可变用户意图和控制状态模型：`RunSpec`、`SelectionSpec`、`AgentSpec`、`ExecutionSpec`、
`RunControl`、`Invocation`、`SelectedRecord`。不复制 `GroupRecordResult`。

### 5.2 `benchmark_orchestrator.runtime_adapter`

负责：

- 解析 capability；
- 构造 preview/run/resume argv；
- 解析 `--print-selected-records` 的机器可读 stdout；
- 复用 canonical output-path helper 生成分类 Run 路径；
- 生成脱敏 argv digest；
- 读取 runtime revision 和 VGB release identity。

它不 import runner、wrapper 或 VGB package，也不解析 `openclaw agent` stdout。

### 5.3 `benchmark_orchestrator.supervisor`

负责 process handle、PID/PGID、stdout/stderr、signal、wait 和启动恢复。它只知道
`RuntimeCommand`，不知道 dataset、record 或 verifier score。

### 5.4 `benchmark_orchestrator.registry`

负责 control root containment、原子写、单 Run lock 和 sidecar 读取。它不扫描 per-record；execution
artifact 查询由 Orchestrator 的 `ArtifactReader` 按外部 runtime contract 完成。

### 5.5 `benchmark_orchestrator.service`

组合 adapter、supervisor、registry 和 `ArtifactReader`，实现业务不变量：

- preview 后才能创建；
- run ID/output path 唯一；
- 全局最多一个 active Run；
- 同一 Run 最多一个 invocation；
- completed/无缺失项 Run 不 Resume；
- Cancel/Resume 幂等和状态冲突处理。

### 5.6 `benchmark_orchestrator.api`

只做 Pydantic request/response、错误映射和 HTTP routing。不得在 route 内拼 argv、发 signal 或修改
sidecar。

## 6. 数据模型

### 6.1 RunSpec

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


GroupId = Literal["single_llm_skills_on", "single_llm_skills_off"]
DatasetId = Literal[
    "verifier_grounded_rdkit",
    "verifier_grounded_xtb_xyz",
    "verifier_grounded_property_calculation",
]
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]


class SelectionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_ids: list[str] = Field(default_factory=list)
    offset: int = Field(default=0, ge=0)
    limit: int | None = Field(default=None, ge=1)


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=200)
    thinking: ThinkingLevel = "high"


class ExecutionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout_seconds: int | None = Field(default=900, ge=1)
    timeout_retries: int = Field(default=3, ge=0, le=10)
    timeout_retry_backoff_seconds: list[float] = Field(
        default_factory=lambda: [5, 15, 45]
    )
    max_concurrent_groups: int = Field(default=1, ge=1, le=2)
    inter_wave_delay_seconds: int = Field(default=0, ge=0, le=3600)
    analysis: bool = False


class RunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    name: str | None = Field(default=None, max_length=120)
    groups: list[GroupId]
    datasets: list[DatasetId]
    selection: SelectionSpec = Field(default_factory=SelectionSpec)
    agent: AgentSpec
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
```

附加 validator：

- `groups`、`datasets`、`record_ids` 非空项去空白后不得重复；
- groups 至少 1 个，最多 2 个；datasets 至少 1 个；
- model 不能包含控制字符；
- backoff 必须为有限非负数；长度不足 retry 次数时拒绝，而不是在 UI 层猜默认值；
- record ID 必须匹配 runtime 接受的安全字符集，最终存在性由 preview CLI 校验；
- `timeout_seconds=None` 明确表示 `--no-timeout`，不是字段缺失。

MVP 不暴露 `--single-agent-id-override`。skills-on/off 的 agent ID 继续由现有 experiment spec 决定，
避免 GUI 破坏既定组语义。

### 6.2 FrozenRun

创建时由 Backend 补全，不接受 Browser 指定：

```python
class FrozenRun(BaseModel):
    schema_version: Literal[1] = 1
    run_id: str
    run_category: Literal["formal", "temporary"]
    benchmark_slug: str
    model_slug: str
    output_dir: str
    spec: RunSpec
    spec_sha256: str
    selected_records: list["SelectedRecord"]
    selected_pairs: list[tuple[str, str]]  # group_id, record_id
    runtime_revision: str | None
    vgb_release_version: str
    vgb_wheel_sha256: str
    created_at: str
```

`spec_sha256` 对 canonical JSON 计算；YAML 只用于可读持久化，不能直接按文本 hash。

### 6.3 RunControl 与 Invocation

```python
ControlState = Literal[
    "created",
    "starting",
    "running",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]


class Invocation(BaseModel):
    invocation_id: str
    kind: Literal["start", "resume"]
    state: ControlState
    pid: int | None = None
    pgid: int | None = None
    process_started_at: str | None = None
    process_fingerprint: str | None = None
    argv_sha256: str
    launcher_log: str
    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None
    terminating_signal: int | None = None
    cancel_requested_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class RunControl(BaseModel):
    schema_version: Literal[1] = 1
    run_id: str
    state: ControlState
    active_invocation_id: str | None
    invocations: list[Invocation]
    updated_at: str
```

时间使用 UTC RFC 3339 `Z`。`process_fingerprint` 至少绑定 PID、OS process start time、executable 和
argv digest，防 PID 复用导致误杀。

### 6.4 TaskView

Task 只在查询时派生，不落到 control store：

```python
class TaskView(BaseModel):
    group_id: GroupId
    record_id: str
    selected: bool
    checkpoint: Literal["missing", "committed", "invalid"]
    progress_status: str | None
    result: dict | None
```

派生规则：

1. frozen preview 确定 selected pair；
2. 对应 per-record 不存在：`missing`；
3. 文件存在且 Orchestrator `ArtifactReader` 可按 runtime contract 解析：`committed`；
4. 文件存在但不可解析：`invalid`，Run failed，禁止自动覆盖；
5. result 中沿用 runtime schema v3 的全部独立状态轴；
6. 历史 schema v2 由现有 compatibility path 读取，不在 TaskView 重复升级。

### 6.5 WorkspaceView

Workspace 也是从 per-record `runner_meta.workspace_isolation` 和 artifact 列表派生的证据视图：

```python
class WorkspaceView(BaseModel):
    group_id: str
    record_id: str
    session_id: str | None
    attempt_index: int | None
    template_id: str | None
    audit_execution_status: str | None
    boundary_status: str | None
    contamination_status: str | None
    adjudication: str | None
    archive_asset: str | None
    quarantine_asset: str | None
```

不把绝对 workspace path 作为 Browser 可编辑字段。

## 7. 持久化设计

### 7.1 真相分层

| 数据 | 权威来源 | 写入者 |
| --- | --- | --- |
| 用户运行意图 | `control/.../spec.yaml` | Control Backend |
| process/invocation | `control.json` | Control Backend |
| selector preview | `preview.json` | Control Backend |
| launcher stdout/stderr | `launcher.log` | ProcessDriver |
| record checkpoint/result | `per-record/...json` | canonical CLI |
| runtime progress | `progress/state.json`, `events.jsonl` | `ProgressWriter` |
| aggregate/report | `results.json`, `runtime-manifest.json` | canonical CLI |
| annotation | Orchestrator annotation SQLite（可选） | AnnotationStore |

不建立一张同时复制这些字段的 Run/Task SQLite 表。

### 7.2 原子写

`spec.yaml`、`preview.json` 和 `control.json` 的协议：

1. 在同目录创建临时文件；
2. 写完整内容并 flush；
3. 对需要崩溃一致性的 control state 执行 `fsync`；
4. `os.replace()` 原子替换；
5. 必要时 fsync parent directory；
6. 文件权限默认 `0600`，目录 `0700`。

`launcher.log` 是 append-only 诊断流，不参与事务。

### 7.3 锁与并发

- `backend.lock` 使用 OS advisory lock，MVP 同一 control root 只允许一个 Backend；
- Backend 内每个 run ID 使用 `asyncio.Lock` 串行化 create/cancel/resume/reconcile；
- `max_active_runs=1` 是 MVP 固定策略，避免多个完整 benchmark Run 争抢本机 provider/CPU；
- group 内并发仍由 canonical CLI 的 `--max-concurrent-groups` 控制；
- 不实现数据库 lease 或跨主机锁。

## 8. Canonical CLI 映射

### 8.1 参数白名单

| Run Spec | CLI 参数 |
| --- | --- |
| `groups` | `--groups`，逗号连接 |
| `datasets` | `--datasets`，逗号连接 |
| `selection.record_ids` | `--record-ids`，逗号连接 |
| `selection.offset` | `--offset` |
| `selection.limit` | `--limit` |
| `agent.model` | `--single-agent-model` |
| `agent.thinking` | `--single-agent-thinking` |
| bounded `timeout_seconds` | `--single-timeout` |
| unbounded `timeout_seconds: null` | `--no-timeout` |
| `timeout_retries` | `--single-timeout-retries` |
| `timeout_retry_backoff_seconds` | `--single-timeout-retry-backoff-seconds`，逗号连接 |
| `max_concurrent_groups` | `--max-concurrent-groups` |
| `inter_wave_delay_seconds` | `--inter-wave-delay-seconds` |
| `analysis: false` | `--no-analysis` |
| canonical classified output | `--exact-output-dir` |
| Resume only | `--merge-existing-per-record` |
| Preview only | `--print-selected-records` |

不得映射 `--files`、`--subsets`、ChemQA/judge 参数、`--openclaw-config`、任意 raw args 或 shell
fragment。若以后暴露额外 canonical 参数，必须同时增加 schema、映射和 contract test。

### 8.2 argv 构造

```python
def build_command(spec: FrozenRun, *, preview: bool, resume: bool) -> RuntimeCommand:
    argv = ["uv", "run", "--project", str(workspace_root), "python", "-m", "benchmarking.workflow.cli"]
    argv += ["--groups", ",".join(spec.spec.groups)]
    argv += ["--datasets", ",".join(spec.spec.datasets)]

    selection = spec.spec.selection
    if selection.record_ids:
        argv += ["--record-ids", ",".join(selection.record_ids)]
    if selection.offset:
        argv += ["--offset", str(selection.offset)]
    if selection.limit is not None:
        argv += ["--limit", str(selection.limit)]

    if preview:
        argv.append("--print-selected-records")
        return RuntimeCommand(tuple(argv), workspace_root, sanitized_env())

    argv += ["--single-agent-model", spec.spec.agent.model]
    argv += ["--single-agent-thinking", spec.spec.agent.thinking]
    execution = spec.spec.execution
    if execution.timeout_seconds is None:
        argv.append("--no-timeout")
    else:
        argv += ["--single-timeout", str(execution.timeout_seconds)]
    argv += ["--single-timeout-retries", str(execution.timeout_retries)]
    argv += [
        "--single-timeout-retry-backoff-seconds",
        ",".join(str(value) for value in execution.timeout_retry_backoff_seconds),
    ]
    argv += ["--max-concurrent-groups", str(execution.max_concurrent_groups)]
    argv += ["--inter-wave-delay-seconds", str(execution.inter_wave_delay_seconds)]
    if not execution.analysis:
        argv.append("--no-analysis")
    argv += ["--exact-output-dir", spec.output_dir]
    if resume:
        argv.append("--merge-existing-per-record")
    return RuntimeCommand(tuple(argv), workspace_root, sanitized_env())
```

实际实现应拆为小型 helper 并由 snapshot test 覆盖，以上代码只定义顺序和字段语义。

### 8.3 环境

控制面启动 canonical CLI 时：

- 继承运行 OpenClaw workspace 所需的常规环境；
- 不向 Browser 或 launcher log 输出 env value；
- 不设置 `PYTHONPATH` 指向 Benchmark-Orchestrator checkout；
- 不覆盖 VGB scorer/agent 环境隔离；
- 可增加非敏感 `BENCHMARK_ORCHESTRATOR_INVOCATION_ID` 供诊断，但 runtime 不依赖它；
- `cwd` 必须是已 resolve 的 OpenClaw workspace root。

### 8.4 输出目录与 Run ID

当前 canonical CLI 在未指定 `--exact-output-dir` 时使用：

```text
<run-root>/<formal|temporary>/<benchmark>/<model>/<run-id>
```

Orchestrator 必须在实现 control 前把现有纯函数 `default_run_output_root()` 提取为 CLI/control 共用的
无副作用 helper，或直接复用等价的共享 helper；禁止在 control 中维护第二份路径拼接规则。因为
Control API 在启动前就必须知道稳定输出路径并支持 Resume，start/resume 仍显式传
`--exact-output-dir`，但该 exact path 必须是共享 helper 生成的 canonical 分类路径。

MVP 只允许 formal VGB datasets，因此 `run_category` 固定为 `formal`。当前 canonical run ID 示例：

```text
verifier-grounded-rdkit-qwen3-5-plus-20260721-153000
```

规则：

- 由 Backend 生成，不接受完整用户 ID；
- 输入文件全部来自 temporary benchmark root 时分类为 `temporary`，否则为 `formal`；
- 单 dataset 使用 dataset slug，多 dataset 使用 `mixed-datasets`；
- model 使用 provider/model 中最后一段的安全 slug；
- `run_id = <benchmark>-<model>-<YYYYMMDD-HHMMSS>`；
- slug 只允许 ASCII 小写字母、数字和 `-`；
- `output_dir = resolve(run_root / category / benchmark / model / run_id)` 且必须 containment；
- run ID 在整个递归 run root 下必须唯一，否则 ArtifactReader 或现有 OpenClaw dashboard 会按 basename 查询时报 ambiguous；
- 已存在非空 output/control 目录时拒绝 create，不自动覆盖。

## 9. 日志流与进度

### 9.1 三类数据流

| 流 | 文件 | 用途 |
| --- | --- | --- |
| Control | `control.json` | PID、invocation、cancel、exit |
| Runtime progress | `progress/state.json`, `events.jsonl` | group/record 进度 |
| Launcher diagnostics | `invocations/.../launcher.log` | CLI stdout/stderr/traceback |

三者不得合并成单个“日志状态机”。

### 9.2 现有 ProgressWriter 契约

现有事件类型：

```text
run_started
group_started
record_started
record_completed
group_completed
error
run_completed
```

`events.jsonl` append，`state.json` 保存 GUI-friendly snapshot。写入由进程内 lock 串行化，
但事件没有全局 sequence ID；MVP 不用它做 exactly-once 消息总线。

Resume 会启动新的 `ProgressWriter`：新的 invocation 追加 events，并重写 snapshot；ArtifactReader 会把
snapshot 与现存 per-record 文件 reconciliation。因此 UI 必须读 `/progress` snapshot，不能只计算
本次 invocation 的 event 行数。

### 9.3 Polling

- active Run：建议 1 秒轮询 control + progress；
- record 列表：completed count 变化时刷新，避免每秒读取全部 record；
- terminal Run：停止自动轮询；
- 失败请求采用指数退避，上限 10 秒；
- 页面恢复时直接 GET snapshot，不要求回放 events；
- 首版不提供 WebSocket。

### 9.4 Launcher log

ProcessDriver 使用 `stdout=PIPE, stderr=STDOUT` 获得单一字节流，后台 task 持续排空并原样追加到
`launcher.log`。不逐行增加时间或来源前缀，避免改变 traceback；invocation 的开始/结束时间保存在
`control.json`。达到大小上限后停止写盘但继续排空 pipe，并设置 `log_truncated=true`，防止日志限制
反向阻塞 CLI。

日志 API 使用 byte offset/limit：

```text
GET /api/runs/{run_id}/control/log?invocation_id=...&offset=0&limit=65536
```

最大单次 64 KiB；响应返回 `next_offset` 和 `eof`。禁止通过路径参数直接选择任意文件。

### 9.5 敏感数据

- 不在 control log 额外打印 argv 中的 prompt（canonical Run argv 本身不包含 record prompt）；
- 不记录 env values、runtime config 内容、OpenClaw token；
- per-record 中已有 prompt/response 属于 benchmark artifact，沿用现有 asset/read 权限，不复制到
  control log；
- launcher log 限制总大小，例如每 invocation 50 MiB，达到上限继续排空进程输出但截断落盘并记录
  `log_truncated=true`。

## 10. Resume 实现方案

### 10.1 恢复保证

Resume 保证：同一 selected `(group_id, record_id)` 如果已有可解析 per-record 文件，则 canonical
CLI 不再次运行它；缺失项使用新 invocation、session 和 attempt workspace 执行。

Resume 不保证：

- 从模型 token、Python instruction 或 OpenClaw session 内继续；
- 重跑已有失败 per-record；
- 保留相同 invocation ID；
- `results.json`/`runtime-manifest.json` append-only；
- 在 runtime/release 漂移后无提示继续。

### 10.2 缺失项计算

```python
selected = set(frozen_run.selected_pairs)
committed = artifact_reader.list_committed_pairs(run_id)
invalid = artifact_reader.list_invalid_checkpoint_pairs(run_id)
missing = selected - committed
```

- `invalid` 非空：拒绝 Resume，保留损坏文件供人工诊断；
- `missing` 为空：返回 `409 no_missing_records`；
- existing failure payload 只要可解析即属于 committed；
- 不能通过控制 API 删除/改名 per-record 来制造 missing。

如果 ArtifactReader 暂无 `list_committed_pairs`，新增 helper 只能复用已声明的 runtime artifact
contract 和受控 run root，不能建立第二个 schema upconverter。

### 10.3 Resume 算法

```python
async def resume_run(run_id: str, request_id: str) -> RunControl:
    async with run_lock(run_id):
        frozen = registry.load_frozen_run(run_id)
        control = await supervisor.reconcile(run_id)
        ensure_state(control, {"failed", "cancelled", "interrupted"})
        ensure_no_active_invocation(control)
        ensure_runtime_identity_compatible(frozen)
        ensure_missing_records_exist(frozen)

        command = runtime_adapter.build_run_command(frozen, resume=True)
        invocation = registry.append_starting_invocation(
            run_id=run_id,
            kind="resume",
            request_id=request_id,
            argv_sha256=sha256_argv(command.argv),
        )
        return await supervisor.start(run_id, invocation, command)
```

幂等：相同 `request_id` 返回已有 invocation；不同请求在 active Run 上返回 `409 run_active`。

### 10.4 身份校验

首次创建保存：

- normalized spec SHA；
- selected record IDs/group pairs；
- workspace root；
- runtime Git revision（可获取时）和 dirty 标记；
- VGB version/wheel SHA；
- canonical CLI argv digest。

Resume 必须重用 frozen spec。若 VGB version/hash 变化，直接拒绝；若 runtime revision/dirty state
变化，返回可解释 drift error 并要求创建新 Run。MVP 不提供 `force_resume` 绕过。

### 10.5 Progress 重建注意事项

canonical CLI 的 `--merge-existing-per-record` 根据文件存在性跳过，并在结束时从 per-record 重建
aggregate。Orchestrator 不预先编辑 `progress/state.json` 或 `results.json`。运行中的 ArtifactReader
reconciliation 负责把旧 checkpoints 与新 snapshot 合并显示。

## 11. Cancel 与启动恢复

### 11.1 启动

ProcessDriver：

1. 在 sidecar 中原子写 `starting` invocation；
2. 创建 launcher log；
3. `create_subprocess_exec(*argv, cwd=..., env=..., start_new_session=True)`；
4. 获取 PID/PGID/process start time 并写 `running`；
5. 后台 task `wait()`，结束后读取 artifacts 决定 terminal state；
6. API 返回 202，不阻塞到 benchmark 完成。

### 11.2 Cancel

```text
running
  -> persist cancelling + cancel_requested_at
  -> SIGTERM to canonical CLI PID
  -> wait cancel_grace_seconds
  -> if alive and fingerprint matches: SIGTERM process group
  -> wait kill_after_seconds
  -> if alive and fingerprint matches: SIGKILL process group
  -> persist cancelled or failed(cleanup/ownership error)
```

先 signal CLI PID 是为了触发现有 `cleanroom.py` 的 SIGTERM handler。不能第一步就同时杀死所有子
进程，否则可能阻止正常 archive/cleanup。

### 11.3 Backend 重启对账

启动时逐个读取非 terminal control：

1. 如果 final progress/results 完整且 selected checkpoints 齐全，标记 `completed`；
2. 如果 PID 不存在且没有 final evidence，标记 `interrupted`；
3. 如果 PID 存在且 process fingerprint 匹配，保持 `running`，标记
   `ownership="detached"` 并只轮询；
4. detached 进程退出后按 artifacts 判定 completed/interrupted；
5. PID 存在但 fingerprint 不匹配时不得 signal，标记 `interrupted` 并阻止 Resume，直到冲突 PID
   不再存在或人工确认；
6. 重启后无法获得原始 exit code时，不伪造 `failed`。

为了避免并发写 execution root，任何仍可能存活的旧 invocation 都阻止 Resume。

### 11.4 终态判定

| 证据 | Control state |
| --- | --- |
| current handle exit 0 + final artifacts valid | `completed` |
| current handle nonzero，非 cancel | `failed` |
| cancel requested 后进程退出 | `cancelled` |
| 重启后 PID 消失 + final artifacts valid | `completed` |
| 重启后 PID 消失 + final artifacts 不完整 | `interrupted` |
| exit 0 但 final artifact 无效 | `failed` |

`completed` 表示执行流程完成，不表示所有 record 得分成功。record-level failure 仍由 results 状态轴
展示。

## 12. 插件（Harness Adapter）加载机制

### 12.1 MVP 决策

MVP 没有动态 Harness plugin loader，不扫描 Python entry point，不加载用户路径模块，也没有
`HarnessAdapterFactory`。

Bootstrap 显式构造唯一 runtime adapter：

```python
runtime_adapter = CanonicalCliRuntimeAdapter(config.workspace_root)
supervisor = LocalRunSupervisor(config, registry)
artifact_reader = ArtifactReader(config.run_root)
service = RunService(runtime_adapter, supervisor, registry, artifact_reader)
```

OpenClaw integration 继续由 canonical runtime 内部的 `SingleLLMRunner` 和 wrapper 提供。

### 12.2 未来触发条件

只有第二种真实 Harness 已实现、无法通过现有 canonical CLI 的 group/runner 机制接入，并且与
OpenClaw 存在经过验证的差异时，才从两个实现提炼接口。届时必须先回答：

- 是否仍由同一个 canonical CLI 负责执行；
- session/workspace/answer contract 谁拥有；
- 两个 Harness 的 cancellation/resume 语义是否真的同构；
- plugin 是否可信代码、运行在进程内还是隔离进程。

在此之前，不为 Hermes、Gateway 或未知 Harness 在 YAML/API 中预留 discriminator。

## 13. 配置文件 Schema（YAML）

### 13.1 Backend 配置 `orchestrator.yaml`

```yaml
schema_version: 1

workspace_root: /Users/xutao/.openclaw/workspace
run_root: /Users/xutao/.openclaw/workspace/state/benchmark-runs
control_root: /Users/xutao/.benchmark-orchestrator/state

http:
  host: 127.0.0.1
  port: 8875
  poll_interval_ms: 1000

launcher:
  max_active_runs: 1
  cancel_grace_seconds: 15
  kill_after_seconds: 10
  max_log_bytes: 52428800
```

约束：

- `schema_version` 必须等于 1；未知字段拒绝；
- workspace 必须包含 `pyproject.toml` 和 importable `benchmarking.workflow.cli`；
- `run_root`、`control_root` 必须为绝对 resolve 路径，不能相同或互相嵌套；
- host 非 loopback 时启动失败，直到未来安全设计显式升级；
- MVP `max_active_runs` 必须为 1；
- 秒数和大小必须有合理上下界；
- YAML 使用 `safe_load`，不支持自定义 tag、环境变量替换或任意 Python object。

### 13.2 Run Spec `spec.yaml`

```yaml
schema_version: 1
name: rdkit-skills-comparison

groups:
  - single_llm_skills_on
  - single_llm_skills_off

datasets:
  - verifier_grounded_rdkit

selection:
  record_ids:
    - rdkit_qed_max_001
    - rdkit_sa_min_002
  offset: 0
  limit: null

agent:
  model: qwen3.5-plus
  thinking: high

execution:
  timeout_seconds: 900
  timeout_retries: 3
  timeout_retry_backoff_seconds: [5, 15, 45]
  max_concurrent_groups: 1
  inter_wave_delay_seconds: 0
  analysis: false
```

这是 Orchestrator 冻结的用户意图，不是新的 benchmark parser。真正 selector 校验仍由 preview CLI
完成。

### 13.3 无 timeout 示例

```yaml
execution:
  timeout_seconds: null
  timeout_retries: 3
  timeout_retry_backoff_seconds: [5, 15, 45]
  max_concurrent_groups: 1
  inter_wave_delay_seconds: 0
  analysis: false
```

`null` 映射 `--no-timeout`。它仍保留现有 runtime 的进程级安全阀。

### 13.4 禁止字段

以下内容不能出现在 Run Spec：

```text
raw_args
command
shell
environment
openclaw_config
gateway
session_key
harness_plugin
launcher_backend
benchmark_python_path
```

## 14. API 设计

### 14.1 Orchestrator read API

Orchestrator 提供与现有 artifact 语义一致的 read API。OpenClaw 仓库中的 dashboard API 仍保持原状，
但不是 Orchestrator 的进程内依赖：

| Method | Path | 作用 |
| --- | --- | --- |
| GET | `/api/runs` | 列出 Run |
| GET | `/api/runs/{run_id}` | Run snapshot |
| PATCH | `/api/runs/{run_id}` | alias/favorite/hidden |
| GET | `/api/runs/{run_id}/records` | record 列表 |
| GET | `/api/runs/{run_id}/records/{record_id}` | record 详情 |
| GET | `/api/runs/{run_id}/progress` | reconciled progress |
| GET | `/api/runs/{run_id}/assets/{asset_path}` | 受控 artifact |
| POST/PATCH/DELETE | `/api/annotations...` | Orchestrator annotation/metadata（可选） |

### 14.2 新增控制 API

| Method | Path | 返回 |
| --- | --- | --- |
| GET | `/api/capabilities` | runtime/dataset/group/preflight |
| POST | `/api/runs/preview` | normalized spec + selected records |
| POST | `/api/runs` | 创建并异步启动，`202` |
| GET | `/api/runs/{run_id}/control` | control + invocation snapshot |
| POST | `/api/runs/{run_id}/cancel` | 幂等取消，`202/200` |
| POST | `/api/runs/{run_id}/resume` | 缺失项 Resume，`202` |
| GET | `/api/runs/{run_id}/control/log` | launcher log 分页 |

Orchestrator 在自己的 FastAPI app 中注册这些 read/control routes；不会修改 OpenClaw 仓库的 dashboard
路由。两者都读取同一 execution artifact contract 时，字段语义必须保持兼容。

### 14.3 Capability 响应

```json
{
  "schema_version": 1,
  "ready": true,
  "workspace_root": "/Users/xutao/.openclaw/workspace",
  "runtime_revision": "<git-revision-or-null>",
  "groups": ["single_llm_skills_on", "single_llm_skills_off"],
  "datasets": [
    {"id": "verifier_grounded_rdkit", "task_count": 11},
    {"id": "verifier_grounded_xtb_xyz", "task_count": 18},
    {"id": "verifier_grounded_property_calculation", "task_count": 2}
  ],
  "thinking_levels": ["off", "minimal", "low", "medium", "high", "xhigh"],
  "default_model": "qwen3.5-plus",
  "vgb_release": {
    "version": "0.3.0",
    "wheel_sha256": "b93c18b818e8d19993e817de6439ccea910b36a8f386c551078b7c6b10420381"
  },
  "checks": []
}
```

`ready=false` 时附结构化 checks；不返回 env 或 token。

### 14.4 Preview 请求与响应

请求 body 是 `RunSpec`。响应：

```json
{
  "schema_version": 1,
  "preview_id": "01...",
  "spec_sha256": "...",
  "normalized_spec": {},
  "records": [
    {
      "record_id": "rdkit_qed_max_001",
      "dataset": "verifier_grounded_rdkit",
      "subset": "..."
    }
  ],
  "task_count": 2,
  "group_count": 2,
  "execution_count": 4,
  "expires_at": "2026-07-21T08:10:00Z"
}
```

`execution_count = record count * group count`。Preview stdout 解析失败返回 502
`runtime_contract_error`，不尝试正则猜测另一种格式。

### 14.5 Create 请求

```json
{
  "preview_id": "01...",
  "spec_sha256": "...",
  "request_id": "client-generated-id"
}
```

Backend 必须确认 preview 未过期、Spec/runtime identity 未漂移，并重新验证无 active Run。成功返回：

```json
{
  "run_id": "verifier-grounded-rdkit-qwen3-5-plus-20260721-153000",
  "state": "starting",
  "output_dir": "/.../state/benchmark-runs/formal/verifier-grounded-rdkit/qwen3-5-plus/<run-id>",
  "control_url": "/api/runs/<run-id>/control",
  "progress_url": "/api/runs/<run-id>/progress"
}
```

### 14.6 Cancel/Resume 请求

```json
{"request_id": "client-generated-id"}
```

- Cancel 对已经 cancelled 的相同请求返回 200；
- Resume 对 active/completed/no-missing-records 返回 409；
- Resume 不接收新的 Run Spec；
- 所有命令响应包含最新 control snapshot。

### 14.7 组合 Run snapshot

现有 `GET /api/runs/{run_id}` 可增加可选字段，不修改原有字段：

```json
{
  "run_id": "...",
  "progress": {},
  "summary": {},
  "control": {
    "state": "running",
    "active_invocation_id": "...",
    "can_cancel": true,
    "can_resume": false
  }
}
```

没有 control sidecar 的历史 Run 返回 `control=null`，仍可正常查看。

### 14.8 错误格式

```json
{
  "error": {
    "code": "run_active",
    "message": "Run already has an active invocation.",
    "details": {"run_id": "..."},
    "request_id": "..."
  }
}
```

主要映射：

| HTTP | code |
| ---: | --- |
| 400 | `invalid_request` |
| 404 | `run_not_found`, `preview_not_found` |
| 409 | `run_active`, `run_completed`, `no_missing_records`, `runtime_drift` |
| 422 | Pydantic schema error / `selection_invalid` |
| 502 | `runtime_contract_error` |
| 503 | `runtime_unavailable`, `active_run_limit` |

不把 traceback 返回 Browser；完整异常写 Backend 日志。

## 15. 本地安全设计

### 15.1 HTTP

- 默认仅 `127.0.0.1`；
- 变更请求校验 `Origin`/`Host`；
- 不启用宽泛 CORS；
- JSON body 大小限制；
- preview/create 速率限制，防止大量本地子进程；
- 非 loopback 部署不属于 MVP。

### 15.2 子进程

- `create_subprocess_exec`，禁止 `shell=True`；
- argv 只从 schema 白名单生成；
- model、record ID 不进入 shell；
- 只对 fingerprint 匹配的 owned process 发 signal；
- 不直接运行 `openclaw agent`；
- 不接受 Browser 提供 cwd/env/executable。

### 15.3 文件

- 所有 root 在启动时 `expanduser().resolve()`；
- output/control/asset 使用组件级 containment，不用字符串 `startswith`；
- symlink escape 拒绝；
- control 文件权限最小化；
- execution root 只由 canonical CLI 写；
- API 不公开 runtime config、session store、scorer runtime 或 protected roots 的任意文件浏览。

## 16. 测试设计

### 16.1 Unit

- RunSpec extra/duplicate/bounds validator；
- YAML safe load 和 schema version；
- 每个字段到 CLI flag 的精确映射；
- `timeout_seconds=None` 与 bounded timeout；
- start/resume argv 唯一差异中的 merge flag；
- canonical output helper 的 formal/temporary、单 dataset/mixed-datasets 和 model slug；
- output/control containment 和 symlink escape；
- state transition、idempotency、PID fingerprint；
- missing/committed/invalid TaskView 派生；
- launcher log offset/limit。

### 16.2 Contract

从配置的 OpenClaw workspace 中通过 subprocess `--help`（或 OpenClaw 侧允许的轻量 contract helper）
验证所有文档化 flag 仍存在；Orchestrator 不直接 import `benchmarking.workflow.cli.parse_args()`：

```text
--groups
--datasets
--output-dir
--record-ids
--limit
--offset
--single-agent-model
--single-agent-thinking
--single-timeout
--single-timeout-retries
--single-timeout-retry-backoff-seconds
--no-timeout
--no-analysis
--max-concurrent-groups
--inter-wave-delay-seconds
--print-selected-records
--exact-output-dir
--merge-existing-per-record
```

同时用分类目录内的真实 fixture Run 验证 artifact 路径和 schema v3 状态轴。历史 schema v2 fixture
验证 ArtifactReader compatibility，不在 Orchestrator 写第二套 fixture decoder。OpenClaw 仓库可提供
契约 fixture/export helper；VGB capability fixture 必须匹配当前 `release.json` 的 `0.3.0` 和 wheel
SHA，不能把版本常量复制到 control 代码。

### 16.3 Integration（无模型调用）

1. `--list-datasets` 返回同步 VGB dataset；
2. `--print-selected-records` 精确选择已知 RDKit task；
3. preview unknown/duplicate record ID 返回结构化错误；
4. output helper 为 VGB 生成 `formal/<benchmark>/<model>/<run-id>`；
5. ArtifactReader 从分类 root 递归发现 Run，并在 Run 内停止继续扫描；
6. fake canonical CLI 进程写 progress/per-record，Supervisor 正确对账；
7. fake CLI 响应 SIGTERM，验证先 signal PID 后升级 process group；
8. Backend 重启后 PID 消失/存活/fingerprint mismatch 三种恢复；
9. Orchestrator read/control routes 回归通过；OpenClaw dashboard 作为独立兼容性 smoke 保持可用。

这里的 fake 位于完整 canonical CLI 边界，不模拟 `openclaw agent` JSON；OpenClaw wrapper 已有自己的
contract tests。

### 16.4 Resume Integration

在临时 exact output 中预置一个有效 per-record，选择两个 records，执行带
`--merge-existing-per-record` 的 fake/runtime test：

- 已有 record 不被调用；
- 缺失 record 被调用一次；
- aggregate 包含两者；
- failure per-record 也被跳过；
- 损坏 per-record 被 Orchestrator 拒绝，不覆盖；
- resume 使用新 invocation ID。

### 16.5 E2E

- GUI preview/create/progress/record/result；
- running Cancel -> cancelled -> Resume missing -> completed；
- Browser 刷新后恢复 snapshot；
- 历史无 control sidecar Run 仍可浏览；
- 不同桌面尺寸下控制区不遮挡现有结果表。

真实模型 smoke test 限制为一个 RDKit record、一个 group，并明确计费/耗时；常规 CI 不调用模型。

## 17. 实施顺序

### Milestone 1：Read-only Contract

- capability/preflight；
- RunSpec/Pydantic/YAML；
- preview command 和 parser；
- canonical flag/artifact contract tests。

### Milestone 2：Control Plane

- registry/atomic sidecars/backend lock；
- ProcessDriver/RunSupervisor；
- create/start/wait/reconcile；
- fake CLI integration。

### Milestone 3：Cancel 与 Resume

- SIGTERM-first cancellation；
- PID fingerprint/restart recovery；
- missing checkpoint 派生；
- exact output + merge Resume；
- drift/idempotency tests。

### Milestone 4：API 与 GUI

- FastAPI control routes；
- Orchestrator Run snapshot 合并 control 字段；
- static GUI 创建、预览、取消和 Resume；
- polling/log paging；
- Orchestrator API/GUI regression/E2E，及 OpenClaw artifact contract smoke。

### Milestone 5：Hardening

- path/Origin/body/log limits；
- crash and corrupt sidecar tests；
- one-record real smoke；
- 文档/CLI/artifact contract 最终核对。

每个 Milestone 都应保持 canonical CLI 可独立运行，不能要求 dashboard/control Backend 才能执行
benchmark。

## 18. 已确认的 Run 并发约束

MVP 已确认全局只允许一个 active Run，同时仍允许一个 Run 内最多两个 skills-on/off group 并发。
该限制用于控制本机 provider、CPU/xTB、cleanroom 和 Cancel/Resume 的资源竞争。

后续可以根据实际资源情况评估多个 active Run，但放开前必须完成容量测量、provider 配额验证、
workspace/cleanroom 并发隔离测试和控制锁设计；不能只把 `max_active_runs` 改大。
