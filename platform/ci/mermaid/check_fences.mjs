// Parse documentation diagrams with mermaid itself.
//
// The published site renders ```mermaid fences in the reader's browser, so an invalid diagram
// costs nothing at build time and shows the reader a red error box. Two of them survived months
// that way. `mermaid.parse` is the same grammar the browser runs, which is why this check uses
// the library rather than a pattern of its own: a hand-written approximation would both miss
// real breakage and reject valid diagrams, and a checker that cries wolf gets switched off.
//
// mermaid is a browser library, so it is given a DOM. Input arrives as JSON on stdin —
// [{file, line, text}, …] — and the verdict leaves as JSON on stdout.

import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { pretendToBeVisual: true });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
// `navigator` is a getter-only global in Node 21+, so plain assignment throws.
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
for (const name of ["Element", "SVGElement", "HTMLElement", "DOMParser", "Node"]) {
  globalThis[name] = dom.window[name];
}

const mermaid = (await import("mermaid")).default;
mermaid.initialize({ startOnLoad: false });

const read = async (stream) => {
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
};

const fences = JSON.parse(await read(process.stdin));
const failures = [];

for (const fence of fences) {
  try {
    await mermaid.parse(fence.text);
  } catch (error) {
    // mermaid's parse errors carry the offending line and a caret diagram; the first line names
    // the problem and the rest is noise in a CI log.
    const message = String(error?.message ?? error)
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .slice(0, 2)
      .join(" — ");
    failures.push({ file: fence.file, line: fence.line, error: message });
  }
}

process.stdout.write(JSON.stringify({ checked: fences.length, failures }));
process.exit(failures.length ? 1 : 0);
