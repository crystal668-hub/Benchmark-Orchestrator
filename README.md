# Benchmark Orchestrator

Benchmark Orchestrator 是 OpenClaw benchmark runtime 的本地控制面。它负责 Run Spec、无模型 selector
预览、canonical CLI 进程监督、Cancel、补齐缺失 checkpoint 式 Resume，以及 artifact/结果展示；它不
重新实现 OpenClaw agent、workspace/session 隔离或 VGB scorer。

## 环境

- Python `>=3.12`
- `uv`
- 外部 OpenClaw workspace，包含 `benchmarking.workflow.cli`
- `openclaw` CLI 可从 `PATH` 访问
- VGB release manifest 位于外部 workspace 的
  `benchmarking/resources/verifier_grounded/release.json`

## 启动

```bash
uv sync --extra dev
mkdir -p ~/.benchmark-orchestrator
cp config/orchestrator.example.yaml ~/.benchmark-orchestrator/orchestrator.yaml
uv run python -m benchmark_orchestrator.app \
  --config ~/.benchmark-orchestrator/orchestrator.yaml
```

默认 GUI/API 地址为 [http://127.0.0.1:8875](http://127.0.0.1:8875)。服务仅允许 loopback 监听。

## 运行边界

所有真实 Run 只通过以下外部入口启动：

```text
uv run --project <workspace_root> python -m benchmarking.workflow.cli
```

浏览器输入只会映射到白名单参数。Orchestrator 不使用 shell，不执行 `openclaw agent`，不设置跨仓库
`PYTHONPATH`，也不读取或改写 runtime credential/config。执行 artifacts 由 canonical CLI 单写；控制
sidecar 位于独立 `control_root`，使用原子替换和最小文件权限。

Resume 复用冻结 spec、相同 `--exact-output-dir` 和 `--merge-existing-per-record`。任何可解析的现有
per-record，包括失败结果，都会保留并跳过；损坏 checkpoint 会阻止 Resume。

## 测试

```bash
uv run pytest -q
```

常规测试使用 fake canonical CLI，不调用模型。真实无模型 contract 检查可通过 GUI capability/preview，
或直接运行：

```bash
uv run --project ~/.openclaw/workspace python -m benchmarking.workflow.cli \
  --groups single_llm_skills_on \
  --datasets verifier_grounded_rdkit \
  --record-ids rdkit_qed_max_001 \
  --print-selected-records
```

兼容性身份见 `src/benchmark_orchestrator/compatibility.json`。当前 MVP 支持 OpenClaw skills-on/off 与
VGB `0.3.0` 的 RDKit、xTB/XYZ、property calculation 三个 track，全局最多一个 active Run。
