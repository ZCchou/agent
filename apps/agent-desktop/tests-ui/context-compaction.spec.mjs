import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

test("background organization keeps the next message available", async ({ page }) => {
  const { work } = await installTauriMock(page, { get_context_compaction_status: { compaction: {
    id: "background", conversation_id: "work-1", run_id: "old-run", status: "running",
    background: true, blocks_send: false, error_code: null, created_at: "2026-11-11T00:00:00Z",
  } } });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();
  await page.getByRole("textbox", { name: "Ask agent..." }).fill("Use the current project evidence to answer a new question");
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
  await expect(page.getByText("Organizing history in the background; you can send a message.")).toBeVisible();
});

test("context timeout shows the actual reason without claiming an exhausted allowance", async ({ page }) => {
  const now = "2026-11-10T00:00:00+00:00";
  const user = { id: "pending-user", conversation_id: "pending-chat", role: "user", content: "Read the template",
    event_type: null, source_message_id: null, created_at: now, attachments: [] };
  const chat = { id: "pending-chat", kind: "standard_chat", title: "Local context timeout", category: "research",
    resolved_category: "research", project_id: null, chat_group_id: null, sort_order: 0,
    created_at: now, updated_at: now, deleted_at: null, messages: [user] };
  const run = { id: "pending-run", conversation_id: chat.id, status: "waiting_context", user_message_id: user.id,
    provider: "fake", provider_profile_id: "builtin-demo", model_id: "demo", error_code: "request_timeout", draft: null };
  await installTauriMock(page, { list_conversations: [chat], get_conversation: chat,
    get_conversation_model_run: { run }, get_model_run: { run }, get_context_compaction_status: { compaction: null } });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: chat.title, exact: true }).click();
  await expect(page.getByText("The model did not start organizing in time.")).toBeVisible();
  await expect(page.getByText(/used its automatic compaction allowance|may incur/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Continue organizing and send" })).toBeVisible();
});

test("active context organization blocks send but keeps the draft editable", async ({ page }) => {
  const now = "2026-09-16T00:00:00+00:00";
  const { work } = await installTauriMock(page, {
    get_context_compaction_status: {
      compaction: {
        id: "compact-1", conversation_id: "work-1", run_id: "run-1",
        status: "running", trigger: "post_run", error_code: null,
        created_at: now,
      },
    },
  });

  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();

  const editor = page.getByRole("textbox", { name: "Ask agent..." });
  await editor.fill("Keep this draft while context is organized");
  await expect(editor).toBeEditable();
  await expect(page.getByRole("button", { name: "Send" })).toBeDisabled();
  await expect(page.getByText("Organizing earlier context; sending is temporarily paused")).toBeVisible();
  await expect(page.getByRole("button", { name: "Pause organization" })).toBeVisible();
});

test("failed context organization has an explicit retry action", async ({ page }) => {
  const now = "2026-09-16T00:00:00+00:00";
  const failed = {
    id: "compact-2", conversation_id: "work-1", run_id: "run-2",
    status: "failed", trigger: "pre_send", error_code: "context_compaction_failed",
    created_at: now,
  };
  const running = { ...failed, status: "running", error_code: null };
  const { work } = await installTauriMock(page, {
    get_context_compaction_status: { compaction: failed },
    resume_context_compaction: { compaction: running },
  });

  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();

  await expect(page.getByText("Context organization failed. Try again later.")).toBeVisible();
  await page.getByRole("button", { name: "Retry organization" }).click();
  await expect(page.getByText("Organizing earlier context; sending is temporarily paused")).toBeVisible();
  await expect(page.getByRole("button", { name: "Pause organization" })).toBeVisible();
});

test("unreliable compaction can resume with an allowed model using identity only", async ({ page }) => {
  const failed = {
    id: "compact-switch", conversation_id: "work-1", run_id: "run-existing",
    status: "failed", trigger: "pre_send", error_code: "context_compaction_unreliable",
    created_at: "2026-10-13T00:00:00+00:00",
  };
  const { work } = await installTauriMock(page, {
    get_context_compaction_status: { compaction: failed },
    resume_context_compaction: { compaction: { ...failed, status: "running", error_code: null } },
    get_project_model_scope: { project_id: "project-1", policy: "inherit_global", models: [] },
  });
  await page.addInitScript(() => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.__compactionRequests = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "resume_context_compaction" || command === "start_model_run") {
        window.__compactionRequests.push({ command, args });
      }
      return invoke(command, args);
    };
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();
  const draft = page.getByRole("textbox", { name: "Ask agent..." });
  await draft.fill("Keep this new message while reviewing the failed request");
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
  await draft.press("Enter");
  expect(await page.evaluate(() => window.__compactionRequests)).toEqual([]);
  await page.getByRole("combobox", { name: "Model for context organization" }).selectOption("builtin-demo::demo");
  await page.getByRole("button", { name: "Retry organization", exact: true }).click();
  await expect(page.getByText("Organizing earlier context; sending is temporarily paused")).toBeVisible();
  expect(await page.evaluate(() => window.__compactionRequests)).toEqual([{
    command: "resume_context_compaction",
    args: { conversationId: "work-1", target: { provider_profile_id: "builtin-demo", model_id: "demo" } },
  }]);
});

test("resumed foreground generation keeps one user request and allows a separate draft until completion", async ({ page }) => {
  const now = "2026-10-13T00:00:00+00:00";
  const user = { id: "original-user", conversation_id: "resume-chat", role: "user", content: "Original pending question",
    event_type: null, source_message_id: null, created_at: now, attachments: [] };
  const chat = { id: "resume-chat", kind: "standard_chat", title: "Pending context request", category: "research",
    resolved_category: "research", project_id: null, chat_group_id: null, sort_order: 0,
    created_at: now, updated_at: now, deleted_at: null, messages: [user] };
  const run = { id: "resume-run", conversation_id: chat.id, status: "waiting_context", user_message_id: user.id,
    provider: "fake", provider_profile_id: "builtin-demo", model_id: "demo", draft: null };
  const failed = { id: "resume-compact", conversation_id: chat.id, run_id: run.id, status: "failed",
    trigger: "pre_send", error_code: "context_compaction_failed", created_at: now };
  await installTauriMock(page, { list_conversations: [chat], get_conversation: chat,
    get_conversation_model_run: { run }, get_model_run: { run }, list_tool_operations: { operations: [] },
    get_context_compaction_status: { compaction: failed },
    resume_context_compaction: { compaction: { ...failed, status: "running", error_code: null } } });
  await page.addInitScript(() => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    const listeners = new Map();
    window.__recoveryCommands = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "plugin:event|listen") listeners.set(args.event, args.handler);
      if (["resume_context_compaction", "start_model_run"].includes(command)) window.__recoveryCommands.push(command);
      if (command === "get_context_compaction_status" && window.__contextReady) {
        return { compaction: { id: "resume-compact", status: "valid" } };
      }
      return invoke(command, args);
    };
    window.__emitRecoveryEvent = (event_type, data) => {
      const handler = listeners.get("model-run-event");
      if (!handler) throw new Error("Missing model-run subscription");
      window[`_${handler}`]({ event: "model-run-event", id: 1,
        payload: { run_id: "resume-run", event_type, data } });
    };
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: chat.title, exact: true }).click();
  const editor = page.getByRole("textbox", { name: "Ask agent..." });
  await editor.fill("Keep this separate draft");
  await page.getByRole("button", { name: "Retry organization", exact: true }).click();
  await page.evaluate(() => {
    window.__contextReady = true;
    window.__emitRecoveryEvent("run.context_compaction_completed", { conversation_id: "resume-chat" });
    window.__emitRecoveryEvent("run.state_changed", { status: "generating" });
  });
  await expect(editor).toBeEditable();
  await expect(page.locator(".chat-composer").getByRole("button", { name: "Stop generating", exact: true })).toBeEnabled();
  const answer = { id: "resumed-answer", conversation_id: chat.id, role: "assistant", content: "Recovered final answer",
    event_type: null, source_message_id: null, created_at: now, attachments: [] };
  await page.evaluate((message) => {
    window.__emitRecoveryEvent("run.completed", { message });
    window.__emitRecoveryEvent("run.completed", { message });
  }, answer);
  await expect(page.locator("article.chat-message--user")).toHaveCount(1);
  await expect(page.getByText("Recovered final answer", { exact: true })).toHaveCount(1);
  await expect(editor).toBeEditable();
  await expect(editor).toHaveValue("Keep this separate draft");
  expect(await page.evaluate(() => window.__recoveryCommands)).toEqual(["resume_context_compaction"]);
});

test("expanded image retry presents the returned child budget plan even without an event", async ({ page }) => {
  const parent = { id: "parent-image", conversation_id: "work-1", user_message_id: "original-image-user", status: "completed",
    provider_profile_id: "builtin-demo", model_id: "demo", error_code: null, error_message: null, draft: null, assistant_message: null };
  const child = { ...parent, id: "child-image", status: "waiting_budget", execution_plan_id: "child-plan" };
  const plan = { id: "child-plan", run_id: child.id, plan_hash: "new-approved-child-hash", status: "waiting_budget",
    estimated_extra_online_cost_usd: null,
    steps: [{ id: "vision-step", phase: "specialist", step_type: "vision_analysis", model_id: "vision-model" }] };
  const { work } = await installTauriMock(page, {
    get_conversation_model_run: { run: parent }, get_model_run: { run: parent }, retry_model_run: { run: child }, get_model_run_plan: { plan },
    resolve_model_run_budget: { run: { ...child, status: "queued" } },
    get_run_context: { snapshot: { id: "parent-snapshot", run_id: parent.id, round_no: 1, policy_version: "v1",
      context_generation: 1, global_instruction_revision: 0, global_instruction_hash: null,
      context_window: 10000, input_target: 8000, output_reserve: 2000, estimated_tokens: 500,
      stable_prefix_hash: "prefix-hash", retrieval_confidence: "high", compression_applied: 1, omitted_items: 1,
      compaction: null, run_usage: null, items: [] } },
  });
  await page.addInitScript(() => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.__retryBudgetRequests = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === "resolve_model_run_budget") window.__retryBudgetRequests.push(args);
      return invoke(command, args);
    };
  });
  await page.goto("http://127.0.0.1:5173");
  await page.getByRole("button", { name: work.title, exact: true }).click();
  await page.getByRole("button", { name: "Context details", exact: true }).click();
  await page.getByRole("button", { name: "Retry with more history", exact: true }).click();
  await expect(page.getByText("Confirm additional model calls", { exact: true })).toBeVisible();
  await expect(page.getByText("vision-model", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Approve this plan", exact: true }).click();
  expect(await page.evaluate(() => window.__retryBudgetRequests)).toEqual([{
    runId: "child-image", planHash: "new-approved-child-hash", approved: true,
  }]);
});
