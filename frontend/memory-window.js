/*
 * SPDX-FileCopyrightText: 2026 Behnam Farnaghinejad <behnam.farnaghinejad@polito.it>
 * SPDX-License-Identifier: GPL-2.0-only
 */
const $ = selector => document.querySelector(selector);
const channel = "BroadcastChannel" in window
  ? new BroadcastChannel("ase-studio-memory-window") : null;
let changeContextMenu = null;

function send(action, details = {}) {
  if (channel) channel.postMessage({type: "memory-action", action, ...details});
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
      send("navigate-change", {...item, direction});
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

function applyState(state) {
  document.documentElement.dataset.theme = state.theme || "light";
  document.title = state.project
    ? `ASE Studio — Memory — ${state.project}` : "ASE Studio — Memory";
  $("#memory-window-project").textContent = state.project || "No project is open";
  $("#memory-symbol").innerHTML = state.symbolOptionsHtml;
  $("#memory-symbol").disabled = state.symbolDisabled;
  $("#memory-symbol-form button").disabled = state.addDisabled;
  if ([...$("#memory-symbol").options].some(option => option.value === state.symbolValue)) {
    $("#memory-symbol").value = state.symbolValue;
  }
  $("#memory-format").value = state.format;
  $("#memory-watch-clear").disabled = state.clearDisabled;
  $("#memory-watch-list").innerHTML = state.watchesHtml;
  $("#memory-summary").textContent = state.summary;
  $("#memory-table").innerHTML = state.tableHtml;
  $("#memory-map-sections").innerHTML = state.mapHtml;
}

if (channel) {
  channel.onmessage = event => {
    if (event.data?.type === "memory-state") applyState(event.data);
  };
  channel.postMessage({type: "memory-request-state"});
} else {
  $("#memory-summary").textContent = "This WebKit version cannot synchronize a separate memory window.";
}

document.addEventListener("contextmenu", event => {
  const memory = event.target.closest("[data-memory-address]");
  if (!memory) return;
  openChangeContextMenu(event, {
    address: memory.dataset.memoryAddress,
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

$("#memory-symbol-form").onsubmit = event => {
  event.preventDefault();
  send("add", {name: $("#memory-symbol").value});
};
$("#memory-format").onchange = event => send("format", {value: event.target.value});
$("#memory-watch-clear").onclick = () => send("clear");
$("#memory-watch-list").onclick = event => {
  const button = event.target.closest("[data-memory-watch-remove]");
  if (button) send("remove", {index: Number(button.dataset.memoryWatchRemove)});
};
window.addEventListener("beforeunload", () => channel?.close());
