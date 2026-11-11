import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

const now = "2026-10-13T00:00:00+00:00";
const audit = { complete: false, truncated: true, stop_reason: "bytes", inspected_files: 8 };
const message = (id, role, content, event_type = null) => ({
  id, conversation_id: "work-1", role, content, event_type, source_message_id: null, created_at: now, attachments: [],
});
const run = { id: "run-1", conversation_id: "work-1", user_message_id: "user-1", status: "completed",
  provider_profile_id: "builtin-demo", model_id: "demo", error_code: null, error_message: null,
  draft: null, assistant_message: null };

test("attachment context shows source range, route, reason, and safe text preview", async ({ page }) => {
  const { work } = await installTauriMock(page, {
    get_conversation: { id: "work-1", kind: "project_work", title: "Source audit", category: null,
      resolved_category: "coding", chat_group_id: null, project_id: "project-1", sort_order: 0,
      derived_from_conversation_id: null, derived_through_message_id: null, derived_at: null,
      copied_from_conversation_id: null, copied_through_message_id: null, copied_at: null,
      created_at: now, updated_at: now, deleted_at: null,
      messages: [message("user-1", "user", "Use the selected passage"), message("answer-1", "assistant", "Done")],
    },
    get_conversation_model_run: { run },
    get_run_context: { snapshot: {
      id: "snapshot-1", run_id: "run-1", round_no: 1, policy_version: "v1", context_generation: 1,
      global_instruction_revision: 0, global_instruction_hash: null, context_window: 8192,
      input_target: 3932, input_hard_limit: 4915, body_input_limit: 4145, input_scope: 'compiled_request', request_constraint_tokens: 300,
      output_reserve: 2048, token_method: "estimate", estimated_tokens: 4500,
      stable_prefix_hash: "abc0123456789", dynamic_suffix_hash: "def", retrieval_confidence: "high",
      compression_applied: 0, omitted_items: 0, compaction: null, run_usage: { input_tokens: 5170, output_tokens: 236, cached_input_tokens: null, estimated_cost: null },
      items: [{ id: "context-attachment", source_type: "attachment", source_id: "clone-1",
        section: "current", representation: "original", trust: "user_authored", estimated_tokens: 8,
        included: 1, inclusion_reason: "current_attachment", exclusion_reason: null,
        source_start: 2, source_end: 7, source_hash: "abc", metadata: {
          name: "研究笔记.txt", kind: "text", source_attachment_id: "original-1",
          current_attachment_id: "clone-1", receiving_route: "legacy.primary", effective_attachment_ids: ["clone-1"],
          document_locator: { heading_path: ["试验程序", "振动试验", "试验目的"], paragraph: 612 },
        } }],
    } },
    get_context_source: { source: { item_id: "context-attachment", source_type: "attachment",
      source: { name: "研究笔记.txt", kind: "text", content: "🙂<script>window.leak=true</script> notes" } } },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();
  await page.getByRole("button", { name: "Context details" }).click();
  const panel = page.locator(".context-panel");
  await expect(panel).toContainText("研究笔记.txt");
  await expect(panel).toContainText("Complete request estimate 4500 / 4915 tokens");
  await expect(panel).toContainText("Near input limit");
  await expect(panel).toContainText('Model-reported input 5170 · output 236 tokens');
  await panel.getByText('Budget details', { exact: true }).click();
  await expect(panel).toContainText("Input limit 4915 tokens");
  await expect(panel).toContainText("Body allowance 4145 tokens");
  await expect(panel).toContainText("Remaining input capacity 415 tokens");
  await expect(panel).toContainText("0 historical messages omitted this turn");
  await expect(panel).toContainText("试验程序 → 振动试验 → 试验目的");
  await expect(panel).toContainText("2–7");
  await expect(panel).toContainText("Direct model request");
  await expect(panel).toContainText("Selected range included");
  await panel.getByRole("button", { name: "View source" }).click();
  await expect(panel.locator("pre")).toContainText("<script>window.leak=true</script>");
  expect(await page.evaluate(() => window.leak)).toBeUndefined();
});

test("search activity shows incomplete and unknown audits without exposing matches or cursors", async ({ page }) => {
  const event = message("event-1", "system_event", JSON.stringify({ run_id: "run-1", operation_id: "search-1",
    tool_name: "fs.search", status: "completed", output_bytes: 400, search_completeness: audit,
    matches: ["private-file"], next_cursor: "private-cursor" }), "tool_operation");
  const legacyEvent = message("event-2", "system_event", JSON.stringify({ run_id: "run-1", operation_id: "search-2",
    tool_name: "fs.search", status: "completed", output_bytes: 0 }), "tool_operation");
  const { work } = await installTauriMock(page, {
    get_conversation: { id: "work-1", kind: "project_work", title: "Search audit", category: null,
      resolved_category: "coding", chat_group_id: null, project_id: "project-1", sort_order: 0,
      derived_from_conversation_id: null, derived_through_message_id: null, derived_at: null,
      copied_from_conversation_id: null, copied_through_message_id: null, copied_at: null,
      created_at: now, updated_at: now, deleted_at: null,
      messages: [message("user-1", "user", "Search"), event, legacyEvent, message("answer-1", "assistant", "Done")],
    },
    get_conversation_model_run: { run },
    list_tool_operations: { operations: [{ id: "search-1", run_id: "run-1", sequence: 1,
      tool_name: "fs.search", status: "completed", error_code: null, search_completeness: audit },
    { id: "search-2", run_id: "run-1", sequence: 2, tool_name: "fs.search", status: "completed", error_code: null }] },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();
  const activity = page.locator(".run-activity");
  await expect(activity.getByRole("button", { name: "Search incomplete" })).toBeVisible();
  await activity.locator(".run-activity__summary").click();
  await expect(activity).toContainText("Search stopped after 8 files");
  await expect(activity).toContainText("Search completeness unknown");
  await expect(activity).not.toContainText("private-file");
  await expect(activity).not.toContainText("private-cursor");
});

test("an unknown-only search audit uses an unknown activity summary", async ({ page }) => {
  const legacyEvent = message("event-legacy", "system_event", JSON.stringify({
    run_id: "run-1", operation_id: "search-legacy", tool_name: "fs.search", status: "completed", output_bytes: 0,
  }), "tool_operation");
  const { work } = await installTauriMock(page, {
    get_conversation: { id: "work-1", kind: "project_work", title: "Legacy search", category: null,
      resolved_category: "coding", chat_group_id: null, project_id: "project-1", sort_order: 0,
      created_at: now, updated_at: now, deleted_at: null,
      messages: [message("user-1", "user", "Search"), legacyEvent, message("answer-1", "assistant", "Done")],
    },
    get_conversation_model_run: { run },
    list_tool_operations: { operations: [{ id: "search-legacy", run_id: "run-1", sequence: 1,
      tool_name: "fs.search", status: "completed", error_code: null }] },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();
  const activity = page.locator(".run-activity");
  await expect(activity.getByRole("button", { name: "Search completeness unknown" })).toBeVisible();
  await expect(activity).toContainText("Search completeness unknown");
});

test("a late source response cannot replace the newly selected image preview", async ({ page }) => {
  const attachmentItem = (id, name, kind, receiving_route = "legacy.primary") => ({
    id, source_type: "attachment", source_id: id, section: "current", representation: "original",
    trust: "user_authored", estimated_tokens: 1, included: 1, inclusion_reason: "current_attachment",
    exclusion_reason: null, metadata: { name, kind, receiving_route },
  });
  const { work } = await installTauriMock(page, {
    get_conversation_model_run: { run },
    get_run_context: { snapshot: {
      id: "snapshot-two", run_id: "run-1", round_no: 1, policy_version: "v1", context_generation: 1,
      global_instruction_revision: 0, global_instruction_hash: null, context_window: 10000,
      input_target: 8000, output_reserve: 2000, token_method: "estimate", estimated_tokens: 2,
      stable_prefix_hash: "abc0123456789", dynamic_suffix_hash: "def", retrieval_confidence: "high",
      compression_applied: 0, omitted_items: 0, compaction: null, run_usage: null,
      items: [attachmentItem("item-a", "Earlier.txt", "text"),
        attachmentItem("item-b", "Photo.png", "image", "route.opaque-uuid")],
    } },
    get_context_source: { source: null },
  });
  await page.addInitScript(() => {
    const original = window.__TAURI_INTERNALS__.invoke;
    window.__TAURI_INTERNALS__.invoke = (command, args) => {
      if (command === "get_context_source" && args.itemId === "item-a") {
        return new Promise((resolve) => { window.resolveEarlierSource = resolve; });
      }
      if (command === "get_context_source" && args.itemId === "item-b") {
        return Promise.resolve({ source: { item_id: "item-b", source_type: "attachment",
          source: { name: "Photo.png", kind: "image", data_url:
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg==" } } });
      }
      return original(command, args);
    };
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();
  await page.getByRole("button", { name: "Context details" }).click();
  await expect(page.locator(".context-panel")).toContainText("Selected authorized model route");
  await expect(page.locator(".context-panel")).not.toContainText("route.opaque-uuid");
  const buttons = page.locator(".context-panel__source-button");
  await buttons.nth(0).click();
  await buttons.nth(1).click();
  const preview = page.locator(".context-source-preview");
  await expect(preview.getByRole("img", { name: "Attachment source image" })).toHaveAttribute("src", /^data:image\/png;base64,/);
  await page.evaluate(() => window.resolveEarlierSource({ source: { item_id: "item-a", source_type: "attachment",
    source: { content: "stale text" } } }));
  await expect(preview).toContainText("Photo.png");
  await expect(preview).not.toContainText("stale text");
});

test("a live operation reports an incomplete scan before a persisted event exists", async ({ page }) => {
  const { work } = await installTauriMock(page, {
    get_conversation_model_run: { run: { ...run, status: "waiting_tool" } },
    list_tool_operations: { operations: [{ id: "live-search", run_id: "run-1", sequence: 1,
      tool_name: "fs.search", status: "completed", error_code: null, search_completeness: audit }] },
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();
  const activity = page.locator(".run-activity");
  await expect(activity).toContainText("Search incomplete");
  await activity.locator(".run-activity__summary").click();
  await expect(activity).toContainText("Search stopped after 8 files");
});
