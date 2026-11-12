import { expect, test } from '@playwright/test';
import { installTauriMock } from './tauri-mock.mjs';

test('force RAG persists for the conversation and can return to automatic', async ({ page }) => {
  const { work } = await installTauriMock(page);
  await page.addInitScript(() => {
    const original = window.__TAURI_INTERNALS__.invoke;
    const modes = {};
    window.__ragModeUpdates = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args = {}) => {
      if (command === 'update_conversation_rag') {
        modes[args.conversationId] = args.mode;
        window.__ragModeUpdates.push(args);
        return { ...await original('get_conversation', { conversationId: args.conversationId }), rag_mode: args.mode };
      }
      const value = await original(command, args);
      if (command === 'get_conversation') return { ...value, rag_mode: modes[value.id] ?? 'auto' };
      if (command === 'list_conversations') return value.map(item => ({ ...item, rag_mode: modes[item.id] ?? 'auto' }));
      return value;
    };
  });
  await page.goto('http://127.0.0.1:5173');
  await page.getByRole('button', { name: work.title, exact: true }).click();
  const toggle = page.getByRole('button', { name: 'Force RAG', exact: true });
  await expect(toggle).toHaveAttribute('aria-pressed', 'false');
  await toggle.click();
  await expect(page.getByRole('button', { name: 'Force RAG · On', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.locator('.sidebar-new-chat').click();
  await expect(toggle).toHaveAttribute('aria-pressed', 'false');
  await page.getByRole('button', { name: work.title, exact: true }).click();
  await page.getByRole('button', { name: 'Force RAG · On', exact: true }).click();
  await expect(toggle).toHaveAttribute('aria-pressed', 'false');
  expect(await page.evaluate(() => window.__ragModeUpdates.map(item => item.mode))).toEqual(['required', 'auto']);
});

test('forced draft without a project retains the message and offers project setup', async ({ page }) => {
  await installTauriMock(page);
  await page.goto('http://127.0.0.1:5173');
  await page.getByRole('button', { name: 'Force RAG', exact: true }).click();
  const input = page.getByRole('textbox', { name: 'Ask agent...' });
  await input.fill('Hello');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(input).toHaveValue('Hello');
  await expect(page.getByRole('button', { name: 'Choose a project and its libraries' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Force RAG · On', exact: true })).toHaveAttribute('aria-pressed', 'true');
});
