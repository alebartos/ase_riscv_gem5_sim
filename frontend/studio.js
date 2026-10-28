/*
 * SPDX-FileCopyrightText: 2026 Behnam Farnaghinejad <behnam.farnaghinejad@polito.it>
 * SPDX-License-Identifier: GPL-2.0-only
 */
const $ = selector => document.querySelector(selector);

let current = null;
let pipelineData = null;
let selectedRow = null;
let selectedColumn = null;
let selectedSourceLine = null;
let searchSourceLine = null;
let searchMatchIndex = -1;
let playbackCycle = null;
let stepMode = false;
let lastNormalLog = "";
let lastAdvancedLog = lastNormalLog;
let savedSource = "";
let sourceDirty = false;
let scaffoldProtectionEnabled = true;
let scaffoldEditAuthorized = false;
let undoStack = [];
let redoStack = [];
let pendingEditorSnapshot = null;
let pendingEditorViewport = null;
let applyingEditorHistory = false;
let messageDialogResolver = null;
let messageDialogHasInput = false;
let messageDialogConfirmValue = true;
let messageDialogAlternateValue = null;
let messageDialogTrimInput = true;
let pipelineSelectTarget = null;
let externalSyncBusy = false;
let memoryWatches = [];
let cpuConfigurationOpenedAs = null;
let o3Defaults = {};
let predictorParameterDraft = {};
const predictorNames = {
  local: "Local counter (2-bit baseline)",
  tournament: "Tournament (local + global)",
  tage: "TAGE (tagged hybrid)",
  bimode: "Bi-mode"
};
const predictorFields = {
  local: [
    ["localPredictorSize", "Counter storage (bits)", 2, 65536, "Power of two; entries = storage ÷ counter bits."],
    ["localCtrBits", "Counter bits (n)", 1, 8, "Each branch-addressed entry has a saturating counter."],
    ["BTBEntries", "Branch target buffer entries", 2, 65536, "Power of two."],
  ],
  tournament: [
    ["localPredictorSize", "Local counters (entries)", 2, 65536, "Power of two."],
    ["localCtrBits", "Local counter bits", 1, 8, ""],
    ["localHistoryTableSize", "Local history entries", 2, 65536, "Power of two."],
    ["globalPredictorSize", "Global counters (entries; 2^m)", 2, 65536, "Power of two; m = log₂(entries)."],
    ["globalCtrBits", "Global counter bits (n)", 1, 8, ""],
    ["choicePredictorSize", "Selector entries", 2, 65536, "Power of two; also affects global-history length."],
    ["choiceCtrBits", "Selector counter bits", 1, 8, ""],
    ["BTBEntries", "Branch target buffer entries", 2, 65536, "Power of two."],
  ],
  bimode: [
    ["globalPredictorSize", "Direction counters per bank (entries)", 2, 65536, "Power of two; history length = log₂(entries)."],
    ["globalCtrBits", "Direction counter bits", 1, 8, ""],
    ["choicePredictorSize", "Selector entries", 2, 65536, "Power of two."],
    ["choiceCtrBits", "Selector counter bits", 1, 8, ""],
    ["BTBEntries", "Branch target buffer entries", 2, 65536, "Power of two."],
  ],
  tage: [
    ["minHist", "Shortest tagged history (branches)", 1, 8192, ""],
    ["maxHist", "Longest tagged history (branches)", 1, 8192, "Must exceed the shortest history."],
    ["tagTableCounterBits", "Prediction counter bits", 1, 8, ""],
    ["tagTableUBits", "Usefulness bits", 1, 2, "This gem5 implementation supports 1 or 2."],
    ["tagTableTagWidths", "Tag widths (bits)", null, null, "Eight comma-separated values: 0 for the base, then seven tagged tables."],
    ["logTagTableSizes", "Log₂ table entries", null, null, "Eight comma-separated values: base, then seven tagged tables."],
    ["BTBEntries", "Branch target buffer entries", 2, 65536, "Power of two."],
  ]
};
let studioFeatures = {
  developerMode: false,
  memoryConfiguration: false,
  multiIssueCpu: false
};
let configuredProgramsDirectory = "";
let memoryPopoutWindow = null;
let pipelinePaintFrame = null;
let pipelineResizeObserver = null;
let changeContextMenu = null;
let opcodePopupState = null;
let opcodeSnippetSession = null;
let opcodePopupSuppressed = false;
let draftSaveTimer = null;
const memoryWindowChannel = "BroadcastChannel" in window
  ? new BroadcastChannel("ase-studio-memory-window") : null;

if (memoryWindowChannel) {
  memoryWindowChannel.onmessage = event => {
    const message = event.data;
    if (message?.type === "memory-request-state") {
      publishMemoryWindowState();
      return;
    }
    if (message?.type !== "memory-action") return;
    if (message.action === "add") addMemoryWatch(message.name);
    else if (message.action === "remove") removeMemoryWatch(message.index);
    else if (message.action === "clear") clearMemoryWatches();
    else if (message.action === "format") setMemoryFormat(message.value);
    else if (message.action === "navigate-change") {
      navigateToItemChange({
        kind: "memory", key: message.address,
        width: Number(message.width), label: message.label
      }, message.direction);
    }
  };
}

function closeActionDialog(value) {
  const dialog = $("#message-dialog");
  if (dialog.open) dialog.close();
  const resolver = messageDialogResolver;
  messageDialogResolver = null;
  if (resolver) resolver(value);
}

function actionDialog({title, message, confirmLabel = "OK", cancelLabel = null,
  inputLabel = null, inputValue = "", danger = false, confirmValue = true,
  alternateLabel = null, alternateValue = null, trimInput = true}) {
  if (messageDialogResolver) closeActionDialog(null);
  $("#message-title").textContent = title;
  $("#message-text").textContent = message;
  $("#message-confirm").textContent = confirmLabel;
  $("#message-confirm").classList.toggle("danger", danger);
  $("#message-cancel").hidden = !cancelLabel;
  $("#message-cancel").textContent = cancelLabel || "Cancel";
  $("#message-alternate").hidden = !alternateLabel;
  $("#message-alternate").textContent = alternateLabel || "";
  messageDialogConfirmValue = confirmValue;
  messageDialogAlternateValue = alternateValue;
  messageDialogTrimInput = trimInput;
  messageDialogHasInput = Boolean(inputLabel);
  $("#message-input-wrap").hidden = !messageDialogHasInput;
  $("#message-input-label").textContent = inputLabel || "";
  $("#message-input").value = inputValue;
  $("#message-dialog").showModal();
  setTimeout(() => (messageDialogHasInput ? $("#message-input") : $("#message-confirm")).focus(), 0);
  return new Promise(resolve => { messageDialogResolver = resolve; });
}

function showActionMessage(title, message) {
  return actionDialog({title, message});
}

function editorSnapshot() {
  const editor = $("#body");
  return {text: editor.value, start: editor.selectionStart, end: editor.selectionEnd};
}

function editorViewport() {
  const area = document.querySelector(".editor-area");
  return area ? {top: area.scrollTop, left: area.scrollLeft} : null;
}

function restoreEditorViewport(viewport) {
  if (!viewport) return;
  const restore = () => {
    const area = document.querySelector(".editor-area");
    if (!area) return;
    area.scrollTop = viewport.top;
    area.scrollLeft = viewport.left;
  };
  restore();
  requestAnimationFrame(() => {
    restore();
    requestAnimationFrame(restore);
  });
  setTimeout(restore, 0);
}

function resetEditorHistory() {
  undoStack = [];
  redoStack = [];
  pendingEditorSnapshot = null;
  pendingEditorViewport = null;
}

function restoreEditorSnapshot(snapshot) {
  if (!snapshot) return;
  clearOpcodeSnippetSession();
  closeOpcodePopup();
  const editor = $("#body");
  const viewport = editorViewport();
  applyingEditorHistory = true;
  editor.value = snapshot.text;
  editor.setSelectionRange(snapshot.start, snapshot.end);
  applyingEditorHistory = false;
  markSourceEdited();
  scheduleLocalDraft();
  clearDiagnostics();
  refreshScaffoldProtectionUi();
  selectedSourceLine = null;
  syncEditor();
  restoreEditorViewport(viewport);
}

function undoEditor() {
  if (!current || !undoStack.length) return;
  clearOpcodeSnippetSession();
  closeOpcodePopup();
  redoStack.push(editorSnapshot());
  restoreEditorSnapshot(undoStack.pop());
}

function redoEditor() {
  if (!current || !redoStack.length) return;
  clearOpcodeSnippetSession();
  closeOpcodePopup();
  undoStack.push(editorSnapshot());
  restoreEditorSnapshot(redoStack.pop());
}

const savedTheme = localStorage.getItem("ase-studio-theme");
document.documentElement.dataset.theme = savedTheme
  || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");

function updateThemeButton() {
  const dark = document.documentElement.dataset.theme === "dark";
  $("#theme-toggle").textContent = dark ? "Light mode" : "Dark mode";
  $("#theme-toggle").title = dark ? "Use the light color theme" : "Use the dark color theme";
}

function setEditorFontSize(value) {
  const size = Math.max(11, Math.min(22, Number(value) || 14));
  const lineHeight = Math.round(size * 1.65);
  document.documentElement.style.setProperty("--editor-font-size", `${size}px`);
  document.documentElement.style.setProperty("--editor-line-height", `${lineHeight}px`);
  $("#editor-font-size").value = size;
  $("#editor-font-value").textContent = `${size} px`;
  localStorage.setItem("ase-studio-editor-font-size", String(size));
  if (current) syncEditor();
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch (_error) {
    throw Error(`ASE Studio received an incompatible server response (${response.status}). Stop the old server and restart ./ase-studio.sh.`);
  }
  if (!response.ok) {
    const error = Error(data.error || "Request failed");
    error.status = response.status;
    throw error;
  }
  return data;
}

async function loadStudioVersion() {
  try {
    const health = await api("/api/health");
    o3Defaults = health.o3Defaults || {};
    $("#about-version").textContent = `Version ${health.version}`;
    applyDeveloperFeatures(health.features || {});
  } catch (_error) {
    $("#about-version").textContent = "Version unavailable";
    applyDeveloperFeatures({});
  }
}

function applyDeveloperFeatures(features) {
  studioFeatures = {
    developerMode: features.developerMode === true,
    memoryConfiguration: features.memoryConfiguration === true,
    multiIssueCpu: features.multiIssueCpu === true
  };
  $("#developer-mode-badge").hidden = !studioFeatures.developerMode;
  $("#cpu-model-field").hidden = !studioFeatures.multiIssueCpu;
  const multiIssueOption = $("#cpu-model").querySelector('option[value="out-of-order"]');
  multiIssueOption.hidden = !studioFeatures.multiIssueCpu;
  multiIssueOption.disabled = !studioFeatures.multiIssueCpu;
  if (!studioFeatures.multiIssueCpu) $("#cpu-model").value = "in-order";
  $("#memory-management").hidden = !studioFeatures.memoryConfiguration;
  updateForwardingControl();
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
}

function closeOpcodePopup({suppress = false} = {}) {
  opcodePopupState = null;
  if (suppress) opcodePopupSuppressed = true;
  const popup = $("#opcode-popup");
  if (popup) popup.hidden = true;
}

function clearOpcodeSnippetSession() {
  opcodeSnippetSession = null;
}

function opcodeContext(editor = $("#body")) {
  if (!current || !editor || editor.selectionStart !== editor.selectionEnd) return null;
  const caret = editor.selectionStart;
  const lineStart = caret > 0 ? editor.value.lastIndexOf("\n", caret - 1) + 1 : 0;
  const lineEndIndex = editor.value.indexOf("\n", caret);
  const lineEnd = lineEndIndex < 0 ? editor.value.length : lineEndIndex;
  const beforeCaret = editor.value.slice(lineStart, caret);
  const afterCaret = editor.value.slice(caret, lineEnd);
  const codeBeforeCaret = beforeCaret.split("#", 1)[0];
  const codeAfterCaret = afterCaret.split("#", 1)[0];
  if (codeAfterCaret.trim()) return null;
  const match = codeBeforeCaret.match(/^(\s*)(?:[A-Za-z_.$][\w.$]*:\s*)?([A-Za-z][A-Za-z0-9_.]*)$/);
  if (!match || match[2].startsWith(".")) return null;
  const tokenStart = lineStart + codeBeforeCaret.length - match[2].length;
  const tokenEnd = tokenStart + match[2].length;
  if (editTouchesProtected(editor.value, tokenStart, tokenEnd)) return null;
  return {
    prefix: match[2].toLowerCase(), tokenStart, tokenEnd,
    lineNumber: editor.value.slice(0, lineStart).split("\n").length,
    linePrefix: codeBeforeCaret
  };
}

function opcodeExtensionEnabled(entry) {
  if (entry.extension === "Pseudo") return true;
  const extensions = new Set((current?.isa?.extensions
    || ["i", "m", "a", "f", "zicsr", "zifencei"])
    .map(extension => extension.toLowerCase()));
  return extensions.has(entry.extension.toLowerCase());
}

function activeOpcodeCandidates(prefix) {
  return RISCV_OPCODE_CATALOG
    .filter(entry => opcodeExtensionEnabled(entry)
      && entry.mnemonic.toLowerCase().startsWith(prefix));
}

function opcodeExtensionLabel(extension) {
  return extension === "Pseudo" ? "pseudo" : extension;
}

function renderOpcodePopup() {
  const popup = $("#opcode-popup");
  if (!popup || !opcodePopupState) return;
  const {context, matches, selectedIndex} = opcodePopupState;
  $("#opcode-popup-isa").textContent = `ISA ${(current?.isa?.march || "rv32imaf_zicsr_zifencei").toUpperCase()} · ABI ${current?.isa?.abi || "ilp32f"}`;
  $("#opcode-popup-filter").textContent = `Filter: ${context.prefix}`;
  const list = $("#opcode-popup-list");
  if (!matches.length) {
    list.innerHTML = `<div class="opcode-empty">No enabled instruction matches “${escapeHtml(context.prefix)}”.</div>`;
  } else {
    list.innerHTML = matches.map((entry, index) => `
      <button type="button" class="opcode-option" role="option" data-opcode-index="${index}"
        aria-selected="${index === selectedIndex ? "true" : "false"}">
        <code>${escapeHtml(entry.mnemonic)}</code>
        <span class="opcode-syntax">${escapeHtml(entry.syntax)}</span>
        <span class="opcode-extension">${escapeHtml(opcodeExtensionLabel(entry.extension))}</span>
        <span class="opcode-description">${escapeHtml(entry.description || "")}</span>
      </button>`).join("");
  }
  popup.hidden = false;
  positionOpcodePopup();
}

function positionOpcodePopup() {
  const popup = $("#opcode-popup");
  const editor = $("#body");
  const wrap = document.querySelector(".editor-wrap");
  const area = document.querySelector(".editor-area");
  const context = opcodePopupState?.context;
  if (!popup || popup.hidden || !editor || !wrap || !area || !context) return;
  const line = document.querySelector(`#highlight .source-line[data-line="${context.lineNumber}"]`);
  if (!line) return;
  const lineRect = line.getBoundingClientRect();
  const wrapRect = wrap.getBoundingClientRect();
  const font = getComputedStyle(editor);
  const canvas = positionOpcodePopup.canvas
    || (positionOpcodePopup.canvas = document.createElement("canvas"));
  const context2d = canvas.getContext("2d");
  context2d.font = font.font;
  const left = lineRect.left - wrapRect.left
    + Math.min(context2d.measureText(context.linePrefix).width, Math.max(0, wrap.clientWidth - 120));
  const lineTop = lineRect.top - wrapRect.top;
  let top = lineRect.bottom - wrapRect.top + 4;
  const minLeft = 10;
  const maxLeft = Math.max(minLeft, wrap.clientWidth - popup.offsetWidth - 10);
  const visibleTop = area.scrollTop + 8;
  const visibleBottom = area.scrollTop + area.clientHeight - 8;
  if (top + popup.offsetHeight > visibleBottom && lineTop - popup.offsetHeight - 4 >= visibleTop) {
    top = lineTop - popup.offsetHeight - 4;
  }
  popup.style.left = `${Math.min(maxLeft, Math.max(minLeft, left))}px`;
  popup.style.top = `${Math.max(visibleTop, top)}px`;
}

function updateOpcodePopup() {
  if (!current || opcodePopupSuppressed) return closeOpcodePopup();
  const context = opcodeContext();
  if (!context) return closeOpcodePopup();
  const matches = activeOpcodeCandidates(context.prefix);
  const previousMnemonic = opcodePopupState?.matches?.[opcodePopupState.selectedIndex]?.mnemonic;
  let selectedIndex = matches.findIndex(entry => entry.mnemonic === previousMnemonic);
  if (selectedIndex < 0) selectedIndex = 0;
  opcodePopupState = {context, matches, selectedIndex};
  renderOpcodePopup();
}

function selectOpcodeSuggestion(delta) {
  if (!opcodePopupState?.matches.length) return;
  const count = opcodePopupState.matches.length;
  opcodePopupState.selectedIndex = (opcodePopupState.selectedIndex + delta + count) % count;
  renderOpcodePopup();
}

function instantiateOpcode(entry) {
  const fields = [];
  const pattern = /\{(\d+)\}/g;
  let output = "";
  let cursor = 0;
  let match;
  while ((match = pattern.exec(entry.template))) {
    output += entry.template.slice(cursor, match.index);
    const fieldIndex = Number(match[1]);
    const value = entry.operands[fieldIndex]?.placeholder || "";
    const start = output.length;
    output += value;
    fields.push({
      start, end: output.length,
      role: entry.operands[fieldIndex]?.role || "operand"
    });
    cursor = match.index + match[0].length;
  }
  output += entry.template.slice(cursor);
  return {text: output, fields};
}

function completeOpcodeSuggestion() {
  if (!opcodePopupState) return;
  const editor = $("#body");
  const {context, matches, selectedIndex} = opcodePopupState;
  const entry = matches[selectedIndex];
  if (!entry || editTouchesProtected(editor.value, context.tokenStart, context.tokenEnd)) return;
  const snippet = instantiateOpcode(entry);
  const viewport = editorViewport();
  const absoluteFields = snippet.fields.map(field => ({
    start: context.tokenStart + field.start,
    end: context.tokenStart + field.end,
    role: field.role
  }));
  const first = absoluteFields[0];
  const caret = context.tokenStart + snippet.text.length;
  applyEditorChange(context.tokenStart, context.tokenEnd, snippet.text,
    first?.start ?? caret, first?.end ?? caret, viewport);
  opcodePopupSuppressed = false;
  closeOpcodePopup();
  opcodeSnippetSession = absoluteFields.length
    ? {fields: absoluteFields, index: 0} : null;
  if (first) editor.setSelectionRange(first.start, first.end);
}

function moveOpcodeSnippet(delta) {
  const session = opcodeSnippetSession;
  const editor = $("#body");
  if (!session || !editor) return false;
  const next = session.index + delta;
  if (next < 0 || next >= session.fields.length) {
    const currentField = session.fields[session.index];
    const caret = delta > 0 ? currentField.end : currentField.start;
    editor.setSelectionRange(caret, caret);
    clearOpcodeSnippetSession();
    return true;
  }
  session.index = next;
  const field = session.fields[next];
  editor.setSelectionRange(field.start, field.end);
  return true;
}

function handleOpcodeSelectionChange() {
  const editor = $("#body");
  if (!editor || document.activeElement !== editor) return;
  if (opcodeSnippetSession) {
    const field = opcodeSnippetSession.fields[opcodeSnippetSession.index];
    if (!field || editor.selectionStart !== field.start || editor.selectionEnd !== field.end) {
      clearOpcodeSnippetSession();
    }
  }
  if (!opcodeSnippetSession) updateOpcodePopup();
}

function updateDirtyIndicator() {
  document.title = sourceDirty ? "ASE Studio • Unsaved" : "ASE Studio";
  if (!current) return;
  $("#project-title").textContent = current.name + (sourceDirty ? " • Unsaved" : "");
}

function markSourceEdited() {
  sourceDirty = Boolean(current) && $("#body").value !== savedSource;
  updateDirtyIndicator();
}

function draftStorageKey(name) {
  return name ? `ase-studio-draft:${name}` : "";
}

function setDraftStatus(message = "") {
  const status = $("#draft-status");
  if (!status) return;
  status.textContent = message;
  status.hidden = !message;
  status.title = message;
}

function readLocalDraft(name) {
  const key = draftStorageKey(name);
  if (!key) return null;
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const draft = JSON.parse(raw);
    if (!draft || typeof draft.text !== "string" || typeof draft.timestamp !== "number") return null;
    return draft;
  } catch (_error) {
    return null;
  }
}

function clearLocalDraft(name = current?.name) {
  if (draftSaveTimer !== null) {
    clearTimeout(draftSaveTimer);
    draftSaveTimer = null;
  }
  const key = draftStorageKey(name);
  if (key) {
    try { localStorage.removeItem(key); } catch (_error) { /* Storage may be unavailable. */ }
  }
  setDraftStatus();
}

function writeLocalDraft() {
  draftSaveTimer = null;
  if (!current || !sourceDirty) return;
  const text = $("#body").value;
  if (text === savedSource) {
    clearLocalDraft(current.name);
    return;
  }
  try {
    localStorage.setItem(draftStorageKey(current.name), JSON.stringify({
      text,
      timestamp: Date.now(),
      sourceName: current.sourceName
    }));
    setDraftStatus("Local draft saved");
  } catch (_error) {
    setDraftStatus("Local draft unavailable");
  }
}

function scheduleLocalDraft() {
  if (!current) return;
  if (!sourceDirty) {
    clearLocalDraft(current.name);
    return;
  }
  if (draftSaveTimer !== null) clearTimeout(draftSaveTimer);
  draftSaveTimer = setTimeout(writeLocalDraft, 250);
}

function clearDiagnostics() {
  const container = $("#diagnostics");
  if (!container) return;
  container.hidden = true;
  container.innerHTML = "";
}

function jumpToSourcePosition(lineNumber, columnNumber = 1) {
  if (!current) return;
  const editor = $("#body");
  const lines = editor.value.split(/\r?\n/);
  const line = Math.max(1, Math.min(Number(lineNumber) || 1, lines.length));
  const column = Math.max(1, Number(columnNumber) || 1);
  let offset = 0;
  for (let index = 0; index < line - 1; index++) offset += lines[index].length + 1;
  offset += Math.min(lines[line - 1].length, column - 1);
  editor.focus();
  highlightSourceLine(line);
  editor.setSelectionRange(offset, offset);
  scrollEditorToLine(line);
}

function linkerSymbolFromDiagnostic(text) {
  const match = String(text || "").match(
    /(?:undefined reference to|multiple definition of|against (?:the )?symbol)\s+[`'\"]([^`'\"]+)[`'\"]/i
  );
  return match ? match[1].trim() : "";
}

function linkerFunctionFromDiagnostic(text) {
  const quoted = String(text || "").match(/\bin function\s+[`'\"]([^`'\"]+)[`'\"]/i);
  if (quoted) return quoted[1].trim();
  const plain = String(text || "").match(/\bin function\s+([A-Za-z_.$][\w.$]*)/i);
  return plain ? plain[1].trim() : "";
}

function linkerSourceLocation(sourceText, symbol, functionName) {
  if (!sourceText) return null;
  const lines = String(sourceText).split(/\r?\n/);
  const escapeRegExp = value => String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const functionPattern = functionName
    ? new RegExp(`^\\s*${escapeRegExp(functionName)}\\s*:`)
    : null;
  const symbolPattern = symbol
    ? new RegExp(`(^|[^A-Za-z0-9_.$])${escapeRegExp(symbol)}(?=$|[^A-Za-z0-9_.$])`, "g")
    : null;
  const candidates = [];
  let functionLine = -1;

  lines.forEach((rawLine, index) => {
    const code = rawLine.split("#", 1)[0];
    if (functionPattern && functionLine < 0 && functionPattern.test(code)) functionLine = index;
    if (!symbolPattern || !code.trim()
        || /^\s*\.(?:globl|global|extern|weak|hidden|type|size|local|set)\b/i.test(code)
        || new RegExp(`^\\s*${escapeRegExp(symbol)}\\s*:`).test(code)) return;
    symbolPattern.lastIndex = 0;
    let match;
    while ((match = symbolPattern.exec(code))) {
      const tokenOffset = match.index + (match[1] ? 1 : 0);
      candidates.push({line: index + 1, column: tokenOffset + 1});
      if (match.index === symbolPattern.lastIndex) symbolPattern.lastIndex += 1;
    }
  });

  const scopedCandidates = functionLine >= 0
    ? candidates.filter(candidate => candidate.line >= functionLine + 1)
    : candidates;
  const location = scopedCandidates[0] || candidates[0];
  if (location) return location;
  if (functionLine >= 0) {
    const column = Math.max(1, lines[functionLine].search(/\S/) + 1);
    return {line: functionLine + 1, column};
  }
  return null;
}

function parseCompilerDiagnostics(output) {
  const sourceName = String(current?.sourceName || "").toLowerCase();
  const sourceText = typeof document === "undefined"
    ? "" : document.getElementById("body")?.value || "";
  const diagnostics = [];
  let linkerFunction = "";
  String(output || "").split(/\r?\n/).forEach((line, index) => {
    const match = line.match(/^(.*?\.(?:s|asm)):(\d+)(?::(\d+))?:\s*(?:(?:fatal\s+)?(error|warning|note)\s*:\s*)?(.*)$/i);
    if (match) {
      const file = match[1].trim();
      const basename = file.split(/[\\/]/).pop().toLowerCase();
      if (!sourceName || (basename !== sourceName && file.toLowerCase() !== sourceName)) return;
      const severity = (match[4] || "error").toLowerCase();
      const message = match[5].trim() || "Assembler diagnostic";
      diagnostics.push({
        severity,
        file: basename,
        line: Number(match[2]),
        column: Number(match[3] || 1),
        message,
        outputLine: index + 1,
        kind: "compiler"
      });
      return;
    }
    const trimmed = line.trim();
    if (!trimmed || /^make(?:\[.*\])?:/i.test(trimmed)) return;
    const functionName = linkerFunctionFromDiagnostic(trimmed);
    if (functionName) linkerFunction = functionName;
    if (!/(?:undefined reference to|multiple definition of|relocation .*\bagainst\s+(?:the\s+)?symbol|collect2:\s*error:|cannot find\s+-l|(?:ld|as):.*\bfatal error:|(?:ld|as):.*\berror:)/i.test(trimmed)) return;
    const symbol = linkerSymbolFromDiagnostic(trimmed);
    const location = symbol ? linkerSourceLocation(sourceText, symbol, linkerFunction) : null;
    diagnostics.push({severity: "error", file: location ? sourceName : "",
      line: location?.line || null, column: location?.column || 1,
      message: trimmed, outputLine: index + 1, kind: "linker",
      inferred: Boolean(location)});
  });
  return diagnostics;
}

function renderDiagnostics(entries, heading = "Source diagnostics") {
  const container = $("#diagnostics");
  if (!container) return;
  if (!entries.length) {
    clearDiagnostics();
    return;
  }
  const shown = entries.slice(0, 40);
  const remaining = entries.length - shown.length;
  container.hidden = false;
  container.innerHTML = `<div class="diagnostics-header">${escapeHtml(heading)} · ${entries.length}</div><div class="diagnostics-list">${shown.map((entry, index) => {
    const location = entry.line
      ? `${entry.file ? `${entry.file}:` : ""}${entry.line}:${entry.column || 1}`
      : entry.kind === "linker" ? "linker" : "source";
    const content = `<span class="diagnostic-location">${escapeHtml(location)}</span><span class="diagnostic-message">${escapeHtml(entry.message)}</span>`;
    if (!entry.line) return `<div class="diagnostic-item ${escapeHtml(entry.severity || "error")}">${content}</div>`;
    const title = entry.inferred ? "Jump to the inferred source location" : "Jump to source location";
    return `<button type="button" class="diagnostic-item ${escapeHtml(entry.severity || "error")}" data-diagnostic-index="${index}" title="${title}">${content}</button>`;
  }).join("")}${remaining > 0 ? `<div class="diagnostic-message">… and ${remaining} more diagnostics</div>` : ""}</div>`;
  container.querySelectorAll("[data-diagnostic-index]").forEach(button => {
    button.onclick = () => {
      const entry = shown[Number(button.dataset.diagnosticIndex)];
      if (entry?.line) jumpToSourcePosition(entry.line, entry.column);
    };
  });
}

function setDiagnostics(entries, heading = "Source diagnostics") {
  renderDiagnostics(entries, heading);
}

function scaffoldEditsAllowed() {
  return !scaffoldProtectionEnabled || scaffoldEditAuthorized;
}

function updateScaffoldProtectionUi(message = "") {
  const control = $("#scaffold-protection");
  const status = $("#scaffold-protection-status");
  control.checked = scaffoldProtectionEnabled;
  control.disabled = !current;
  control.title = scaffoldProtectionEnabled
    ? "Required _start and End labels and the exit block are read-only."
    : "Label protection is off. Required labels and the exit block can be edited.";
  status.hidden = !current || (scaffoldProtectionEnabled && !message);
  status.textContent = message || (scaffoldProtectionEnabled
    ? ""
    : "Label protection is off. Simulation requires one _start label and one valid End exit block.");
}

function validateSimulationScaffold(text) {
  const labels = {start: [], end: []};
  const statements = [];
  let startIsGlobal = false;
  const labelPattern = /^([A-Za-z_.$][\w.$]*)\s*:\s*/;

  String(text).split(/\r?\n/).forEach((rawLine, index) => {
    let code = rawLine.split("#", 1)[0].trim();
    if (/^\.(?:globl|global)\s+_start\b/i.test(code)) startIsGlobal = true;
    while (true) {
      const match = code.match(labelPattern);
      if (!match) break;
      const label = match[1];
      if (label === "_start") {
        labels.start.push({line: index + 1, nextStatement: statements.length});
      } else if (label.toLowerCase() === "end") {
        labels.end.push({line: index + 1, nextStatement: statements.length});
      }
      code = code.slice(match[0].length).trimStart();
    }
    if (code) statements.push(code);
  });

  const issues = [];
  const describeLabels = (name, found) => {
    if (!found.length) issues.push(`Missing required ${name}: label.`);
    else if (found.length > 1) {
      issues.push(`Found ${name}: on lines ${found.map(item => item.line).join(", ")}; keep only one.`);
    }
  };
  describeLabels("_start", labels.start);
  if (labels.start.length === 1 && !startIsGlobal) {
    issues.push("The _start label must be exported with .globl _start.");
  }
  describeLabels("End", labels.end);

  if (labels.end.length === 1) {
    const end = labels.end[0];
    const exit = statements.slice(end.nextStatement, end.nextStatement + 3);
    if (exit.length < 3
        || !/^li\s+(?:a0|x10)\s*,\s*0$/i.test(exit[0])
        || !/^li\s+(?:a7|x17)\s*,\s*93$/i.test(exit[1])
        || !/^ecall$/i.test(exit[2])) {
      issues.push(`End: on line ${end.line} must be followed by li a0, 0; li a7, 93; and ecall.`);
    }
  }
  return issues;
}

const INTEGER_REGISTER_ALIASES = {
  zero: "x0", ra: "x1", sp: "x2", gp: "x3", tp: "x4",
  t0: "x5", t1: "x6", t2: "x7", s0: "x8", fp: "x8", s1: "x9",
  a0: "x10", a1: "x11", a2: "x12", a3: "x13", a4: "x14", a5: "x15",
  a6: "x16", a7: "x17", s2: "x18", s3: "x19", s4: "x20", s5: "x21",
  s6: "x22", s7: "x23", s8: "x24", s9: "x25", s10: "x26", s11: "x27",
  t3: "x28", t4: "x29", t5: "x30", t6: "x31"
};
const FLOAT_REGISTER_ALIASES = {
  ft0: "f0", ft1: "f1", ft2: "f2", ft3: "f3", ft4: "f4", ft5: "f5",
  ft6: "f6", ft7: "f7", fs0: "f8", fs1: "f9", fa0: "f10", fa1: "f11",
  fa2: "f12", fa3: "f13", fa4: "f14", fa5: "f15", fa6: "f16", fa7: "f17",
  fs2: "f18", fs3: "f19", fs4: "f20", fs5: "f21", fs6: "f22", fs7: "f23",
  fs8: "f24", fs9: "f25", fs10: "f26", fs11: "f27", ft8: "f28", ft9: "f29",
  ft10: "f30", ft11: "f31"
};

function splitAssemblyOperands(text) {
  const operands = [];
  let token = "";
  let depth = 0;
  for (const character of String(text || "")) {
    if (character === "(") depth += 1;
    if (character === ")") depth = Math.max(0, depth - 1);
    if (character === "," && depth === 0) {
      operands.push(token.trim());
      token = "";
    } else token += character;
  }
  if (token.trim() || operands.length) operands.push(token.trim());
  return operands;
}

function parseAssemblyLine(rawLine, lineNumber) {
  const sourceCode = String(rawLine).split("#", 1)[0];
  let code = sourceCode.trim();
  let codeOffset = sourceCode.search(/\S/);
  const labels = [];
  const labelPattern = /^([A-Za-z_.$][\w.$]*)\s*:\s*/;
  while (code) {
    const label = code.match(labelPattern);
    if (!label) break;
    labels.push(label[1]);
    code = code.slice(label[0].length);
    codeOffset += label[0].length;
    const leadingWhitespace = code.length - code.trimStart().length;
    code = code.trimStart();
    codeOffset += leadingWhitespace;
  }
  if (!code || code.startsWith(".")) return {line: lineNumber, labels, instruction: null};
  const match = code.match(/^([A-Za-z][A-Za-z0-9_.]*)(?:\s+(.*))?$/);
  if (!match) return {line: lineNumber, labels, instruction: null, malformed: code};
  const operandsText = match[2] || "";
  return {
    line: lineNumber,
    labels,
    instruction: {
      mnemonic: match[1].toLowerCase(),
      operandsText,
      operandsOffset: operandsText ? codeOffset + match[0].length - operandsText.length : null
    }
  };
}

function canonicalRegister(token) {
  const value = String(token || "").trim().toLowerCase();
  if (INTEGER_REGISTER_ALIASES[value]) return INTEGER_REGISTER_ALIASES[value];
  if (FLOAT_REGISTER_ALIASES[value]) return FLOAT_REGISTER_ALIASES[value];
  const integer = value.match(/^x(\d+)$/);
  if (integer && Number(integer[1]) < 32) return `x${Number(integer[1])}`;
  const floating = value.match(/^f(\d+)$/);
  if (floating && Number(floating[1]) < 32) return `f${Number(floating[1])}`;
  return null;
}

function registerTokens(text) {
  const pattern = /\b(?:x\d+|f\d+|zero|ra|sp|gp|tp|fp|[ast]\d+|f[tsa]\d+)\b/gi;
  return [...String(text || "").matchAll(pattern)].map(match => ({token: match[0], index: match.index}));
}

function instructionTemplateOperandCount(entry) {
  const sample = String(entry.template || "").replace(/\{\d+\}/g, "x1");
  const operandText = sample.replace(/^[^\s]+\s*/, "");
  return operandText ? splitAssemblyOperands(operandText).length : 0;
}

function numericTokens(text) {
  return splitAssemblyOperands(text).flatMap(operand => operand
    .split(/[\s()]+/).map(token => token.trim()).filter(token =>
      /^[-+]?(?:0x[0-9a-f]+|0b[01]+|0o[0-7]+|\d+)$/i.test(token)));
}

function numericValue(token) {
  try {
    const value = String(token).trim();
    const sign = value.startsWith("-") ? -1 : 1;
    const unsigned = value.replace(/^[+-]/, "");
    return sign * Number(BigInt(unsigned));
  } catch (_error) {
    return null;
  }
}

function immediateConstraint(mnemonic, value) {
  if (value === null || mnemonic.startsWith("c.")) return null;
  if (/^(?:slli|srli|srai)$/.test(mnemonic)) return {min: 0, max: 31, label: "shift amount"};
  if (/^csr(?:rw|rs|rc)i$/.test(mnemonic)) return {min: 0, max: 31, label: "CSR immediate"};
  if (/^(?:beq|bne|blt|bge|bltu|bgeu)$/.test(mnemonic)) {
    return {min: -4096, max: 4094, alignment: 2, label: "branch offset"};
  }
  if (/^(?:jal)$/.test(mnemonic)) {
    return {min: -1048576, max: 1048574, alignment: 2, label: "jump offset"};
  }
  if (/^(?:lui|auipc)$/.test(mnemonic)) return {min: -524288, max: 1048575, label: "upper immediate"};
  if (/^(?:addi|slti|sltiu|xori|ori|andi|jalr|lb|lh|lw|lbu|lhu|sb|sh|sw|flw|fld|fsw|fsd)$/.test(mnemonic)) {
    return {min: -2048, max: 2047, label: "immediate"};
  }
  return null;
}

function validateProgramSource(text) {
  const sourceLines = String(text).split(/\r?\n/);
  const parsedLines = sourceLines.map((line, index) => parseAssemblyLine(line, index + 1));
  const labels = new Map();
  const errors = [];
  parsedLines.forEach(parsed => parsed.labels.forEach(label => {
    if (labels.has(label)) {
      errors.push({severity: "error", line: parsed.line, column: 1,
        message: `Duplicate label “${label}”.`, kind: "validation"});
    } else labels.set(label, parsed.line);
  }));

  parsedLines.forEach(parsed => {
    if (parsed.malformed) {
      errors.push({severity: "error", line: parsed.line, column: 1,
        message: "Could not parse this assembly statement.", kind: "validation"});
      return;
    }
    if (!parsed.instruction) return;
    const {mnemonic, operandsText} = parsed.instruction;
    const allMatches = RISCV_OPCODE_CATALOG.filter(entry => entry.mnemonic.toLowerCase() === mnemonic);
    const enabledMatches = activeOpcodeCandidates(mnemonic).filter(entry =>
      entry.mnemonic.toLowerCase() === mnemonic);
    if (!enabledMatches.length) {
      const reason = allMatches.length
        ? `Opcode “${mnemonic}” is not enabled by the active ISA.`
        : `Unknown opcode “${mnemonic}”.`;
      errors.push({severity: "error", line: parsed.line, column: 1, message: reason, kind: "validation"});
      return;
    }
    const entry = enabledMatches[0];
    const operands = splitAssemblyOperands(operandsText);
    const expected = instructionTemplateOperandCount(entry);
    const acceptsSymbolBaseForm = /^(?:l[bhwd]|fl[wd]|s[bhwd]|fs[wd])$/.test(mnemonic)
      && operands.length === 3 && canonicalRegister(operands[2]);
    if (operands.length !== expected && !acceptsSymbolBaseForm) {
      errors.push({severity: "error", line: parsed.line, column: 1,
        message: `${mnemonic} expects ${expected}${acceptsSymbolBaseForm ? " or 3" : ""} operand${expected === 1 ? "" : "s"}; found ${operands.length}.`,
        kind: "validation"});
    }

    registerTokens(operandsText).forEach(({token, index}) => {
      const canonical = canonicalRegister(token);
      if (canonical) return;
      errors.push({severity: "error", line: parsed.line,
        column: Math.max(1, parsed.instruction.operandsOffset + index + 1),
        message: `Invalid register “${token}”. Register numbers must be x0–x31 or f0–f31.`,
        kind: "validation"});
    });

    const numbers = numericTokens(operandsText);
    const range = immediateConstraint(mnemonic, numericValue(numbers[numbers.length - 1]));
    if (range && numbers.length) {
      const value = numericValue(numbers[numbers.length - 1]);
      if (value < range.min || value > range.max) {
        errors.push({severity: "error", line: parsed.line, column: 1,
          message: `${range.label} ${value} is outside ${range.min}…${range.max}.`, kind: "validation"});
      } else if (range.alignment && value % range.alignment !== 0) {
        errors.push({severity: "error", line: parsed.line, column: 1,
          message: `${range.label} must be aligned to ${range.alignment} bytes.`, kind: "validation"});
      }
    }

    if (entry.operands.some(operand => operand.kind === "label") && operands.length) {
      const target = operands[operands.length - 1].trim().replace(/[()]/g, "");
      const targetMatch = target.match(/^([A-Za-z_.$][\w.$]*)/);
      if (targetMatch && !canonicalRegister(targetMatch[1])
          && !/^[-+]?(?:0x[0-9a-f]+|0b[01]+|0o[0-7]+|\d+)$/i.test(targetMatch[1])
          && !labels.has(targetMatch[1])) {
        errors.push({severity: "error", line: parsed.line, column: 1,
          message: `Label “${targetMatch[1]}” is not defined in this source.`, kind: "validation"});
      }
    }
  });
  return errors;
}

function refreshScaffoldProtectionUi() {
  if (!current) {
    updateScaffoldProtectionUi();
    return;
  }
  const issues = validateSimulationScaffold($("#body").value);
  if (scaffoldProtectionEnabled && issues.length) {
    scaffoldProtectionEnabled = false;
    scaffoldEditAuthorized = true;
  }
  updateScaffoldProtectionUi(issues.length
    ? `Label protection is off because: ${issues.join(" ")}`
    : "");
}

function resetScaffoldProtection() {
  scaffoldProtectionEnabled = true;
  scaffoldEditAuthorized = false;
  refreshScaffoldProtectionUi();
}

$("#scaffold-protection").addEventListener("change", event => {
  if (!current) return;
  if (event.target.checked) {
    const issues = validateSimulationScaffold($("#body").value);
    if (issues.length) {
      scaffoldProtectionEnabled = false;
      scaffoldEditAuthorized = true;
      refreshScaffoldProtectionUi();
      return;
    }
    scaffoldProtectionEnabled = true;
  } else {
    scaffoldProtectionEnabled = false;
    scaffoldEditAuthorized = true;
  }
  refreshScaffoldProtectionUi();
  syncEditor();
});

function highlightAsmLine(line) {
  const commentAt = line.indexOf("#");
  let code = commentAt >= 0 ? line.slice(0, commentAt) : line;
  const comment = commentAt >= 0 ? line.slice(commentAt) : "";

  code = escapeHtml(code)
    .replace(/(^|\s)(\.[\w.]+)/g, '$1<span class="directive">$2</span>')
    .replace(/\b(x(?:[12]?\d|3[01]|0)|f(?:[12]?\d|3[01]|0)|zero|ra|sp|gp|tp|[ast]([0-9]|10|11)|ft(?:[0-9]|10|11)|fs(?:[0-9]|10|11)|fa[0-7])\b/g,
      '<span class="register">$&</span>')
    .replace(/\b(c[._][a-z][\w.]*|add|addi|sub|sll|slli|slt|slti|sltu|sltiu|xor|xori|srl|srli|sra|srai|or|ori|and|andi|lui|auipc|lb|lbu|lh|lhu|lw|lwu|ld|sb|sh|sw|sd|mul|mulh|mulhsu|mulhu|div|divu|rem|remu|flw|fld|fsw|fsd|f(?:add|sub|mul|div|min|max)\.[sd]|f(?:madd|msub|nmsub|nmadd)\.[sd]|fsqrt\.[sd]|fsgnj[nx]?\.[sd]|f(?:eq|lt|le|class)\.[sd]|fcvt\.(?:w|wu|l|lu|s|d)\.(?:w|wu|l|lu|s|d)|fmv\.(?:x\.w|w\.x)|(?:lr|sc|amo(?:swap|add|xor|and|or|min|max|minu|maxu))\.w|beqz|bnez|beq|bne|blt|bge|bltu|bgeu|jal|jalr|j|jr|ret|call|tail|li|la|mv|neg|not|seqz|snez|sltz|sgtz|csrrw|csrrs|csrrc|csrrwi|csrrsi|csrrci|csrr|csrw|csrs|csrc|csrwi|csrsi|csrci|rdcycle|rdtime|rdinstret|fence\.i|fence|nop|ecall|ebreak|wfi|mret)\b/gi,
      '<span class="opcode">$&</span>')
    .replace(/^(\s*)([\w.]+):/, '$1<span class="label">$2</span>:');

  return code + (comment ? `<span class="comment">${escapeHtml(comment)}</span>` : "");
}

function computeProtectedLines(text) {
  const lines = text.split("\n");
  const locked = new Set();
  if (!scaffoldProtectionEnabled) return locked;
  const code = lines.map(line => line.split("#", 1)[0].trim());
  const scaffoldComments = new Set([
    "# The text section contains the instructions that the CPU runs.",
    "# Make _start visible as the point where the program begins.",
    "# The End block stops the program and returns control to the simulator."
  ]);
  code.forEach((line, index) => {
    if (/^(?:\.section\s+\.text(?:\b|,)|\.text(?:\b|,))/.test(line)
        || /^\.glob(?:l|al)\s+_start\b/.test(line)
        || /^_start:\s*$/.test(line)
        || scaffoldComments.has(lines[index].trim())) locked.add(index);
  });
  const significant = code.map((line, index) => ({line, index})).filter(item => item.line);
  for (let position = significant.length - 1; position >= 2; position--) {
    if (significant[position].line !== "ecall") continue;
    const a7 = significant[position - 1].line;
    const a0 = significant[position - 2].line;
    if (/^li\s+(?:a7|x17)\s*,\s*93$/.test(a7) && /^li\s+(?:a0|x10)\s*,\s*0$/.test(a0)) {
      locked.add(significant[position].index);
      locked.add(significant[position - 1].index);
      locked.add(significant[position - 2].index);
      const preceding = significant[position - 3];
      if (preceding && /^End:\s*$/i.test(preceding.line)) locked.add(preceding.index);
      break;
    }
  }
  return locked;
}

function protectedRanges(text) {
  const ranges = [];
  const locked = computeProtectedLines(text);
  let offset = 0;
  text.split("\n").forEach((line, index) => {
    const end = offset + line.length;
    if (locked.has(index)) ranges.push({start: offset, end: end + 1});
    offset = end + 1;
  });
  return ranges;
}

function syncEditor() {
  const editorArea = document.querySelector(".editor-area");
  const savedScrollTop = editorArea?.scrollTop || 0;
  const savedScrollLeft = editorArea?.scrollLeft || 0;
  const text = $("#body").value;
  const lines = text.split("\n");
  const lockedLines = computeProtectedLines(text);
  $("#highlight").innerHTML = lines.map((line, index) => {
    const lineNumber = index + 1;
    const selected = lineNumber === selectedSourceLine ? " pipeline-selected" : "";
    const locked = lockedLines.has(index) ? " protected" : "";
    return `<span class="source-line${selected}${locked}" data-line="${lineNumber}">${highlightAsmLine(line) || "&nbsp;"}</span>`;
  }).join("");
  const query = $("#search")?.value.trim();
  if (query) {
    document.querySelectorAll("#highlight .source-line").forEach(sourceLine => {
      const walker = document.createTreeWalker(sourceLine, NodeFilter.SHOW_TEXT);
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach(node => {
        const lower = node.data.toLowerCase();
        const needle = query.toLowerCase();
        let cursor = 0;
        let match = lower.indexOf(needle);
        if (match < 0) return;
        const fragment = document.createDocumentFragment();
        while (match >= 0) {
          fragment.append(document.createTextNode(node.data.slice(cursor, match)));
          const mark = document.createElement("mark");
          mark.className = "search-hit";
          mark.textContent = node.data.slice(match, match + query.length);
          fragment.append(mark);
          cursor = match + query.length;
          match = lower.indexOf(needle, cursor);
        }
        fragment.append(document.createTextNode(node.data.slice(cursor)));
        node.replaceWith(fragment);
      });
    });
  }
  const lineHeight = parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue("--editor-line-height")) || 23;
  $("#body").style.height = Math.max(300, lines.length * lineHeight + 20) + "px";
  if (editorArea) {
    editorArea.scrollTop = savedScrollTop;
    editorArea.scrollLeft = savedScrollLeft;
  }
  if (opcodePopupState) positionOpcodePopup();
}

function scrollEditorToLine(lineNumber) {
  const editorArea = document.querySelector(".editor-area");
  const sourceLine = document.querySelector(`.source-line[data-line="${lineNumber}"]`);
  if (!editorArea || !sourceLine) return;
  const lineHeight = parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue("--editor-line-height")) || 23;
  editorArea.scrollTop = Math.max(0,
    sourceLine.offsetTop - (editorArea.clientHeight - lineHeight) / 2);
}

function highlightSourceLine(lineNumber) {
  selectedSourceLine = lineNumber || null;
  syncEditor();
  if (!selectedSourceLine) return;
  scrollEditorToLine(selectedSourceLine);
}

function clearSourceLineHighlight() {
  if (selectedSourceLine === null) return;
  selectedSourceLine = null;
  syncEditor();
}

async function loadProjects() {
  const data = await api("/api/projects");
  const heading = document.querySelector(".project-panel-header h2");
  if (heading) heading.title = `Programs folder: ${data.directory || "unknown"}`;
  $("#projects").innerHTML = data.projects.map(name =>
    `<button class="project ${name === current?.name ? "selected" : ""}" data-name="${escapeHtml(name)}">${escapeHtml(name)}</button>`
  ).join("");
  document.querySelectorAll(".project").forEach(button => {
    button.onclick = () => openProject(button.dataset.name);
  });
}

async function resolveUnsavedProject(action) {
  if (!current || !sourceDirty) return true;
  const choice = await actionDialog({
    title: "Unsaved changes",
    message: `Save changes to "${current.name}" before you ${action}?`,
    confirmLabel: "Save",
    confirmValue: "save",
    alternateLabel: "Discard",
    alternateValue: "discard",
    cancelLabel: "Cancel"
  });
  if (choice === "discard") {
    clearLocalDraft(current.name);
    return true;
  }
  if (choice !== "save") return false;
  try {
    await save();
    return true;
  } catch (error) {
    await showActionMessage("Save project", error.message);
    return false;
  }
}

async function openProject(name, {skipUnsavedCheck = false} = {}) {
  if (name === current?.name) return true;
  if (!skipUnsavedCheck && !await resolveUnsavedProject("open another project")) {
    await loadProjects();
    return false;
  }
  const encodedName = encodeURIComponent(name);
  const [opened, symbolData] = await Promise.all([
    api("/api/project?name=" + encodedName),
    api("/api/symbols?name=" + encodedName).catch(() => ({symbols: []}))
  ]);
  const localDraft = readLocalDraft(name);
  let recoveredDraft = null;
  if (localDraft && localDraft.text !== opened.text) {
    const timestamp = new Date(localDraft.timestamp).toLocaleString();
    const choice = await actionDialog({
      title: "Recover local draft?",
      message: `ASE Studio found a local draft for “${name}” saved ${timestamp}. Recover it instead of the saved source?`,
      confirmLabel: "Recover",
      confirmValue: "recover",
      alternateLabel: "Discard draft",
      alternateValue: "discard",
      cancelLabel: "Cancel"
    });
    if (choice === null) {
      await loadProjects();
      return false;
    }
    if (choice === "recover") recoveredDraft = localDraft;
    else clearLocalDraft(name);
  } else if (localDraft) {
    clearLocalDraft(name);
  }
  opened.dataSymbols = symbolData.symbols || [];
  opened.initialMemory = symbolData.initialMemory || {};
  opened.memoryMap = symbolData.memoryMap || [];
  current = opened;
  clearOpcodeSnippetSession();
  closeOpcodePopup();
  opcodePopupSuppressed = false;
  loadMemoryWatches();
  selectedSourceLine = null;
  selectedRow = null;
  selectedColumn = null;
  pipelineData = null;
  playbackCycle = null;
  stepMode = false;
  searchSourceLine = null;
  searchMatchIndex = -1;
  $("#project-title").textContent = current.name;
  $("#source-name").textContent = current.sourceName;
  $("#body").value = recoveredDraft ? recoveredDraft.text : current.text;
  resetScaffoldProtection();
  resetEditorHistory();
  savedSource = current.text;
  sourceDirty = Boolean(recoveredDraft);
  updateDirtyIndicator();
  setDraftStatus(recoveredDraft
    ? `Recovered draft · ${new Date(recoveredDraft.timestamp).toLocaleTimeString()}` : "");
  clearDiagnostics();
  $("#save").disabled = false;
  $("#duplicate").disabled = false;
  $("#open-with").disabled = false;
  $("#rename").disabled = false;
  $("#submit").disabled = false;
  $("#run").disabled = false;
  $("#configure").disabled = false;
  $("#delete").disabled = false;
  $("#reset").disabled = false;
  $("#export-pipeline").disabled = true;
  $("#expand-loops").disabled = false;
  $("#cycle-prev").disabled = true;
  $("#cycle-next").disabled = true;
  $("#cycle-position").textContent = "Cycle —/—";
  $("#step").disabled = false;
  $("#step").textContent = "Run Step";
  updateCycleNavigation();
  syncEditor();
  renderRegisters();
  renderMemory();
  await loadProjects();
  lastNormalLog = "";
  lastAdvancedLog = lastNormalLog;
  if (recoveredDraft) {
    lastNormalLog = `Recovered a local draft for ${current.sourceName}. Save it when you are ready.`;
    lastAdvancedLog = lastNormalLog;
  }
  updateLog();
  showTab("output");
  return true;
}

async function save() {
  if (!current) return;
  await api("/api/save", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name: current.name, text: $("#body").value,
      allowScaffoldEdit: scaffoldEditsAllowed()})
  });
  scaffoldEditAuthorized = false;
  savedSource = $("#body").value;
  sourceDirty = false;
  updateDirtyIndicator();
  clearLocalDraft(current.name);
  clearSourceLineHighlight();
  lastNormalLog = "Saved " + current.sourceName + ".";
  lastAdvancedLog = lastNormalLog;
  updateLog();
}

async function openWithChooser() {
  if (!current) return;
  try {
    if (sourceDirty) await save();
    const result = await api("/api/open-with", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: current.name})
    });
    lastNormalLog = result.output;
    lastAdvancedLog = lastNormalLog;
    updateLog();
  } catch (error) {
    await showActionMessage("Open with", error.message);
  }
}

async function renameCurrentProject() {
  if (!current) return;
  const oldName = current.name;
  const newName = await actionDialog({
    title: "Rename project",
    message: "Enter a new name for this project.",
    inputLabel: "Project name",
    inputValue: oldName,
    confirmLabel: "Rename",
    cancelLabel: "Cancel",
    trimInput: false
  });
  if (!newName || newName === oldName) return;
  try {
    const oldWatchKey = memoryWatchStorageKey(oldName);
    const savedWatches = oldWatchKey ? localStorage.getItem(oldWatchKey) : null;
    const savedDraft = readLocalDraft(oldName);
    const result = await api("/api/projects", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: oldName, newName})
    });
    current.name = result.name;
    if (savedWatches !== null) {
      localStorage.setItem(memoryWatchStorageKey(result.name), savedWatches);
      localStorage.removeItem(oldWatchKey);
    }
    if (savedDraft) {
      localStorage.setItem(draftStorageKey(result.name), JSON.stringify(savedDraft));
      localStorage.removeItem(draftStorageKey(oldName));
    }
    updateDirtyIndicator();
    await loadProjects();
    lastNormalLog = `Renamed project "${oldName}" to "${result.name}".`;
    lastAdvancedLog = lastNormalLog;
    updateLog();
  } catch (error) {
    await showActionMessage("Rename project", error.message);
  }
}

async function duplicateCurrentProject() {
  if (!current) return;
  const sourceName = current.name;
  const newName = await actionDialog({
    title: "Duplicate project",
    message: `Create a copy of "${sourceName}".`,
    inputLabel: "New project name",
    inputValue: `${sourceName} copy`,
    confirmLabel: "Duplicate",
    cancelLabel: "Cancel",
    trimInput: false
  });
  if (!newName) return;
  try {
    // The duplicate should include the source currently visible in Studio,
    // including edits that have not yet been written to disk.
    if (sourceDirty) await save();
    const result = await api("/api/projects/duplicate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: sourceName, newName})
    });
    await loadProjects();
    await openProject(result.name, {skipUnsavedCheck: true});
    lastNormalLog = `Duplicated project "${sourceName}" as "${result.name}".`;
    lastAdvancedLog = lastNormalLog;
    updateLog();
  } catch (error) {
    await showActionMessage("Duplicate project", error.message);
  }
}

async function synchronizeExternalSource() {
  if (!current || sourceDirty || externalSyncBusy) return;
  const projectName = current.name;
  externalSyncBusy = true;
  try {
    const disk = await api("/api/project?name=" + encodeURIComponent(projectName));
    if (!current || current.name !== projectName || sourceDirty || disk.text === savedSource) return;
    const editor = $("#body");
    const start = Math.min(editor.selectionStart, disk.text.length);
    const end = Math.min(editor.selectionEnd, disk.text.length);
    current = disk;
    editor.value = disk.text;
    editor.setSelectionRange(start, end);
    savedSource = disk.text;
    resetEditorHistory();
    pipelineData = null;
    playbackCycle = null;
    stepMode = false;
    $("#source-name").textContent = disk.sourceName;
    $("#export-pipeline").disabled = true;
    $("#cycle-prev").disabled = true;
    $("#cycle-next").disabled = true;
    $("#cycle-position").textContent = "Cycle —/—";
    updateDirtyIndicator();
    refreshScaffoldProtectionUi();
    syncEditor();
    renderRegisters();
    renderPipeline();
  } catch (_error) {
    // A project can briefly be unavailable while it is being renamed/deleted.
  } finally {
    externalSyncBusy = false;
  }
}

function formatSubmissionFileSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KiB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MiB";
}

function renderSubmissionFiles() {
  const files = [...($("#submission-files").files || [])];
  $("#submission-file-list").innerHTML = files.length
    ? files.map(file => '<div class="submission-file-entry"><strong>' + escapeHtml(file.name) + "</strong><span>" + formatSubmissionFileSize(file.size) + "</span></div>").join("")
    : "No additional files selected.";
}

function submissionAttachment(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Could not read " + file.name + "."));
    reader.onload = () => {
      const result = String(reader.result || "");
      const separator = result.indexOf(",");
      if (separator < 0) {
        reject(new Error("Could not encode " + file.name + "."));
        return;
      }
      resolve({
        name: file.name,
        mimeType: file.type || "application/octet-stream",
        contentBase64: result.slice(separator + 1)
      });
    };
    reader.readAsDataURL(file);
  });
}

const PORTAL_WEBSITE = "https://didattica.polito.it/";
let portalState = {loggedIn: false, username: "", remembered: false, ssoAvailable: false};
let portalLoginResolver = null;
let portalSsoUid = null;
let portalReviewResolver = null;

function jsonRequest(body) {
  return {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
}

function updatePortalAccount() {
  const status = portalState.loggedIn
    ? "Signed in as " + (portalState.username || "student") + "."
    : "Not signed in.";
  const action = portalState.loggedIn ? "Sign out" : "Sign in";
  $("#portal-account-status").textContent = status;
  $("#portal-account-button").textContent = action;
  $("#elaborati-account").textContent = status;
  $("#elaborati-account-button").textContent = action;
}

async function refreshPortalStatus() {
  try {
    portalState = await api("/api/portal/status");
  } catch (_error) {
    portalState = {loggedIn: false, username: "", remembered: false, ssoAvailable: false};
  }
  updatePortalAccount();
}

// Runs a portal request, asking the student to sign in again once if the
// session is missing or has expired.
async function portalCall(request) {
  try {
    return await request();
  } catch (error) {
    if (error.status !== 401) throw error;
    portalState.loggedIn = false;
    updatePortalAccount();
    if (!(await portalSignIn())) throw error;
    return await request();
  }
}

function setPortalLoginBusy(busy) {
  ["#portal-username", "#portal-password", "#portal-remember", "#portal-login-sso", "#portal-login-confirm"]
    .forEach(selector => { $(selector).disabled = busy; });
  $("#portal-login-confirm").textContent = busy && !portalSsoUid ? "Signing in…" : "Sign in";
}

function showPortalLoginMessage(message, isError = false) {
  const element = $("#portal-login-message");
  element.textContent = message || "";
  element.hidden = !message;
  element.classList.toggle("error", isError);
}

function portalSignIn() {
  if (portalLoginResolver) return Promise.resolve(false);
  $("#portal-username").value = portalState.username || "";
  $("#portal-password").value = "";
  $("#portal-remember").checked = localStorage.getItem("ase-studio-portal-remember") === "1";
  $("#portal-remember-note").textContent = portalState.keyring
    ? "(stored in the " + portalState.keyring + ")"
    : "(stored in a file only you can read)";
  $("#portal-login-sso").hidden = !portalState.ssoAvailable;
  showPortalLoginMessage("");
  setPortalLoginBusy(false);
  $("#portal-login-dialog").showModal();
  setTimeout(() => ($("#portal-username").value ? $("#portal-password") : $("#portal-username")).focus(), 0);
  return new Promise(resolve => { portalLoginResolver = resolve; });
}

function finishPortalLogin(signedIn) {
  if (portalSsoUid) {
    api("/api/portal/sso/cancel", jsonRequest({uid: portalSsoUid})).catch(() => {});
    portalSsoUid = null;
  }
  $("#portal-password").value = "";
  const dialog = $("#portal-login-dialog");
  if (dialog.open) dialog.close();
  const resolver = portalLoginResolver;
  portalLoginResolver = null;
  updatePortalAccount();
  if (resolver) resolver(signedIn);
}

async function submitPortalLogin() {
  const username = $("#portal-username").value.trim();
  const password = $("#portal-password").value;
  if (!username || !password) {
    showPortalLoginMessage("Enter your username and password.", true);
    return;
  }
  const remember = $("#portal-remember").checked;
  localStorage.setItem("ase-studio-portal-remember", remember ? "1" : "0");
  setPortalLoginBusy(true);
  showPortalLoginMessage("");
  try {
    portalState = await api("/api/portal/login", jsonRequest({username, password, remember}));
    finishPortalLogin(true);
  } catch (error) {
    setPortalLoginBusy(false);
    showPortalLoginMessage(error.message, true);
    $("#portal-password").select();
  }
}

async function startPortalSso() {
  const remember = $("#portal-remember").checked;
  localStorage.setItem("ase-studio-portal-remember", remember ? "1" : "0");
  let uid = null;
  try {
    uid = (await api("/api/portal/sso/start", jsonRequest({remember}))).uid;
    portalSsoUid = uid;
    setPortalLoginBusy(true);
    showPortalLoginMessage("Complete the sign-in in the PoliTO window.");
    while (portalSsoUid === uid) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      if (portalSsoUid !== uid) return;
      const state = await api("/api/portal/sso?uid=" + encodeURIComponent(uid));
      if (state.state === "pending" || state.state === "completing") continue;
      portalSsoUid = null;
      if (state.state === "done") {
        portalState = state;
        finishPortalLogin(true);
        return;
      }
      if (state.state === "error") throw Error(state.error || "The PoliTO sign-in failed.");
      setPortalLoginBusy(false);
      showPortalLoginMessage("The PoliTO sign-in was cancelled.");
    }
  } catch (error) {
    if (portalSsoUid && portalSsoUid !== uid) return;
    portalSsoUid = null;
    setPortalLoginBusy(false);
    showPortalLoginMessage(error.message, true);
  }
}

async function portalSignOut() {
  try {
    portalState = await api("/api/portal/logout", jsonRequest({}));
  } catch (error) {
    await showActionMessage("Portale della Didattica", error.message);
  }
  updatePortalAccount();
}

async function togglePortalAccount() {
  if (portalState.loggedIn) {
    await portalSignOut();
    return;
  }
  if (await portalSignIn()) showPortalCourse();
}

async function togglePortalUpload() {
  const enabled = $("#portal-upload").checked;
  $("#portal-upload-options").hidden = !enabled;
  localStorage.setItem("ase-studio-portal-upload", enabled ? "1" : "0");
  if (!enabled) return;
  if (portalState.loggedIn || await portalSignIn()) showPortalCourse();
}

function appendSubmissionLog(text) {
  lastNormalLog += "\n\n" + text;
  lastAdvancedLog += "\n\n" + text;
  updateLog();
}

function formatPortalDate(value) {
  const date = new Date(value);
  return value && !Number.isNaN(date.getTime()) ? date.toLocaleString() : (value || "—");
}

function saveSubmissionZip(archive) {
  const link = document.createElement("a");
  link.href = "/api/submission/download?archive=" + encodeURIComponent(archive);
  link.download = "";
  document.body.append(link);
  link.click();
  link.remove();
}

async function offerManualUpload(archive, reason) {
  saveSubmissionZip(archive);
  appendSubmissionLog("Saving the ZIP for manual upload. Upload it on the Portale della Didattica: "
    + "open the course, then Elaborati.");
  const choice = await actionDialog({
    title: "Save ZIP for manual upload",
    message: (reason ? reason + "\n\n" : "")
      + "Choose where to save the ZIP, then upload it yourself on the Portale della Didattica: open the course page and use the Elaborati section.",
    confirmLabel: "Open Portale", confirmValue: "portal",
    cancelLabel: "Close"
  });
  if (choice === "portal") window.open(PORTAL_WEBSITE, "_blank", "noopener");
}

function portalCourseLabel(course) {
  return course.name + (course.shortcode ? " · " + course.shortcode : "");
}

// Shows the student's ASE course (Italian or English edition) once known.
async function showPortalCourse() {
  try {
    const label = portalCourseLabel((await api("/api/portal/course")).course);
    $("#portal-course").textContent = label;
    $("#elaborati-course").textContent = label;
  } catch (_error) {
    // The upload reports a missing course or session with a clear message.
  }
}

function reviewPortalUpload(preview) {
  $("#portal-review-course").textContent = portalCourseLabel(preview.course);
  $("#portal-review-file").textContent = preview.filename;
  $("#portal-review-size").textContent = formatSubmissionFileSize(preview.size);
  $("#portal-review-description").value = preview.description;
  $("#portal-review-contents-title").textContent = "Contents · " + preview.entries.length + " file(s)";
  $("#portal-review-entries").innerHTML = preview.entries.map(entry =>
    '<div class="submission-file-entry"><strong title="' + escapeHtml(entry.path) + '">' + escapeHtml(entry.path)
    + "</strong><span>" + formatSubmissionFileSize(entry.size) + "</span></div>"
  ).join("");
  $("#portal-review-existing").hidden = !preview.existing.length;
  $("#portal-review-existing-list").innerHTML = preview.existing.map(entry =>
    '<div class="submission-file-entry"><strong>' + escapeHtml(entry.filename) + "</strong><span>"
    + escapeHtml(formatPortalDate(entry.uploadedAt)) + " · " + entry.sizeInKiloBytes + " KiB</span></div>"
  ).join("");
  document.querySelector('[name="portal-review-replace"][value="replace"]').checked = true;
  $("#portal-review-dialog").showModal();
  $("#portal-review-confirm").focus();
  return new Promise(resolve => { portalReviewResolver = resolve; });
}

function finishPortalReview(decision) {
  const dialog = $("#portal-review-dialog");
  if (dialog.open) dialog.close();
  const resolver = portalReviewResolver;
  portalReviewResolver = null;
  if (resolver) resolver(decision);
}

// Returns true when the ZIP was handed off (uploaded or saved for manual
// upload), false when it stays only in the submission folder.
async function uploadSubmissionToPortal(archive) {
  let preview;
  try {
    preview = await portalCall(() => api("/api/portal/preview", jsonRequest({archive})));
  } catch (error) {
    appendSubmissionLog("Upload failed: " + error.message);
    await offerManualUpload(archive, "The upload could not be prepared: " + error.message);
    return true;
  }
  const decision = await reviewPortalUpload(preview);
  if (decision.action === "manual") {
    await offerManualUpload(archive);
    return true;
  }
  if (decision.action !== "upload") {
    appendSubmissionLog("Upload cancelled. The ZIP is saved in the submission folder only.");
    return false;
  }
  appendSubmissionLog("Uploading " + preview.filename + " to the Elaborati of " + portalCourseLabel(preview.course) + "…");
  $("#submit").textContent = "Uploading…";
  let replace = preview.existing.length ? decision.replace : null;
  while (true) {
    try {
      const upload = await portalCall(() => api("/api/portal/upload", jsonRequest({
        archive, description: decision.description, replace
      })));
      if (upload.conflict) {
        // Someone uploaded the same file since the review; ask again.
        const choice = await actionDialog({
          title: "Elaborato already uploaded",
          message: "The course's Elaborati now already contain " + upload.existing[0].filename + ".",
          confirmLabel: "Replace", confirmValue: "replace",
          alternateLabel: "Keep both", alternateValue: "keep",
          cancelLabel: "Don't upload"
        });
        if (choice !== "replace" && choice !== "keep") {
          appendSubmissionLog("Upload cancelled. The ZIP is saved in the submission folder only.");
          return false;
        }
        replace = choice === "replace";
        continue;
      }
      const lines = [];
      if (upload.assignment) {
        lines.push("Uploaded to Elaborati: " + upload.assignment.filename + " ("
          + upload.assignment.sizeInKiloBytes + " KiB, " + formatPortalDate(upload.assignment.uploadedAt) + ").");
      } else {
        lines.push("Uploaded " + upload.filename + " to Elaborati.");
      }
      if (upload.withdrawn.length) lines.push("Withdrew " + upload.withdrawn.length + " previous copy(ies).");
      upload.warnings.forEach(warning => lines.push("Warning: " + warning));
      appendSubmissionLog(lines.join("\n"));
      return true;
    } catch (error) {
      appendSubmissionLog("Upload failed: " + error.message);
      await offerManualUpload(archive, "The upload failed: " + error.message);
      return true;
    }
  }
}

function renderElaboratiMessage(message) {
  $("#elaborati-list").innerHTML = '<p class="elaborati-empty">' + escapeHtml(message) + "</p>";
}

function renderElaborati(assignments) {
  if (!assignments.length) {
    renderElaboratiMessage("No Elaborati uploaded for this course.");
    return;
  }
  $("#elaborati-list").innerHTML = assignments.map(entry => {
    const withdrawn = Boolean(entry.deletedAt);
    const name = entry.url
      ? '<a href="' + escapeHtml(entry.url) + '" target="_blank" rel="noreferrer" title="Download from the Portale">' + escapeHtml(entry.filename) + "</a>"
      : escapeHtml(entry.filename);
    return '<div class="elaborato' + (withdrawn ? " withdrawn" : "") + '">'
      + '<div class="elaborato-main"><strong>' + name + "</strong>"
      + "<small>" + escapeHtml(entry.description || "") + "</small></div>"
      + '<div class="elaborato-meta"><span>' + escapeHtml(formatPortalDate(entry.uploadedAt)) + "</span>"
      + "<span>" + entry.sizeInKiloBytes + " KiB</span>"
      + '<span class="elaborato-status">' + (withdrawn ? "Withdrawn " + escapeHtml(formatPortalDate(entry.deletedAt)) : "Active") + "</span></div>"
      + '<button type="button" data-elaborato="' + entry.id + '" data-filename="' + escapeHtml(entry.filename) + '" data-withdraw="' + (!withdrawn) + '"'
      + (withdrawn ? ">Restore" : ' class="danger">Withdraw') + "</button></div>";
  }).join("");
}

async function loadElaborati() {
  $("#elaborati-refresh").disabled = !portalState.loggedIn;
  if (!portalState.loggedIn) {
    renderElaboratiMessage("Sign in to see your Elaborati.");
    return;
  }
  renderElaboratiMessage("Loading Elaborati…");
  try {
    const data = await portalCall(() => api("/api/portal/assignments"));
    $("#elaborati-course").textContent = portalCourseLabel(data.course);
    renderElaborati(data.assignments);
  } catch (error) {
    await refreshPortalStatus();
    $("#elaborati-refresh").disabled = !portalState.loggedIn;
    renderElaboratiMessage(error.message);
  }
}

async function changeElaborato(button) {
  const assignmentId = Number(button.dataset.elaborato);
  const withdrawn = button.dataset.withdraw === "true";
  if (withdrawn) {
    const confirmed = await actionDialog({
      title: "Withdraw Elaborato",
      message: "Withdraw " + button.dataset.filename + " from the course's Elaborati? You can restore it later.",
      confirmLabel: "Withdraw", cancelLabel: "Cancel", danger: true
    });
    if (confirmed !== true) return;
  }
  button.disabled = true;
  try {
    const data = await portalCall(() => api("/api/portal/assignment",
      jsonRequest({assignmentId, withdrawn})));
    renderElaborati(data.assignments);
  } catch (error) {
    button.disabled = false;
    await showActionMessage("Elaborati", error.message);
  }
}

async function openElaborati() {
  await refreshPortalStatus();
  renderElaboratiMessage(portalState.loggedIn ? "Loading Elaborati…" : "Sign in to see your Elaborati.");
  $("#elaborati-refresh").disabled = true;
  $("#elaborati-dialog").showModal();
  if (!portalState.loggedIn && !(await portalSignIn())) return;
  await loadElaborati();
}

async function toggleElaboratiAccount() {
  if (portalState.loggedIn) {
    await portalSignOut();
    $("#elaborati-refresh").disabled = true;
    renderElaboratiMessage("Sign in to see your Elaborati.");
    return;
  }
  if (await portalSignIn()) await loadElaborati();
}

async function submitAssignment() {
  if (!current) return;
  try {
    const data = await api("/api/projects");
    $("#submission-projects").innerHTML = data.projects.map(name =>
      '<label class="submission-project"><input type="checkbox" data-submission-project value="' + escapeHtml(name) + '" ' + (name === current.name ? "checked" : "") + '><span title="' + escapeHtml(name) + '">' + escapeHtml(name) + "</span></label>"
    ).join("");
    $("#submission-assignment").value = "";
    $("#submission-files").value = "";
    $("#submission-expand-loops").checked = $("#expand-loops").checked;
    renderSubmissionFiles();
    const portalUpload = localStorage.getItem("ase-studio-portal-upload") === "1";
    $("#portal-upload").checked = portalUpload;
    $("#portal-upload-options").hidden = !portalUpload;
    await refreshPortalStatus();
    $("#submission-dialog").showModal();
    $("#submission-assignment").focus();
    if (portalState.loggedIn) showPortalCourse();
  } catch (error) {
    await showActionMessage("Prepare submission", error.message);
  }
}

async function prepareSubmission(mode = "submit") {
  if (!current) return;
  const manualUpload = mode === "manual";
  const assignment = $("#submission-assignment").value.trim();
  if (!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(assignment)) {
    await showActionMessage("Invalid assignment name",
      "Use a name beginning with a letter and containing only letters, digits, '_' or '-'.");
    return;
  }
  const selectedNames = [...document.querySelectorAll("[data-submission-project]:checked")]
    .map(input => input.value);
  if (!selectedNames.length) {
    await showActionMessage("Prepare submission", "Select at least one project.");
    return;
  }
  if (selectedNames.includes(current.name)) {
    const scaffoldIssues = validateSimulationScaffold($("#body").value);
    if (scaffoldIssues.length) {
      const hint = scaffoldProtectionEnabled
        ? "Turn off Protect labels to edit protected lines."
        : "Correct the required labels and exit block, then try again.";
      await showActionMessage("Cannot prepare submission",
        `${scaffoldIssues.join("\n")}\n\n${hint}`);
      return;
    }
  }
  const files = [...($("#submission-files").files || [])];
  if (files.length > 20) {
    await showActionMessage("Prepare submission", "Choose no more than 20 additional files.");
    return;
  }
  const tooLarge = files.find(file => file.size > 25 * 1024 * 1024);
  if (tooLarge) {
    await showActionMessage("Prepare submission", tooLarge.name + " exceeds the 25 MiB limit.");
    return;
  }
  const totalSize = files.reduce((total, file) => total + file.size, 0);
  if (totalSize > 100 * 1024 * 1024) {
    await showActionMessage("Prepare submission", "Additional files exceed the combined 100 MiB limit.");
    return;
  }
  const portalUpload = !manualUpload && $("#portal-upload").checked;
  if (portalUpload && !portalState.loggedIn && !(await portalSignIn())) return;

  const confirmButton = $("#submission-confirm");
  const cancelButton = $("#cancel-submission");
  confirmButton.disabled = true;
  cancelButton.disabled = true;
  confirmButton.textContent = "Reading files…";
  let attachments;
  try {
    attachments = await Promise.all(files.map(submissionAttachment));
  } catch (error) {
    confirmButton.disabled = false;
    cancelButton.disabled = false;
    confirmButton.textContent = "Create submission";
    await showActionMessage("Prepare submission", error.message);
    return;
  }

  const submittedProjectName = current.name;
  const submittedSource = $("#body").value;
  const currentWasSelected = selectedNames.includes(submittedProjectName);
  const projects = selectedNames.map(name => ({
    name,
    source: name === submittedProjectName ? submittedSource : null,
    allowScaffoldEdit: name === submittedProjectName && scaffoldEditsAllowed()
  }));
  $("#submission-dialog").close();
  confirmButton.disabled = false;
  cancelButton.disabled = false;
  confirmButton.textContent = "Create submission";

  const controls = ["#submit", "#run", "#step", "#reset", "#configure"];
  controls.forEach(selector => { $(selector).disabled = true; });
  $("#submit").textContent = "Submitting…";
  lastNormalLog = "Building and simulating " + projects.length + " selected project(s)…";
  lastAdvancedLog = lastNormalLog;
  updateLog();
  showTab("output");
  try {
    const result = await api("/api/submit", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        assignment,
        projects,
        attachments,
        expandLoops: $("#submission-expand-loops").checked
      })
    });
    if (currentWasSelected && current?.name === submittedProjectName) {
      scaffoldEditAuthorized = false;
      savedSource = submittedSource;
      sourceDirty = $("#body").value !== submittedSource;
      updateDirtyIndicator();
    }
    lastNormalLog = result.output;
    lastAdvancedLog = result.advancedOutput || result.output;
    if (currentWasSelected && current?.name === submittedProjectName) {
      const diagnostics = parseCompilerDiagnostics(lastAdvancedLog);
      setDiagnostics(diagnostics, diagnostics.some(item => item.severity === "error")
        ? "Compiler diagnostics" : "Compiler messages");
      if (!sourceDirty) clearLocalDraft(current.name);
    }
    updateLog();
    let uploaded = false;
    if (result.ok && result.archive && manualUpload) {
      await offerManualUpload(result.archive);
      uploaded = true;
    } else if (result.ok && result.archive && portalUpload) {
      uploaded = await uploadSubmissionToPortal(result.archive);
    }
    if (result.ok && result.archive && !uploaded) {
      try {
        await api("/api/submission/reveal", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({archive: result.archive})
        });
      } catch (revealError) {
        const warning = "The submission was created, but its folder could not be opened: " + revealError.message;
        lastNormalLog += "\n\n" + warning;
        lastAdvancedLog += "\n\n" + warning;
        updateLog();
      }
    }
    if (result.ok && currentWasSelected && current?.name === submittedProjectName) {
      const trace = await api("/api/pipeline?name=" + encodeURIComponent(current.name));
      appendSimulationReports(trace);
      current.dataSymbols = trace.dataSymbols || [];
      current.initialMemory = trace.initialMemory || {};
      current.memoryMap = trace.memoryMap || [];
      if (trace.tooLarge) {
        pipelineData = null;
        playbackCycle = null;
        stepMode = false;
        $("#step").textContent = "Run Step";
        const notice = "Pipeline not displayed: " + trace.cycles + " cycles exceeds the " + trace.limit + "-cycle limit.\nExecuted instructions: " + trace.instructions + " · Stalls: " + trace.stalls + " · CPI: " + trace.cpi + "\nFull pipeline CSV: " + trace.csvPath;
        lastNormalLog += "\n\n" + notice;
        lastAdvancedLog += "\n\n" + notice;
        updateLog();
        renderPipeline();
        showTab("output");
      } else {
        pipelineData = trace;
        playbackCycle = pipelineData.cycles;
        selectedColumn = null;
        stepMode = false;
        $("#step").textContent = "Run Step";
        $("#export-pipeline").disabled = false;
        $("#expand-loops").disabled = false;
        renderPipeline();
      }
    }
  } catch (error) {
    lastNormalLog = "Submission failed\n" + error.message;
    lastAdvancedLog = lastNormalLog;
    updateLog();
  } finally {
    controls.forEach(selector => { $(selector).disabled = false; });
    $("#submit").textContent = "Submit";
    $("#export-pipeline").disabled = !pipelineData;
    $("#expand-loops").disabled = false;
  }
}

function renderLogOutput(log) {
  const output = $("#output");
  if (!output) return;
  const text = String(log || "");
  const diagnostics = parseCompilerDiagnostics(text);
  const byLine = new Map();
  diagnostics.filter(entry => entry.outputLine).forEach(entry => {
    const entries = byLine.get(entry.outputLine) || [];
    entries.push(entry);
    byLine.set(entry.outputLine, entries);
  });
  const preferredDiagnostic = entries => {
    const located = entries.filter(entry => entry.line);
    const candidates = located.length ? located : entries;
    const rank = {error: 0, warning: 1, note: 2};
    return candidates.reduce((preferred, entry) => {
      if (!preferred || (rank[entry.severity] ?? 3) < (rank[preferred.severity] ?? 3)) return entry;
      return preferred;
    }, null);
  };
  output.innerHTML = text.split(/\r?\n/).map((line, index) => {
    const lineDiagnostics = byLine.get(index + 1) || [];
    const diagnostic = preferredDiagnostic(lineDiagnostics);
    const content = escapeHtml(line) || "&#8203;";
    if (diagnostic?.line) {
      const count = lineDiagnostics.length > 1
        ? ` (${lineDiagnostics.length} diagnostics on this output line)` : "";
      return `<button type="button" class="log-diagnostic ${escapeHtml(diagnostic.severity || "error")}" data-log-line="${index + 1}" title="Jump to ${escapeHtml(diagnostic.file || current?.sourceName || "source")} line ${diagnostic.line}${count}">${content}</button>`;
    }
    return `<span class="log-line">${content}</span>`;
  }).join("");
  output.querySelectorAll("[data-log-line]").forEach(button => {
    button.onclick = () => {
      const entry = preferredDiagnostic(byLine.get(Number(button.dataset.logLine)) || []);
      if (entry?.line) jumpToSourcePosition(entry.line, entry.column);
    };
  });
}

function updateLog() {
  renderLogOutput($("#advanced-logs").checked ? lastAdvancedLog : lastNormalLog);
}

function showTab(tab) {
  document.querySelectorAll("#bottom nav button").forEach(button => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });
  $("#output-pane").hidden = tab !== "output";
  $("#pipeline").hidden = tab !== "pipeline";
  $("#memory").hidden = tab !== "memory";
  if (tab === "memory") renderMemory();
}

function resetPipelineViewport() {
  const scroll = $("#pipeline-scroll");
  const reset = () => {
    scroll.scrollTop = 0;
    scroll.scrollLeft = 0;
  };
  reset();
  requestAnimationFrame(reset);
}

async function run(stepAfterRun = false) {
  if (!current) return;
  const source = $("#body").value;
  const sourceIssues = validateProgramSource(source);
  const scaffoldIssues = validateSimulationScaffold(source);
  const preRunIssues = [
    ...sourceIssues,
    ...scaffoldIssues.map(message => ({severity: "error", message, kind: "scaffold"}))
  ];
  clearDiagnostics();
  clearSourceLineHighlight();
  if (!stepAfterRun) {
    stepMode = false;
    $("#step").textContent = "Run Step";
  }
  $("#run").disabled = true;
  $("#submit").disabled = true;
  $("#reset").disabled = true;
  $("#export-pipeline").disabled = true;
  $("#expand-loops").disabled = true;
  $("#step").disabled = true;
  $("#cycle-jump").disabled = true;
  $("#cycle-jump-form button").disabled = true;
  $("#run").textContent = "Running…";
  lastNormalLog = "Building and simulating…";
  lastAdvancedLog = lastNormalLog;
  updateLog();
  showTab("output");
  try {
    const result = await api("/api/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: current.name, text: source,
        allowScaffoldEdit: scaffoldEditsAllowed()})
    });
    scaffoldEditAuthorized = false;
    savedSource = $("#body").value;
    sourceDirty = false;
    updateDirtyIndicator();
    clearLocalDraft(current.name);
    lastNormalLog = result.output;
    lastAdvancedLog = result.advancedOutput || result.output;
    const compilerDiagnostics = parseCompilerDiagnostics(lastAdvancedLog);
    if (compilerDiagnostics.length) {
      setDiagnostics(compilerDiagnostics, compilerDiagnostics.some(item => item.severity === "error")
        ? "Compiler diagnostics" : "Compiler messages");
    } else if (!result.ok && preRunIssues.length) {
      setDiagnostics(preRunIssues.map(issue => ({...issue, severity: "warning"})), "Pre-run hints");
    } else {
      clearDiagnostics();
    }
    updateLog();
    if (result.ok) {
      const trace = await api("/api/pipeline?name=" + encodeURIComponent(current.name));
      appendSimulationReports(trace);
      current.dataSymbols = trace.dataSymbols || [];
      current.initialMemory = trace.initialMemory || {};
      current.memoryMap = trace.memoryMap || [];
      if (trace.tooLarge) {
        pipelineData = null;
        playbackCycle = null;
        stepMode = false;
        $("#step").textContent = "Run Step";
        const notice = `Pipeline not displayed: ${trace.cycles} cycles exceeds the ${trace.limit}-cycle limit.\nExecuted instructions: ${trace.instructions} · Stalls: ${trace.stalls} · CPI: ${trace.cpi}\nFull pipeline CSV: ${trace.csvPath}`;
        lastNormalLog += `\n\n${notice}`;
        lastAdvancedLog += `\n\n${notice}`;
        updateLog();
        renderPipeline();
        showTab("output");
      } else {
        pipelineData = trace;
        playbackCycle = stepAfterRun ? 1 : pipelineData.cycles;
        selectedRow = null;
        selectedColumn = stepAfterRun ? 1 : null;
        stepMode = stepAfterRun;
        $("#export-pipeline").disabled = false;
        $("#expand-loops").disabled = false;
        $("#step").textContent = stepAfterRun ? "Next Cycle" : "Run Step";
        resetPipelineViewport();
        renderPipeline();
        resetPipelineViewport();
        showTab("pipeline");
      }
    }
  } catch (error) {
    lastNormalLog = "Request failed\n" + error.message;
    lastAdvancedLog = lastNormalLog;
    updateLog();
  } finally {
    $("#run").disabled = false;
    $("#submit").disabled = false;
    $("#reset").disabled = false;
    $("#export-pipeline").disabled = !pipelineData;
    $("#expand-loops").disabled = false;
    $("#step").disabled = false;
    $("#run").textContent = "Run ▶";
    updateCycleNavigation();
  }
}

function resetSimulation() {
  if (!current) return;
  pipelineData = null;
  selectedRow = null;
  selectedColumn = null;
  selectedSourceLine = null;
  playbackCycle = null;
  stepMode = false;
  $("#step").textContent = "Run Step";
  $("#export-pipeline").disabled = true;
  $("#expand-loops").disabled = false;
  $("#cycle-prev").disabled = true;
  $("#cycle-next").disabled = true;
  $("#cycle-position").textContent = "Cycle —/—";
  updateCycleNavigation();
  $("#pipeline-grid").innerHTML = "";
  $("#pipeline-diagram").innerHTML = "";
  $("#pipeline-summary").textContent = "No pipeline trace loaded. Press Run or Run Step.";
  $("#detail").textContent = "Hover a pipeline cell to inspect it.";
  resetPipelineViewport();
  renderRegisters();
  renderMemory();
  syncEditor();
  showTab("pipeline");
}

function formatWordValue(value, format, width = 32) {
  let bits;
  try {
    bits = BigInt.asUintN(width, BigInt(value || 0));
  } catch (_error) {
    bits = 0n;
  }
  switch (format) {
    case "binary":
      return "0b" + bits.toString(2).padStart(width, "0");
    case "signed":
      return BigInt.asIntN(width, bits).toString();
    case "unsigned":
      return bits.toString();
    case "float": {
      const singlePrecision = width === 32;
      const floatBits = singlePrecision ? BigInt.asUintN(32, bits) : bits;
      const buffer = new ArrayBuffer(singlePrecision ? 4 : 8);
      const view = new DataView(buffer);
      if (singlePrecision) view.setUint32(0, Number(floatBits), false);
      else view.setBigUint64(0, floatBits, false);
      const number = singlePrecision ? view.getFloat32(0, false) : view.getFloat64(0, false);
      if (Number.isNaN(number)) return "NaN";
      if (!Number.isFinite(number)) return number < 0 ? "-Infinity" : "Infinity";
      if (Object.is(number, -0)) return "-0";
      return Number(number.toPrecision(8)).toString();
    }
    default:
      return "0x" + bits.toString(16).padStart(width / 4, "0");
  }
}

function renderRegisters() {
  const integerContainer = $("#integer-registers");
  const floatContainer = $("#float-registers");
  if (!pipelineData || playbackCycle === null) {
    integerContainer.innerHTML = "";
    floatContainer.innerHTML = "";
    $("#pipeline-pc").textContent = "PC —";
    $("#pipeline-pc").classList.remove("changed");
    return;
  }
  const deltas = pipelineData.registerDeltas || {};
  const pcDeltas = pipelineData.pcDeltas || {};
  const state = {...(pipelineData.initialRegisters || {})};
  const changed = new Set();
  let pc = "—";
  let pcChanged = false;
  for (let cycle = 1; cycle <= playbackCycle; cycle++) {
    const update = deltas[String(cycle)];
    if (update) {
      if (cycle === playbackCycle) Object.keys(update).forEach(register => changed.add(register));
      Object.assign(state, update);
    }
    if (pcDeltas[String(cycle)]) {
      pc = pcDeltas[String(cycle)];
      pcChanged = cycle === playbackCycle;
    }
  }
  const integerAbi = ["zero", "ra", "sp", "gp", "tp", "t0", "t1", "t2", "s0/fp", "s1", "a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s11", "t3", "t4", "t5", "t6"];
  const floatAbi = ["ft0", "ft1", "ft2", "ft3", "ft4", "ft5", "ft6", "ft7", "fs0", "fs1", "fa0", "fa1", "fa2", "fa3", "fa4", "fa5", "fa6", "fa7", "fs2", "fs3", "fs4", "fs5", "fs6", "fs7", "fs8", "fs9", "fs10", "fs11", "ft8", "ft9", "ft10", "ft11"];
  const integerRoles = ["constant zero", "return address", "stack pointer", "global pointer", "thread pointer", "temporary", "temporary", "temporary", "saved register / frame pointer", "saved register", "argument / return value", "argument / return value", "argument / return value", "argument / return value", "argument / return value", "argument / return value", "argument / return value", "argument / return value", "saved register", "saved register", "saved register", "saved register", "saved register", "saved register", "saved register", "saved register", "saved register", "saved register", "temporary", "temporary", "temporary", "temporary"];
  const floatRoles = ["temporary FP register", "temporary FP register", "temporary FP register", "temporary FP register", "temporary FP register", "temporary FP register", "temporary FP register", "temporary FP register", "saved FP register", "saved FP register", "FP argument / return value", "FP argument / return value", "FP argument / return value", "FP argument / return value", "FP argument / return value", "FP argument / return value", "FP argument / return value", "FP argument / return value", "saved FP register", "saved FP register", "saved FP register", "saved FP register", "saved FP register", "saved FP register", "saved FP register", "saved FP register", "saved FP register", "saved FP register", "temporary FP register", "temporary FP register", "temporary FP register", "temporary FP register"];
  const renderColumn = (prefix, aliases, roles, format, width) => aliases.map((alias, index) => {
    const name = `${prefix}${index}`;
    const label = `${name} / ${alias.replace("/", " / ")} — ${roles[index] || "register"}`;
    return `<div class="register-value${changed.has(name) ? " changed" : ""}" data-register-change="${name}" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}"><strong>${name}</strong><span>${escapeHtml(alias)}</span><code>${formatWordValue(state[name] || 0, format, width)}</code></div>`;
  }).join("");
  $("#pipeline-pc").textContent = `PC ${pc}`;
  $("#pipeline-pc").classList.toggle("changed", pcChanged);
  const floatWidth = pipelineData.configuration?.floatingPointPrecision === "double" ? 64 : 32;
  integerContainer.innerHTML = renderColumn("x", integerAbi, integerRoles, $("#integer-register-format").value, 32);
  floatContainer.innerHTML = renderColumn("f", floatAbi, floatRoles, $("#float-register-format").value, floatWidth);
}

async function runStep() {
  if (!current) return;
  if (!stepMode || !pipelineData || playbackCycle >= pipelineData.cycles) {
    await run(true);
    return;
  }
  playbackCycle += 1;
  selectedColumn = playbackCycle;
  $("#step").textContent = playbackCycle >= pipelineData.cycles ? "Restart Step" : "Next Cycle";
  renderPipeline();
  showTab("pipeline");
}

function movePlaybackCycle(delta) {
  if (!stepMode || !pipelineData || playbackCycle === null) return;
  playbackCycle = Math.max(1, Math.min(pipelineData.cycles, playbackCycle + delta));
  selectedColumn = playbackCycle;
  $("#step").textContent = playbackCycle >= pipelineData.cycles ? "Restart Step" : "Next Cycle";
  renderPipeline();
  showTab("pipeline");
}

function positionPipelineCycle(cycle) {
  const scroll = $("#pipeline-scroll");
  const grid = $("#pipeline-grid");
  if (!pipelineData) return;
  const frozenWidth = Number.parseFloat(
    getComputedStyle(document.documentElement).getPropertyValue("--address-width"))
    + Number.parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue("--instruction-width"))
    + Number.parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue("--flow-width"));
  const cellWidth = Number($("#cell-width").value);
  const displayCycles = pipelineDisplayCycles();
  const cycleIndex = displayCycles.findIndex(value => value >= cycle);
  // The three frozen columns occupy positions 1–3. Leave one preceding
  // cycle visible so the requested cycle appears at table column 5.
  const pipelineWidth = frozenWidth + displayCycles.length * cellWidth;
  const horizontalTail = Math.max(
    0, scroll.clientWidth - frozenWidth - 2 * cellWidth);
  grid.style.width = `${pipelineWidth + horizontalTail}px`;
  scroll.scrollLeft = Math.max(
    0, (cycleIndex - 1) * cellWidth);

  const eventRow = displayedPipelineRows().findIndex(row =>
    row.cycles?.[String(cycle)] !== undefined);
  if (eventRow < 0) return;
  const rowHeight = Number.parseFloat(
    getComputedStyle(document.documentElement).getPropertyValue("--row-height"));
  // The sticky header is row 1. Preserve one preceding instruction when
  // possible so the first event for this cycle appears at table row 3.
  const rows = grid.querySelector(".pipeline-rows");
  const verticalTail = Math.max(0, scroll.clientHeight - 3 * rowHeight);
  rows.style.height = `${displayedPipelineRows().length * rowHeight + verticalTail}px`;
  scroll.scrollTop = Math.max(0, (eventRow - 1) * rowHeight);
}

async function jumpToCycle() {
  if (!stepMode || !pipelineData || playbackCycle === null) {
    await showActionMessage(
      "Jump to cycle", "Jump to cycle is available after starting Run Step.");
    return;
  }
  const rawCycle = $("#cycle-jump").value.trim();
  if (!/^\d+$/.test(rawCycle)) {
    await showActionMessage("Jump to cycle", "Enter a whole cycle number.");
    return;
  }
  const targetCycle = Number(rawCycle);
  if (targetCycle < 1 || targetCycle > pipelineData.cycles) {
    await showActionMessage(
      "Jump to cycle",
      `Cycle ${targetCycle} is outside this trace. Enter a cycle from 1 to ${pipelineData.cycles}.`);
    return;
  }
  playbackCycle = targetCycle;
  selectedColumn = targetCycle;
  $("#step").textContent = playbackCycle >= pipelineData.cycles
    ? "Restart Step" : "Next Cycle";
  renderPipeline();
  showTab("pipeline");
  positionPipelineCycle(targetCycle);
}

function updateCycleNavigation() {
  const hasTrace = Boolean(pipelineData && playbackCycle !== null);
  $("#cycle-position").textContent = hasTrace
    ? `Cycle ${playbackCycle}/${pipelineData.cycles}` : "Cycle —/—";
  $("#cycle-prev").disabled = !hasTrace || !stepMode || playbackCycle <= 1;
  $("#cycle-next").disabled = !hasTrace || !stepMode || playbackCycle >= pipelineData.cycles;
  const canJump = hasTrace && stepMode;
  const jumpInput = $("#cycle-jump");
  jumpInput.disabled = !canJump;
  $("#cycle-jump-form button").disabled = !canJump;
  if (hasTrace) {
    jumpInput.max = pipelineData.cycles;
    jumpInput.placeholder = `1–${pipelineData.cycles}`;
  } else {
    jumpInput.removeAttribute("max");
    jumpInput.placeholder = "Cycle";
    jumpInput.value = "";
  }
}

function renderPipelineDiagram() {
  if (!pipelineData) return;
  const config = pipelineData.configuration || {};
  const o3 = config.cpu === "out-of-order";
  const visibleO3Stages = new Set(config.o3VisibleStages || ["issue", "execute", "memory", "cdb", "commit"]);
  const o3Stages = [
    ["fetch", `Fetch × ${config.fetchWidth || 2}`, "F"],
    ["decode", `Decode × ${config.decodeWidth || 2}`, "D"],
    ["rename", `Rename × ${config.renameWidth || 2}`, "R"],
    ["issue", `Issue × ${config.dispatchWidth || 2}`, "I"],
    ["execute", "EXE", "E"], ["memory", "MEM", "M"],
    ["cdb", `CDB × ${config.writebackWidth || 1}`, "C"],
    ["commit", `Commit × ${config.commitWidth || 2}`, "W"]
  ];
  const stages = o3
    ? o3Stages.filter(([key]) => visibleO3Stages.has(key)).map(([_key, label, code]) => [label, code])
    : [["Fetch", "F"], ["Decode", "D"], ["Execute", "E"], ["Memory", "M"], ["Writeback", "W"]];
  const blocks = stages.map(([stage, code], index) => `${index ? '<div class="diagram-arrow">↓</div>' : ""}<div class="diagram-stage stage-${code}">${stage}</div>`).join("");
  const memoryMode = config.memoryMode === "cache" ? "cache" : "direct";
  const memoryDetails = memoryMode === "cache"
    ? `L1 I ${config.iCacheSize} · D ${config.dCacheSize}<br>Hit ${config.cacheLatency} cycles · RAM ${config.memoryLatency} cycles`
    : `Direct memory · I ${config.instructionMemoryLatency} · Read ${config.dataReadLatency} · Write ${config.dataWriteLatency} cycles`;
  const predictorName = {
    ideal: "Ideal (teaching baseline)",
    local: "Local counter (2-bit baseline)",
    tournament: "Tournament (local + global)",
    bimode: "Bi-mode", tage: "TAGE (tagged hybrid)"
  }[config.branchPredictor] || "Ideal (teaching baseline)";
  const details = (o3
    ? `Out-of-order execution · speculation on<br>Predictor: ${predictorName}<br>Fetch ${config.fetchWidth} · Decode ${config.decodeWidth} · Rename ${config.renameWidth}<br>Issue ${config.dispatchWidth} · branches/jumps issue alone<br>CDB ${config.writebackWidth} · Commit ${config.commitWidth}<br>EXE: Int ALU ${config.intAluUnits || 1} · Mul ${config.intMulUnits || 1} · Div ${config.intDivUnits || 1}<br>FP ALU ${config.floatAluUnits || 1} · Mul ${config.floatMulUnits || 1} · Div ${config.floatDivUnits || 1}<br>Shared load/store units: ${config.memoryUnits || 1}<br>ROB · RS/IQ · LQ · SQ: unlimited`
    : `Forwarding: ${config.forwarding ? "on" : "off"}<br>Out-of-order execution: ${config.outOfOrderExecution !== false ? "on" : "off"}`)
    + `<br>Floating point: ${config.floatingPointPrecision === "double" ? "double (64-bit)" : "single (32-bit)"}`
    + `<br>Compressed instructions: ${config.compressedInstructions ? "on" : "off"}`
    + `<br>${memoryDetails}`;
  $("#pipeline-diagram").innerHTML = `<div class="diagram-title">${o3 ? "Superscalar" : "Five-Stage Pipeline (F/D/E/M/W)"}</div>${blocks}<div class="diagram-detail">${details}</div>`;
}

function memoryWatchStorageKey(projectName = current?.name) {
  return projectName ? `ase-studio-memory-watches:${projectName}` : null;
}

function canonicalMemoryAddress(value) {
  const text = String(value).trim();
  if (!/^(?:0[xX][0-9a-fA-F]+|[0-9]+)$/.test(text)) {
    throw Error("Enter a hexadecimal address such as 0x11120 or a decimal address.");
  }
  return `0x${BigInt(text).toString(16)}`;
}

function loadMemoryWatches() {
  memoryWatches = [];
  const key = memoryWatchStorageKey();
  if (!key) return;
  try {
    const saved = JSON.parse(localStorage.getItem(key) || "[]");
    if (!Array.isArray(saved)) return;
    saved.forEach(entry => {
      if (entry?.type === "symbol" && typeof entry.name === "string" && entry.name) {
        memoryWatches.push({type: "symbol", name: entry.name});
      }
    });
    // Rewrite once to discard obsolete address-based watches from older releases.
    localStorage.setItem(key, JSON.stringify(memoryWatches));
  } catch (_error) {
    memoryWatches = [];
  }
}

function saveMemoryWatches() {
  const key = memoryWatchStorageKey();
  if (key) localStorage.setItem(key, JSON.stringify(memoryWatches));
}

const MEMORY_ELEMENT_NAMES = {1: "byte", 2: "halfword", 4: "word", 8: "doubleword"};
const MEMORY_ROWS_PER_SYMBOL = 256;

function availableMemorySymbols() {
  const source = pipelineData?.dataSymbols?.length
    ? pipelineData.dataSymbols : (current?.dataSymbols || []);
  return source.map(symbol => {
    const size = Math.max(1, Number(symbol.size) || 4);
    let elementSize = Number(symbol.elementSize);
    if (!MEMORY_ELEMENT_NAMES[elementSize]) elementSize = size % 4 === 0 ? 4 : 1;
    if (size % elementSize) elementSize = 1;
    return {
      ...symbol,
      address: canonicalMemoryAddress(symbol.address),
      size,
      elementSize,
      elementCount: size / elementSize
    };
  }).sort((left, right) => BigInt(left.address) < BigInt(right.address)
    ? -1 : BigInt(left.address) > BigInt(right.address) ? 1 : 0);
}

function describeMemorySymbol(symbol) {
  const unit = MEMORY_ELEMENT_NAMES[symbol.elementSize];
  return `${symbol.elementCount} ${unit}${symbol.elementCount === 1 ? "" : "s"}`;
}

function memoryDisplayLayout(symbol, format) {
  let elementSize = symbol.elementSize;
  if (format === "float" && symbol.untypedStorage) {
    const floatingBytes = pipelineData?.configuration?.floatingPointPrecision === "double" ? 8 : 4;
    if (symbol.size % floatingBytes === 0) elementSize = floatingBytes;
  }
  return {elementSize, elementCount: symbol.size / elementSize};
}

function memorySymbolHue(symbol, symbols) {
  const index = Math.max(0, symbols.indexOf(symbol));
  return Math.round((index * 137.508) % 360);
}

function renderMemoryWatchControls(symbols) {
  const select = $("#memory-symbol");
  const previous = select.value;
  select.innerHTML = symbols.length
    ? symbols.map(symbol => `<option value="${escapeHtml(symbol.name)}">${escapeHtml(symbol.name)} · ${describeMemorySymbol(symbol)} · ${symbol.address}</option>`).join("")
    : '<option value="">No symbols available</option>';
  select.disabled = !symbols.length;
  $("#memory-symbol-form button").disabled = !symbols.length;
  if (symbols.some(symbol => symbol.name === previous)) select.value = previous;

  $("#memory-watch-list").innerHTML = memoryWatches.map((watch, index) => {
    const symbol = symbols.find(candidate => candidate.name === watch.name);
    const label = `${watch.name}${symbol ? ` · ${describeMemorySymbol(symbol)}` : " · unavailable"}`;
    const color = symbol ? " memory-symbol-color" : "";
    const style = symbol ? ` style="--memory-symbol-hue:${memorySymbolHue(symbol, symbols)}deg"` : "";
    return `<span class="memory-watch${color}"${style}>${escapeHtml(label)}<button type="button" data-memory-watch-remove="${index}" aria-label="Remove ${escapeHtml(label)} from the watch list">×</button></span>`;
  }).join("");
  $("#memory-watch-clear").disabled = memoryWatches.length === 0;
}

function renderMemoryMap() {
  const container = $("#memory-map-sections");
  const sections = pipelineData?.memoryMap?.length
    ? pipelineData.memoryMap : (current?.memoryMap || []);
  if (!sections.length) {
    container.innerHTML = '<div class="memory-map-empty">Build the project to see its allocated ELF sections.</div>';
    return;
  }
  const ordered = [...sections].sort((left, right) =>
    BigInt(left.start) < BigInt(right.start) ? 1 : -1);
  container.innerHTML = ordered.map(section => {
    const type = String(section.kind || "allocated").replaceAll(" ", "-");
    const parts = [...(section.parts || [])].reverse().map(part =>
      `<div class="memory-map-part"><strong>${escapeHtml(part.name)}</strong><span>${Number(part.size).toLocaleString()} bytes</span><code>${escapeHtml(part.start)}–${escapeHtml(part.end)}</code></div>`
    ).join("");
    return `<div class="memory-map-section memory-map-${escapeHtml(type)}" title="${escapeHtml(`${section.name}: ${section.start}–${section.end}`)}"><div class="memory-map-address"><span>End</span><code>${escapeHtml(section.end)}</code></div><strong>${escapeHtml(section.name)}</strong><span>${escapeHtml(section.kind)} · ${Number(section.size).toLocaleString()} bytes</span>${parts}<div class="memory-map-address"><span>Begin</span><code>${escapeHtml(section.start)}</code></div></div>`;
  }).join("");
}

function publishMemoryWindowState() {
  if (!memoryWindowChannel) return;
  const symbol = $("#memory-symbol");
  memoryWindowChannel.postMessage({
    type: "memory-state",
    project: current?.name || "",
    theme: document.documentElement.dataset.theme,
    symbolOptionsHtml: symbol.innerHTML,
    symbolValue: symbol.value,
    symbolDisabled: symbol.disabled,
    addDisabled: $("#memory-symbol-form button").disabled,
    clearDisabled: $("#memory-watch-clear").disabled,
    format: $("#memory-format").value,
    watchesHtml: $("#memory-watch-list").innerHTML,
    summary: $("#memory-summary").textContent,
    tableHtml: $("#memory-table").innerHTML,
    mapHtml: $("#memory-map-sections").innerHTML
  });
}

function addMemoryWatch(name) {
  if (!current || !name) return;
  if (!availableMemorySymbols().some(symbol => symbol.name === name)) return;
  if (!memoryWatches.some(watch => watch.type === "symbol" && watch.name === name)) {
    memoryWatches.push({type: "symbol", name});
    saveMemoryWatches();
  }
  renderMemory();
}

function removeMemoryWatch(index) {
  const selected = Number(index);
  if (!Number.isInteger(selected) || selected < 0 || selected >= memoryWatches.length) return;
  memoryWatches.splice(selected, 1);
  saveMemoryWatches();
  renderMemory();
}

function clearMemoryWatches() {
  memoryWatches = [];
  saveMemoryWatches();
  renderMemory();
}

function setMemoryFormat(value) {
  const formats = new Set(["hex", "binary", "signed", "unsigned", "float"]);
  if (!formats.has(value)) return;
  $("#memory-format").value = value;
  localStorage.setItem("ase-studio-memory-format", value);
  renderMemory();
}

async function openMemoryWindow() {
  memoryPopoutWindow = window.open(
    "memory-window.html", "ase-studio-memory", "popup,width=1050,height=720");
  if (!memoryPopoutWindow) {
    await showActionMessage("Open memory window", "The separate memory window was blocked.");
    return;
  }
  memoryPopoutWindow.focus();
}

function renderMemory() {
  const table = $("#memory-table");
  const symbols = availableMemorySymbols();
  renderMemoryMap();
  renderMemoryWatchControls(symbols);
  if (!pipelineData || playbackCycle === null) {
    $("#memory-summary").textContent = memoryWatches.length
      ? "Run a trace to inspect the watched memory locations."
      : "Add a variable or vector to the watch list.";
    table.innerHTML = '<div class="memory-empty">Memory values appear here after a successful run.</div>';
    publishMemoryWindowState();
    return;
  }

  // Replay memory one byte at a time so byte and halfword stores change only
  // the bytes they write; each watched element is then rebuilt from bytes.
  const initialRanges = Object.entries(pipelineData.initialMemory || current?.initialMemory || {})
    .filter(([, hex]) => typeof hex === "string")
    .map(([address, hex]) => ({start: Number(BigInt(address)), hex}));
  const accessed = new Map();
  const deltas = pipelineData.memoryDeltas || {};
  for (let cycle = 1; cycle <= playbackCycle; cycle++) {
    const update = deltas[String(cycle)];
    if (!update) continue;
    Object.entries(update).forEach(([address, event]) => {
      const start = Number(BigInt(address));
      const width = Math.max(1, Math.floor((Number(event.bits) || 32) / 8));
      let bits = null;
      try {
        bits = BigInt(event.value);
      } catch (_error) {
        bits = null;
      }
      for (let offset = 0; offset < width; offset++) {
        const previous = accessed.get(start + offset);
        accessed.set(start + offset, {
          value: bits === null ? (previous?.value ?? null) : Number((bits >> BigInt(8 * offset)) & 0xffn),
          access: event.access,
          cycle
        });
      }
    });
  }
  const byteAt = address => {
    const value = accessed.get(address)?.value;
    if (value !== null && value !== undefined) return value;
    const range = initialRanges.find(candidate =>
      address >= candidate.start && address < candidate.start + candidate.hex.length / 2);
    if (!range) return null;
    const offset = (address - range.start) * 2;
    return Number.parseInt(range.hex.slice(offset, offset + 2), 16);
  };

  if (!memoryWatches.length) {
    $("#memory-summary").textContent = `Memory watch through cycle ${playbackCycle}`;
    table.innerHTML = '<div class="memory-empty">The watch list is empty. Add a variable or vector above.</div>';
    publishMemoryWindowState();
    return;
  }

  const format = $("#memory-format").value;
  const rows = [];
  let truncated = false;
  memoryWatches.forEach(watch => {
    const symbol = symbols.find(candidate => candidate.name === watch.name);
    if (!symbol) return;
    const start = Number(BigInt(symbol.address));
    const layout = memoryDisplayLayout(symbol, format);
    const shown = Math.min(layout.elementCount, MEMORY_ROWS_PER_SYMBOL);
    truncated ||= shown < layout.elementCount;
    for (let index = 0; index < shown; index++) {
      const address = start + index * layout.elementSize;
      let value = 0n;
      let known = true;
      let latest = null;
      // RISC-V is little-endian: the highest address holds the most significant byte.
      for (let offset = layout.elementSize - 1; offset >= 0; offset--) {
        const byte = byteAt(address + offset);
        if (byte === null) known = false;
        else value = (value << 8n) | BigInt(byte);
        const event = accessed.get(address + offset);
        if (event && (!latest || event.cycle > latest.cycle)) latest = event;
      }
      const width = layout.elementSize * 8;
      // Floating-point views only exist for single and double precision.
      const elementFormat = format === "float" && width < 32 ? "hex" : format;
      rows.push({
        address,
        width: layout.elementSize,
        symbol,
        label: layout.elementCount > 1 ? `${symbol.name}[${index}]` : symbol.name,
        access: latest ? latest.access : known ? "initial value" : "not accessed",
        value: known ? formatWordValue(value, elementFormat, width) : "—",
        changed: latest?.cycle === playbackCycle
      });
    }
  });
  rows.sort((left, right) => left.address - right.address);
  const limitNote = truncated ? ` · first ${MEMORY_ROWS_PER_SYMBOL} elements per vector` : "";
  $("#memory-summary").textContent = `Memory watch through cycle ${playbackCycle} · ${rows.length} location${rows.length === 1 ? "" : "s"}${limitNote}`;
  table.innerHTML = rows.length ? rows.map(row => {
    const style = ` style="--memory-symbol-hue:${memorySymbolHue(row.symbol, symbols)}deg"`;
    return `<div class="memory-value memory-symbol-color${row.changed ? " changed" : ""}" data-memory-address="0x${row.address.toString(16)}" data-memory-width="${row.width}" data-memory-label="${escapeHtml(row.label)}"${style}><strong class="memory-symbol-name">${escapeHtml(row.label)}</strong><span class="memory-address">0x${row.address.toString(16)}</span><span>${row.access}</span><code>${row.value}</code></div>`;
  }).join("") : '<div class="memory-empty">No matching memory locations were found in this build.</div>';
  publishMemoryWindowState();
}

function closeChangeContextMenu() {
  changeContextMenu?.remove();
  changeContextMenu = null;
}

function openChangeContextMenu(event, item) {
  closeChangeContextMenu();
  event.preventDefault();
  const menu = document.createElement("div");
  menu.className = "change-context-menu";
  menu.setAttribute("role", "menu");
  [
    ["next", "Go to next change"],
    ["previous", "Go to previous change"]
  ].forEach(([direction, label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("role", "menuitem");
    button.textContent = label;
    button.onclick = () => {
      closeChangeContextMenu();
      navigateToItemChange(item, direction);
    };
    menu.append(button);
  });
  document.body.append(menu);
  const bounds = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(event.clientX, window.innerWidth - bounds.width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(event.clientY, window.innerHeight - bounds.height - 8))}px`;
  changeContextMenu = menu;
  menu.querySelector("button")?.focus();
}

function sameTraceValue(left, right) {
  try {
    return BigInt(left) === BigInt(right);
  } catch (_error) {
    return String(left) === String(right);
  }
}

function registerChangeCycles(register) {
  const deltas = pipelineData?.registerDeltas || {};
  const initial = pipelineData?.initialRegisters || {};
  let previous = initial[register] ?? "0x00000000";
  const changes = [];
  Object.keys(deltas).map(Number).sort((left, right) => left - right).forEach(cycle => {
    const update = deltas[String(cycle)];
    if (!Object.prototype.hasOwnProperty.call(update, register)) return;
    const value = update[register];
    if (!sameTraceValue(previous, value)) changes.push(cycle);
    previous = value;
  });
  return changes;
}

function memoryChangeCycles(address, width) {
  const start = Number(BigInt(canonicalMemoryAddress(address)));
  const byteCount = Math.max(1, Math.floor(Number(width) || 1));
  const bytes = Array(byteCount).fill(null);
  const initialMemory = pipelineData?.initialMemory || current?.initialMemory || {};
  Object.entries(initialMemory).forEach(([baseText, hex]) => {
    if (typeof hex !== "string") return;
    const base = Number(BigInt(baseText));
    const first = Math.max(start, base);
    const last = Math.min(start + byteCount, base + Math.floor(hex.length / 2));
    for (let location = first; location < last; location++) {
      const offset = (location - base) * 2;
      const byte = Number.parseInt(hex.slice(offset, offset + 2), 16);
      if (Number.isFinite(byte)) bytes[location - start] = byte;
    }
  });

  const deltas = pipelineData?.memoryDeltas || {};
  const changes = [];
  Object.keys(deltas).map(Number).sort((left, right) => left - right).forEach(cycle => {
    const before = bytes.slice();
    Object.entries(deltas[String(cycle)] || {}).forEach(([eventAddress, event]) => {
      if (event.access !== "write") return;
      let value;
      try {
        value = BigInt(event.value);
      } catch (_error) {
        return;
      }
      const eventStart = Number(BigInt(eventAddress));
      const eventWidth = Math.max(1, Math.floor((Number(event.bits) || 32) / 8));
      const first = Math.max(start, eventStart);
      const last = Math.min(start + byteCount, eventStart + eventWidth);
      for (let location = first; location < last; location++) {
        const offset = location - eventStart;
        bytes[location - start] = Number((value >> BigInt(offset * 8)) & 0xffn);
      }
    });
    if (bytes.some((value, index) => value !== before[index])) changes.push(cycle);
  });
  return changes;
}

async function navigateToItemChange(item, direction) {
  const label = item.label || item.key;
  const action = direction === "previous" ? "Go to previous change" : "Go to next change";
  if (!stepMode || !pipelineData || playbackCycle === null) {
    await showActionMessage(action, `${action} is available after starting Run Step.`);
    return;
  }

  let changes = [];
  try {
    changes = item.kind === "register"
      ? registerChangeCycles(item.key)
      : memoryChangeCycles(item.key, item.width);
  } catch (_error) {
    changes = [];
  }
  const target = direction === "previous"
    ? [...changes].reverse().find(cycle => cycle < playbackCycle)
    : changes.find(cycle => cycle > playbackCycle);
  if (target === undefined) {
    await showActionMessage(action, `No ${direction === "previous" ? "previous" : "next"} value change for ${label} was found in this trace.`);
    return;
  }
  navigateToPipelineCycle(target);
}

function navigateToPipelineCycle(cycle) {
  playbackCycle = cycle;
  selectedColumn = cycle;
  $("#step").textContent = playbackCycle >= pipelineData.cycles
    ? "Restart Step" : "Next Cycle";
  renderPipeline();
  showTab("pipeline");
  positionPipelineCycle(cycle);
  const eventRow = displayedPipelineRows().findIndex(row =>
    row.cycles?.[String(cycle)] !== undefined);
  if (eventRow >= 0) {
    requestAnimationFrame(() => {
      const instruction = $("#pipeline-grid")
        .querySelector(`.cell.inst[data-row="${eventRow}"]`);
      if (instruction) pipelineSelectTarget?.(instruction);
    });
  }
}

function expandedLoopView() {
  return Boolean($("#expand-loops").checked && pipelineData?.dynamicInstructions?.length);
}

function displayedPipelineRows() {
  if (!pipelineData) return [];
  return expandedLoopView() ? pipelineData.dynamicInstructions : pipelineData.instructions;
}

function pipelineRowBounds(row) {
  const cycles = Object.keys(row.cycles).map(Number);
  return {first: Math.min(...cycles), last: Math.max(...cycles)};
}

function jumpsForPipelineRow(row, lastVisibleCycle, expanded = expandedLoopView()) {
  if (row.squashed) return [];
  let jumps = (pipelineData.jumps || []).filter(jump =>
    jump.fromAddress === row.address && jump.cycle <= lastVisibleCycle);
  if (expanded) {
    const bounds = pipelineRowBounds(row);
    jumps = jumps.filter(jump => jump.instructionSequence != null && row.sequence != null
      ? jump.instructionSequence === row.sequence
      : bounds.first <= jump.cycle && jump.cycle <= bounds.last);
  }
  const grouped = new Map();
  jumps.forEach(jump => {
    const entry = grouped.get(jump.toAddress) || {target: jump.toAddress, cycles: []};
    entry.cycles.push(jump.cycle);
    grouped.set(jump.toAddress, entry);
  });
  return [...grouped.values()];
}

function cacheEventsForPipelineRow(row, lastVisibleCycle) {
  const grouped = new Map();
  (row.cacheEvents || []).filter(event =>
    Number(event.cycle) <= lastVisibleCycle
  ).forEach(event => {
    const key = `${event.cache}:${event.result}`;
    const entry = grouped.get(key) || {...event, count: 0};
    entry.count += 1;
    grouped.set(key, entry);
  });
  return [...grouped.values()];
}

function appendSimulationReports(trace) {
  const reports = [trace?.cacheReport, trace?.branchReport].filter(Boolean);
  if (!reports.length) return;
  lastNormalLog += `\n\n${reports.join("\n\n")}`;
  lastAdvancedLog += `\n\n${reports.join("\n\n")}`;
  updateLog();
}

function jumpTargetRow(rows, address, afterCycle = 0) {
  if (!expandedLoopView()) return rows.findIndex(row => row.address === address);
  let fallback = -1;
  for (let index = 0; index < rows.length; index++) {
    if (rows[index].address !== address) continue;
    if (fallback < 0) fallback = index;
    if (pipelineRowBounds(rows[index]).first > afterCycle) return index;
  }
  return fallback;
}

function pipelineStageTokens(stage) {
  return stage.match(/[A-Z]\*?/g) || [];
}

function pipelineStageLabel(stage, o3) {
  const tokens = pipelineStageTokens(stage);
  if (o3) {
    return tokens.map(token =>
      ({C: "CDB", W: "C"}[token[0]] || token[0]) + token.slice(1)).join("/");
  }
  return tokens.length > 1 ? tokens.join("/") : stage;
}

function pipelineDisplayCycles(data = pipelineData) {
  if (!data) return [];
  const hidden = new Set(data.collapsedRecoveryCycles || []);
  const cycles = [];
  for (let cycle = 1; cycle <= data.cycles; cycle++) {
    if (!hidden.has(cycle)) cycles.push(cycle);
  }
  return cycles;
}

function renderPipeline() {
  if (pipelinePaintFrame !== null) {
    cancelAnimationFrame(pipelinePaintFrame);
    pipelinePaintFrame = null;
  }
  if (pipelineResizeObserver) {
    pipelineResizeObserver.disconnect();
    pipelineResizeObserver = null;
  }
  const data = pipelineData;
  if (!data) {
    $("#pipeline-grid").innerHTML = "";
    updatePipelineScrollButtons();
    $("#pipeline-diagram").innerHTML = "";
    $("#pipeline-pc").textContent = "PC —";
    $("#pipeline-summary").textContent = "No pipeline trace loaded.";
    $("#detail").textContent = "Hover a pipeline cell to inspect it.";
    updateCycleNavigation();
    renderMemory();
    return;
  }
  const displayRows = displayedPipelineRows();
  const displayCycles = pipelineDisplayCycles(data);
  const columnCount = displayCycles.length;
  const dynamicRows = (data.dynamicInstructions?.length ? data.dynamicInstructions : data.instructions)
    .filter(row => !row.squashed);
  const cellWidth = Number($("#cell-width").value);
  const rowHeight = 27;
  const addressWidth = 88;
  const instructionWidth = window.innerWidth <= 700 ? 150 : 182;
  const flowWidth = window.innerWidth <= 700 ? 132 : 140;
  const frozenWidth = addressWidth + instructionWidth + flowWidth;
  const totalWidth = addressWidth + instructionWidth + flowWidth + columnCount * cellWidth;
  const grid = $("#pipeline-grid");
  const scroll = $("#pipeline-scroll");
  const savedLeft = scroll.scrollLeft;
  const savedTop = scroll.scrollTop;

  document.documentElement.style.setProperty("--cell", cellWidth + "px");
  document.documentElement.style.setProperty("--address-width", addressWidth + "px");
  document.documentElement.style.setProperty("--instruction-width", instructionWidth + "px");
  document.documentElement.style.setProperty("--flow-width", flowWidth + "px");
  const visibleCycle = playbackCycle === null ? data.cycles : playbackCycle;
  const cpi = dynamicRows.length ? (data.cycles / dynamicRows.length).toFixed(2) : "—";
  const codeBytes = data.codeBytes ?? new Set(data.instructions.map(row => row.address)).size * 4;
  const instructionCount = displayRows.filter(row =>
    !row.squashed && row.iterations !== 0).length;
  const predictionMisses = displayRows.reduce((total, row) =>
    total + Number(row.mispredictions || (row.mispredicted ? 1 : 0)), 0);
  const predictionSummary = predictionMisses ? ` · ${predictionMisses} BP misses` : "";
  $("#pipeline-summary").textContent = `${instructionCount} instructions · ${codeBytes} bytes pipeline code${predictionSummary} · CPI ${cpi}`;
  updateCycleNavigation();
  // Leave one cell of scrollable breathing room without adding fake cycles
  // or instructions to the table, playback, statistics, or CSV exports.
  grid.style.width = (totalWidth + cellWidth) + "px";
  grid.innerHTML = `<div class="pipeline-header" style="width:${totalWidth}px"></div><div class="pipeline-rows" style="height:${(displayRows.length + 1) * rowHeight}px;width:${totalWidth}px"></div>`;
  const header = grid.querySelector(".pipeline-header");
  const rows = grid.querySelector(".pipeline-rows");
  const cycleOverscan = 8;
  let paintedFirstRow = -1;
  let paintedLastRow = -1;
  let paintedFirstCycle = -1;
  let paintedLastCycle = -1;
  let forceNextPaint = false;

  function visibleCycleWindow() {
    const cycleViewportWidth = Math.max(
      cellWidth, scroll.clientWidth - frozenWidth);
    const firstVisible = Math.max(
      1, Math.floor(scroll.scrollLeft / cellWidth) + 1);
    const lastVisible = Math.min(
      columnCount,
      Math.ceil((scroll.scrollLeft + cycleViewportWidth) / cellWidth));
    return {
      first: Math.max(1, firstVisible - cycleOverscan),
      last: Math.min(columnCount, lastVisible + cycleOverscan)
    };
  }

  function virtualColumns(firstCycle, lastCycle) {
    const beforeWidth = (firstCycle - 1) * cellWidth;
    const cycleCount = Math.max(0, lastCycle - firstCycle + 1);
    const afterWidth = Math.max(0, columnCount - lastCycle) * cellWidth;
    return `${addressWidth}px ${instructionWidth}px ${flowWidth}px ${beforeWidth}px repeat(${cycleCount}, ${cellWidth}px) ${afterWidth}px`;
  }

  function paintHeader(firstCycle, lastCycle, columns) {
    let html = '<div class="cell address-cell head corner">PC Address</div>';
    html += '<div class="cell inst head corner">Instruction</div>';
    html += '<div class="cell flow-cell head corner" title="Control-flow transfers, prediction misses, and cache misses">Control / cache</div>';
    html += '<div class="pipeline-cycle-spacer" aria-hidden="true"></div>';
    for (let column = firstCycle; column <= lastCycle; column++) {
      const cycle = displayCycles[column - 1];
      html += `<div class="cell head ${selectedColumn === cycle ? "column-selected" : ""}${cycle > visibleCycle ? " future" : ""}" data-col="${cycle}">${cycle}</div>`;
    }
    html += '<div class="pipeline-cycle-spacer" aria-hidden="true"></div>';
    header.style.gridTemplateColumns = columns;
    header.innerHTML = html;
  }

  function paintRows(first, last, firstCycle, lastCycle, columns) {
    let html = "";

    for (let rowIndex = first; rowIndex < last; rowIndex++) {
      const row = displayRows[rowIndex];
      html += `<div class="pipeline-row ${selectedRow === rowIndex ? "row-selected" : ""}${row.squashed ? " wrong-path" : ""}" style="top:${rowIndex * rowHeight}px;grid-template-columns:${columns};width:${totalWidth}px">`;
      const iterationText = row.iterations > 1 ? ` · ${row.iterations} iterations` : "";
      const address = row.address ? `0x${escapeHtml(row.address)}` : "—";
      html += `<div class="cell address-cell" data-row="${rowIndex}" title="Program counter">${address}</div>`;
      html += `<div class="cell inst" data-row="${rowIndex}" title="${escapeHtml(row.instruction + iterationText)}">${highlightAsmLine(row.instruction)}</div>`;
      const jumpLinks = jumpsForPipelineRow(row, visibleCycle).map(jump => {
        const targetRow = jumpTargetRow(displayRows, jump.target, jump.cycles[0]);
        const arrow = targetRow >= 0 && targetRow < rowIndex ? "↶" : "↷";
        const count = jump.cycles.length > 1 ? ` ×${jump.cycles.length}` : "";
        const cycles = jump.cycles.join(", ");
        if (expandedLoopView()) {
          return `<span class="jump-label" title="Taken at cycle${jump.cycles.length > 1 ? "s" : ""} ${cycles}">${arrow} 0x${escapeHtml(jump.target)}${count}</span>`;
        }
        return `<button class="jump-arrow" data-jump-target="${escapeHtml(jump.target)}" data-jump-cycle="${jump.cycles[0]}" title="Taken at cycle${jump.cycles.length > 1 ? "s" : ""} ${cycles}">${arrow} 0x${escapeHtml(jump.target)}${count}</button>`;
      }).join("");
      const cacheEvents = cacheEventsForPipelineRow(row, visibleCycle).map(event => {
        const count = event.count > 1 ? ` ×${event.count}` : "";
        const status = `${event.cache}$ ${event.result}`;
        return `<span class="cache-event cache-${event.result}" title="${escapeHtml(`${status}; see Log Output for address and timing details`)}">${status}${count}</span>`;
      }).join("");
      const predictionMisses = Number(row.mispredictions || (row.mispredicted ? 1 : 0));
      const predictionEvent = predictionMisses
        ? `<span class="branch-miss">BP miss${predictionMisses > 1 ? ` ×${predictionMisses}` : ""}</span>`
        : "";
      const squashEvent = row.squashed
        ? `<span class="squash-label" title="Wrong-path instruction discarded after a branch misprediction">Squashed</span>`
        : "";
      html += `<div class="cell flow-cell" data-row="${rowIndex}">${jumpLinks}${predictionEvent}${cacheEvents}${squashEvent}</div>`;
      html += '<div class="pipeline-cycle-spacer" aria-hidden="true"></div>';
      for (let column = firstCycle; column <= lastCycle; column++) {
        const cycle = displayCycles[column - 1];
        const stage = row.cycles[cycle] || "";
        const label = pipelineStageLabel(stage, pipelineData.format === "o3");
        const tokens = pipelineStageTokens(stage);
        const stageClass = tokens.length ? "stage-" + tokens.at(-1)[0] : "";
        const squashedClass = stage.includes("*") ? " squashed-stage" : "";
        const columnClass = selectedColumn === cycle ? " column-selected" : "";
        html += `<div class="cell ${stageClass}${squashedClass}${columnClass}${cycle > visibleCycle ? " future" : ""}" data-row="${rowIndex}" data-col="${cycle}" data-stage="${stage}">${label}</div>`;
      }
      html += '<div class="pipeline-cycle-spacer" aria-hidden="true"></div>';
      html += "</div>";
    }
    rows.innerHTML = html;
  }

  function paintViewport(force = false) {
    const visibleTop = Math.max(0, scroll.scrollTop - rowHeight);
    const firstRow = Math.max(0, Math.floor(visibleTop / rowHeight) - 6);
    const visibleRowCount = Math.ceil(scroll.clientHeight / rowHeight) + 14;
    const lastRow = Math.min(displayRows.length, firstRow + visibleRowCount);
    const cycleWindow = visibleCycleWindow();
    const rowsChanged = firstRow !== paintedFirstRow || lastRow !== paintedLastRow;
    const cyclesChanged = cycleWindow.first !== paintedFirstCycle
      || cycleWindow.last !== paintedLastCycle;
    if (!force && !rowsChanged && !cyclesChanged) return;
    const columns = virtualColumns(cycleWindow.first, cycleWindow.last);
    if (force || cyclesChanged) {
      paintHeader(cycleWindow.first, cycleWindow.last, columns);
    }
    if (force || rowsChanged || cyclesChanged) {
      paintRows(firstRow, lastRow, cycleWindow.first, cycleWindow.last, columns);
    }
    paintedFirstRow = firstRow;
    paintedLastRow = lastRow;
    paintedFirstCycle = cycleWindow.first;
    paintedLastCycle = cycleWindow.last;
  }

  function schedulePaint(force = false) {
    forceNextPaint ||= force;
    if (pipelinePaintFrame !== null) return;
    pipelinePaintFrame = requestAnimationFrame(() => {
      pipelinePaintFrame = null;
      const repaintAll = forceNextPaint;
      forceNextPaint = false;
      paintViewport(repaintAll);
    });
  }

  grid.onmousemove = event => {
    const cell = event.target.closest("[data-col]");
    if (!cell) return;
    const stage = cell.dataset.stage;
    if (stage) {
      const row = displayRows[Number(cell.dataset.row)];
      const stageNames = {
        F: "Fetch",
        D: "Decode",
        R: "Rename",
        I: "In-order issue to reservation station / IQ",
        E: "Execute",
        M: "Memory access / request",
        C: "Write result on CDB",
        W: pipelineData.format === "o3" ? "Commit" : "Writeback",
        S: pipelineData.format === "o3" ? "Wait in reservation station / ROB" : "Pipeline stall / wait",
        X: "Squashed after incorrect speculation"
      };
      const stageName = pipelineStageTokens(stage)
        .map(token => (stageNames[token[0]] || token[0])
          + (token.endsWith("*") ? " (wrong path; later squashed)" : ""))
        .join(" / ");
      $("#detail").textContent = `Instruction: ${row.instruction} | Cycle: ${cell.dataset.col} | Stage: ${stageName} | Iterations: ${row.iterations}`;
    } else {
      $("#detail").textContent = `Clock cycle ${cell.dataset.col}`;
    }
  };

  pipelineSelectTarget = target => {
    const jumpLink = target.closest("[data-jump-target]");
    if (jumpLink) {
      const targetRow = jumpTargetRow(displayRows, jumpLink.dataset.jumpTarget,
        Number(jumpLink.dataset.jumpCycle || 0));
      if (targetRow >= 0) {
        selectedRow = targetRow;
        scroll.scrollTop = Math.max(0, targetRow * rowHeight - rowHeight * 2);
        highlightSourceLine(displayRows[targetRow].sourceLine);
        paintViewport(true);
      }
      return;
    }
    const cell = target.closest("[data-row], [data-col]");
    if (!cell) return;
    if (cell.dataset.row !== undefined) {
      selectedRow = Number(cell.dataset.row);
      highlightSourceLine(displayRows[selectedRow].sourceLine);
    }
    if (cell.dataset.col !== undefined) selectedColumn = Number(cell.dataset.col);
    paintViewport(true);
  };
  grid.onclick = null;
  grid.oncontextmenu = event => {
    if (!event.target.closest("[data-row], [data-col], [data-jump-target]")) return;
    event.preventDefault();
    pipelineSelectTarget(event.target);
  };

  scroll.onscroll = () => schedulePaint();
  scroll.scrollLeft = savedLeft;
  scroll.scrollTop = savedTop;
  paintViewport(true);
  if ("ResizeObserver" in window) {
    pipelineResizeObserver = new ResizeObserver(() => schedulePaint(true));
    pipelineResizeObserver.observe(scroll);
  }
  // renderPipeline can run while its tab is hidden. Recalculate once the
  // browser has laid out the newly visible pipeline viewport.
  schedulePaint(true);
  updatePipelineScrollButtons();
  renderRegisters();
  renderPipelineDiagram();
  renderMemory();
}

function adjustCellWidth(delta) {
  const slider = $("#cell-width");
  slider.value = Math.max(Number(slider.min), Math.min(Number(slider.max), Number(slider.value) + delta));
  if (pipelineData) renderPipeline();
}

function exportVisiblePipeline() {
  if (!pipelineData || !current) return;
  const lastCycle = playbackCycle === null ? pipelineData.cycles : playbackCycle;
  const displayCycles = pipelineDisplayCycles(pipelineData)
    .filter(cycle => cycle <= lastCycle);
  const quote = value => `"${String(value ?? "").replaceAll('"', '""')}"`;
  const dynamicRows = (pipelineData.dynamicInstructions?.length
    ? pipelineData.dynamicInstructions : pipelineData.instructions)
    .filter(row => !row.squashed);
  const stalls = dynamicRows.reduce((total, row) =>
    total + Object.values(row.cycles).filter(stage => stage === "S").length, 0);
  const instructionCount = dynamicRows.length;
  const predictionMisses = displayedPipelineRows().reduce((total, row) =>
    total + Number(row.mispredictions || (row.mispredicted ? 1 : 0)), 0);
  const cpi = instructionCount ? (pipelineData.cycles / instructionCount).toFixed(3) : "0";
  const headers = ["PC Address", "Instruction", "Control flow / cache"];
  for (const cycle of displayCycles) {
    headers.push(`Cycle ${cycle}`);
  }
  const rows = displayedPipelineRows().map(row => {
    const flow = [
      ...jumpsForPipelineRow(row, lastCycle).map(jump =>
        `0x${row.address} -> 0x${jump.target}${jump.cycles.length > 1 ? ` x${jump.cycles.length}` : ""}`),
      ...cacheEventsForPipelineRow(row, lastCycle).map(event =>
        `${event.cache}$ ${event.result}${event.count > 1 ? ` x${event.count}` : ""}`),
      ...(Number(row.mispredictions || (row.mispredicted ? 1 : 0))
        ? [`BP miss${Number(row.mispredictions || 1) > 1 ? ` x${Number(row.mispredictions)}` : ""}`]
        : []),
      ...(pipelineData.format === "o3" && row.squashed ? ["Squashed"] : [])
    ].join("; ");
    const values = [row.address ? `0x${row.address}` : "", row.instruction, flow];
    for (const cycle of displayCycles) {
      values.push(pipelineStageLabel(row.cycles[cycle] || "", pipelineData.format === "o3"));
    }
    return values;
  });
  const summary = [
    ["Pipeline summary", "Value"],
    ["Total cycles", pipelineData.cycles],
    ["Executed instructions", instructionCount],
    ["Pipeline code size (bytes)", pipelineData.codeBytes || 0],
    ["Branch prediction misses", predictionMisses],
    ["Stalls", stalls],
    ["CPI", cpi],
    []
  ];
  const csv = [...summary, headers, ...rows]
    .map(row => row.map(quote).join(",")).join("\r\n") + "\r\n";
  const blob = new Blob(["\ufeff", csv], {type: "text/csv;charset=utf-8"});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${current.name}-pipeline-cycle-${lastCycle}.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function updateForwardingControl() {
  const outOfOrder = studioFeatures.multiIssueCpu
    && $("#cpu-model").value === "out-of-order";
  const integerPanel = $("#integer-unit-settings");
  const floatingPanel = $("#floating-unit-settings");
  integerPanel.hidden = false;
  floatingPanel.closest(".units-grid").classList.toggle("superscalar", outOfOrder);
  integerPanel.querySelector("legend").textContent = outOfOrder
    ? "Integer and load/store units · clock cycles" : "Integer unit · clock cycles";
  floatingPanel.querySelector("legend").textContent = outOfOrder
    ? "Floating-point units · clock cycles" : "Floating-point unit · clock cycles";
  $("#o3-memory-units-field").hidden = !outOfOrder;
  $("#forwarding").disabled = outOfOrder;
  if (outOfOrder) $("#forwarding").checked = true;
  $("#forwarding-note").textContent = outOfOrder ? "(results use CDB/ROB forwarding)" : "";
  $("#o3-management").hidden = !outOfOrder;
  $("#memory-management").hidden = !studioFeatures.memoryConfiguration;
  $("#out-of-order-execution").closest("label").hidden = outOfOrder;
  $("#out-of-order-execution").disabled = outOfOrder;
  if (outOfOrder) {
    $("#out-of-order-execution").checked = true;
    $("#speculative-execution").checked = true;
  }
}

function updateMemoryControls() {
  const mode = $("#memory-mode").value;
  const available = studioFeatures.memoryConfiguration;
  const direct = mode === "direct";
  const cache = mode === "cache";
  $("#memory-mode").disabled = !available;
  $("#direct-memory-fields").hidden = !direct;
  $("#cache-memory-fields").hidden = !cache;
  ["#instruction-memory-latency", "#data-read-latency", "#data-write-latency"]
    .forEach(selector => { $(selector).disabled = !available || !direct; });
  ["#i-cache-size", "#d-cache-size", "#cache-line", "#cache-latency", "#memory-latency"]
    .forEach(selector => { $(selector).disabled = !available || !cache; });
}

function updatePredictorControls() {
  $("#o3-predictor-parameters").hidden = $("#cpu-model").value !== "out-of-order"
    || $("#o3-branch-predictor").value === "ideal";
}

function openPredictorParameters() {
  const predictor = $("#o3-branch-predictor").value;
  if (!predictorFields[predictor]) return;
  const parameters = predictorParameterDraft[predictor]
    || o3Defaults.branchPredictorParameters?.[predictor] || {};
  $("#predictor-title").textContent = predictorNames[predictor] + " parameters";
  $("#predictor-fields").innerHTML = predictorFields[predictor].map(
    ([key, label, minimum, maximum, help]) => {
      const value = parameters[key];
      const displayed = Array.isArray(value) ? value.join(", ") : value ?? "";
      const kind = minimum === null ? "text" : "number";
      const range = minimum === null ? "" : ` min="${minimum}" max="${maximum}" step="1"`;
      return `<label class="${minimum === null ? "wide" : ""}">${escapeHtml(label)}<input name="${key}" type="${kind}" value="${escapeHtml(displayed)}"${range} required>${help ? `<small>${escapeHtml(help)}</small>` : ""}</label>`;
    }).join("");
  $("#predictor-dialog").showModal();
}

async function savePredictorParameters(event) {
  event.preventDefault();
  const predictor = $("#o3-branch-predictor").value;
  const parameters = {};
  for (const [key] of predictorFields[predictor]) {
    const input = $("#predictor-form").elements.namedItem(key);
    if (key === "tagTableTagWidths" || key === "logTagTableSizes") {
      const parts = input.value.trim().split(/[\s,]+/);
      parameters[key] = parts.map(Number);
      if (parts.length !== 8 || parameters[key].some(number => !Number.isInteger(number))) {
        await showActionMessage("Branch predictor parameters", `${key} needs eight comma-separated whole numbers.`);
        return;
      }
    } else {
      parameters[key] = Number(input.value);
      if (!Number.isInteger(parameters[key])) {
        await showActionMessage("Branch predictor parameters", `${key} must be a whole number.`);
        return;
      }
    }
  }
  const powerOfTwo = number => number > 0 && Number.isInteger(Math.log2(number));
  for (const key of ["localPredictorSize", "localHistoryTableSize", "globalPredictorSize", "choicePredictorSize", "BTBEntries"]) {
    if (key in parameters && !powerOfTwo(parameters[key])) {
      await showActionMessage("Branch predictor parameters", `${key} must be a power of two.`);
      return;
    }
  }
  if (predictor === "local" && !powerOfTwo(parameters.localPredictorSize / parameters.localCtrBits)) {
    await showActionMessage("Branch predictor parameters", "Local storage divided by counter bits must give a power-of-two number of entries.");
    return;
  }
  if (predictor === "tage" && parameters.maxHist <= parameters.minHist) {
    await showActionMessage("Branch predictor parameters", "TAGE maximum history must exceed its minimum history.");
    return;
  }
  predictorParameterDraft[predictor] = parameters;
  $("#predictor-dialog").close();
}

async function openCpuConfiguration() {
  if (!current) return;
  try {
    const config = await api("/api/config?name=" + encodeURIComponent(current.name));
    cpuConfigurationOpenedAs = studioFeatures.multiIssueCpu ? config.cpu : "in-order";
    $("#cpu-model").value = studioFeatures.multiIssueCpu ? config.cpu : "in-order";
    $("#int-alu").value = config.intAlu;
    $("#int-mul").value = config.intMul;
    $("#int-div").value = config.intDiv;
    $("#float-alu").value = config.floatAlu;
    $("#float-mul").value = config.floatMul;
    $("#float-div").value = config.floatDiv;
    $("#floating-point-precision").value = config.floatingPointPrecision || "single";
    $("#int-alu-pipelined").checked = config.intAluPipelined;
    $("#int-mul-pipelined").checked = config.intMulPipelined;
    $("#int-div-pipelined").checked = config.intDivPipelined;
    $("#float-alu-pipelined").checked = config.floatAluPipelined;
    $("#float-mul-pipelined").checked = config.floatMulPipelined;
    $("#float-div-pipelined").checked = config.floatDivPipelined;
    $("#forwarding").checked = config.forwarding;
    $("#compressed-instructions").checked = Boolean(config.compressedInstructions);
    $("#memory-mode").value = config.memoryMode === "cache" ? "cache" : "direct";
    $("#instruction-memory-latency").value = config.instructionMemoryLatency || 1;
    $("#data-read-latency").value = config.dataReadLatency || 1;
    $("#data-write-latency").value = config.dataWriteLatency || 1;
    $("#i-cache-size").value = config.iCacheSize;
    $("#d-cache-size").value = config.dCacheSize;
    $("#cache-line").value = config.cacheLine;
    $("#cache-latency").value = config.cacheLatency;
    $("#memory-latency").value = config.memoryLatency;
    $("#fetch-width").value = config.fetchWidth;
    $("#decode-width").value = config.decodeWidth;
    $("#rename-width").value = config.renameWidth;
    $("#dispatch-width").value = config.dispatchWidth;
    $("#issue-width").value = config.issueWidth;
    $("#writeback-width").value = config.writebackWidth;
    $("#commit-width").value = config.commitWidth;
    $("#int-alu-units").value = config.intAluUnits || 1;
    $("#int-mul-units").value = config.intMulUnits || 1;
    $("#int-div-units").value = config.intDivUnits || 1;
    $("#float-alu-units").value = config.floatAluUnits || 1;
    $("#float-mul-units").value = config.floatMulUnits || 1;
    $("#float-div-units").value = config.floatDivUnits || 1;
    $("#memory-units").value = config.memoryUnits || 1;
    $("#rob-entries").value = config.robEntries;
    $("#iq-entries").value = config.iqEntries;
    $("#lq-entries").value = config.lqEntries;
    $("#sq-entries").value = config.sqEntries;
    $("#o3-branch-predictor").value = config.branchPredictor || "ideal";
    predictorParameterDraft = JSON.parse(JSON.stringify(
      config.branchPredictorParameters || o3Defaults.branchPredictorParameters || {}));
    updatePredictorControls();
    $("#o3-evaluation-label").value = config.o3EvaluationLabel || "";
    $("#out-of-order-execution").checked = config.outOfOrderExecution !== false;
    $("#speculative-execution").checked = config.speculativeExecution !== false;
    const visibleStages = new Set(config.o3VisibleStages || ["issue", "execute", "memory", "cdb", "commit"]);
    document.querySelectorAll("[data-o3-stage]").forEach(input => {
      input.checked = visibleStages.has(input.dataset.o3Stage);
    });
    updateForwardingControl();
    updateMemoryControls();
    $("#cpu-dialog").showModal();
  } catch (error) {
    await showActionMessage("CPU configuration", error.message);
  }
}

const environmentInputs = {
  PROGRAMS_DIRECTORY: "#env-programs-directory",
  SUBMISSION_DIRECTORY: "#env-submission-directory",
  RISCV_TOOLCHAIN_PATH: "#env-riscv-toolchain",
  OPTIMIZATION_FLAGS: "#env-optimization-flags",
  GEM5_INSTALLATION_PATH: "#env-gem5-installation",
  PIPELINE_DISPLAY_CYCLE_LIMIT: "#env-pipeline-cycle-limit",
  SUBMISSION_NAME_PREFIX: "#env-submission-prefix",
  SUBMISSION_NAME_SUFFIX: "#env-submission-suffix"
};
const environmentCheckTokens = {};

function environmentFormValues() {
  return Object.fromEntries(Object.entries(environmentInputs)
    .map(([key, selector]) => [key, $(selector).value]));
}

function setEnvironmentValidation(key, state = "", message = "") {
  const status = document.querySelector(`[data-environment-status="${key}"]`);
  const field = document.querySelector(`[data-environment-key="${key}"]`);
  if (!status || !field) return;
  status.className = `environment-validation${state ? ` ${state}` : ""}`;
  status.title = message;
  status.setAttribute("aria-label", message || "Not checked");
  field.classList.toggle("valid", state === "valid");
  field.classList.toggle("invalid", state === "invalid");
  field.classList.toggle("warning", state === "warning");
}

async function validateEnvironmentInput(key) {
  const token = (environmentCheckTokens[key] || 0) + 1;
  environmentCheckTokens[key] = token;
  setEnvironmentValidation(key, "checking", "Checking…");
  try {
    const result = await api("/api/environment/check", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({key, values: environmentFormValues()})
    });
    if (environmentCheckTokens[key] !== token) return;
    const state = result.ok ? (result.warning ? "warning" : "valid") : "invalid";
    setEnvironmentValidation(key, state, result.message);
  } catch (error) {
    if (environmentCheckTokens[key] !== token) return;
    setEnvironmentValidation(key, "invalid", error.message);
  }
}

Object.entries(environmentInputs).forEach(([key, selector]) => {
  $(selector).addEventListener("input", () => {
    environmentCheckTokens[key] = (environmentCheckTokens[key] || 0) + 1;
    setEnvironmentValidation(key);
  });
  $(selector).addEventListener("blur", () => validateEnvironmentInput(key));
});

document.querySelectorAll("[data-install-tool]").forEach(button => {
  button.onclick = async event => {
    event.preventDefault();
    const component = button.dataset.installTool;
    const confirmed = await actionDialog({
      title: `Install ${component}`,
      message: `Open a terminal and install ${component}? This may download source code, install system packages, and request your administrator password.`,
      confirmLabel: "Open installer",
      cancelLabel: "Cancel"
    });
    if (!confirmed) return;
    try {
      const result = await api("/api/install-tool", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({component})
      });
      lastNormalLog = result.output;
      lastAdvancedLog = result.output;
      updateLog();
    } catch (error) {
      await showActionMessage(`Install ${component}`, error.message);
    }
  };
});

function fillEnvironmentForm(settings) {
  Object.entries(environmentInputs).forEach(([key, selector]) => {
    $(selector).value = settings.values?.[key] || "";
    environmentCheckTokens[key] = (environmentCheckTokens[key] || 0) + 1;
    setEnvironmentValidation(key);
  });
}

async function openEnvironmentSettings() {
  try {
    const settings = await api("/api/environment");
    configuredProgramsDirectory = settings.values?.PROGRAMS_DIRECTORY || "";
    fillEnvironmentForm(settings);
    $("#environment-dialog").showModal();
  } catch (error) {
    await showActionMessage("Settings", error.message);
  }
}

function clearCurrentProject() {
  if (draftSaveTimer !== null) {
    clearTimeout(draftSaveTimer);
    draftSaveTimer = null;
  }
  current = null;
  clearOpcodeSnippetSession();
  closeOpcodePopup();
  opcodePopupSuppressed = false;
  scaffoldProtectionEnabled = true;
  scaffoldEditAuthorized = false;
  updateScaffoldProtectionUi();
  pipelineData = null;
  memoryWatches = [];
  savedSource = "";
  sourceDirty = false;
  updateDirtyIndicator();
  setDraftStatus();
  clearDiagnostics();
  $("#project-title").textContent = "Open a project";
  $("#source-name").textContent = "Assembly Editor";
  $("#body").value = "";
  resetEditorHistory();
  $("#highlight").innerHTML = "";
  renderRegisters();
  renderMemory();
  $("#save").disabled = true;
  $("#duplicate").disabled = true;
  $("#open-with").disabled = true;
  $("#rename").disabled = true;
  $("#submit").disabled = true;
  $("#run").disabled = true;
  $("#configure").disabled = true;
  $("#delete").disabled = true;
  $("#reset").disabled = true;
  $("#export-pipeline").disabled = true;
  $("#expand-loops").disabled = true;
  $("#cycle-prev").disabled = true;
  $("#cycle-next").disabled = true;
  $("#cycle-position").textContent = "Cycle —/—";
  $("#step").disabled = true;
  updateCycleNavigation();
}


async function deleteCurrentProject() {
  if (!current) return;
  const name = current.name;
  const confirmation = await actionDialog({
    title: "Delete project",
    message: `Delete project "${name}"? This cannot be undone.\n\nType the project name to confirm.`,
    inputLabel: "Project name",
    confirmLabel: "Delete",
    cancelLabel: "Cancel",
    danger: true
  });
  if (confirmation !== name) return;
  try {
    await api("/api/projects", {
      method: "DELETE",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name, confirmation})
    });
    localStorage.removeItem(memoryWatchStorageKey(name));
    clearLocalDraft(name);
    clearCurrentProject();
    lastNormalLog = `Deleted project ${name}. Generated results were kept.`;
    lastAdvancedLog = lastNormalLog;
    updateLog();
    showTab("output");
    await loadProjects();
  } catch (error) {
    await showActionMessage("Delete project", error.message);
  }
}

function applyEditorChange(start, end, replacement, selectionStart, selectionEnd, viewport) {
  const editor = $("#body");
  undoStack.push(editorSnapshot());
  if (undoStack.length > 500) undoStack.shift();
  redoStack = [];
  pendingEditorSnapshot = null;
  pendingEditorViewport = null;
  editor.value = editor.value.slice(0, start) + replacement + editor.value.slice(end);
  markSourceEdited();
  scheduleLocalDraft();
  clearDiagnostics();
  refreshScaffoldProtectionUi();
  selectedSourceLine = null;
  syncEditor();
  editor.setSelectionRange(selectionStart, selectionEnd);
  restoreEditorViewport(viewport);
}

function selectedLineRange(text, start, end) {
  const lineStart = start > 0 ? text.lastIndexOf("\n", start - 1) + 1 : 0;
  const effectiveEnd = end > start && text[end - 1] === "\n" ? end - 1 : end;
  const followingNewline = text.indexOf("\n", effectiveEnd);
  return {start: lineStart, end: followingNewline < 0 ? text.length : followingNewline};
}

function editTouchesProtected(text, start, end) {
  return protectedRanges(text).some(range => start < range.end && end >= range.start);
}

$("#body").addEventListener("beforeinput", event => {
  if (!current) return;
  clearOpcodeSnippetSession();
  opcodePopupSuppressed = false;
  pendingEditorViewport = editorViewport();
  if (event.inputType === "historyUndo" || event.inputType === "historyRedo") {
    event.preventDefault();
    if (event.inputType === "historyUndo") undoEditor();
    else redoEditor();
    return;
  }
  const editor = event.target;
  let start = editor.selectionStart;
  let end = editor.selectionEnd;
  if (start === end && event.inputType.startsWith("delete")) {
    if (event.inputType.includes("Backward")) start = Math.max(0, start - 1);
    else end = Math.min(editor.value.length, end + 1);
  }
  const touchesProtected = protectedRanges(editor.value).some(range => start < range.end && end >= range.start);
  if (touchesProtected) {
    event.preventDefault();
    pendingEditorSnapshot = null;
    pendingEditorViewport = null;
  } else {
    pendingEditorSnapshot = editorSnapshot();
  }
});
$("#body").addEventListener("input", () => {
  const viewport = pendingEditorViewport;
  if (!applyingEditorHistory && pendingEditorSnapshot
      && pendingEditorSnapshot.text !== $("#body").value) {
    undoStack.push(pendingEditorSnapshot);
    if (undoStack.length > 500) undoStack.shift();
    redoStack = [];
  }
  pendingEditorSnapshot = null;
  pendingEditorViewport = null;
  markSourceEdited();
  scheduleLocalDraft();
  clearDiagnostics();
  refreshScaffoldProtectionUi();
  selectedSourceLine = null;
  syncEditor();
  restoreEditorViewport(viewport);
  updateOpcodePopup();
});
$("#body").addEventListener("blur", () => {
  clearOpcodeSnippetSession();
  closeOpcodePopup();
});
$("#body").addEventListener("keydown", event => {
  const isTab = event.key === "Tab" || event.key === "ISO_Left_Tab" || event.code === "Tab";
  if (isTab && opcodeSnippetSession) {
    event.preventDefault();
    moveOpcodeSnippet(event.shiftKey ? -1 : 1);
    return;
  }
  if (isTab && !event.shiftKey && opcodePopupState?.matches.length) {
    event.preventDefault();
    completeOpcodeSuggestion();
    return;
  }
  if (opcodePopupState && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
    event.preventDefault();
    selectOpcodeSuggestion(event.key === "ArrowDown" ? 1 : -1);
    return;
  }
  if (event.key === "Escape" && (opcodePopupState || opcodeSnippetSession)) {
    event.preventDefault();
    clearOpcodeSnippetSession();
    closeOpcodePopup({suppress: true});
    return;
  }
  clearOpcodeSnippetSession();
  if (isTab) {
    event.preventDefault();
    const editor = event.target;
    const viewport = editorViewport();
    const start = editor.selectionStart;
    const end = editor.selectionEnd;
    if (start === end && !event.shiftKey) {
      if (editTouchesProtected(editor.value, start, end)) return;
      applyEditorChange(start, end, "    ", start + 4, start + 4, viewport);
      return;
    }
    const range = selectedLineRange(editor.value, start, end);
    if (editTouchesProtected(editor.value, range.start, range.end)) return;
    const block = editor.value.slice(range.start, range.end);
    if (event.shiftKey) {
      const lines = block.split("\n");
      const unindented = lines.map(line => line.startsWith("\t")
        ? line.slice(1) : line.replace(/^ {1,4}/, "")).join("\n");
      if (unindented === block) return;
      applyEditorChange(range.start, range.end, unindented,
        range.start, range.start + unindented.length, viewport);
    } else {
      const indented = block.split("\n").map(line => "    " + line).join("\n");
      applyEditorChange(range.start, range.end, indented,
        range.start, range.start + indented.length, viewport);
    }
    return;
  }
  if (event.key === "Enter" && !event.isComposing) {
    event.preventDefault();
    const editor = event.target;
    const viewport = editorViewport();
    const start = editor.selectionStart;
    const end = editor.selectionEnd;
    if (editTouchesProtected(editor.value, start, end)) return;
    const lineStart = start > 0 ? editor.value.lastIndexOf("\n", start - 1) + 1 : 0;
    const indentation = (editor.value.slice(lineStart).match(/^[\t ]*/) || [""])[0];
    const replacement = "\n" + indentation;
    const caret = start + replacement.length;
    applyEditorChange(start, end, replacement, caret, caret, viewport);
  }
});
document.addEventListener("selectionchange", handleOpcodeSelectionChange);
$("#opcode-popup-close").addEventListener("mousedown", event => event.preventDefault());
$("#opcode-popup-close").onclick = () => {
  closeOpcodePopup({suppress: true});
  $("#body").focus();
};
$("#opcode-popup-list").addEventListener("mousedown", event => {
  if (event.target.closest(".opcode-option")) event.preventDefault();
});
$("#opcode-popup-list").addEventListener("click", event => {
  const option = event.target.closest(".opcode-option");
  if (!option || !opcodePopupState) return;
  opcodePopupState.selectedIndex = Number(option.dataset.opcodeIndex);
  completeOpcodeSuggestion();
  $("#body").focus();
});
$("#save").onclick = save;
$("#duplicate").onclick = duplicateCurrentProject;
$("#open-with").onclick = openWithChooser;
$("#rename").onclick = renameCurrentProject;
$("#submit").onclick = submitAssignment;
$("#run").onclick = () => run(false);
$("#reset").onclick = resetSimulation;
$("#step").onclick = runStep;
$("#configure").onclick = openCpuConfiguration;
$("#environment-settings").onclick = openEnvironmentSettings;
$("#delete").onclick = deleteCurrentProject;
$("#export-pipeline").onclick = exportVisiblePipeline;
$("#integer-register-format").value = localStorage.getItem("ase-studio-integer-register-format") || "hex";
$("#integer-register-format").onchange = event => {
  localStorage.setItem("ase-studio-integer-register-format", event.target.value);
  renderRegisters();
};
$("#float-register-format").value = localStorage.getItem("ase-studio-float-register-format") || "float";
$("#float-register-format").onchange = event => {
  localStorage.setItem("ase-studio-float-register-format", event.target.value);
  renderRegisters();
};
$("#memory-format").value = localStorage.getItem("ase-studio-memory-format") || "hex";
$("#memory-format").onchange = event => setMemoryFormat(event.target.value);
setEditorFontSize(localStorage.getItem("ase-studio-editor-font-size") || 14);
$("#editor-font-size").oninput = event => setEditorFontSize(event.target.value);
$("#theme-toggle").onclick = () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("ase-studio-theme", theme);
  updateThemeButton();
  publishMemoryWindowState();
};
updateThemeButton();
$("#about-button").onclick = () => $("#about-dialog").showModal();
$("#close-about").onclick = () => $("#about-dialog").close();
$("#about-dialog").onclick = event => {
  if (event.target === $("#about-dialog")) $("#about-dialog").close();
};
$("#new").onclick = async () => {
  if (!await resolveUnsavedProject("create a new project")) return;
  const name = await actionDialog({
    title: "New project",
    message: "Create a new assembly project.",
    inputLabel: "Project name",
    confirmLabel: "Create",
    cancelLabel: "Cancel",
    trimInput: false
  });
  if (!name) return;
  try {
    await api("/api/projects", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name})
    });
    await loadProjects();
    await openProject(name, {skipUnsavedCheck: true});
  } catch (error) {
    await showActionMessage("New project", error.message);
  }
};
$("#message-form").onsubmit = event => {
  event.preventDefault();
  const value = $("#message-input").value;
  closeActionDialog(messageDialogHasInput
    ? (messageDialogTrimInput ? value.trim() : value)
    : messageDialogConfirmValue);
};
$("#message-cancel").onclick = () => closeActionDialog(null);
$("#message-alternate").onclick = () => closeActionDialog(messageDialogAlternateValue);
$("#message-dialog").addEventListener("cancel", event => {
  event.preventDefault();
  closeActionDialog(null);
});
document.querySelectorAll("#bottom nav button").forEach(button => button.onclick = () => showTab(button.dataset.tab));
document.addEventListener("contextmenu", event => {
  const register = event.target.closest("[data-register-change]");
  if (register) {
    openChangeContextMenu(event, {
      kind: "register", key: register.dataset.registerChange,
      label: register.dataset.registerChange
    });
    return;
  }
  const memory = event.target.closest("[data-memory-address]");
  if (!memory) return;
  openChangeContextMenu(event, {
    kind: "memory", key: memory.dataset.memoryAddress,
    width: Number(memory.dataset.memoryWidth),
    label: memory.dataset.memoryLabel || memory.dataset.memoryAddress
  });
});
document.addEventListener("pointerdown", event => {
  if (changeContextMenu && !changeContextMenu.contains(event.target)) {
    closeChangeContextMenu();
  }
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeChangeContextMenu();
});
window.addEventListener("resize", closeChangeContextMenu);
document.querySelector(".editor-area").addEventListener("scroll", positionOpcodePopup);
window.addEventListener("resize", positionOpcodePopup);
$("#memory-symbol-form").onsubmit = event => {
  event.preventDefault();
  addMemoryWatch($("#memory-symbol").value);
};
$("#memory-watch-list").onclick = event => {
  const button = event.target.closest("[data-memory-watch-remove]");
  if (!button) return;
  removeMemoryWatch(button.dataset.memoryWatchRemove);
};
$("#memory-watch-clear").onclick = clearMemoryWatches;
$("#memory-popout").onclick = openMemoryWindow;
$("#cell-width").oninput = () => pipelineData && renderPipeline();
$("#cell-smaller").onclick = () => adjustCellWidth(-6);
$("#cell-larger").onclick = () => adjustCellWidth(6);
$("#cycle-prev").onclick = () => movePlaybackCycle(-1);
$("#cycle-next").onclick = () => movePlaybackCycle(1);
$("#cycle-jump-form").onsubmit = event => {
  event.preventDefault();
  jumpToCycle();
};
$("#expand-loops").onchange = () => {
  selectedRow = null;
  if (pipelineData) renderPipeline();
};
$("#advanced-logs").onchange = updateLog;
$("#cpu-model").onchange = () => {
  if (cpuConfigurationOpenedAs === "in-order"
      && $("#cpu-model").value === "out-of-order") {
    const defaultInputs = {
      intAlu: "int-alu", intMul: "int-mul", intDiv: "int-div",
      floatAlu: "float-alu", floatMul: "float-mul", floatDiv: "float-div",
      fetchWidth: "fetch-width", decodeWidth: "decode-width",
      renameWidth: "rename-width", dispatchWidth: "dispatch-width",
      issueWidth: "issue-width",
      writebackWidth: "writeback-width", commitWidth: "commit-width",
      intAluUnits: "int-alu-units", intMulUnits: "int-mul-units",
      intDivUnits: "int-div-units", floatAluUnits: "float-alu-units",
      floatMulUnits: "float-mul-units", floatDivUnits: "float-div-units",
      memoryUnits: "memory-units"
    };
    for (const [key, id] of Object.entries(defaultInputs)) {
      if (o3Defaults[key] !== undefined) $("#" + id).value = o3Defaults[key];
    }
    for (const unit of ["int-alu", "int-mul", "int-div", "float-alu", "float-mul", "float-div"]) {
      const key = unit.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase()) + "Pipelined";
      if (o3Defaults[key] !== undefined) $("#" + unit + "-pipelined").checked = o3Defaults[key];
    }
    const defaultStages = new Set(["issue", "execute", "memory", "cdb", "commit"]);
    $("#o3-branch-predictor").value = o3Defaults.branchPredictor || "ideal";
    document.querySelectorAll("[data-o3-stage]").forEach(input => {
      input.checked = defaultStages.has(input.dataset.o3Stage);
    });
    cpuConfigurationOpenedAs = "out-of-order";
  }
  updateForwardingControl();
  updateMemoryControls();
  updatePredictorControls();
};
$("#o3-branch-predictor").onchange = updatePredictorControls;
$("#o3-predictor-parameters").onclick = openPredictorParameters;
$("#cancel-predictor").onclick = () => $("#predictor-dialog").close();
$("#predictor-form").onsubmit = savePredictorParameters;
$("#memory-mode").onchange = updateMemoryControls;
$("#cancel-submission").onclick = () => $("#submission-dialog").close();
$("#submission-dialog").addEventListener("cancel", event => {
  if ($("#submission-confirm").disabled) event.preventDefault();
});
$("#submission-files").onchange = renderSubmissionFiles;
$("#portal-upload").onchange = togglePortalUpload;
$("#portal-account-button").onclick = togglePortalAccount;
$("#portal-login-form").onsubmit = event => {
  event.preventDefault();
  submitPortalLogin();
};
$("#portal-login-sso").onclick = startPortalSso;
$("#portal-login-cancel").onclick = () => finishPortalLogin(false);
$("#portal-login-dialog").addEventListener("cancel", event => {
  event.preventDefault();
  finishPortalLogin(false);
});
$("#submission-select-all").onclick = () => {
  document.querySelectorAll("[data-submission-project]").forEach(input => { input.checked = true; });
};
$("#submission-select-none").onclick = () => {
  document.querySelectorAll("[data-submission-project]").forEach(input => { input.checked = false; });
};
$("#submission-form").onsubmit = event => {
  event.preventDefault();
  prepareSubmission();
};
$("#submission-manual").onclick = () => {
  if ($("#submission-form").reportValidity()) prepareSubmission("manual");
};
$("#portal-review-form").onsubmit = event => {
  event.preventDefault();
  const description = $("#portal-review-description").value.trim();
  if (!description) {
    $("#portal-review-description").focus();
    return;
  }
  finishPortalReview({
    action: "upload",
    description,
    replace: document.querySelector('[name="portal-review-replace"]:checked').value === "replace"
  });
};
$("#portal-review-manual").onclick = () => finishPortalReview({action: "manual"});
$("#portal-review-cancel").onclick = () => finishPortalReview({action: "cancel"});
$("#portal-review-dialog").addEventListener("cancel", event => {
  event.preventDefault();
  finishPortalReview({action: "cancel"});
});
$("#elaborati").onclick = openElaborati;
$("#elaborati-close").onclick = () => $("#elaborati-dialog").close();
$("#elaborati-form").onsubmit = event => event.preventDefault();
$("#elaborati-refresh").onclick = loadElaborati;
$("#elaborati-account-button").onclick = toggleElaboratiAccount;
$("#elaborati-list").addEventListener("click", event => {
  const button = event.target.closest("button[data-elaborato]");
  if (button) changeElaborato(button);
});
$("#cancel-config").onclick = () => $("#cpu-dialog").close();
$("#cancel-environment").onclick = () => $("#environment-dialog").close();
$("#import-environment").onclick = () => $("#environment-import-file").click();
$("#environment-import-file").onchange = async event => {
  const input = event.target;
  const file = input.files?.[0];
  if (!file) return;
  try {
    const result = await api("/api/environment/import", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({filename: file.name, content: await file.text()})
    });
    fillEnvironmentForm(result);
    lastNormalLog = `Imported settings from ${file.name}. Review them, then select Save settings.`;
    lastAdvancedLog = lastNormalLog;
    updateLog();
  } catch (error) {
    await showActionMessage("Import settings", error.message);
  } finally {
    input.value = "";
  }
};
$("#cpu-form").onsubmit = async event => {
  event.preventDefault();
  if (!current) return;
  const config = {
    cpu: $("#cpu-model").value,
    intAlu: Number($("#int-alu").value),
    intMul: Number($("#int-mul").value),
    intDiv: Number($("#int-div").value),
    floatAlu: Number($("#float-alu").value),
    floatMul: Number($("#float-mul").value),
    floatDiv: Number($("#float-div").value),
    floatingPointPrecision: $("#floating-point-precision").value,
    intAluPipelined: $("#int-alu-pipelined").checked,
    intMulPipelined: $("#int-mul-pipelined").checked,
    intDivPipelined: $("#int-div-pipelined").checked,
    floatAluPipelined: $("#float-alu-pipelined").checked,
    floatMulPipelined: $("#float-mul-pipelined").checked,
    floatDivPipelined: $("#float-div-pipelined").checked,
    forwarding: $("#forwarding").checked,
    compressedInstructions: $("#compressed-instructions").checked,
    memoryMode: $("#memory-mode").value,
    cacheStalls: $("#memory-mode").value === "cache",
    instructionMemoryLatency: Number($("#instruction-memory-latency").value),
    dataReadLatency: Number($("#data-read-latency").value),
    dataWriteLatency: Number($("#data-write-latency").value),
    iCacheSize: $("#i-cache-size").value,
    dCacheSize: $("#d-cache-size").value,
    cacheLine: Number($("#cache-line").value),
    cacheLatency: Number($("#cache-latency").value),
    memoryLatency: Number($("#memory-latency").value),
    fetchWidth: Number($("#fetch-width").value),
    decodeWidth: Number($("#decode-width").value),
    renameWidth: Number($("#rename-width").value),
    dispatchWidth: Number($("#dispatch-width").value),
    issueWidth: Number($("#issue-width").value),
    writebackWidth: Number($("#writeback-width").value),
    commitWidth: Number($("#commit-width").value),
    intAluUnits: Number($("#int-alu-units").value),
    intMulUnits: Number($("#int-mul-units").value),
    intDivUnits: Number($("#int-div-units").value),
    floatAluUnits: Number($("#float-alu-units").value),
    floatMulUnits: Number($("#float-mul-units").value),
    floatDivUnits: Number($("#float-div-units").value),
    memoryUnits: Number($("#memory-units").value),
    robEntries: Number($("#rob-entries").value),
    iqEntries: Number($("#iq-entries").value),
    lqEntries: Number($("#lq-entries").value),
    sqEntries: Number($("#sq-entries").value),
    branchPredictor: $("#cpu-model").value === "out-of-order"
      ? $("#o3-branch-predictor").value : "local",
    branchPredictorParameters: predictorParameterDraft,
    speculativeExecution: $("#speculative-execution").checked,
    outOfOrderExecution: $("#out-of-order-execution").checked,
    o3EvaluationLabel: $("#o3-evaluation-label").value.trim(),
    o3VisibleStages: [...document.querySelectorAll("[data-o3-stage]:checked")]
      .map(input => input.dataset.o3Stage)
  };
  try {
    await api("/api/config", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: current.name, config})
    });
    const updatedProject = await api("/api/project?name=" + encodeURIComponent(current.name));
    current.isa = updatedProject.isa || current.isa;
    closeOpcodePopup();
    $("#cpu-dialog").close();
    lastNormalLog = "CPU configuration saved. It will be used on the next Run.";
    lastAdvancedLog = lastNormalLog;
    updateLog();
    showTab("output");
  } catch (error) {
    await showActionMessage("CPU configuration", error.message);
  }
};
$("#environment-form").onsubmit = async event => {
  event.preventDefault();
  const values = environmentFormValues();
  const cycleLimitText = values.PIPELINE_DISPLAY_CYCLE_LIMIT.trim();
  const cycleLimit = Number(cycleLimitText);
  if (!/^[0-9]+$/.test(cycleLimitText)
      || !Number.isInteger(cycleLimit) || cycleLimit < 100 || cycleLimit > 20000) {
    const message = "Maximum visible pipeline cycles must be a whole number from 100 to 20,000. Higher values can use excessive browser memory; larger traces are saved automatically as a complete CSV instead.";
    setEnvironmentValidation("PIPELINE_DISPLAY_CYCLE_LIMIT", "invalid", message);
    await showActionMessage("Maximum pipeline cycles", message);
    $("#env-pipeline-cycle-limit").focus();
    return;
  }
  const programsDirectoryChanged = values.PROGRAMS_DIRECTORY.trim()
    !== configuredProgramsDirectory.trim();
  if (programsDirectoryChanged
      && !await resolveUnsavedProject("change the programs folder")) return;
  try {
    const settings = await api("/api/environment", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({values})
    });
    configuredProgramsDirectory = settings.values?.PROGRAMS_DIRECTORY || "";
    fillEnvironmentForm(settings);
    $("#environment-dialog").close();
    if (programsDirectoryChanged) {
      clearCurrentProject();
      await loadProjects();
    }
    lastNormalLog = programsDirectoryChanged
      ? `Settings saved. Projects are now loaded from ${configuredProgramsDirectory}.`
      : "Settings saved. They will be used by the next build and simulation.";
    lastAdvancedLog = lastNormalLog;
    updateLog();
    showTab("output");
  } catch (error) {
    await showActionMessage("Settings", error.message);
  }
};
$("#search").oninput = event => {
  const query = event.target.value.trim().toLowerCase();
  const source = $("#body").value.toLowerCase();
  searchMatchIndex = query ? source.indexOf(query) : -1;
  searchSourceLine = searchMatchIndex >= 0 ? source.slice(0, searchMatchIndex).split("\n").length : null;
  syncEditor();
  if (searchSourceLine) {
    scrollEditorToLine(searchSourceLine);
  }
};

async function checkForUpdate() {
  try {
    const status = await api("/api/update-status");
    const offered = status.available || Boolean(status.appRelease);
    $("#update").hidden = !offered;
    $("#update").disabled = !offered;
    $("#update").title = status.message;
  } catch (_error) {
    $("#update").hidden = true;
  }
}

$("#update").onclick = async () => {
  $("#update").disabled = true;
  try {
    const status = await api("/api/update-status");
    if (status.appRelease) {
      const choice = await actionDialog({
        title: "New ASE Studio app",
        message: "ASE Studio " + status.appRelease.version + " is available. Besides the interface, it can update "
          + "Python, the RISC-V compiler and the app window.\n\nDownload the new disk image, quit ASE Studio and "
          + "replace the app in Applications. Your projects are kept.",
        confirmLabel: "Download", confirmValue: "download",
        alternateLabel: status.available ? "Update components only" : null, alternateValue: "components",
        cancelLabel: "Cancel"
      });
      if (choice === "download") window.open(status.appRelease.url, "_blank", "noopener");
      if (choice !== "components") {
        await checkForUpdate();
        return;
      }
    }
    let discardLocalChanges = false;
    const blocked = (status.repositories || []).filter(repository => repository.dirty);
    if (blocked.length) {
      const details = blocked.map(repository => {
        const changes = (repository.changes || []).slice(0, 12).join("\n");
        const remainder = (repository.changes || []).length > 12
          ? `\n… and ${repository.changes.length - 12} more` : "";
        return `${repository.label}:\n${changes || "Local tracked changes"}${remainder}`;
      }).join("\n\n");
      const choice = await actionDialog({
        title: "Local changes block update",
        message: `The following tracked files would be overwritten by the update:\n\n${details}\n\nYou can open an issue to discuss preserving these changes, or discard them and align the local repositories with their remote branches. Untracked projects are not deleted.`,
        confirmLabel: "Discard and update",
        confirmValue: "discard",
        alternateLabel: "Open issue",
        alternateValue: "issue",
        cancelLabel: "Cancel",
        danger: true
      });
      if (choice === "issue") {
        const title = encodeURIComponent("Help updating ASE Studio with local changes");
        const body = encodeURIComponent(`ASE Studio detected local tracked changes before updating:\n\n${details}\n\nPlease advise how these changes should be preserved or integrated.`);
        window.open(`https://github.com/cad-polito-it/ase-studio/issues/new?title=${title}&body=${body}`, "_blank", "noopener");
        await checkForUpdate();
        return;
      }
      if (choice !== "discard") {
        await checkForUpdate();
        return;
      }
      discardLocalChanges = true;
    }
    lastNormalLog = discardLocalChanges
      ? "Discarding selected local tracked changes and updating repositories…"
      : "Updating repositories…";
    lastAdvancedLog = lastNormalLog;
    updateLog();
    showTab("output");
    const result = await api("/api/update", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({discardLocalChanges})
    });
    lastNormalLog = result.output;
    lastAdvancedLog = result.advancedOutput || result.output;
    updateLog();
    if (result.restartRequired) {
      // Files on disk now belong to the new release, but this page and Python
      // process still execute the old release. Do not update again until the
      // application has restarted.
      $("#update").hidden = true;
      $("#update").disabled = true;
      $("#update").title = "Restart ASE Studio to finish the update.";
      await showActionMessage("Update completed",
        "Restart ASE Studio now to load the updated simulator and interface.");
      return;
    }
    if (!result.ok) {
      await showActionMessage("Update failed",
        result.output || "ASE Studio could not complete the repository update.");
    }
  } catch (error) {
    lastNormalLog = `Repository update failed\n${error.message}`;
    lastAdvancedLog = lastNormalLog;
    updateLog();
    await showActionMessage("Update failed", error.message);
  }
  await checkForUpdate();
};
document.addEventListener("keydown", event => {
  if (!event.ctrlKey && !event.metaKey) return;
  const key = event.key.toLowerCase();
  if (key === "z" || event.code === "KeyZ") {
    event.preventDefault();
    event.stopPropagation();
    if (event.shiftKey) redoEditor();
    else undoEditor();
    return;
  }
  if (key === "y" || event.code === "KeyY") {
    event.preventDefault();
    event.stopPropagation();
    redoEditor();
    return;
  }
  if (key === "s" || event.code === "KeyS") {
    event.preventDefault();
    event.stopPropagation();
    save();
  }
  if (key === "f" || event.code === "KeyF") {
    event.preventDefault();
    event.stopPropagation();
    $("#search").focus();
  }
}, true);
window.aseHasUnsavedChanges = () => sourceDirty;
window.addEventListener("beforeunload", writeLocalDraft);

const splitter = $("#splitter");
splitter.addEventListener("pointerdown", event => {
  event.preventDefault();
  splitter.classList.add("dragging");
  splitter.setPointerCapture(event.pointerId);
});
splitter.addEventListener("pointermove", event => {
  if (!splitter.hasPointerCapture(event.pointerId)) return;
  const minimum = 180;
  const maximum = Math.max(minimum, window.innerHeight - 230);
  const height = Math.max(minimum, Math.min(maximum, window.innerHeight - event.clientY));
  document.documentElement.style.setProperty("--bottom-height", height + "px");
});
splitter.addEventListener("pointerup", event => {
  splitter.releasePointerCapture(event.pointerId);
  splitter.classList.remove("dragging");
  if (pipelineData) renderPipeline();
});

const registerResizer = $("#register-resizer");
registerResizer.addEventListener("pointerdown", event => {
  event.preventDefault();
  registerResizer.classList.add("dragging");
  registerResizer.setPointerCapture(event.pointerId);
});
registerResizer.addEventListener("pointermove", event => {
  if (!registerResizer.hasPointerCapture(event.pointerId)) return;
  const width = Math.max(220, Math.min(window.innerWidth * 0.45, window.innerWidth - event.clientX));
  document.documentElement.style.setProperty("--register-width", width + "px");
});
registerResizer.addEventListener("pointerup", event => {
  registerResizer.releasePointerCapture(event.pointerId);
  registerResizer.classList.remove("dragging");
});

function pipelineScrollbarMetrics(viewport, extent, position, trackLength) {
  const limit = Math.max(0, extent - viewport);
  const length = Math.min(trackLength, Math.max(24,
    extent > 0 ? trackLength * viewport / extent : trackLength));
  const travel = Math.max(0, trackLength - length);
  const offset = limit ? travel * Math.max(0, Math.min(limit, position)) / limit : 0;
  return {limit, length, travel, offset};
}

function pipelineScrollbarsNeeded(width, height, contentWidth, contentHeight, size) {
  let horizontal = false;
  let vertical = false;
  // An overflowing axis reduces the other axis's available space. Start with
  // the full frame each time so a resize cannot leave unnecessary bars behind.
  for (let pass = 0; pass < 3; pass++) {
    horizontal = contentWidth > width - (vertical ? size : 0) + 1;
    vertical = contentHeight > height - (horizontal ? size : 0) + 1;
  }
  return {horizontal: width > 0 && horizontal, vertical: height > 0 && vertical};
}

function installPipelineScrollControls(scroll, buttons, getStep, tracks = []) {
  const frame = scroll.parentElement;
  const scrollbarSize = 18;
  const integrated = tracks.length === 2;
  if (integrated) frame.classList.add("scrollbars-ready");
  const axes = {
    horizontal: ["scrollLeft", "scrollWidth", "clientWidth", "clientX", "left", "width", "left", "right"],
    vertical: ["scrollTop", "scrollHeight", "clientHeight", "clientY", "top", "height", "up", "down"]
  };
  const directions = {
    left: ["scrollLeft", "scrollWidth", "clientWidth", -1],
    right: ["scrollLeft", "scrollWidth", "clientWidth", 1],
    up: ["scrollTop", "scrollHeight", "clientHeight", -1],
    down: ["scrollTop", "scrollHeight", "clientHeight", 1]
  };
  function update() {
    if (integrated) {
      const needed = pipelineScrollbarsNeeded(frame.clientWidth, frame.clientHeight,
        scroll.scrollWidth, scroll.scrollHeight, scrollbarSize);
      frame.style.setProperty("--pipeline-scrollbar-x", needed.horizontal ? scrollbarSize + "px" : "0px");
      frame.style.setProperty("--pipeline-scrollbar-y", needed.vertical ? scrollbarSize + "px" : "0px");
      for (const track of tracks) {
        track.parentElement.hidden = !needed[track.dataset.pipelineScrollAxis];
      }
    }
    for (const button of buttons) {
      const [position, extent, viewport, sign] = directions[button.dataset.pipelineScroll];
      const limit = Math.max(0, scroll[extent] - scroll[viewport]);
      button.disabled = scroll[viewport] === 0 || (sign < 0
        ? scroll[position] <= 0 : scroll[position] >= limit - 1);
    }
    for (const track of tracks) {
      const [position, extent, viewport, , offset, length] = axes[track.dataset.pipelineScrollAxis];
      const metrics = pipelineScrollbarMetrics(scroll[viewport], scroll[extent],
        scroll[position], track[viewport]);
      const thumb = track.firstElementChild;
      thumb.style[length] = metrics.length + "px";
      thumb.style[offset] = metrics.offset + "px";
      thumb.setAttribute("aria-valuemax", String(Math.round(metrics.limit)));
      thumb.setAttribute("aria-valuenow", String(Math.round(
        Math.max(0, Math.min(metrics.limit, scroll[position])))));
    }
  }
  for (const button of buttons) {
    const direction = button.dataset.pipelineScroll;
    let repeatTimer = null;
    let activePointer = null;
    let repeated = false;
    function move() {
      if (button.disabled) return;
      const [position, extent, viewport, sign] = directions[direction];
      const limit = Math.max(0, scroll[extent] - scroll[viewport]);
      scroll[position] = Math.max(0, Math.min(limit,
        scroll[position] + sign * getStep(direction)));
      update();
    }
    function stop() {
      clearTimeout(repeatTimer);
      repeatTimer = null;
      if (activePointer !== null && button.hasPointerCapture(activePointer)) {
        button.releasePointerCapture(activePointer);
      }
      activePointer = null;
    }
    function repeat() {
      repeated = true;
      move();
      if (!button.disabled) repeatTimer = setTimeout(repeat, 70);
    }
    button.addEventListener("click", event => {
      // Pointer holds already scrolled; keyboard and ordinary clicks move once.
      if (!repeated || event.detail === 0) move();
      repeated = false;
    });
    button.addEventListener("pointerdown", event => {
      if (event.button !== 0 || button.disabled) return;
      stop();
      repeated = false;
      activePointer = event.pointerId;
      button.setPointerCapture(activePointer);
      repeatTimer = setTimeout(repeat, 350);
    });
    button.addEventListener("pointerup", stop);
    button.addEventListener("pointercancel", stop);
    button.addEventListener("lostpointercapture", stop);
    window.addEventListener("blur", stop);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stop();
    });
  }
  for (const track of tracks) {
    const [position, extent, viewport, coordinate, offset, , backward, forward] = axes[track.dataset.pipelineScrollAxis];
    const thumb = track.firstElementChild;
    let drag = null;
    function moveTo(value) {
      scroll[position] = Math.max(0, Math.min(scroll[extent] - scroll[viewport], value));
      update();
    }
    function stopDrag() {
      const pointerId = drag?.pointerId;
      drag = null;
      thumb.classList.remove("dragging");
      if (pointerId !== undefined && track.hasPointerCapture(pointerId)) {
        track.releasePointerCapture(pointerId);
      }
    }
    track.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      event.preventDefault();
      thumb.focus({preventScroll: true});
      if (event.target === thumb) {
        drag = {pointerId: event.pointerId, coordinate: event[coordinate], position: scroll[position]};
        track.setPointerCapture(event.pointerId);
        thumb.classList.add("dragging");
      } else {
        const metrics = pipelineScrollbarMetrics(scroll[viewport], scroll[extent],
          scroll[position], track[viewport]);
        const before = event[coordinate] - track.getBoundingClientRect()[offset] < metrics.offset;
        moveTo(scroll[position] + (before ? -1 : 1) * scroll[viewport]);
      }
    });
    track.addEventListener("pointermove", event => {
      if (!drag || drag.pointerId !== event.pointerId) return;
      const metrics = pipelineScrollbarMetrics(scroll[viewport], scroll[extent],
        scroll[position], track[viewport]);
      if (metrics.travel) moveTo(drag.position
        + (event[coordinate] - drag.coordinate) * metrics.limit / metrics.travel);
    });
    track.addEventListener("pointerup", stopDrag);
    track.addEventListener("pointercancel", stopDrag);
    track.addEventListener("lostpointercapture", stopDrag);
    window.addEventListener("blur", stopDrag);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopDrag();
    });
    thumb.addEventListener("keydown", event => {
      const keys = track.dataset.pipelineScrollAxis === "horizontal"
        ? {ArrowLeft: -getStep(backward), ArrowRight: getStep(forward)}
        : {ArrowUp: -getStep(backward), ArrowDown: getStep(forward)};
      keys.PageUp = -scroll[viewport];
      keys.PageDown = scroll[viewport];
      let target;
      if (event.key === "Home") target = 0;
      else if (event.key === "End") target = scroll[extent] - scroll[viewport];
      else if (Object.hasOwn(keys, event.key)) target = scroll[position] + keys[event.key];
      else return;
      event.preventDefault();
      moveTo(target);
    });
    // Scroll-wheel events over the replacement track still reach the viewport.
    track.parentElement.addEventListener("wheel", event => {
      const scale = event.deltaMode === 1 ? 27
        : event.deltaMode === 2 ? scroll[viewport] : 1;
      const delta = track.dataset.pipelineScrollAxis === "horizontal"
        ? event.deltaX || event.deltaY : event.deltaY;
      if (!delta) return;
      event.preventDefault();
      moveTo(scroll[position] + delta * scale);
    }, {passive: false});
  }
  scroll.addEventListener("scroll", update, {passive: true});
  if ("ResizeObserver" in window) {
    let resizePending = false;
    const observer = new ResizeObserver(() => {
      // Changing scrollbar visibility changes the observed viewport size.
      // Defer that layout change to avoid a ResizeObserver delivery loop.
      if (resizePending) return;
      resizePending = true;
      window.requestAnimationFrame(() => {
        resizePending = false;
        update();
      });
    });
    observer.observe(scroll);
    observer.observe(scroll.firstElementChild);
    if (integrated) observer.observe(frame);
  }
  update();
  return update;
}

function pipelinePointerOnScrollbar(scroll, event) {
  const bounds = scroll.getBoundingClientRect();
  const x = event.clientX - bounds.left;
  const y = event.clientY - bounds.top;
  return x < scroll.clientLeft || x >= scroll.clientLeft + scroll.clientWidth
    || y < scroll.clientTop || y >= scroll.clientTop + scroll.clientHeight;
}

const pipelineScroll = $("#pipeline-scroll");
const updatePipelineScrollButtons = installPipelineScrollControls(
  pipelineScroll, document.querySelectorAll("[data-pipeline-scroll]"),
  direction => direction === "left" || direction === "right"
    ? Number($("#cell-width").value) : 27,
  document.querySelectorAll("[data-pipeline-scroll-axis]"));
let pipelinePan = null;
pipelineScroll.addEventListener("pointerdown", event => {
  if (event.button !== 0 && event.button !== 1) return;
  if (pipelinePointerOnScrollbar(pipelineScroll, event)) return;
  if (event.button === 0 && event.target.closest(".jump-arrow")) return;
  if (event.button === 1) event.preventDefault();
  pipelinePan = {
    pointerId: event.pointerId,
    button: event.button,
    x: event.clientX,
    y: event.clientY,
    left: pipelineScroll.scrollLeft,
    top: pipelineScroll.scrollTop,
    target: event.target,
    moved: false
  };
  pipelineScroll.setPointerCapture(event.pointerId);
});
pipelineScroll.addEventListener("pointermove", event => {
  if (!pipelinePan || event.pointerId !== pipelinePan.pointerId) return;
  const distance = Math.hypot(event.clientX - pipelinePan.x, event.clientY - pipelinePan.y);
  if (!pipelinePan.moved && distance < 3) return;
  pipelinePan.moved = true;
  pipelineScroll.classList.add("panning");
  pipelineScroll.scrollLeft = pipelinePan.left - (event.clientX - pipelinePan.x);
  pipelineScroll.scrollTop = pipelinePan.top - (event.clientY - pipelinePan.y);
});
function finishPipelinePan(event) {
  if (!pipelinePan || event.pointerId !== pipelinePan.pointerId) return;
  if (pipelineScroll.hasPointerCapture(event.pointerId)) pipelineScroll.releasePointerCapture(event.pointerId);
  pipelinePan = null;
  pipelineScroll.classList.remove("panning");
}
pipelineScroll.addEventListener("pointerup", finishPipelinePan);
pipelineScroll.addEventListener("pointercancel", finishPipelinePan);
pipelineScroll.addEventListener("auxclick", event => {
  if (event.button === 1) event.preventDefault();
});

window.addEventListener("focus", synchronizeExternalSource);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) synchronizeExternalSource();
});
setInterval(synchronizeExternalSource, 1000);

loadStudioVersion();
loadProjects().then(checkForUpdate);
