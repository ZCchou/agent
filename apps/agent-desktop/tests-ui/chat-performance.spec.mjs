import { expect, test } from "@playwright/test";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import { installTauriMock } from "./tauri-mock.mjs";

const repeats = Number(process.env.CHAT_PERF_REPEATS ?? 1);
const baseUrl = process.env.CHAT_PERF_URL ?? "http://127.0.0.1:5173";
const baseline = process.env.CHAT_PERF_PHASE === "baseline";
const reportPath = new URL("../../../.artifacts/reliability-repair/frontend-performance.json", import.meta.url);
const now = "2026-10-13T00:00:00+00:00";
const seed = "History sample\n\n```ts\nconst ready = true;\n```\n\n| Area | State |\n| --- | --- |\n| Chat | Ready |\n\nFormula: $x^2 + y^2$.\n\n";
const content = seed + "Synthetic history text. ".repeat(50).slice(0, 1024 - seed.length);

function fixture(count) {
  return {
    id: "performance-chat", kind: "project_work", title: "Performance history", category: null,
    resolved_category: "coding", chat_group_id: null, project_id: "project-1", sort_order: 0,
    derived_from_conversation_id: null, derived_through_message_id: null, derived_at: null,
    copied_from_conversation_id: null, copied_through_message_id: null, copied_at: null,
    provider_profile_id: null, model_id: null, created_at: now, updated_at: now, deleted_at: null,
    messages: Array.from({ length: count }, (_, index) => ({
      id: `history-${index}`, conversation_id: "performance-chat", role: index % 2 ? "assistant" : "user",
      content, event_type: null, source_message_id: null, created_at: now, attachments: [],
    })),
  };
}

async function installProbe(page, count) {
  await page.addInitScript(({ count }) => {
    const probe = window.__chatPerf = { enabled: false, rows: 0, parses: 0, commits: 0, initialMs: null };
    let previous = new WeakSet();
    window.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
      supportsFiber: true, renderers: new Map(), inject(renderer) { this.renderers.set(1, renderer); return 1; }, onCommitFiberUnmount() {},
      onCommitFiberRoot(_renderer, root) {
        const next = new WeakSet();
        function visit(fiber, historical = false, reused = false) {
          if (!fiber) return;
          const unchanged = reused || previous.has(fiber);
          next.add(fiber);
          const functional = typeof fiber.type === "function";
          const id = fiber.memoizedProps?.message?.id;
          const inHistory = historical || id?.startsWith("history-");
          // A bailout can reuse the previous child tree with old flags. Do not
          // count those fibers as work performed by the current commit.
          if (probe.enabled && !unchanged && (fiber.flags & 1)) {
            if (functional && id?.startsWith("history-")) probe.rows++;
            if (functional && fiber.memoizedProps?.remarkPlugins && inHistory) probe.parses++;
          }
          visit(fiber.child, inHistory, unchanged);
          visit(fiber.sibling, historical, reused);
        }
        visit(root.current);
        previous = next;
        if (probe.enabled) probe.commits++;
      },
    };
    const invoke = window.__TAURI_INTERNALS__.invoke;
    const listeners = new Map();
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "plugin:event|listen") {
        listeners.set(args.event, args.handler);
        return invoke(command, args);
      }
      if (command === "get_conversation_model_run") {
        return new Promise((resolve) => { probe.resume = resolve; });
      }
      return invoke(command, args);
    };
    probe.emit = (delta) => {
      const handler = listeners.get("model-run-event");
      if (!handler) throw new Error("Missing real model-run event subscription");
      window[`_${handler}`]({ event: "model-run-event", id: 1, payload: {
        run_id: "performance-run", event_type: "run.chunk", data: { delta },
      } });
    };
    document.addEventListener("click", (event) => {
      if (!event.target.closest("button")?.textContent.includes("Performance history")) return;
      const started = performance.now();
      const observer = new MutationObserver(() => {
        if (document.querySelectorAll("article.chat-message").length !== count) return;
        observer.disconnect();
        requestAnimationFrame(() => requestAnimationFrame(() => { probe.initialMs = performance.now() - started; }));
      });
      observer.observe(document, { childList: true, subtree: true });
    }, true);
  }, { count });
}

function percentile(values, fraction) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * fraction) - 1)];
}

async function recordMeasurements(count, samples, browser) {
  if (!process.env.CHAT_PERF_PHASE) return;
  let report = {};
  try { report = JSON.parse(await readFile(reportPath, "utf8")); } catch { /* First measurement. */ }
  const phase = baseline ? "baseline" : "after";
  report.environment = {
    platform: os.platform(), release: os.release(), cpu: os.cpus()[0].model,
    logicalCpus: os.cpus().length, memoryGiB: Math.round(os.totalmem() / 1024 ** 3),
    node: process.version, browser: browser.version(), mode: `${baseUrl.endsWith(":4173") ? "Vite production preview" : "Vite development, React StrictMode"}, headless Edge, 900x500`,
  };
  report.method = "Fixed alternating user/assistant rows, 1024 ASCII bytes each; assistant code/table/math. Test-only React DevTools PerformedWork probe counts historical row/ReactMarkdown commits, excluding reused bailout fibers; DOM code identity confirms no remount. Initial history click to two animation frames after full DOM insertion; 8 input events to two frames before generation (composer is disabled during generation); 12 scroll updates to two frames while deltas arrive every 60ms (capped at 20 periodic deltas; timers can be delayed by main-thread work). Timing targets are reported, not brittle CI assertions.";
  report[phase] ??= {};
  const allInput = samples.flatMap((sample) => sample.inputMs);
  const allScroll = samples.flatMap((sample) => sample.scrollMs);
  report[phase][count] = {
    measuredAt: new Date().toISOString(), repeats: samples.length, requestedRepeats: repeats, samples,
    summary: {
      initialMedianMs: percentile(samples.map((sample) => sample.initialMs), .5),
      initialP95Ms: percentile(samples.map((sample) => sample.initialMs), .95),
      inputMedianMs: percentile(allInput, .5), inputP95Ms: percentile(allInput, .95),
      scrollMedianMs: percentile(allScroll, .5), scrollP95Ms: percentile(allScroll, .95),
    },
  };
  await mkdir(new URL(".", reportPath), { recursive: true });
  await writeFile(reportPath, JSON.stringify(report, null, 2) + "\n");
  console.log(`${phase} ${count}: ${JSON.stringify(report[phase][count].summary)}`);
}

for (const count of [100, 1000]) {
  test(`${count} history rows stay stable during streaming and retain the scroll anchor`, async ({ page, browser }) => {
    test.setTimeout(300_000);
    const samples = [];
    const chat = fixture(count);
    expect(Buffer.byteLength(content)).toBe(1024);
    await installTauriMock(page, {
      list_conversations: [chat], get_conversation: chat, list_agent_change_sets: [],
      list_tool_operations: { operations: [] }, list_conversation_web_operations: { operations: [] },
    });
    await installProbe(page, count);
    for (let repeat = 0; repeat < repeats; repeat++) {
      await page.goto(baseUrl);
      await page.getByRole("button", { name: "Performance history", exact: true }).click();
      await expect(page.locator("article.chat-message")).toHaveCount(count);
      await page.waitForFunction(() => window.__chatPerf.initialMs !== null && window.__chatPerf.resume, null, { timeout: 10000 });
      await page.evaluate(() => document.fonts.ready);
      const inputMs = [];
      const input = page.getByPlaceholder("Ask agent...");
      await input.focus();
      for (let index = 0; index < 8; index++) {
        await page.evaluate(() => {
          window.__chatPerf.inputMs = null;
          document.querySelector("textarea").addEventListener("input", () => {
            const start = performance.now();
            requestAnimationFrame(() => requestAnimationFrame(() => { window.__chatPerf.inputMs = performance.now() - start; }));
          }, { once: true, capture: true });
        });
        await input.press("a");
        await page.waitForFunction(() => window.__chatPerf.inputMs !== null);
        inputMs.push(await page.evaluate(() => window.__chatPerf.inputMs));
      }
      await page.evaluate(() => window.__chatPerf.resume({ run: {
        id: "performance-run", conversation_id: "performance-chat", user_message_id: "history-98",
        status: "generating", provider_profile_id: "builtin-demo", model_id: "demo",
        error_code: null, error_message: null, draft: { content: "Stream start. " }, assistant_message: null,
      } }));
      await expect(input).toBeEditable();
      await page.locator(".chat-run-message .work-process__toggle").click();
      await page.locator(".chat-messages").evaluate((node) => { node.scrollTop = 100; node.dispatchEvent(new Event("scroll")); });
      await expect(page.getByRole("button", { name: "Jump to latest message" })).toBeVisible();
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const measured = await page.evaluate(async () => {
        const probe = window.__chatPerf;
        const node = document.querySelector(".chat-messages");
        const firstCode = document.querySelector(".markdown-code-block");
        const before = node.scrollTop;
        probe.rows = 0; probe.parses = 0; probe.commits = 0; probe.enabled = true;
        probe.emit("one-delta ");
        await new Promise((resolve) => setTimeout(resolve, 180));
        const singleDelta = { rows: probe.rows, parses: probe.parses, commits: probe.commits };
        const anchorShift = Math.abs(node.scrollTop - before);
        let deltas = 0;
        const streamInterval = setInterval(() => {
          probe.emit("next-delta ");
          if (++deltas === 20) clearInterval(streamInterval);
        }, 60);
        const scrollMs = [];
        for (let index = 0; index < 12; index++) {
          await new Promise((resolve) => setTimeout(resolve, 60));
          const start = performance.now();
          node.scrollTop = 100 + (index % 2) * 50;
          node.dispatchEvent(new Event("scroll"));
          await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          scrollMs.push(performance.now() - start);
        }
        clearInterval(streamInterval);
        await new Promise((resolve) => setTimeout(resolve, 180));
        probe.enabled = false;
        return {
          initialMs: probe.initialMs, singleDelta, streamRows: probe.rows, streamParses: probe.parses,
          streamCommits: probe.commits, anchorShift, historicalCodeNodePreserved: firstCode.isConnected,
          scrollMs, domNodes: document.querySelectorAll("*").length,
        };
      });
      samples.push({ repeat: repeat + 1, ...measured, inputMs });
      await recordMeasurements(count, samples, browser);
      await page.getByRole("button", { name: "Jump to latest message" }).click();
      if (!baseline) await expect.poll(() => page.locator(".chat-messages").evaluate((node) => node.scrollHeight - node.scrollTop - node.clientHeight)).toBeLessThanOrEqual(64);
      await expect(page.locator(".chat-message--assistant").last()).toContainText("one-delta");
    }
    if (!baseline) {
      for (const sample of samples) {
        expect(sample.singleDelta.commits, "probe must observe the real streaming update").toBeGreaterThan(0);
        expect(sample.singleDelta.rows, "a delta must not render unchanged historical rows").toBe(0);
        expect(sample.singleDelta.parses, "a delta must not parse unchanged historical Markdown").toBe(0);
        expect(sample.streamRows).toBe(0);
        expect(sample.streamParses).toBe(0);
        expect(sample.historicalCodeNodePreserved, "historical code must not remount").toBe(true);
        expect(sample.anchorShift).toBeLessThanOrEqual(1);
      }
    }
  });
}

test("a partial streamed code language can become Mermaid without losing the message", async ({ page }) => {
  const errors = [];
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  const chat = fixture(2);
  await installTauriMock(page, {
    list_conversations: [chat], get_conversation: chat, list_agent_change_sets: [],
    list_tool_operations: { operations: [] }, list_conversation_web_operations: { operations: [] },
  });
  await installProbe(page, 2);
  await page.goto(baseUrl);
  await page.getByRole("button", { name: "Performance history", exact: true }).click();
  await page.waitForFunction(() => window.__chatPerf.resume);
  await page.evaluate(() => window.__chatPerf.resume({ run: {
    id: "performance-run", conversation_id: "performance-chat", user_message_id: "history-0",
    status: "generating", provider_profile_id: "builtin-demo", model_id: "demo",
    error_code: null, error_message: null, draft: { content: "```m" }, assistant_message: null,
  } }));
  await page.locator(".chat-run-message .work-process__toggle").click();
  await expect(page.locator(".chat-run-message .markdown-code-block")).toBeVisible();
  await page.evaluate(() => window.__chatPerf.emit("ermaid\ngraph TD\n  A --> B\n```"));
  await expect(page.locator(".chat-run-message .mermaid-source")).toContainText("A --> B");
  await expect(page.locator(".chat-run-message .markdown-fallback")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("memoized history retains file and attachment previews after composer updates", async ({ page }) => {
  const chat = fixture(2);
  chat.messages[0].attachments = [{
    id: "attachment-1", kind: "text", name: "notes.txt", media_type: "text/plain",
    byte_size: 14, sha256: "0".repeat(64), width: null, height: null,
  }];
  chat.messages[1].content = "Open [project source](src/main.ts#L2).";
  await installTauriMock(page, {
    list_conversations: [chat], get_conversation: chat, list_agent_change_sets: [],
    get_conversation_model_run: { run: null },
    preview_message_attachment: { id: "attachment-1", kind: "text", name: "notes.txt", media_type: "text/plain", data_url: null, content: "Saved attachment content" },
    preview_project_file: { path: "src/main.ts", kind: "text", content: "first line\nselected source line", data_url: null, truncated: false },
  });
  await page.goto(baseUrl);
  await page.getByRole("button", { name: "Performance history", exact: true }).click();
  await page.getByPlaceholder("Ask agent...").fill("A new draft keeps the old message actions usable.");
  await page.getByRole("button", { name: "notes.txt", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "notes.txt" })).toContainText("Saved attachment content");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "project source", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "src/main.ts" })).toContainText("selected source line");
  await expect(page.locator(".chat-inspector__file-line--highlight")).toContainText("selected source line");
});

test("a visible mid-history message stays anchored when delayed media above it gains height", async ({ page }) => {
  const chat = fixture(40);
  for (const [index, message] of chat.messages.entries()) {
    message.content = `History row ${index}.\n\n${"Existing conversation text. ".repeat(10)}`;
  }
  chat.messages[7].content = "Earlier illustration\n\n![Deferred history illustration](https://images.example/late-history.svg)";
  chat.messages[20].content = "Reader anchor: middle of history.";
  let delayedImage;
  let notifyImageRequested;
  const imageRequested = new Promise((resolve) => { notifyImageRequested = resolve; });
  await page.route("https://images.example/late-history.svg", (route) => {
    delayedImage = route;
    notifyImageRequested();
  });
  await installTauriMock(page, {
    list_conversations: [chat], get_conversation: chat, list_agent_change_sets: [],
    list_tool_operations: { operations: [] }, list_conversation_web_operations: { operations: [] },
  });
  await installProbe(page, chat.messages.length);
  await page.goto(baseUrl);
  await page.getByRole("button", { name: "Performance history", exact: true }).click();
  // Start the real lazy image request while it is visible, but hold its bytes
  // until the reader has moved below it. No product DOM/style is patched.
  await page.getByRole("button", { name: "Allow all remote images" }).click();
  await imageRequested;
  await page.waitForFunction(() => window.__chatPerf.resume);
  await page.evaluate(() => window.__chatPerf.resume({ run: {
    id: "performance-run", conversation_id: "performance-chat", user_message_id: "history-38",
    status: "generating", provider_profile_id: "builtin-demo", model_id: "demo",
    error_code: null, error_message: null, draft: { content: "Streaming below the reader. " }, assistant_message: null,
  } }));
  await page.locator(".chat-run-message .work-process__toggle").click();
  await expect(page.locator(".chat-run-message")).toContainText("Streaming below the reader.");
  const anchor = page.getByText("Reader anchor: middle of history.", { exact: true });
  const image = page.getByRole("img", { name: "Deferred history illustration" });
  await anchor.evaluate((node) => {
    const scroller = document.querySelector(".chat-messages");
    scroller.scrollTop += node.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 60;
    scroller.dispatchEvent(new Event("scroll"));
    window.__visibleHistoryAnchor = node;
  });
  const jump = page.getByRole("button", { name: "Jump to latest message" });
  await expect(jump).toBeVisible();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const before = await anchor.evaluate((node) => {
    const scroller = document.querySelector(".chat-messages");
    return { offset: node.getBoundingClientRect().top - scroller.getBoundingClientRect().top,
      height: scroller.scrollHeight, scrollTop: scroller.scrollTop };
  });
  expect(before.offset).toBeGreaterThanOrEqual(58);
  expect(before.offset).toBeLessThanOrEqual(62);
  expect(await image.evaluate((node) => node.getBoundingClientRect().bottom
    < document.querySelector(".chat-messages").getBoundingClientRect().top)).toBe(true);
  await delayedImage.fulfill({
    status: 200, contentType: "image/svg+xml",
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="480"><rect width="480" height="480" fill="#b8d9f3"/></svg>',
  });
  await expect(image).toHaveJSProperty("naturalHeight", 480);
  await expect.poll(() => page.locator(".chat-messages").evaluate((node, position) => node.scrollHeight - position.height, before))
    .toBeGreaterThan(300);
  const visibleOffset = () => page.evaluate(() => {
    const node = window.__visibleHistoryAnchor;
    return node.isConnected ? node.getBoundingClientRect().top
      - document.querySelector(".chat-messages").getBoundingClientRect().top : null;
  });
  await expect.poll(async () => Math.abs((await visibleOffset() ?? Infinity) - before.offset)).toBeLessThanOrEqual(2);
  expect(await page.locator(".chat-messages").evaluate((node, position) => node.scrollTop - position.scrollTop, before)).toBeGreaterThan(300);
  await page.evaluate(() => window.__chatPerf.emit("A new delta arrives after the image."));
  await expect(page.locator(".chat-run-message")).toContainText("A new delta arrives after the image.");
  await expect.poll(async () => Math.abs((await visibleOffset() ?? Infinity) - before.offset)).toBeLessThanOrEqual(2);
  await expect(jump).toBeVisible();
  await jump.click();
  await expect.poll(() => page.locator(".chat-messages").evaluate((node) => node.scrollHeight - node.scrollTop - node.clientHeight))
    .toBeLessThanOrEqual(64);
});
