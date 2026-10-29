"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const els = {
  form: $("#search-form"),
  users: $("#users"),
  usersCombo: $("#users-combo"),
  usersList: $("#users-list"),
  from: $("#from"),
  to: $("#to"),
  limit: $("#limit"),
  delay: $("#delay"),
  match: $("#match"),
  exclude: $("#exclude"),
  previewBtn: $("#preview-btn"),
  applyBtn: $("#apply-btn"),
  cancelBtn: $("#cancel-btn"),
  resultSub: $("#result-sub"),
  exports: $("#exports"),
  exportCsv: $("#export-csv"),
  exportMd: $("#export-md"),
  jobPanel: $("#job-panel"),
  jobSpinner: $("#job-spinner"),
  jobStatus: $("#job-status-text"),
  jobElapsed: $("#job-elapsed"),
  jobCancel: $("#job-cancel"),
  logBox: $("#log-box"),
  jobLog: $("#job-log"),
  empty: $("#empty"),
  tableWrap: $("#table-wrap"),
  postsBody: $("#posts-body"),
  resultFoot: $("#result-foot"),
  applyNote: $("#apply-note"),
  chipEngine: $("#chip-engine"),
  chipChrome: $("#chip-chrome"),
  historyEmpty: $("#history-empty"),
  historyWrap: $("#history-wrap"),
  historyBody: $("#history-body"),
  refreshHistory: $("#refresh-history"),
  undoLast: $("#undo-last"),
  confirmDialog: $("#confirm-dialog"),
  confirmSummary: $("#confirm-summary"),
  confirmCheck: $("#confirm-check"),
  confirmGo: $("#confirm-go"),
  confirmCancel: $("#confirm-cancel"),
  undoDialog: $("#undo-dialog"),
  undoTitle: $("#undo-title"),
  undoText: $("#undo-text"),
  undoConfirm: $("#undo-confirm"),
  toast: $("#toast"),
};

const state = {
  preview: null,
  previewParams: null,
  previewStale: false,
  busy: false,
  engineOk: null,
  jobId: null,
  poll: null,
  undoTarget: null,
};

let toastTimer = null;

// --- autocompletado de cuentas ---------------------------------------------

const combo = {
  items: [],
  active: -1,
  open: false,
  timer: null,
  controller: null,
  seq: 0,
};

function currentToken(input) {
  const value = input.value;
  const caret = input.selectionStart ?? value.length;
  const before = value.slice(0, caret);
  const start = before.lastIndexOf(",") + 1;
  let end = value.indexOf(",", caret);
  if (end === -1) end = value.length;
  const query = value.slice(start, end).trim().replace(/^@/, "");
  return { start, end, query, caret };
}

function comboUsed(value, start) {
  const out = [];
  let offset = 0;
  for (const part of value.split(",")) {
    const handle = part.trim().replace(/^@/, "");
    if (offset !== start && /^[A-Za-z0-9_]{1,15}$/.test(handle)) {
      out.push(handle);
    }
    offset += part.length + 1;
  }
  return out;
}

function appendHighlighted(element, text, query) {
  const needle = (query || "").toLowerCase();
  const index = needle ? text.toLowerCase().indexOf(needle) : -1;
  if (index < 0) {
    element.textContent = text;
    return;
  }
  element.append(text.slice(0, index));
  const mark = document.createElement("mark");
  mark.textContent = text.slice(index, index + needle.length);
  element.append(mark, text.slice(index + needle.length));
}

function setComboActive(index) {
  const options = Array.from(els.usersList.querySelectorAll(".combo-option"));
  if (!options.length) {
    combo.active = -1;
    els.users.removeAttribute("aria-activedescendant");
    return;
  }
  const next = Math.max(0, Math.min(index, options.length - 1));
  combo.active = next;
  options.forEach((option, position) => {
    option.setAttribute("aria-selected", position === next ? "true" : "false");
  });
  const current = options[next];
  els.users.setAttribute("aria-activedescendant", current.id);
  current.scrollIntoView({ block: "nearest" });
}

function hideCombo() {
  combo.open = false;
  combo.active = -1;
  combo.items = [];
  combo.seq += 1;
  clearTimeout(combo.timer);
  combo.timer = null;
  if (combo.controller) combo.controller.abort();
  combo.controller = null;
  els.usersList.replaceChildren();
  els.usersList.hidden = true;
  els.users.setAttribute("aria-expanded", "false");
  els.users.removeAttribute("aria-activedescendant");
}

function renderCombo(items, query) {
  const list = els.usersList;
  list.replaceChildren();
  combo.items = items;
  combo.active = -1;

  if (!items.length) {
    if (query) {
      const empty = document.createElement("li");
      empty.className = "combo-empty";
      empty.textContent = "Sin cuentas usadas que coincidan.";
      list.append(empty);
      combo.open = true;
      list.hidden = false;
      els.users.setAttribute("aria-expanded", "true");
    } else {
      hideCombo();
    }
    return;
  }

  const fragment = document.createDocumentFragment();
  items.forEach((item, index) => {
    const option = document.createElement("li");
    option.className = "combo-option";
    option.id = `users-option-${index}`;
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    option.dataset.handle = item.handle;

    const name = document.createElement("strong");
    appendHighlighted(name, "@" + item.handle, query);
    option.append(name);

    const uses = Number(item.count || 0);
    if (uses > 1) {
      const meta = document.createElement("small");
      meta.textContent = `${uses} usos`;
      option.append(meta);
    }

    option.addEventListener("mousedown", (event) => event.preventDefault());
    option.addEventListener("mouseenter", () => setComboActive(index));
    option.addEventListener("click", () => chooseCombo(index));
    fragment.append(option);
  });

  list.append(fragment);
  combo.open = true;
  list.hidden = false;
  els.users.setAttribute("aria-expanded", "true");
}

async function fetchCombo() {
  if (document.activeElement !== els.users) return;
  const token = currentToken(els.users);
  const exclude = comboUsed(els.users.value, token.start);
  const params = new URLSearchParams({ q: token.query, limit: "8" });
  if (exclude.length) params.set("exclude", exclude.join(","));

  if (combo.controller) combo.controller.abort();
  combo.controller = new AbortController();
  const seq = ++combo.seq;
  try {
    const response = await fetch(`/api/accounts?${params}`, {
      signal: combo.controller.signal,
      headers: { Accept: "application/json" },
    });
    if (seq !== combo.seq) return;
    if (!response.ok) {
      hideCombo();
      return;
    }
    const data = await response.json();
    if (seq !== combo.seq) return;
    renderCombo(data.accounts || [], token.query);
  } catch (error) {
    if (error.name !== "AbortError" && seq === combo.seq) hideCombo();
  }
}

function scheduleCombo(immediate = false) {
  clearTimeout(combo.timer);
  if (immediate) {
    fetchCombo();
    return;
  }
  combo.timer = setTimeout(fetchCombo, 120);
}

function chooseCombo(index) {
  const item = combo.items[index];
  if (!item) return;
  const value = els.users.value;
  const token = currentToken(els.users);
  const raw = value.slice(token.start, token.end);
  const lead = (raw.match(/^\s*/) || [""])[0];
  const suffix = value.slice(token.end);
  let replacement = lead + "@" + item.handle;
  let caret = token.start + replacement.length;
  if (!suffix.startsWith(",")) {
    replacement += ", ";
    caret += 2;
  }
  els.users.value = value.slice(0, token.start) + replacement + suffix;
  els.users.setSelectionRange(caret, caret);
  hideCombo();
  els.users.dispatchEvent(new Event("input", { bubbles: true }));
  scheduleCombo(true);
}

function todayISO() {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
}

function toast(message, kind = "") {
  els.toast.textContent = message;
  els.toast.className = "toast " + kind;
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    els.toast.hidden = true;
  }, 6500);
}

async function api(path, { method = "GET", body = null } = {}) {
  const options = { method, headers: {} };
  if (body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = null;
  try {
    data = await response.json();
  } catch (_) {
    data = null;
  }
  if (!response.ok) {
    throw new Error((data && data.error) || `Error ${response.status}`);
  }
  return data;
}

function syncButtons() {
  els.previewBtn.disabled = state.busy || state.engineOk === false;
  els.applyBtn.disabled = state.busy || !state.preview || state.previewStale;
  els.cancelBtn.hidden = !state.busy;
}

function setBusy(busy) {
  state.busy = busy;
  syncButtons();
}

function formParams() {
  const scope = (document.querySelector('input[name="scope"]:checked') || {}).value || "originals";
  return {
    users: els.users.value.trim(),
    from: els.from.value,
    to: els.to.value,
    scope,
    limit: Number(els.limit.value || 10),
    delay: Number(els.delay.value || 20),
    match: els.match.value.trim(),
    exclude: els.exclude.value.trim(),
  };
}

function validateParams(params) {
  if (!params.users) throw new Error("Indica al menos una cuenta.");
  if (!params.from || !params.to) throw new Error("Elige la fecha inicial y la final.");
  if (params.from > params.to) throw new Error("La fecha inicial no puede ser posterior a la final.");
  if (!(params.limit >= 1 && params.limit <= 50)) throw new Error("El máximo debe estar entre 1 y 50.");
  if (!(params.delay >= 5 && params.delay <= 300)) throw new Error("La pausa debe estar entre 5 y 300 segundos.");
}

function invalidatePreview() {
  if (!state.preview) return;
  state.previewStale = true;
  els.applyNote.textContent = "Los datos cambiaron; vuelve a previsualizar para repostear.";
  els.resultFoot.hidden = false;
  syncButtons();
}

function showJobPanel(label) {
  els.jobPanel.hidden = false;
  els.jobSpinner.hidden = false;
  els.jobCancel.hidden = false;
  els.jobStatus.textContent = label;
  els.jobElapsed.textContent = "";
  els.jobLog.textContent = "";
  els.logBox.open = false;
}

function hideJobPanel() {
  els.jobPanel.hidden = true;
  els.jobSpinner.hidden = true;
  els.jobCancel.hidden = true;
}

function renderLog(lines) {
  const nearBottom =
    els.jobLog.scrollTop + els.jobLog.clientHeight >= els.jobLog.scrollHeight - 24;
  els.jobLog.textContent = lines.join("\n");
  if (nearBottom) els.jobLog.scrollTop = els.jobLog.scrollHeight;
}

function statusLabel(job) {
  if (job.status === "running") {
    if (job.kind === "apply") return "Reposteando…";
    if (job.kind === "undo") return "Deshaciendo…";
    return "Buscando en X…";
  }
  if (job.status === "done") return "Completado";
  if (job.status === "cancelled") return "Cancelado";
  return "Error";
}

function watchJob(id, onDone, onFail) {
  state.jobId = id;
  setBusy(true);
  if (state.poll) clearInterval(state.poll);

  const tick = async () => {
    let job;
    try {
      job = await api(`/api/jobs/${id}`);
    } catch (error) {
      clearInterval(state.poll);
      state.poll = null;
      state.jobId = null;
      setBusy(false);
      toast(error.message, "bad");
      return;
    }

    renderLog(job.lines || []);
    els.jobElapsed.textContent = job.elapsed_label || "";
    els.jobStatus.textContent = statusLabel(job);
    els.jobSpinner.hidden = job.status !== "running";
    els.jobCancel.hidden = job.status !== "running";

    if (job.status === "running") return;

    clearInterval(state.poll);
    state.poll = null;
    state.jobId = null;
    setBusy(false);

    if (job.status === "done") {
      if (job.kind === "apply") {
        const done = (job.posts || []).filter((post) => post.estado === "reposteado").length;
        toast(done ? `${done} repost(s) completados.` : "Ejecución terminada sin reposts nuevos.", "ok");
      } else if (job.kind === "undo") {
        toast("Deshacer completado.", "ok");
      } else {
        toast("Previsualización lista.", "ok");
      }
      if (onDone) onDone(job);
    } else if (job.status === "cancelled") {
      toast("Ejecución cancelada.", "bad");
    } else {
      toast(job.error || "La ejecución falló.", "bad");
      els.logBox.open = true;
      if (onFail) onFail(job);
    }

    if (job.kind === "undo") loadHistory();
    refreshHealth();
  };

  state.poll = setInterval(tick, 700);
  tick();
}

async function runPreview(event) {
  event.preventDefault();
  hideCombo();
  if (state.busy) return;

  let params;
  try {
    params = formParams();
    validateParams(params);
  } catch (error) {
    toast(error.message, "bad");
    return;
  }

  state.preview = null;
  state.previewParams = null;
  state.previewStale = false;
  els.applyBtn.disabled = true;
  els.exports.hidden = true;
  els.tableWrap.hidden = true;
  els.empty.hidden = true;
  els.resultFoot.hidden = true;
  els.resultSub.textContent = "Buscando publicaciones en X…";
  showJobPanel("Buscando en X…");
  setBusy(true);

  try {
    const { id } = await api("/api/preview", { method: "POST", body: params });
    watchJob(id, (job) => {
      if (job.posts.length) {
        state.preview = job;
        state.previewParams = params;
        state.previewStale = false;
        renderPosts(job.posts);
        els.resultSub.textContent = summaryText(job.posts, params);
        els.exports.hidden = false;
        els.exportCsv.href = `/api/jobs/${job.id}/export/csv`;
        els.exportMd.href = `/api/jobs/${job.id}/export/md`;
        els.applyNote.textContent = `Se repostearán como máximo ${params.limit} de estas ${job.posts.length} publicaciones revisadas, exactamente las del listado.`;
        els.resultFoot.hidden = false;
        syncButtons();
      } else {
        renderPosts([]);
        els.resultSub.textContent = "Sin resultados";
        els.empty.hidden = false;
        els.empty.querySelector("p").textContent = "No hay publicaciones en ese rango con los filtros actuales.";
        els.applyBtn.disabled = true;
      }
    }, (job) => {
      renderPosts([]);
      els.resultSub.textContent = "No se pudo previsualizar.";
      els.empty.hidden = false;
      els.empty.querySelector("p").textContent =
        job.error || "El motor no pudo completar la búsqueda. Revisa el registro.";
      els.applyBtn.disabled = true;
    });
  } catch (error) {
    setBusy(false);
    hideJobPanel();
    els.empty.hidden = false;
    els.resultSub.textContent = "No se pudo previsualizar.";
    toast(error.message, "bad");
  }
}

function summaryText(posts, params) {
  const byUser = new Map();
  for (const post of posts) {
    const key = post.usuario || post.autor || "—";
    byUser.set(key, (byUser.get(key) || 0) + 1);
  }
  const detail = Array.from(byUser.entries())
    .map(([user, count]) => `${count} de ${user}`)
    .join(" · ");
  const scope = params.scope === "all" ? "con retweets" : "solo originales";
  return `${posts.length} publicación(es) · ${detail} · ${params.from} → ${params.to} · ${scope}`;
}

function statusClass(text) {
  if (/^(reposteado|deshecho)$/i.test(text)) return "ok";
  if (/^(ya estaba|no |sin )/i.test(text)) return "bad";
  return "info";
}

function renderPosts(posts) {
  els.postsBody.replaceChildren();

  if (!posts.length) {
    els.tableWrap.hidden = true;
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const post of posts) {
    const row = document.createElement("tr");

    const date = document.createElement("td");
    date.className = "date";
    date.textContent = post.fecha || "";

    const author = document.createElement("td");
    author.className = "author";
    const name = document.createElement("strong");
    name.textContent = post.autor || "—";
    const handle = document.createElement("small");
    handle.textContent = post.usuario || "";
    author.append(name, handle);

    const body = document.createElement("td");
    body.className = "post";
    const text = document.createElement("div");
    text.className = "text";
    text.textContent = post.texto || "(sin texto)";
    const link = document.createElement("a");
    link.href = post.url || "#";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Ver en X ↗";
    body.append(text, link);

    const status = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = "pill " + statusClass(post.estado || "");
    pill.textContent = post.estado || "—";
    status.append(pill);

    row.append(date, author, body, status);
    fragment.append(row);
  }

  els.postsBody.append(fragment);
  els.tableWrap.hidden = false;
  els.empty.hidden = true;
}

function openConfirm() {
  if (!state.preview || state.previewStale || state.busy) return;
  const params = state.previewParams;

  els.confirmSummary.replaceChildren();
  const rows = [
    ["Cuentas", params.users],
    ["Fechas", `${params.from} → ${params.to}`],
    ["Alcance", params.scope === "all" ? "Originales y retweets de otras cuentas" : "Solo publicaciones originales"],
    ["Máximo", `${params.limit} publicaciones`],
    ["Pausa", `${params.delay} s entre reposts`],
  ];
  if (params.match) rows.push(["Incluir", params.match]);
  if (params.exclude) rows.push(["Excluir", params.exclude]);

  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    els.confirmSummary.append(dt, dd);
  }

  els.confirmCheck.checked = false;
  els.confirmGo.disabled = true;
  els.confirmDialog.showModal();
}

async function confirmApply() {
  if (!state.preview || !state.previewParams || state.busy) return;
  if (!els.confirmCheck.checked) return;

  const params = state.previewParams;
  const previewId = state.preview.id;
  els.confirmDialog.close();

  showJobPanel("Reposteando…");
  setBusy(true);

  try {
    const body = { ...params, preview_id: previewId, confirm: true };
    const { id } = await api("/api/apply", { method: "POST", body });
    watchJob(id, (job) => {
      state.preview = null;
      state.previewParams = null;
      state.previewStale = false;
      renderPosts(job.posts || []);
      els.resultSub.textContent = `Resultado · ${(job.posts || []).length} publicaciones procesadas`;
      if (job.exports && job.exports.csv) {
        els.exportCsv.href = `/api/jobs/${job.id}/export/csv`;
        els.exportMd.href = `/api/jobs/${job.id}/export/md`;
        els.exports.hidden = false;
      }
      els.applyNote.textContent = "La ejecución terminó. Revisa el historial o vuelve a previsualizar para actualizar el listado.";
      els.resultFoot.hidden = false;
      els.applyBtn.disabled = true;
      els.logBox.open = true;
      loadHistory();
    });
  } catch (error) {
    setBusy(false);
    hideJobPanel();
    toast(error.message, "bad");
  }
}

function openUndo(target) {
  if (state.busy) return;
  state.undoTarget = target;
  if (target.mode === "last") {
    els.undoTitle.textContent = "Deshacer la última tanda";
    els.undoText.textContent =
      "Se quitarán los reposts que la herramienta hizo en la última ejecución y que sigan vigentes. No se tocan los reposts que ya estaban antes en la cuenta.";
  } else {
    els.undoTitle.textContent = "Deshacer este repost";
    els.undoText.textContent =
      "Se quitará el repost de esta publicación. Solo se deshace si lo hizo la herramienta.";
  }
  els.undoDialog.showModal();
}

async function doUndo() {
  if (!state.undoTarget || state.busy) return;
  const target = state.undoTarget;
  els.undoDialog.close();
  showJobPanel("Deshaciendo…");
  setBusy(true);
  try {
    const body = target.mode === "last" ? { mode: "last" } : { mode: "url", url: target.url };
    const { id } = await api("/api/undo", { method: "POST", body });
    watchJob(id, () => loadHistory());
  } catch (error) {
    setBusy(false);
    hideJobPanel();
    toast(error.message, "bad");
  }
}

async function cancelJob() {
  try {
    await api("/api/cancel", { method: "POST", body: {} });
    toast("Cancelando la ejecución…");
  } catch (error) {
    toast(error.message, "bad");
  }
}

function setChip(element, ok, text) {
  element.classList.toggle("ok", ok === true);
  element.classList.toggle("bad", ok === false);
  const dot = element.querySelector(".dot");
  const label = element.lastChild;
  if (dot) dot.setAttribute("aria-hidden", "true");
  if (label && label.nodeType === Node.TEXT_NODE) {
    label.textContent = " " + text;
  } else {
    element.textContent = text;
  }
}

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    state.engineOk = health.engine_ok;
    setChip(els.chipEngine, health.engine_ok, health.engine_ok ? "Motor listo" : "Motor no encontrado");
    setChip(els.chipChrome, health.chrome, health.chrome ? "Chrome conectado" : "Chrome no detectado");
    syncButtons();
  } catch (_) {
    state.engineOk = false;
    setChip(els.chipEngine, false, "Servidor sin respuesta");
    syncButtons();
  }
}

function historyAction(action) {
  if (action === "repost") return "repost";
  if (action === "undo") return "undo";
  return action || "—";
}

function renderHistory(entries) {
  els.historyBody.replaceChildren();
  if (!entries.length) {
    els.historyWrap.hidden = true;
    els.historyEmpty.hidden = false;
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const entry of entries) {
    const row = document.createElement("tr");

    const when = document.createElement("td");
    when.className = "date";
    when.textContent = (entry.ts || "").slice(0, 16).replace("T", " ");
    when.title = entry.ts || "";

    const action = document.createElement("td");
    const actionPill = document.createElement("span");
    actionPill.className = "pill " + (entry.action === "undo" ? "bad" : "info");
    actionPill.textContent = historyAction(entry.action);
    action.append(actionPill);

    const result = document.createElement("td");
    const resultPill = document.createElement("span");
    resultPill.className = "pill " + statusClass(entry.result_label || "");
    resultPill.textContent = entry.result_label || entry.result || "—";
    result.append(resultPill);

    const account = document.createElement("td");
    account.className = "author";
    const accountName = document.createElement("strong");
    accountName.textContent = entry.author || "—";
    const accountHandle = document.createElement("small");
    accountHandle.textContent = entry.handle ? "@" + entry.handle : "";
    account.append(accountName, accountHandle);

    const post = document.createElement("td");
    post.className = "post";
    if (entry.url) {
      const link = document.createElement("a");
      link.href = entry.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Ver en X ↗";
      post.append(link);
    } else {
      post.textContent = entry.id || "—";
    }

    const controls = document.createElement("td");
    if (entry.undoable && entry.url) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn tiny danger-ghost";
      button.dataset.undo = entry.url;
      button.textContent = "Deshacer";
      controls.append(button);
    }

    row.append(when, action, result, account, post, controls);
    fragment.append(row);
  }

  els.historyBody.append(fragment);
  els.historyWrap.hidden = false;
  els.historyEmpty.hidden = true;
}

async function loadHistory() {
  try {
    const data = await api("/api/history?n=40");
    renderHistory(data.entries || []);
  } catch (error) {
    toast(error.message, "bad");
  }
}

function init() {
  const today = todayISO();
  els.from.value = today;
  els.to.value = today;
  els.to.min = today;

  els.from.addEventListener("change", () => {
    els.to.min = els.from.value;
    if (els.to.value && els.from.value && els.to.value < els.from.value) {
      els.to.value = els.from.value;
    }
    invalidatePreview();
  });
  els.to.addEventListener("change", invalidatePreview);
  [els.users, els.limit, els.delay, els.match, els.exclude].forEach((input) => {
    input.addEventListener("input", invalidatePreview);
  });
  els.users.addEventListener("input", () => scheduleCombo());
  els.users.addEventListener("focus", () => scheduleCombo(true));
  els.users.addEventListener("blur", () => setTimeout(hideCombo, 120));
  els.users.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const delta = event.key === "ArrowDown" ? 1 : -1;
      if (!combo.open) {
        fetchCombo().then(() => {
          if (combo.open && combo.items.length) {
            setComboActive(delta > 0 ? 0 : combo.items.length - 1);
          }
        });
        return;
      }
      const total = combo.items.length;
      if (!total) return;
      if (combo.active < 0) {
        setComboActive(delta > 0 ? 0 : total - 1);
      } else {
        setComboActive((combo.active + delta + total) % total);
      }
    } else if (event.key === "Enter" && combo.open && combo.active >= 0) {
      event.preventDefault();
      chooseCombo(combo.active);
    } else if (event.key === "Tab" && !event.shiftKey && combo.open && combo.active >= 0) {
      event.preventDefault();
      chooseCombo(combo.active);
    } else if (event.key === "Escape" && combo.open) {
      event.preventDefault();
      hideCombo();
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (!els.usersCombo.contains(event.target)) hideCombo();
  });
  $$('input[name="scope"]').forEach((radio) => {
    radio.addEventListener("change", invalidatePreview);
  });

  els.form.addEventListener("submit", runPreview);
  els.applyBtn.addEventListener("click", openConfirm);
  els.cancelBtn.addEventListener("click", cancelJob);
  els.jobCancel.addEventListener("click", cancelJob);
  els.confirmCheck.addEventListener("change", () => {
    els.confirmGo.disabled = !els.confirmCheck.checked;
  });
  els.confirmGo.addEventListener("click", confirmApply);
  els.undoLast.addEventListener("click", () => openUndo({ mode: "last" }));
  els.undoConfirm.addEventListener("click", doUndo);
  els.refreshHistory.addEventListener("click", loadHistory);
  els.historyBody.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-undo]");
    if (button) openUndo({ mode: "url", url: button.dataset.undo });
  });

  setChip(els.chipEngine, null, "Comprobando…");
  setChip(els.chipChrome, null, "Comprobando…");
  syncButtons();
  refreshHealth();
  setInterval(refreshHealth, 15000);
  loadHistory();
}

document.addEventListener("DOMContentLoaded", init);
