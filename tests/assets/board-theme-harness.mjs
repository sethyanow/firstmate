// Run a built bearings board's theme script under a minimal browser shim and
// print the theme the page ends up in.
//
// Usage: node board-theme-harness.mjs <built-board.html> <os: dark|light> <stored: none|light|dark> [click: light|system|dark]
// Prints one JSON document: { theme, pressed, stored, osFollowed }
//   theme       the data-theme the page resolved to
//   pressed     the toggle choice shown as pressed
//   stored      what the page left in localStorage (null when nothing)
//   osFollowed  theme after the OS setting flips to the opposite value
import { readFileSync } from "node:fs";

const [, , file, os, stored, click] = process.argv;
const html = readFileSync(file, "utf8");
const script = html.split('<script id="bearings-theme">')[1].split("</script>")[0];

const store = new Map(stored === "none" ? [] : [["bearings-board-theme", stored]]);
const attrs = {};
const buttons = ["light", "system", "dark"].map((choice) => {
  const b = { choice, pressed: "false", handlers: [] };
  b.getAttribute = (k) => (k === "data-theme-choice" ? choice : b.pressed);
  b.setAttribute = (k, v) => { if (k === "aria-pressed") b.pressed = v; };
  b.addEventListener = (_, fn) => b.handlers.push(fn);
  return b;
});
let ready = [];
let osListeners = [];
const query = { matches: os === "dark", addEventListener: (_, fn) => osListeners.push(fn) };

globalThis.window = {
  matchMedia: () => query,
  localStorage: {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, v),
    removeItem: (k) => store.delete(k),
  },
};
globalThis.document = {
  documentElement: { setAttribute: (k, v) => { attrs[k] = v; } },
  querySelectorAll: () => buttons,
  addEventListener: (_, fn) => ready.push(fn),
};
new Function(script)();
ready.forEach((fn) => fn());
if (click) buttons.find((b) => b.choice === click).handlers.forEach((fn) => fn());

const theme = attrs["data-theme"];
const pressed = buttons.find((b) => b.pressed === "true")?.choice ?? null;
const savedStored = store.has("bearings-board-theme") ? store.get("bearings-board-theme") : null;
query.matches = os !== "dark";
osListeners.forEach((fn) => fn());
process.stdout.write(JSON.stringify({ theme, pressed, stored: savedStored, osFollowed: attrs["data-theme"] }) + "\n");
