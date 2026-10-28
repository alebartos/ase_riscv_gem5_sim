// SPDX-FileCopyrightText: 2026 Behnam Farnaghinejad <behnam.farnaghinejad@polito.it>
// SPDX-License-Identifier: GPL-2.0-only
// Run with: gjs ase_studio/tests/test_pipeline_scroll.js
const GLib = imports.gi.GLib;
const filename = GLib.build_filenamev([
  GLib.path_get_dirname(GLib.path_get_dirname(ARGV[0] || imports.system.programPath)),
  "frontend", "studio.js"
]);
const [, bytes] = GLib.file_get_contents(filename);
const source = new TextDecoder().decode(bytes);
new Function(source);
function equal(actual, expected) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}
class Target {
  constructor() {
    this.listeners = {};
    this.capture = null;
    this.attributes = {};
    this.style = {setProperty(name, value) { this[name] = value; }};
    const classes = new Set();
    this.classList = {
      add: name => classes.add(name), remove: name => classes.delete(name),
      contains: name => classes.has(name)
    };
  }
  addEventListener(name, callback) {
    (this.listeners[name] ||= []).push(callback);
  }
  emit(name, event = {}) {
    for (const callback of this.listeners[name] || []) callback(event);
  }
  setPointerCapture(id) { this.capture = id; }
  hasPointerCapture(id) { return this.capture === id; }
  releasePointerCapture() { this.capture = null; this.emit("lostpointercapture"); }
  setAttribute(name, value) { this.attributes[name] = value; }
  focus() { this.focused = true; }
}
let timerId = 0;
const timers = new Map();
const setTimer = callback => { timers.set(++timerId, callback); return timerId; };
function tick() {
  const [id, callback] = timers.entries().next().value;
  timers.delete(id);
  callback();
}
let observer;
class Observer {
  constructor(callback) { this.callback = callback; this.targets = []; observer = this; }
  observe(target) { this.targets.push(target); }
}
const fakeWindow = new Target();
fakeWindow.ResizeObserver = Observer;
fakeWindow.requestAnimationFrame = callback => callback();
const fakeDocument = new Target();
const helpers = source.slice(source.indexOf("function pipelineScrollbarMetrics("),
                             source.indexOf('const pipelineScroll = $("#pipeline-scroll");'));
const {installPipelineScrollControls, pipelinePointerOnScrollbar,
       pipelineScrollbarMetrics, pipelineScrollbarsNeeded} = new Function(
  "window", "document", "ResizeObserver", "setTimeout", "clearTimeout", helpers
  + "return {installPipelineScrollControls, pipelinePointerOnScrollbar, pipelineScrollbarMetrics, pipelineScrollbarsNeeded};"
)(fakeWindow, fakeDocument, Observer, setTimer, id => timers.delete(id));
const scroll = Object.assign(new Target(), {
  scrollLeft: 0, scrollTop: 0, scrollWidth: 1200, scrollHeight: 1000,
  clientWidth: 600, clientHeight: 300, clientLeft: 0, clientTop: 0,
  firstElementChild: {}, getBoundingClientRect: () => ({left: 10, top: 20})
});
const buttons = Object.fromEntries(["left", "right", "up", "down"].map(direction => [
  direction, Object.assign(new Target(), {dataset: {pipelineScroll: direction}})
]));
let cellWidth = 42;
const update = installPipelineScrollControls(scroll, Object.values(buttons),
  direction => ["left", "right"].includes(direction) ? cellWidth : 27);
equal(Object.values(buttons).map(button => button.disabled), [true, false, true, false]);
equal(observer.targets, [scroll, scroll.firstElementChild]);
buttons.right.emit("click", {detail: 1});
buttons.down.emit("click", {detail: 1});
equal([scroll.scrollLeft, scroll.scrollTop], [42, 27]);
buttons.left.emit("click", {detail: 0}); // Keyboard activation.
buttons.up.emit("click", {detail: 1});
equal([scroll.scrollLeft, scroll.scrollTop], [0, 0]);
cellWidth = 64;
buttons.right.emit("click", {detail: 1});
equal(scroll.scrollLeft, 64);
buttons.right.emit("pointerdown", {button: 0, pointerId: 1});
tick(); tick();
equal(scroll.scrollLeft, 192);
buttons.right.emit("pointerup");
equal(timers.size, 0);
buttons.right.emit("click", {detail: 1}); // No extra movement on release.
equal(scroll.scrollLeft, 192);
buttons.right.emit("click", {detail: 0});
equal(scroll.scrollLeft, 256);
for (const stopEvent of ["pointercancel", "lostpointercapture"]) {
  buttons.down.emit("pointerdown", {button: 0, pointerId: 2});
  buttons.down.emit(stopEvent);
  equal(timers.size, 0);
}
buttons.down.emit("pointerdown", {button: 0, pointerId: 3});
fakeWindow.emit("blur");
equal(timers.size, 0);
buttons.down.emit("pointerdown", {button: 0, pointerId: 4});
fakeDocument.hidden = true;
fakeDocument.emit("visibilitychange");
equal(timers.size, 0);
scroll.scrollLeft = 590;
scroll.scrollTop = 695;
scroll.emit("scroll");
buttons.right.emit("click", {detail: 1});
buttons.down.emit("click", {detail: 1});
equal([scroll.scrollLeft, scroll.scrollTop], [600, 700]);
equal([buttons.right.disabled, buttons.down.disabled], [true, true]);
// Resizing, resetting, and emptying the virtual grid refresh availability.
scroll.clientWidth = 1200;
scroll.clientHeight = 1000;
scroll.scrollLeft = 0;
scroll.scrollTop = 0;
observer.callback();
equal(Object.values(buttons).every(button => button.disabled), true);
scroll.clientWidth = 0;
scroll.clientHeight = 0;
update();
equal(Object.values(buttons).every(button => button.disabled), true);
scroll.clientWidth = 600;
scroll.clientHeight = 300;
equal(pipelinePointerOnScrollbar(scroll, {clientX: 609, clientY: 319}), false);
equal(pipelinePointerOnScrollbar(scroll, {clientX: 611, clientY: 40}), true);
equal(pipelinePointerOnScrollbar(scroll, {clientX: 30, clientY: 321}), true);
equal(pipelineScrollbarMetrics(600, 1200, 300, 400),
  {limit: 600, length: 200, travel: 200, offset: 100});
equal(pipelineScrollbarMetrics(100, 100000, 0, 400).length, 24);
equal(pipelineScrollbarMetrics(100, 100, 0, 200),
  {limit: 0, length: 200, travel: 0, offset: 0});
equal(pipelineScrollbarMetrics(0, 0, 0, 0),
  {limit: 0, length: 0, travel: 0, offset: 0});
equal(pipelineScrollbarsNeeded(600, 300, 600, 300, 18),
  {horizontal: false, vertical: false});
equal(pipelineScrollbarsNeeded(600, 300, 590, 600, 18),
  {horizontal: true, vertical: true}); // Vertical track makes horizontal overflow.
equal(pipelineScrollbarsNeeded(600, 300, 900, 290, 18),
  {horizontal: true, vertical: true}); // And vice versa.
equal(pipelineScrollbarsNeeded(0, 0, 900, 600, 18),
  {horizontal: false, vertical: false});

// Replacement bars have a thumb between the existing arrows, not a second
// strip next to a native scrollbar. Both always reflect the real viewport.
const integratedScroll = Object.assign(new Target(), {
  scrollLeft: 0, scrollTop: 0, scrollWidth: 1200, scrollHeight: 1000,
  clientWidth: 600, clientHeight: 300, firstElementChild: {},
  parentElement: Object.assign(new Target(), {clientWidth: 618, clientHeight: 318})
});
function makeTrack(axis) {
  return Object.assign(new Target(), {
    dataset: {pipelineScrollAxis: axis}, clientWidth: 400, clientHeight: 240,
    firstElementChild: new Target(), parentElement: new Target(),
    getBoundingClientRect: () => ({left: 10, top: 20})
  });
}
const horizontal = makeTrack("horizontal");
const vertical = makeTrack("vertical");
const updateIntegrated = installPipelineScrollControls(integratedScroll, [],
  direction => ["left", "right"].includes(direction) ? 42 : 27, [horizontal, vertical]);
equal(integratedScroll.parentElement.classList.contains("scrollbars-ready"), true);
equal(integratedScroll.parentElement.style["--pipeline-scrollbar-x"], "18px");
equal(integratedScroll.parentElement.style["--pipeline-scrollbar-y"], "18px");
equal(horizontal.firstElementChild.style.width, "200px");
equal(vertical.firstElementChild.style.height, "72px");
equal(horizontal.firstElementChild.attributes["aria-valuemax"], "600");
let prevented = 0;
function event(extra) { return {preventDefault() { prevented++; }, ...extra}; }
horizontal.emit("pointerdown", event({button: 0, pointerId: 10, clientX: 10,
  target: horizontal.firstElementChild}));
horizontal.emit("pointermove", event({pointerId: 10, clientX: 110}));
equal(integratedScroll.scrollLeft, 300);
equal(horizontal.firstElementChild.style.left, "100px");
equal(horizontal.firstElementChild.attributes["aria-valuenow"], "300");
horizontal.emit("pointerup");
equal(horizontal.capture, null);
equal(horizontal.firstElementChild.classList.contains("dragging"), false);
horizontal.emit("pointerdown", event({button: 0, clientX: 30, target: horizontal}));
equal(integratedScroll.scrollLeft, 0); // Track clicks page backward/forward.
horizontal.emit("pointerdown", event({button: 0, clientX: 350, target: horizontal}));
equal(integratedScroll.scrollLeft, 600);
horizontal.firstElementChild.emit("keydown", event({key: "Home"}));
horizontal.firstElementChild.emit("keydown", event({key: "ArrowRight"}));
equal(integratedScroll.scrollLeft, 42);
horizontal.firstElementChild.emit("keydown", event({key: "End"}));
equal(integratedScroll.scrollLeft, 600);
vertical.firstElementChild.emit("keydown", event({key: "ArrowDown"}));
equal(integratedScroll.scrollTop, 27);
vertical.firstElementChild.emit("keydown", event({key: "PageDown"}));
equal(integratedScroll.scrollTop, 327);
vertical.parentElement.emit("wheel", event({deltaX: 0, deltaY: 2, deltaMode: 1}));
equal(integratedScroll.scrollTop, 381);
horizontal.parentElement.emit("wheel", event({deltaX: -20, deltaY: 0, deltaMode: 0}));
equal(integratedScroll.scrollLeft, 580);
vertical.emit("pointerdown", event({button: 0, pointerId: 11, clientY: 20,
  target: vertical.firstElementChild}));
vertical.emit("pointermove", event({pointerId: 11, clientY: 1000}));
equal(integratedScroll.scrollTop, 700); // Clamp dragging at the boundary.
fakeWindow.emit("blur");
equal(vertical.capture, null);
const before = integratedScroll.scrollTop;
vertical.emit("pointermove", event({pointerId: 11, clientY: 0}));
equal(integratedScroll.scrollTop, before);
integratedScroll.parentElement.clientWidth = 1400;
integratedScroll.parentElement.clientHeight = 1200;
updateIntegrated();
equal([horizontal.parentElement.hidden, vertical.parentElement.hidden], [true, true]);
equal(integratedScroll.parentElement.style["--pipeline-scrollbar-x"], "0px");
equal(integratedScroll.parentElement.style["--pipeline-scrollbar-y"], "0px");
equal(prevented > 0, true);
print("Pipeline scrolling: integrated thumbs/tracks, arrows, hold/release, keyboard, wheel, resize and bounds passed.");
