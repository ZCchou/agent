import { expect, test } from '@playwright/test';
import { installTauriMock } from './tauri-mock.mjs';

async function setup(page, scenario) {
  await installTauriMock(page, { list_route_health: [] });
  await page.addInitScript((scenario) => {
    localStorage.setItem('agent:update-auto-check', 'false');
    const original = window.__TAURI_INTERNALS__.invoke;
    window.actionCalls = [];
    let settings;
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      window.actionCalls.push({ command, args });
      if (!settings) {
        settings = structuredClone(await original('get_app_settings'));
        settings.models.providers.push({ ...structuredClone(settings.models.providers[0]), id: 'local', name: 'Local', kind: 'ollama',
          models: [{ ...structuredClone(settings.models.providers[0].models[0]), id: 'manual', source: 'manual', tool_mode: 'unknown' }] });
        settings.routing = { new_conversation_mode: 'automatic', default_preset: 'balanced', max_model_calls_per_message: 3,
          max_specialist_steps: 2, max_extra_online_cost_usd: '0.20', allow_unknown_price_automatic: false };
        if (scenario === 'native') {
          settings.models.providers[1].native_search = { adapter_id: 'anthropic_messages_web_search', protocol: 'anthropic_messages', endpoint_base_url: 'http://localhost/anthropic', source: 'official_preset', adapter_version: '1' };
          settings.models.providers[1].models[0].native_web_search_status = 'declared';
        }
        if (scenario === 'connect') {
          settings.web_search.enabled = true; settings.web_search.privacy_acknowledged = true;
        }
        if (scenario === 'scope' || scenario === 'coordinator') {
          settings.web_search.enabled = true; settings.web_search.privacy_acknowledged = true;
          settings.web_search.backends = [{ id: 'search', name: 'Offline Search', kind: 'searxng', base_url: 'http://localhost', enabled: true }];
          settings.web_search.default_backend_id = 'search';
        }
      }
      if (scenario === 'scope' && (command === 'list_conversations' || command === 'get_conversation')) {
        const seed = await original('get_conversation');
        const chats = ['A', 'B'].map(id => ({ ...seed, id, kind: 'standard_chat', title: `Search chat ${id}`, project_id: null, web_search_mode: 'off' }));
        return command === 'list_conversations' ? chats : chats.find(c => c.id === args.conversationId);
      }
      if (command === 'update_conversation_web_search' && scenario === 'scope') return new Promise((_, reject) => { window.failSearchMode = () => reject('offline'); });
      if (command === 'probe_native_web_search') { settings.models.providers[1].models[0].native_web_search_status = 'unsupported'; throw 'native_search_protocol_unsupported'; }
      if (command === 'get_app_settings') {
        if ((scenario === 'load-search' || scenario === 'load-models') && window.failSettings) throw 'offline';
        return structuredClone(settings);
      }
      if (command === 'save_model_provider') return new Promise(resolve => { window.finishProvider = () => {
        settings.models.providers.push({ ...args.provider, models: [] }); resolve(structuredClone(settings));
      }; });
      if (command === 'refresh_provider_models') return structuredClone(settings);
      if (command === 'update_model_context_limits') {
        if (scenario === 'advanced-pending') await new Promise(resolve => { window.finishAdvanced = resolve; });
        const model = settings.models.providers[1].models[0];
        model.capabilities.context_length = args.request.context_length;
        model.capabilities.max_output_tokens = args.request.max_output_tokens;
        return structuredClone(settings);
      }
      if (command === 'update_model_routing_metadata') {
        if (scenario === 'advanced-pending') {
          settings.models.providers[1].models[0].capabilities.input_price_per_million = args.request.input_price;
          return structuredClone(settings);
        }
        throw 'save routing failed';
      }
      if (command === 'add_manual_provider_model') {
        if (scenario === 'manual-pending') {
          await new Promise(resolve => { window.finishManual = resolve; });
          return structuredClone(settings);
        }
        throw 'manual add failed';
      }
      if (command === 'probe_model_tool_capability' && scenario === 'tool-partial') {
        settings.models.providers[1].models[0].tool_mode = 'native'; throw 'runtime sync failed';
      }
      if (command === 'count_conversation_model_references') return 2;
      if (command === 'remove_manual_provider_model') {
        settings.models.providers[1].models = [];
        if (scenario === 'remove-partial') throw 'runtime sync failed';
        return structuredClone(settings);
      }
      if (command === 'update_routing_preferences') {
        if (scenario === 'routing-pending') await new Promise(resolve => { window.finishRouting = resolve; });
        settings.routing = args.routing; return structuredClone(settings);
      }
      if (command === 'update_web_search_settings') { settings.web_search = args.settings; return structuredClone(settings); }
      if (command === 'save_search_backend') {
        if (scenario === 'backend-pending') await new Promise(resolve => { window.finishBackend = resolve; });
        settings.web_search.backends.push({ ...args.backend, base_url: args.backend.base_url });
        settings.web_search.default_backend_id = args.backend.id;
        return structuredClone(settings);
      }
      if (command === 'test_search_backend') return {};
      if (command === 'list_trash') return [{ id: 'deleted', title: 'Deleted chat', entity_type: 'conversation', conversation_kind: 'standard_chat', message_count: 0, expires_at: '2026-12-01' }];
      if (command === 'restore_trash') return new Promise(resolve => { window.finishRestore = resolve; });
      return original(command, args);
    };
  }, scenario);
  await page.goto('http://127.0.0.1:5173');
  await expect(page.getByRole('textbox', { name: 'Ask agent...' })).toBeEnabled();
}
async function navigate(page, name) {
  if (!(await page.getByRole('navigation').getByRole('button', { name, exact: true }).isVisible()))
    await page.getByRole('button', { name: 'System features' }).click();
  await page.getByRole('navigation').getByRole('button', { name, exact: true }).click();
}
async function catalog(page) {
  await navigate(page, 'Models');
  await page.locator('.model-provider-card').filter({ has: page.getByRole('heading', { name: 'Local', exact: true }) }).getByRole('button', { name: 'Manage models' }).click();
  return page.getByRole('dialog', { name: 'Local', exact: true });
}

test('provider submit admits one create and a closed wizard stays closed after completion', async ({ page }) => {
  await setup(page); await navigate(page, 'Models');
  await page.getByRole('button', { name: 'Add provider', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Add provider' });
  await dialog.getByRole('button', { name: /Ollama/ }).click();
  await dialog.locator('form').evaluate(form => { form.requestSubmit(); form.requestSubmit(); });
  expect(await page.evaluate(() => window.actionCalls.filter(c => c.command === 'save_model_provider').length)).toBe(1);
  await dialog.getByRole('button', { name: 'Close' }).click();
  await page.evaluate(() => window.finishProvider());
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('pending provider creation freezes the submitted configuration fields', async ({ page }) => {
  await setup(page); await navigate(page, 'Models');
  await page.getByRole('button', { name: 'Add provider', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Add provider' });
  await dialog.getByRole('button', { name: /Ollama/ }).click();
  await dialog.locator('form').evaluate(form => form.requestSubmit());
  await page.waitForFunction(() => typeof window.finishProvider === 'function');
  for (const input of await dialog.locator('input, select').all()) await expect(input).toBeDisabled();
  await page.evaluate(() => window.finishProvider());
  await expect(dialog).toHaveCount(0);
});

test('pending routing budget save prevents newer input being overwritten by its response', async ({ page }) => {
  await setup(page, 'routing-pending'); await navigate(page, 'Models');
  const calls = page.getByLabel('Calls per message');
  await calls.fill('5'); await calls.press('Tab');
  await page.waitForFunction(() => typeof window.finishRouting === 'function');
  await expect(calls).toBeDisabled();
  await page.evaluate(() => window.finishRouting());
  await expect(calls).toBeEnabled(); await expect(calls).toHaveValue('5');
});

test('advanced partial failure keeps entered routing values and the editor open', async ({ page }) => {
  await setup(page); const dialog = await catalog(page);
  await dialog.getByRole('button', { name: 'Advanced parameters' }).click();
  await dialog.getByLabel('Context window', { exact: true }).fill('16384');
  await dialog.getByLabel('Input price', { exact: false }).fill('2.50');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(dialog.getByLabel('Input price', { exact: false })).toHaveValue('2.50');
  await expect(dialog.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
});

test('advanced parameters cannot accept edits that a pending save would discard', async ({ page }) => {
  await setup(page, 'advanced-pending'); const dialog = await catalog(page);
  const disclosure = dialog.getByRole('button', { name: 'Advanced parameters' });
  await disclosure.click();
  const price = dialog.getByLabel('Input price', { exact: false });await price.fill('2.50');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await page.waitForFunction(() => typeof window.finishAdvanced === 'function');
  await expect(price).toBeDisabled();await expect(disclosure).toBeDisabled();
  await page.evaluate(() => window.finishAdvanced());
  await expect(price).toHaveCount(0);
  await disclosure.click();
  await expect(price).toBeEnabled();await expect(price).toHaveValue('2.50');
});

test('manual add failure keeps the exact model id for retry', async ({ page }) => {
  await setup(page); const dialog = await catalog(page);
  const input = dialog.getByPlaceholder('Enter the exact model ID');
  await input.fill('private-retry');
  await dialog.getByRole('button', { name: 'Add model', exact: true }).click();
  await expect(input).toHaveValue('private-retry');
});

test('pending manual addition freezes its submitted model id until completion', async ({ page }) => {
  await setup(page, 'manual-pending'); const dialog = await catalog(page);
  const input = dialog.getByPlaceholder('Enter the exact model ID');
  await input.fill('submitted-model');
  await dialog.getByRole('button', { name: 'Add model', exact: true }).click();
  await page.waitForFunction(() => typeof window.finishManual === 'function');
  await expect(input).toBeDisabled();
  await page.evaluate(() => window.finishManual());
  await expect(input).toBeEnabled(); await expect(input).toHaveValue('');
});

test('offline search settings provide no backend configuration or requests', async ({ page }) => {
  await setup(page, 'backend-pending'); await navigate(page, 'Settings');
  await page.locator('.settings-navigation-card').filter({ hasText: 'Web & Search' }).getByRole('button', { name: 'Open', exact: true }).click();
  await expect(page.getByText('离线模式已停用联网检索。')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Add search service', exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => window.actionCalls.filter(c => /^(save|test)_search_backend$/.test(c.command)))).toEqual([]);
});

test('committed model removal stays removed when runtime synchronization fails', async ({ page }) => {
  await setup(page, 'remove-partial'); const dialog = await catalog(page);
  await dialog.getByRole('button', { name: 'Remove', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Remove', exact: true }).click();
  await expect(dialog.locator('.model-catalog-item')).toHaveCount(0);
  await expect(dialog.getByRole('alert')).toContainText('Settings were saved');
  expect(await page.evaluate(() => window.actionCalls.filter(c => c.command === 'remove_manual_provider_model').length)).toBe(1);
});

test('tool probe refreshes capability committed before a runtime sync failure', async ({ page }) => {
  await setup(page, 'tool-partial'); const dialog = await catalog(page);
  await dialog.getByRole('button', { name: 'Test file tools', exact: true }).click();
  await expect(dialog.getByText('Native tools', { exact: true })).toBeVisible();
});

test('model settings initial load failure offers an in-place retry', async ({ page }) => {
  await setup(page, 'load-models'); await page.evaluate(() => { window.failSettings = true; });
  await navigate(page, 'Models');
  await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
  await page.evaluate(() => { window.failSettings = false; });
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Add provider', exact: true })).toBeVisible();
});

test('legacy enabled search settings cannot expose online chat controls or submit a draft', async ({ page }) => {
  await setup(page, 'connect');
  const draft = page.getByRole('textbox', { name: 'Ask agent...' });
  await draft.fill('Keep this unsent draft');
  await expect(page.locator('.web-search-control__trigger')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Connect Tavily', exact: true })).toHaveCount(0);
  await expect(draft).toHaveValue('Keep this unsent draft');
  expect(await page.evaluate(() => window.actionCalls.filter(c => /^(save_search_backend|create_conversation|start_model_run|start_conversation_model_run)$/.test(c.command)))).toEqual([]);
});

test('manual removal of a referenced model asks for confirmation before deleting', async ({ page }) => {
  await setup(page); const dialog = await catalog(page);
  await dialog.getByRole('button', { name: 'Remove', exact: true }).click();
  await expect(page.getByRole('alertdialog')).toBeVisible();
  expect(await page.evaluate(() => window.actionCalls.filter(c => c.command === 'remove_manual_provider_model').length)).toBe(0);
  await page.getByRole('alertdialog').getByRole('button', { name: 'Remove', exact: true }).click();
  await expect(dialog.locator('.model-catalog-item')).toHaveCount(0);
  expect(await page.evaluate(() => window.actionCalls.find(c => c.command === 'remove_manual_provider_model').args.confirmAffectedConversations)).toBe(true);
});

test('returning from offline search settings preserves the retained chat draft', async ({ page }) => {
  await setup(page); const draft = page.getByRole('textbox', { name: 'Ask agent...' });
  await draft.fill('Retained offline draft'); await navigate(page, 'Settings');
  await page.locator('.settings-navigation-card').filter({ hasText: 'Web & Search' }).getByRole('button', { name: 'Open', exact: true }).click();
  await expect(page.getByText('离线模式已停用联网检索。')).toBeVisible();
  await page.getByRole('button', { name: '返回', exact: true }).click();
  await page.getByRole('button', { name: 'Return to chat' }).click();
  await expect(draft).toHaveValue('Retained offline draft');
  await expect(page.locator('.web-search-control__trigger')).toHaveCount(0);
});

test('routing blur and preset change preserve both intended fields', async ({ page }) => {
  await setup(page); await navigate(page, 'Models');
  await page.getByLabel('Calls per message').fill('5');
  await page.getByLabel('Routing preset').evaluate(select => {
    select.focus(); select.value = 'quality'; select.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await expect.poll(() => page.evaluate(() => window.actionCalls.filter(c => c.command === 'update_routing_preferences').at(-1)?.args.routing)).toMatchObject({ max_model_calls_per_message: 5, default_preset: 'quality' });
});

test('trash admits a single pending mutation and disables conflicting actions', async ({ page }) => {
  await setup(page); await navigate(page, 'Settings');
  await page.getByRole('button', { name: 'Open Trash' }).click();
  const restore = page.getByRole('button', { name: 'Restore', exact: true });
  await restore.evaluate(button => { button.click(); button.click(); });
  expect(await page.evaluate(() => window.actionCalls.filter(c => c.command === 'restore_trash').length)).toBe(1);
  await expect(restore).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Delete permanently', exact: false })).toBeDisabled();
});

test('offline search notice needs no remote settings load', async ({ page }) => {
  await setup(page, 'load-search'); await navigate(page, 'Settings');
  await page.evaluate(() => { window.failSettings = true; });
  await page.locator('.settings-navigation-card').filter({ hasText: 'Web & Search' }).getByRole('button', { name: 'Open', exact: true }).click();
  await expect(page.getByText('离线模式已停用联网检索。')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Retry', exact: true })).toHaveCount(0);
});

test('offline model catalog disables native search diagnosis', async ({ page }) => {
  await setup(page, 'native'); const dialog = await catalog(page);
  await expect(dialog.getByRole('button', { name: 'Diagnose native search' })).toBeDisabled();
  expect(await page.evaluate(() => window.actionCalls.filter(c => c.command === 'probe_native_web_search'))).toEqual([]);
});

test('switching historical chats retains offline search restrictions', async ({ page }) => {
  await setup(page, 'scope');
  for (const id of ['A', 'B']) {
    await page.getByRole('button', { name: `Search chat ${id}`, exact: true }).click();
    await expect(page.locator('.web-search-control__trigger')).toHaveCount(0);
  }
  expect(await page.evaluate(() => window.actionCalls.filter(c => c.command === 'update_conversation_web_search'))).toEqual([]);
});
