const state = { capabilities: null, preview: null, runs: [], activeRunId: null, pollTimer: null };
const modelPickerState = { models: [], providers: [], activeProvider: "" };
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

function updateRecordSelectors() {
  const selected = new Set([...document.querySelectorAll("[name=datasets]:checked")].map((input) => input.value));
  document.querySelectorAll("[data-record-selector]").forEach((selector) => {
    selector.hidden = !selected.has(selector.dataset.recordSelector);
  });
  $("#recordSelectorEmpty").hidden = selected.size > 0;
  refreshResizablePanels();
}

function parseRecordIds(value) {
  return String(value || "").split(/[\n,，]+/).map((item) => item.trim()).filter(Boolean);
}

function expandRecordRange(startValue, endValue) {
  const start = String(startValue || "").trim();
  const end = String(endValue || "").trim();
  if (!start && !end) return [];
  if (!start || !end) throw new Error("范围选择需要同时填写起始和结束题号");
  if (!/^\d+$/.test(start) || !/^\d+$/.test(end)) throw new Error("范围题号只能包含数字");

  const startNumber = Number(start);
  const endNumber = Number(end);
  if (!Number.isSafeInteger(startNumber) || !Number.isSafeInteger(endNumber)) throw new Error("范围题号过大");
  if (startNumber > endNumber) throw new Error("范围起始题号不能大于结束题号");
  if (endNumber - startNumber > 65535) throw new Error("范围最多支持 65536 道题");

  const width = Math.max(start.length, end.length, 3);
  return Array.from({ length: endNumber - startNumber + 1 }, (_, index) => String(startNumber + index).padStart(width, "0"));
}

function recordIdsForSelector(selector) {
  const directIds = parseRecordIds(selector.querySelector("[data-record-dataset]")?.value);
  const rangeIds = expandRecordRange(
    selector.querySelector("[data-record-range-start]")?.value,
    selector.querySelector("[data-record-range-end]")?.value,
  );
  return [...directIds, ...rangeIds];
}

function invalidatePreview() {
  if (!state.preview) return;
  state.preview = null;
  $("#previewBand").hidden = true;
  $("#formStatus").textContent = "配置已变更，请重新预览";
  $("#specDigest").textContent = "";
  updateRuntimeControls();
}

function currentModel() {
  return modelPickerState.models.find((model) => model.id === $("#modelSelect").value) || null;
}

function modelsForProvider(provider) {
  return modelPickerState.models.filter((model) => model.provider === provider);
}

function refreshPickerIcons() {
  if (window.lucide) window.lucide.createIcons();
}

function updateModelPickerValue() {
  const model = currentModel();
  const value = $("#modelPickerValue");
  const trigger = $("#modelPickerTrigger");
  value.innerHTML = model
    ? `<strong>${escapeHtml(model.label)}</strong><small>${escapeHtml(model.provider)} · ${escapeHtml(model.id)}</small>`
    : '<strong>选择模型</strong><small>先选择 Provider</small>';
  trigger.disabled = $("#modelSelect").disabled;
  trigger.setAttribute("aria-label", model ? `当前模型 ${model.label}` : "选择模型");
}

function renderProviderOptions() {
  $("#providerOptions").innerHTML = modelPickerState.providers.map((provider) => {
    const models = modelsForProvider(provider);
    const active = provider === modelPickerState.activeProvider;
    return `<button class="model-provider-option${active ? " active" : ""}" type="button" role="menuitem" data-provider="${escapeHtml(provider)}" aria-haspopup="true" aria-expanded="${active}">
      <span><strong>${escapeHtml(provider)}</strong><small>${models.length} 个模型</small></span><i data-lucide="chevron-right" aria-hidden="true"></i>
    </button>`;
  }).join("");
  refreshPickerIcons();
}

function renderModelOptions() {
  const panel = $("#modelOptionsPanel");
  const models = modelsForProvider(modelPickerState.activeProvider);
  panel.hidden = !models.length;
  $("#modelOptionsHeading").textContent = `${modelPickerState.activeProvider} / MODELS`;
  $("#modelOptions").innerHTML = models.map((model) => {
    const selected = model.id === $("#modelSelect").value;
    return `<button class="model-option${selected ? " selected" : ""}" type="button" role="menuitem" data-model-id="${escapeHtml(model.id)}" aria-selected="${selected}">
      <span class="model-option-copy"><strong>${escapeHtml(model.label)}</strong><small>${escapeHtml(model.id)}</small></span>
      ${selected ? '<i data-lucide="check" aria-hidden="true"></i>' : ""}
    </button>`;
  }).join("");
  refreshPickerIcons();
}

function setActiveProvider(provider, focusModel = false) {
  if (!modelPickerState.providers.includes(provider)) return;
  const changed = modelPickerState.activeProvider !== provider;
  modelPickerState.activeProvider = provider;
  if (changed) {
    renderProviderOptions();
    renderModelOptions();
  }
  if (focusModel) $("#modelOptions [data-model-id]")?.focus();
}

function closeModelPicker(restoreFocus = false) {
  $("#modelPickerMenu").hidden = true;
  $("#modelPickerTrigger").setAttribute("aria-expanded", "false");
  if (restoreFocus) $("#modelPickerTrigger").focus();
}

function openModelPicker() {
  const trigger = $("#modelPickerTrigger");
  if (trigger.disabled) return;
  const model = currentModel();
  setActiveProvider(model?.provider || modelPickerState.providers[0]);
  $("#modelPickerMenu").hidden = false;
  trigger.setAttribute("aria-expanded", "true");
}

function chooseModel(modelId) {
  const model = modelPickerState.models.find((item) => item.id === modelId);
  if (!model) return;
  const select = $("#modelSelect");
  select.value = model.id;
  updateModelPickerValue();
  closeModelPicker();
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

function moveMenuFocus(items, current, direction) {
  const index = items.indexOf(current);
  if (index < 0) return;
  items[(index + direction + items.length) % items.length]?.focus();
}

function handleModelPickerKeydown(event) {
  const providerButton = event.target.closest("[data-provider]");
  const modelButton = event.target.closest("[data-model-id]");
  if (event.key === "Escape") {
    event.preventDefault();
    closeModelPicker(true);
    return;
  }
  if (providerButton) {
    const providers = [...$("#providerOptions").querySelectorAll("[data-provider]")];
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      moveMenuFocus(providers, providerButton, event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "ArrowRight" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setActiveProvider(providerButton.dataset.provider, true);
    }
    return;
  }
  if (modelButton) {
    const models = [...$("#modelOptions").querySelectorAll("[data-model-id]")];
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      moveMenuFocus(models, modelButton, event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      $("#providerOptions [data-provider].active")?.focus();
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      chooseModel(modelButton.dataset.modelId);
    }
  }
}

function initModelPicker() {
  const picker = $("#modelPicker");
  const menu = $("#modelPickerMenu");
  const trigger = $("#modelPickerTrigger");
  trigger.addEventListener("click", () => {
    if (menu.hidden) openModelPicker();
    else closeModelPicker();
  });
  trigger.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      openModelPicker();
    }
  });
  menu.addEventListener("pointerover", (event) => {
    const providerButton = event.target.closest("[data-provider]");
    if (providerButton) setActiveProvider(providerButton.dataset.provider);
  });
  menu.addEventListener("focusin", (event) => {
    const providerButton = event.target.closest("[data-provider]");
    if (providerButton) setActiveProvider(providerButton.dataset.provider);
  });
  menu.addEventListener("click", (event) => {
    const modelButton = event.target.closest("[data-model-id]");
    if (modelButton) chooseModel(modelButton.dataset.modelId);
  });
  menu.addEventListener("keydown", handleModelPickerKeydown);
  document.addEventListener("pointerdown", (event) => {
    if (!picker.contains(event.target)) closeModelPicker();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) closeModelPicker(true);
  });
}

function renderModels(models, defaultModel) {
  const select = $("#modelSelect");
  select.innerHTML = models.length
    ? models.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.label)} · ${escapeHtml(model.provider)}</option>`).join("")
    : '<option value="">未发现可用模型</option>';
  select.disabled = models.length === 0;
  if (models.some((model) => model.id === defaultModel)) select.value = defaultModel;
  modelPickerState.models = models;
  modelPickerState.providers = [...new Set(models.map((model) => model.provider))];
  modelPickerState.activeProvider = currentModel()?.provider || modelPickerState.providers[0] || "";
  renderProviderOptions();
  renderModelOptions();
  updateModelPickerValue();
  updateRunName();
}

function updateRuntimeControls() {
  const ready = Boolean(state.capabilities?.ready);
  const hasModel = Boolean($("#modelSelect").value);
  $("#previewButton").disabled = !ready || !hasModel;
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
  const datasets = form.getAll("datasets");
  const recordIdsByDataset = Object.fromEntries(
    [...document.querySelectorAll("[data-record-selector]")]
      .filter((selector) => datasets.includes(selector.dataset.recordSelector))
      .map((selector) => [selector.dataset.recordSelector, recordIdsForSelector(selector)])
  );
  const retries = Number(form.get("timeout_retries"));
  return {
    schema_version: 1,
    name: null,
    groups: form.getAll("groups"),
    datasets,
    selection: { record_ids_by_dataset: recordIdsByDataset, offset: Number(form.get("offset") || 0), limit: form.get("limit") ? Number(form.get("limit")) : null },
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
  renderResultMatrix(tasks);
}

function groupLabel(groupId) {
  if (groupId === "single_llm_skills_on") return "Skills On";
  if (groupId === "single_llm_skills_off") return "Skills Off";
  return groupId.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function resultCell(task) {
  if (!task) return '<td class="result-cell result-cell--empty"><span>未选择</span></td>';
  const result = task.result || {};
  const evaluation = result.evaluation || {};
  const score = evaluation.normalized_score ?? evaluation.score;
  const formattedScore = score == null || !Number.isFinite(Number(score)) ? "—" : Number(score).toFixed(4);
  return `<td class="result-cell">
    <div class="result-cell-top"><span class="checkpoint ${escapeHtml(task.checkpoint)}">${escapeHtml(task.checkpoint)}</span><strong>${formattedScore}</strong></div>
    <dl><div><dt>Lifecycle</dt><dd>${escapeHtml(result.run_lifecycle_status || "—")}</dd></div><div><dt>Answer</dt><dd>${escapeHtml(result.answer_availability || "—")}</dd></div></dl>
  </td>`;
}

function renderResultMatrix(tasks) {
  const groups = [...new Set(tasks.map((task) => task.group_id))];
  const records = [...new Set(tasks.map((task) => task.record_id))];
  const taskByPair = new Map(tasks.map((task) => [`${task.group_id}\u0000${task.record_id}`, task]));
  $("#recordCountLabel").textContent = `${records.length} records · ${groups.length} groups`;
  $("#recordHead").innerHTML = `<tr><th class="record-column">Record</th>${groups.map((groupId) => `<th class="group-column"><strong>${escapeHtml(groupLabel(groupId))}</strong><code>${escapeHtml(groupId)}</code></th>`).join("")}</tr>`;
  $("#recordRows").innerHTML = records.map((recordId) => `<tr><th scope="row" class="record-column"><code>${escapeHtml(recordId)}</code></th>${groups.map((groupId) => resultCell(taskByPair.get(`${groupId}\u0000${recordId}`))).join("")}</tr>`).join("");
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
  $("#runForm").addEventListener("input", invalidatePreview);
  $("#runForm").addEventListener("change", invalidatePreview);
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
  initModelPicker();
  $("#modelSelect").addEventListener("change", updateRunName);
  document.querySelectorAll("[name=datasets]").forEach((input) => input.addEventListener("change", () => {
    updateRunName();
    updateRecordSelectors();
  }));
  window.addEventListener("load", () => { if (window.lucide) window.lucide.createIcons(); });
  updateRecordSelectors();
  initResizablePanels();
  updateRuntimeControls();
  Promise.all([loadCapabilities(), loadRuns()]);
}

initialize();
