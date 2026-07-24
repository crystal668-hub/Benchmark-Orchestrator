const state = { capabilities: null, preview: null, runs: [], activeRunId: null, pollTimer: null };
const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error?.message || `${response.status} ${response.statusText}`);
  return payload;
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast show${error ? " error" : ""}`;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.className = "toast"; }, 3500);
}

function requestId() { return crypto.randomUUID(); }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char])); }
function short(value, length = 12) { const text = String(value || ""); return text.length > length ? text.slice(0, length) : text || "—"; }
function slug(value) { return String(value || "item").trim().replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-|-$/g, "").toLowerCase() || "item"; }

const RESIZE_STORAGE_PREFIX = "bo-resize-";
let refreshResizablePanels = () => {};

function resizeRange(minimum, maximum) {
  if (!Number.isFinite(minimum) || !Number.isFinite(maximum) || maximum - minimum < 1) return null;
  return { min: Math.round(minimum), max: Math.round(maximum) };
}

function clamp(value, minimum, maximum) { return Math.min(Math.max(value, minimum), maximum); }

function initResizablePanels() {
  const configs = {
    rail: {
      axis: "x", property: "--rail-user-width", target: "#workspaceShell",
      disabled: () => window.matchMedia("(max-width: 760px)").matches,
      measure: () => $("#runRail").getBoundingClientRect().width,
      range: () => {
        const width = $("#workspaceShell").getBoundingClientRect().width;
        return resizeRange(220, Math.min(440, width - 360));
      },
    },
    "form-column": {
      axis: "x", property: "--form-left-size", target: "#formGrid",
      disabled: () => window.matchMedia("(max-width: 760px)").matches,
      measure: () => $(".dataset-section").getBoundingClientRect().width,
      range: () => {
        const width = $("#formGrid").getBoundingClientRect().width;
        return resizeRange(220, width - 230);
      },
    },
    "form-row": {
      axis: "y", property: "--form-top-size", target: "#formGrid",
      disabled: () => window.matchMedia("(max-width: 760px)").matches,
      measure: () => $(".dataset-section").getBoundingClientRect().height,
      range: () => {
        const height = $("#formGrid").getBoundingClientRect().height;
        return resizeRange(210, height - 220);
      },
    },
    results: {
      axis: "x", property: "--result-primary-size", target: "#resultLayout",
      disabled: () => window.matchMedia("(max-width: 1060px)").matches,
      measure: () => $("#resultsSection").getBoundingClientRect().width,
      range: () => {
        const width = $("#resultLayout").getBoundingClientRect().width;
        return resizeRange(300, width - 290);
      },
    },
  };
  const handles = [...document.querySelectorAll("[data-resize]")];

  function getRange(config) {
    if (config.disabled()) return null;
    return config.range();
  }

  function readSavedSize(name) {
    try { return Number(localStorage.getItem(`${RESIZE_STORAGE_PREFIX}${name}`)); }
    catch (_) { return null; }
  }

  function persist(name, value) {
    try { localStorage.setItem(`${RESIZE_STORAGE_PREFIX}${name}`, String(value)); }
    catch (_) { /* Keep resizing functional when storage is unavailable. */ }
  }

  function applySize(name, handle, value, range, shouldPersist = true) {
    const config = configs[name];
    const next = Math.round(clamp(value, range.min, range.max));
    const target = $(config.target);
    target.style.setProperty(config.property, `${next}px`);
    target.classList.add("has-resized-layout");
    handle.setAttribute("aria-valuemin", range.min);
    handle.setAttribute("aria-valuemax", range.max);
    handle.setAttribute("aria-valuenow", next);
    handle.setAttribute("aria-valuetext", `${next} px`);
    if (shouldPersist) persist(name, next);
  }

  function updateHandle(handle) {
    const config = configs[handle.dataset.resize];
    const range = getRange(config);
    const disabled = !range;
    handle.setAttribute("aria-disabled", String(disabled));
    handle.tabIndex = disabled ? -1 : 0;
    if (!range) return null;
    const value = Math.round(config.measure());
    handle.setAttribute("aria-valuemin", range.min);
    handle.setAttribute("aria-valuemax", range.max);
    handle.setAttribute("aria-valuenow", clamp(value, range.min, range.max));
    handle.setAttribute("aria-valuetext", `${clamp(value, range.min, range.max)} px`);
    return range;
  }

  function refreshHandles() {
    handles.forEach((handle) => {
      const name = handle.dataset.resize;
      const config = configs[name];
      const range = updateHandle(handle);
      if (!range) return;
      const target = $(config.target);
      if (!target.style.getPropertyValue(config.property)) {
        const saved = readSavedSize(name);
        if (Number.isFinite(saved) && saved > 0) {
          applySize(name, handle, saved, range, false);
          return;
        }
      }
      const value = config.measure();
      if (value < range.min || value > range.max) applySize(name, handle, value, range);
    });
  }

  handles.forEach((handle) => {
    const name = handle.dataset.resize;
    const config = configs[name];

    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      const activeRange = updateHandle(handle);
      if (!activeRange) return;
      const startValue = config.measure();
      const startPosition = config.axis === "x" ? event.clientX : event.clientY;
      const pointerId = event.pointerId;
      handle.setPointerCapture(pointerId);
      document.body.classList.add("is-resizing");
      document.body.dataset.resizeAxis = config.axis;
      event.preventDefault();

      const move = (moveEvent) => {
        if (moveEvent.pointerId !== pointerId) return;
        const position = config.axis === "x" ? moveEvent.clientX : moveEvent.clientY;
        applySize(name, handle, startValue + position - startPosition, activeRange, false);
      };
      const stop = (stopEvent) => {
        if (stopEvent.pointerId !== pointerId) return;
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", stop);
        handle.removeEventListener("pointercancel", stop);
        if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
        document.body.classList.remove("is-resizing");
        delete document.body.dataset.resizeAxis;
        persist(name, Math.round(config.measure()));
        updateHandle(handle);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", stop);
      handle.addEventListener("pointercancel", stop);
    });

    handle.addEventListener("keydown", (event) => {
      const activeRange = updateHandle(handle);
      if (!activeRange) return;
      const step = event.shiftKey ? 40 : 16;
      const decrease = config.axis === "x" ? "ArrowLeft" : "ArrowUp";
      const increase = config.axis === "x" ? "ArrowRight" : "ArrowDown";
      let next = null;
      if (event.key === decrease) next = config.measure() - step;
      if (event.key === increase) next = config.measure() + step;
      if (event.key === "Home") next = activeRange.min;
      if (event.key === "End") next = activeRange.max;
      if (next === null) return;
      event.preventDefault();
      applySize(name, handle, next, activeRange);
    });
  });

  refreshResizablePanels = refreshHandles;
  window.addEventListener("resize", refreshHandles);
  refreshHandles();
}

function updateRunName() {
  const datasets = [...document.querySelectorAll("[name=datasets]:checked")].map((input) => input.value);
  const dataset = datasets.length === 1 ? datasets[0] : datasets.length > 1 ? "mixed-datasets" : "dataset";
  const model = $("#modelSelect").value.split("/").at(-1) || "model";
  $("#derivedRunName").value = `${slug(dataset)}-${slug(model)}-{启动时间}`;
}

function renderModels(models, defaultModel) {
  const select = $("#modelSelect");
  select.innerHTML = models.length
    ? models.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.label)} · ${escapeHtml(model.provider)}</option>`).join("")
    : '<option value="">未发现可用模型</option>';
  select.disabled = models.length === 0;
  if (models.some((model) => model.id === defaultModel)) select.value = defaultModel;
  updateRunName();
}

function updateRuntimeControls() {
  const ready = Boolean(state.capabilities?.ready);
  $("#previewButton").disabled = !ready;
  $("#startButton").disabled = !ready || !state.preview;
}

async function loadCapabilities() {
  try {
    const payload = await api("/api/capabilities");
    state.capabilities = payload;
    renderModels(payload.models || [], payload.default_model);
    $("#runtimeSignal").className = `runtime-signal ${payload.ready ? "ready" : "failed"}`;
    $("#runtimeLabel").textContent = payload.ready ? `Runtime ready · VGB ${payload.vgb_release.version}` : "Runtime preflight failed";
    $("#runtimeRevision").textContent = payload.runtime_revision ? payload.runtime_revision.slice(0, 10) + (payload.runtime_dirty ? " · dirty" : "") : "revision unknown";
    updateRuntimeControls();
    for (const dataset of payload.datasets) {
      const suffix = dataset.id.replace("verifier_grounded_", "");
      const count = document.getElementById(`count-${suffix}`);
      if (count) count.textContent = dataset.task_count;
    }
  } catch (error) {
    state.capabilities = null;
    updateRuntimeControls();
    $("#runtimeSignal").className = "runtime-signal failed";
    $("#runtimeLabel").textContent = "Runtime unavailable";
    toast(error.message, true);
  }
}

async function loadRuns() {
  try {
    state.runs = await api("/api/runs");
    renderRunList();
  } catch (error) { toast(error.message, true); }
}

function renderRunList() {
  const list = $("#runList");
  $("#emptyRail").hidden = state.runs.length > 0;
  list.innerHTML = state.runs.map((run) => {
    const controlState = run.control?.state || run.status || "history";
    const completed = run.progress?.completed || 0;
    const total = run.progress?.total || (run.record_count || 0) * Math.max(run.group_count || 1, 1);
    return `<button class="run-item ${run.run_id === state.activeRunId ? "active" : ""}" data-run-id="${escapeHtml(run.run_id)}" type="button">
      <span class="run-item-top"><strong>${escapeHtml(run.alias || run.run_id)}</strong><i class="mini-state ${escapeHtml(controlState)}">${escapeHtml(controlState)}</i></span>
      <small><span>${escapeHtml((run.datasets || []).map((item) => item.replace("verifier_grounded_", "")).join(" · ") || "artifact run")}</span><span>${completed}/${total}</span></small>
    </button>`;
  }).join("");
  list.querySelectorAll("[data-run-id]").forEach((button) => button.addEventListener("click", () => {
    document.body.classList.remove("rail-open");
    openRun(button.dataset.runId);
  }));
}

function formSpec() {
  const form = new FormData($("#runForm"));
  const recordIds = String(form.get("record_ids") || "").split(/[\n,]+/).map((value) => value.trim()).filter(Boolean);
  const retries = Number(form.get("timeout_retries"));
  return {
    schema_version: 1,
    name: null,
    groups: form.getAll("groups"),
    datasets: form.getAll("datasets"),
    selection: { record_ids: recordIds, offset: Number(form.get("offset") || 0), limit: form.get("limit") ? Number(form.get("limit")) : null },
    agent: { model: String(form.get("model") || "").trim(), thinking: form.get("thinking") },
    execution: {
      timeout_seconds: form.get("no_timeout") ? null : Number(form.get("timeout_seconds")),
      timeout_retries: retries,
      timeout_retry_backoff_seconds: String(form.get("backoff") || "").split(",").map((value) => Number(value.trim())).filter((value) => Number.isFinite(value)),
      max_concurrent_groups: Number(form.get("max_concurrent_groups")),
      inter_wave_delay_seconds: Number(form.get("inter_wave_delay_seconds") || 0),
      analysis: Boolean(form.get("analysis")),
    },
  };
}

async function previewRun(event) {
  event.preventDefault();
  const button = $("#previewButton");
  button.disabled = true;
  $("#formStatus").textContent = "Runtime 正在解析 selector";
  try {
    state.preview = await api("/api/runs/preview", { method: "POST", body: JSON.stringify(formSpec()) });
    $("#formStatus").textContent = `已冻结 ${state.preview.task_count} 条选择`;
    $("#specDigest").textContent = `SHA256 ${state.preview.spec_sha256}`;
    $("#previewRecords").textContent = state.preview.task_count;
    $("#previewGroups").textContent = state.preview.group_count;
    $("#previewExecutions").textContent = state.preview.execution_count;
    $("#previewRows").innerHTML = state.preview.records.map((record) => `<tr><td><code>${escapeHtml(record.record_id)}</code></td><td>${escapeHtml(record.dataset)}</td><td>${escapeHtml(record.subset)}</td><td>${escapeHtml(record.eval_kind)}</td></tr>`).join("");
    $("#previewBand").hidden = false;
    updateRuntimeControls();
    $("#previewBand").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    state.preview = null;
    $("#previewBand").hidden = true;
    $("#formStatus").textContent = "预览失败";
    toast(error.message, true);
  } finally { updateRuntimeControls(); }
}

async function startRun() {
  if (!state.preview) return;
  const button = $("#startButton");
  button.disabled = true;
  try {
    const created = await api("/api/runs", { method: "POST", body: JSON.stringify({ preview_id: state.preview.preview_id, spec_sha256: state.preview.spec_sha256, request_id: requestId() }) });
    toast("Run 已启动");
    await loadRuns();
    await openRun(created.run_id);
  } catch (error) { toast(error.message, true); }
  finally { updateRuntimeControls(); }
}

function showCreate() {
  document.body.classList.remove("rail-open");
  state.activeRunId = null;
  clearInterval(state.pollTimer);
  $("#createView").hidden = false;
  $("#runView").hidden = true;
  renderRunList();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function openRun(runId) {
  state.activeRunId = runId;
  $("#createView").hidden = true;
  $("#runView").hidden = false;
  renderRunList();
  refreshResizablePanels();
  const shouldPoll = await refreshRun();
  clearInterval(state.pollTimer);
  if (shouldPoll) state.pollTimer = setInterval(refreshRun, 1000);
  window.scrollTo({ top: 0 });
}

async function refreshRun() {
  if (!state.activeRunId) return false;
  try {
    const [run, control, tasks] = await Promise.all([
      api(`/api/runs/${encodeURIComponent(state.activeRunId)}`),
      api(`/api/runs/${encodeURIComponent(state.activeRunId)}/control`).catch(() => null),
      api(`/api/runs/${encodeURIComponent(state.activeRunId)}/tasks`).catch(() => []),
    ]);
    renderRun(run, control, tasks);
    const active = Boolean(control && ["starting", "running", "cancelling"].includes(control.state));
    if (!active) clearInterval(state.pollTimer);
    return active;
  } catch (error) { clearInterval(state.pollTimer); toast(error.message, true); return false; }
}

function renderRun(run, control, tasks) {
  const frozen = run.frozen;
  $("#runTitle").textContent = run.metadata?.alias || run.run_id;
  $("#runPath").textContent = run.path;
  $("#runCategory").textContent = frozen ? `${frozen.run_category.toUpperCase()} · ${frozen.spec.datasets.map((item) => item.replace("verifier_grounded_", "")).join(" + ")}` : "HISTORICAL ARTIFACT RUN";
  const status = control?.state || run.progress?.status || "history";
  $("#runState").textContent = status;
  $("#runState").className = `state-badge ${status}`;
  $("#cancelButton").hidden = !control?.can_cancel;
  $("#resumeButton").hidden = !control?.can_resume;
  const completed = control?.committed_count ?? run.progress?.completed ?? 0;
  const total = control?.selected_count ?? run.progress?.total ?? 0;
  const percent = total ? Math.round(completed / total * 100) : 0;
  $("#progressFraction").textContent = `${completed} / ${total}`;
  $("#progressFill").style.width = `${percent}%`;
  $("#progressPercent").textContent = `${percent}%`;
  const invocation = control?.invocations?.find((item) => item.invocation_id === control.active_invocation_id) || control?.invocations?.at(-1);
  $("#metricPid").textContent = invocation?.pid || "—";
  $("#metricInvocation").textContent = short(invocation?.invocation_id, 10);
  $("#metricMissing").textContent = control?.missing_count ?? "—";
  $("#metricExit").textContent = invocation?.exit_code ?? "—";
  $("#recordCountLabel").textContent = `${tasks.length} selected pairs`;
  $("#recordRows").innerHTML = tasks.map((task, index) => {
    const result = task.result || {};
    const evaluation = result.evaluation || {};
    const score = evaluation.normalized_score ?? evaluation.score;
    return `<tr data-task-index="${index}"><td>${task.group_id.endsWith("_on") ? "Skills On" : "Skills Off"}</td><td><code>${escapeHtml(task.record_id)}</code></td><td><span class="checkpoint ${task.checkpoint}">${task.checkpoint}</span></td><td>${score == null ? "—" : Number(score).toFixed(4)}</td><td>${escapeHtml(result.run_lifecycle_status || "—")}</td><td>${escapeHtml(result.answer_availability || "—")}</td></tr>`;
  }).join("");
  $("#recordRows").querySelectorAll("tr").forEach((row) => row.addEventListener("click", () => showEvidence(tasks[Number(row.dataset.taskIndex)], row)));
  loadLog(control);
}

function showEvidence(task, row) {
  $("#recordRows").querySelectorAll("tr").forEach((item) => item.classList.remove("selected"));
  row.classList.add("selected");
  const result = task.result || {};
  const evaluation = result.evaluation || {};
  const axes = ["run_lifecycle_status", "protocol_completion_status", "protocol_acceptance_status", "answer_availability", "answer_reliability", "evaluable", "scored", "recovery_mode", "degraded_execution", "execution_error_kind"];
  const entries = [["Group", task.group_id], ["Record", task.record_id], ["Metric", evaluation.primary_metric], ["Score", evaluation.normalized_score ?? evaluation.score], ...axes.map((axis) => [axis, result[axis]])];
  $("#evidenceList").innerHTML = entries.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value ?? "—")}</dd>`).join("");
}

async function loadLog(control) {
  const invocation = control?.invocations?.find((item) => item.invocation_id === control.active_invocation_id) || control?.invocations?.at(-1);
  if (!invocation) { $("#launcherLog").textContent = "Launcher log 尚不可用"; return; }
  try {
    const log = await api(`/api/runs/${encodeURIComponent(state.activeRunId)}/control/log?invocation_id=${encodeURIComponent(invocation.invocation_id)}&offset=0&limit=65536`);
    $("#launcherLog").textContent = log.data || "Launcher log 暂无输出";
    $("#launcherLog").scrollTop = $("#launcherLog").scrollHeight;
  } catch (_) { /* The control poll remains authoritative. */ }
}

async function command(kind) {
  try {
    await api(`/api/runs/${encodeURIComponent(state.activeRunId)}/${kind}`, { method: "POST", body: JSON.stringify({ request_id: requestId() }) });
    toast(kind === "cancel" ? "取消请求已发送" : "Resume invocation 已启动");
    const shouldPoll = await refreshRun();
    clearInterval(state.pollTimer);
    if (shouldPoll) state.pollTimer = setInterval(refreshRun, 1000);
  } catch (error) { toast(error.message, true); }
}

function initialize() {
  $("#themeButton").addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("bo-theme", theme);
  });
  $("#runForm").addEventListener("submit", previewRun);
  $("#startButton").addEventListener("click", startRun);
  $("#homeButton").addEventListener("click", showCreate);
  $("#newRunButton").addEventListener("click", showCreate);
  $("#backButton").addEventListener("click", showCreate);
  $("#refreshButton").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.classList.add("is-refreshing");
    button.setAttribute("aria-busy", "true");
    try {
      await Promise.all([loadCapabilities(), loadRuns()]);
      if (state.activeRunId) await refreshRun();
    } finally {
      button.disabled = false;
      button.classList.remove("is-refreshing");
      button.removeAttribute("aria-busy");
    }
  });
  $("#mobileRunsButton").addEventListener("click", () => document.body.classList.toggle("rail-open"));
  $("#railScrim").addEventListener("click", () => document.body.classList.remove("rail-open"));
  $("#cancelButton").addEventListener("click", () => command("cancel"));
  $("#resumeButton").addEventListener("click", () => command("resume"));
  $("#noTimeout").addEventListener("change", (event) => { $("[name=timeout_seconds]").disabled = event.target.checked; });
  $("#modelSelect").addEventListener("change", updateRunName);
  document.querySelectorAll("[name=datasets]").forEach((input) => input.addEventListener("change", updateRunName));
  window.addEventListener("load", () => { if (window.lucide) window.lucide.createIcons(); });
  initResizablePanels();
  updateRuntimeControls();
  Promise.all([loadCapabilities(), loadRuns()]);
}

initialize();
