import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock.mjs";

async function openRecoveryChat(page, initialFailure = null, managed = false) {
  await installTauriMock(page, { connect_model_provider: null });
  await page.addInitScript(({ initialFailure, managed }) => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.intentStarts = [];
    window.intentTasks = [];
    let failedOnce = false;
    let accepted = null;
    const listeners = new Map();
    window.managedReady = false;
    window.emitIntentRun = (event_type, data) => {
      for (const handler of listeners.get('model-run-event') ?? []) {
        window[`_${handler}`]({ event: 'model-run-event', id: 1, payload: {
          run_id: 'recovered-run', event_type, data, timestamp: '2026-11-14T00:00:00Z',
        } });
      }
    };
    window.completeIntent = (status = 'completed') => {
      const task = window.intentTasks.at(-1);
      const now = "2026-11-13T10:00:00Z";
      const user = { id: "accepted-user", role: "user", content: task.request.content, conversation_id: "recovered-chat", attachments: [], created_at: now };
      const assistant = { id: "accepted-answer", role: "assistant", content: "Recovered answer", conversation_id: "recovered-chat", created_at: now };
      const conversation = { id: "recovered-chat", title: "Recovered conversation", kind: "standard_chat", category: "casual", resolved_category: "casual",
        project_id: null, chat_group_id: null, messages: [user, assistant], model_selection_mode: "fixed", provider_profile_id: "builtin-demo", model_id: "demo", created_at: now, updated_at: now };
      if (status !== 'completed') conversation.messages = [user];
      const run = { id: "recovered-run", conversation_id: conversation.id, user_message_id: user.id, status, provider_profile_id: "builtin-demo", model_id: "demo", assistant_message: status === 'completed' ? assistant : null, draft: null };
      accepted = { status: "accepted", run, conversation, user_message: user, selection_decision: {} };
      Object.assign(task, { status: "completed", result: accepted });
    };
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === 'plugin:event|listen') {
        const handlers = listeners.get(args.event) ?? new Set();
        handlers.add(args.handler); listeners.set(args.event, handlers);
      }
      if (managed && command === 'get_app_settings') {
        const settings = structuredClone(await invoke(command, args));
        settings.models.providers = [{ id: 'managed:status-model', name: 'Fixture local model', kind: 'openai_compatible', enabled: true,
          models: [{ id: 'status-model', enabled: true, source: 'discovered', status: window.managedReady ? 'available' : 'unavailable',
            allow_automatic_routing: true, tool_mode: 'text_only', capabilities: { text: 'yes', vision: 'no', tools: 'no', context_length: 8192 } }] }];
        settings.models.default_provider_profile_id = 'managed:status-model';
        settings.models.default_model_id = 'status-model';
        return settings;
      }
      if (managed && command === 'local_model_request') {
        if (args.action === 'runtime.release') return { released: true };
        if (args.action === 'runtime.prepare') return { preparation_id: 'status-lease', state: window.managedReady ? 'ready' : 'loading' };
      }
      if (command === "start_model_run") {
        window.intentStarts.push(structuredClone(args.request));
        let task = window.intentTasks.find((value) => value.request.idempotency_key === args.request.idempotency_key);
        if (!task) {
          task = { id: `intent-${window.intentTasks.length + 1}`, scope: args.request.intent_scope, revision: 1,
            status: "identifying", request: args.request, attempts: 1, elapsed_seconds: 1, remaining_seconds: 89,
            timeout_seconds: 90, budget_seconds: 270, budget_remaining_seconds: 269, can_retry: false, can_extend: true,
            result: null, clarification: null, error: null };
          window.intentTasks.push(task);
        }
        if (initialFailure && !failedOnce) {
          failedOnce = true;
          if (initialFailure === "uncertain") throw new Error("Admission response lost");
          Object.assign(task, { status: "failed", error: { code: "invalid_request", message: "Definitive preparation failure" } });
        }
        return { task: structuredClone(task) };
      }
      if (command === "intent_request") {
        if (args.action === "list") return { tasks: structuredClone(window.intentTasks.filter((task) => task.scope === args.params.scope).reverse()) };
        const task = window.intentTasks.find((value) => value.id === args.params.task_id);
        if (args.action === "cancel") Object.assign(task, { status: "cancelled", error: { code: "cancelled_by_user", message: "Cancelled" } });
        if (args.action === 'respond') Object.assign(task, { status: 'identifying', revision: task.revision + 1, clarification: null });
        return { task: structuredClone(task) };
      }
      if (command === "get_conversation" && args.conversationId === accepted?.conversation.id) return structuredClone(accepted.conversation);
      if (command === "get_model_run") return { run: structuredClone(accepted.run) };
      if (command === "get_conversation_model_run" && args.conversationId === accepted?.conversation.id) return { run: structuredClone(accepted.run) };
      if (command === "list_conversations" && accepted) return [structuredClone(accepted.conversation)];
      return invoke(command, args);
    };
  }, { initialFailure, managed });
  await page.goto(`${process.env.AGENT_UI_BASE_URL ?? "http://127.0.0.1:5173"}/tests-ui/fixtures/intent-recovery.html`);
  const editor = page.getByRole("textbox", { name: "Ask agent..." });
  await expect(editor).toBeEnabled();
  return editor;
}

test('adaptive execution keeps progress visible across old deadlines and exposes recovery controls', async ({ page }) => {
  const editor = await openRecoveryChat(page);
  await page.evaluate(() => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.executionControls = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command === 'execution_request') {
        if (args.action !== 'status') {
          window.executionControls.push(structuredClone(args));
          window.executionRoot.revision += 1;
          window.executionRoot.sequence += 1;
          window.executionRoot.status = args.action === 'cancel' ? 'cancelled' : 'running';
          if (args.action === 'cancel') Object.assign(window.intentTasks.at(-1), { status: 'cancelled',
            error: { code: 'cancelled_by_user', message: 'Cancelled' } });
        }
        return { task: structuredClone(window.executionRoot ?? null) };
      }
      const result = await invoke(command, args);
      if (command === 'start_model_run' && result.task) {
        result.task.execution_task_id = 'execution-root';
        window.intentTasks.at(-1).execution_task_id = 'execution-root';
        window.executionRoot = { id: 'execution-root', intent_task_id: result.task.id, run_id: null,
          revision: 1, attempt: 1, sequence: 1, status: 'running', stage: 'rag_retrieval', auto_recoveries: 0,
          created_at: 100, updated_at: 500, elapsed_seconds: 400, seconds_since_progress: 2,
          current_stage: { completed: 18, total: 40, details: { stage: 'reranking', device: 'cpu', model_name: 'Fixture reranker' } } };
        window.intentTasks.at(-1).remaining_seconds = 0;
      }
      return result;
    };
  });
  await send(page, editor);
  const card = page.getByRole('region', { name: 'Task execution status' });
  await expect(card).toContainText('Completed 18/40');
  await expect(card).toContainText('Running for 400 seconds');
  await expect(card).toContainText('Fixture reranker');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.evaluate(() => Object.assign(window.executionRoot, { status: 'waiting_recovery', sequence: 2,
    error_code: 'execution_activity_uncertain' }));
  await expect(card).toContainText('Task needs attention');
  await card.getByRole('button', { name: 'Continue waiting', exact: true }).click();
  await expect(card).toContainText('Reranking candidates');
  await page.evaluate(() => Object.assign(window.executionRoot, { status: 'waiting_recovery', sequence: 4 }));
  await card.getByRole('button', { name: 'Recover current stage', exact: true }).click();
  await expect(card).toContainText('Reranking candidates');
  const controls = await page.evaluate(() => window.executionControls);
  expect(controls.map(value => value.action)).toEqual(['continue', 'retry']);
  expect(controls.every(value => value.params.idempotency_key.length > 0)).toBe(true);
  await card.getByRole('button', { name: 'Cancel task', exact: true }).click();
  await expect(card).toHaveCount(0);
});

test('cold local model transitions to intent recognition, clarification and actual run phases', async ({ page }) => {
  const editor = await openRecoveryChat(page, null, true);
  await send(page, editor);
  const status = page.locator('.chat-run-message__status-copy');
  await expect(status).toContainText('Loading Fixture local model');
  await page.evaluate(() => { window.managedReady = true; });
  await expect.poll(() => page.evaluate(() => window.intentStarts.length)).toBe(1);
  await expect(status).toContainText('Identifying task intent');
  await page.evaluate(() => Object.assign(window.intentTasks.at(-1), { status: 'waiting_user',
    clarification: { summary: 'Choose scope', questions: [{ id: 'scope', question: 'Which scope?', options: ['One section', 'Whole template'] }] } }));
  await expect(status).toContainText('Waiting for your clarification');
  await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
  await expect(status).toContainText('Waiting for your clarification');
  await page.getByRole('button', { name: 'Continue providing information', exact: true }).click();
  await page.getByRole('radio', { name: 'One section', exact: true }).check();
  await page.getByRole('button', { name: 'Confirm and continue', exact: true }).click();
  await expect(status).toContainText('Identifying task intent');
  await page.evaluate(() => window.completeIntent('building_context'));
  await expect(page.getByLabel('Current scope')).toHaveText('recovered-chat');
  await page.evaluate(() => window.emitIntentRun('run.state_changed', { status: 'building_context', phase: 'embedding' }));
  await expect(status).toContainText('Embedding the query');
  await page.evaluate(() => window.emitIntentRun('run.document_processing', { phase: 'reranking', completed: 4, total: 20 }));
  await expect(status).toContainText('Reranking document evidence 4/20');
  await page.evaluate(() => window.emitIntentRun('run.state_changed', { status: 'waiting_tool' }));
  await expect(status).toContainText('Waiting for tool results');
  await page.evaluate(() => window.emitIntentRun('run.state_changed', { status: 'waiting_permission' }));
  await expect(status).toContainText('Waiting for operation approval');
  await page.evaluate(() => window.emitIntentRun('run.state_changed', { status: 'generating', phase: 'waitingOutput' }));
  await expect(status).toContainText('Waiting for model output');
  await page.evaluate(() => window.emitIntentRun('run.chunk', { delta: 'The generated answer.' }));
  await expect(status).toContainText('Generating answer');
  await page.evaluate(() => window.emitIntentRun('run.completed', { message: {
    id: 'finished-answer', conversation_id: 'recovered-chat', role: 'assistant', content: 'The generated answer.', created_at: '2026-11-14T00:00:00Z',
  } }));
  await expect(status).toHaveCount(0);
});

async function send(page, editor) {
  await editor.fill("Unchanged original question");
  await page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true }).click();
}

test("Stop then unchanged Send creates a fresh task through ChatPage and the real repository", async ({ page }) => {
  const editor = await openRecoveryChat(page);
  await send(page, editor);
  await expect.poll(() => page.evaluate(() => window.intentStarts.length)).toBe(1);
  await page.getByRole("button", { name: "Stop generating", exact: true }).click();
  await expect(editor).toHaveValue("Unchanged original question");
  await page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.intentStarts.length)).toBe(2);
  const starts = await page.evaluate(() => window.intentStarts);
  expect(starts[1].idempotency_key).not.toBe(starts[0].idempotency_key);
  expect(starts[1].content).toBe(starts[0].content);
  await expect.poll(() => page.evaluate(() => window.intentTasks.length)).toBe(2);
});

test("a definitive preparation failure rotates the unchanged send identity", async ({ page }) => {
  const editor = await openRecoveryChat(page, "terminal");
  await send(page, editor);
  await expect(editor).toHaveValue("Unchanged original question");
  await page.getByRole("dialog").getByRole("button", { name: "Close", exact: true }).click();
  await page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.intentStarts.length)).toBe(2);
  const starts = await page.evaluate(() => window.intentStarts);
  expect(starts[1].idempotency_key).not.toBe(starts[0].idempotency_key);
});

test("an uncertain admission response retains the unchanged send identity", async ({ page }) => {
  const editor = await openRecoveryChat(page, "uncertain");
  await send(page, editor);
  await expect(page.locator(".chat-error")).toContainText("Admission response lost");
  await page.locator(".chat-composer").getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.intentStarts.length)).toBe(2);
  const starts = await page.evaluate(() => window.intentStarts);
  expect(starts[1].idempotency_key).toBe(starts[0].idempotency_key);
  expect(await page.evaluate(() => window.intentTasks.length)).toBe(1);
});

test('an older intent cannot overwrite a new model preparation status', async ({ page }) => {
  const editor = await openRecoveryChat(page, 'uncertain', true);
  await page.evaluate(() => { window.managedReady = true; });
  await send(page, editor);
  await expect(page.locator('.chat-error')).toContainText('Admission response lost');
  await page.evaluate(async () => {
    const { intentTasks } = await import('/src/services/tauri/intentTasks.ts');
    intentTasks.update(structuredClone(window.intentTasks[0]));
    window.managedReady = false;
  });
  await editor.fill('A different question');
  await page.locator('.chat-composer').getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.locator('.chat-run-message__status-copy')).toContainText('Loading Fixture local model');
  await page.evaluate(() => {
    const status = document.querySelector('.chat-run-message__status-copy');
    window.statusSamples = [];
    window.statusObserver = new MutationObserver(() => window.statusSamples.push(status.textContent));
    window.statusObserver.observe(status, { subtree: true, childList: true, characterData: true });
  });
  await page.waitForTimeout(2250);
  expect(await page.evaluate(() => window.statusSamples.filter(value => value.includes('Identifying task intent')))).toEqual([]);
});

for (const completion of ["before", "after"]) test(`draft scope switch adopts completion ${completion} returning`, async ({ page }) => {
  const editor = await openRecoveryChat(page);
  await send(page, editor);
  await expect.poll(() => page.evaluate(() => window.intentStarts.length)).toBe(1);
  await page.getByRole("button", { name: "Other scope", exact: true }).click();
  await expect(page.getByLabel("Current scope")).toHaveText("work-1");
  if (completion === "before") {
    await page.evaluate(() => window.completeIntent());
    await page.waitForTimeout(900);
    await expect(page.getByLabel("Current scope")).toHaveText("work-1");
  }
  await page.getByRole("button", { name: "Original draft", exact: true }).click();
  if (completion === "after") {
    await expect(page.getByLabel("Current scope")).toHaveText("draft-0");
    await page.evaluate(() => window.completeIntent());
  }
  await expect(page.getByLabel("Current scope")).toHaveText("recovered-chat");
  await expect(page.locator(".chat-message--assistant")).toContainText("Recovered answer");
});
