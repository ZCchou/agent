import { expect, test } from '@playwright/test';
import { installTauriMock } from './tauri-mock.mjs';
const view = (state, extra = {}) => ({ state, current_version: '0.14.1', channel: 'preview', update_id: null, version: null, notes: null, downloaded_bytes: 0, total_bytes: null, error_code: null, ...extra });
const available = view('available', { update_id: 'update-1', version: '0.14.2', notes: 'A verified improvement' });
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aFz8AAAAASUVORK5CYII=';
const attachment = { id: 'image-1', name: 'image.png', kind: 'image', media_type: 'image/png', byte_size: 68, sha256: 'hash', storage_key: 'local', width: 1, height: 1, artifacts: [] };
async function setup(page, options = {}) {
  await installTauriMock(page, { app_update_status: view('idle'), load_update_draft: null, ack_update_draft: null, discard_staged_attachment: null,
    list_chat_groups: [{id: 'group-1', name: 'Study group', sort_order: 0}] });
  await page.addInitScript(({ options, available, png, attachment }) => {
    localStorage.setItem('agent:update-auto-check', options.auto ? 'true' : 'false');
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.updateCalls = []; window.updateEvent = null; window.checkCount = 0; window.discarded = [];
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      window.updateCalls.push({ command, args });
      if (command === 'plugin:event|listen' && args.event === 'app-update-progress') window.updateEvent = args.handler;
      if (command === 'app_update_status' && options.delayedStatus) return new Promise(resolve => { window.finishStatus = () => resolve({ ...available, state: 'idle', update_id: null }); });
      if (command === 'check_app_update') {
        window.checkCount++;
        if (options.error && window.checkCount === 1) throw 'app_update_network';
        return { ...available, channel: args.channel, ...options.check };
      }
      if (command === 'download_app_update') {
        if (options.signature) return { ...available, state: 'error', error_code: 'app_update_signature_invalid' };
        window.emitUpdate({ ...available, state: 'downloading', downloaded_bytes: 50, total_bytes: 100 });
        return new Promise((resolve) => { window.finishDownload = () => resolve({ ...available, state: 'ready', downloaded_bytes: 100, total_bytes: 100 }); });
      }
      if (command === 'install_app_update') {
        if (options.deferredInstall) {
          window.emitUpdate({ ...available, state: 'installing' });
          return new Promise((_, reject) => { window.failInstall = () => reject('app_update_install_failed'); });
        }
        throw 'app_update_busy';
      }
      if (command === 'load_update_draft' && options.loadFail && !window.allowDraftLoad) throw 'offline';
      if (command === 'ack_update_draft' && options.ackFail && window.updateCalls.filter(x => x.command === command).length === 1) throw 'offline';
      if (command === 'load_update_draft' && options.restore) return new Promise(resolve => { window.restoreDraft = () => resolve(options.restore); });
      if (command === 'select_message_attachments') return { accepted: [attachment], rejected: [] };
      if (command === 'preview_staged_attachment') return { id: attachment.id, kind: 'image', name: attachment.name, media_type: 'image/png', data_url: `data:image/png;base64,${png}`, content: null };
      if (command === 'discard_staged_attachment') window.discarded.push(args.attachmentId);
      return invoke(command, args);
    };
    window.emitUpdate = (payload) => window[`_${window.updateEvent}`]?.({ event: 'app-update-progress', payload });
  }, { options, available, png: PNG, attachment });
  await page.goto('http://127.0.0.1:5173');
  await expect(page.getByRole('textbox', { name: 'Ask agent...' })).toBeEnabled();
}
async function settings(page) {
  await page.getByRole('button', { name: 'System features' }).click();
  await page.getByRole('navigation').getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(page.getByText('离线模式已停用在线更新。')).toBeVisible();
}
test('offline settings offer no online update actions even with a saved auto-check preference', async ({ page }) => {
  await setup(page, { auto: true }); await settings(page);
  for (const name of ['Check for updates', 'Download update', 'Install and restart']) {
    await expect(page.getByRole('button', { name, exact: true })).toHaveCount(0);
  }
  await expect(page.getByLabel('Update channel')).toHaveCount(0);
  // Cover the former startup timer while other UI work continues.
  await page.getByRole('button', { name: 'Return to chat' }).click();
  await page.getByRole('textbox', { name: 'Ask agent...' }).fill('Offline draft');
  await page.waitForTimeout(1500);
  expect(await page.evaluate(() => window.updateCalls.filter(x => /^(check|download|install)_app_update$/.test(x.command)))).toEqual([]);
});

test('settings navigation preserves draft text and staged images', async ({ page }) => {
  await setup(page); const editor = page.getByRole('textbox', { name: 'Ask agent...' }); await editor.fill('Persistent draft');
  await page.getByRole('button', { name: 'Attach file' }).click(); await expect(page.locator('.attachment-chip img')).toBeVisible();
  await settings(page); expect(await page.evaluate(() => window.discarded)).toEqual([]);
  await page.evaluate(() => window.emitUpdate({ state: 'available', current_version: '0.14.1', channel: 'preview', update_id: 'u', version: '0.14.2' }));
  await page.getByRole('button', { name: 'Return to chat' }).click();
  await expect(editor).toHaveValue('Persistent draft'); await expect(page.locator('.attachment-chip img')).toBeVisible();
});
const restored = { update_id: 'old-update', draft: { conversation_id: null, kind: 'standard_chat', project_id: null, category: 'casual', text: 'Restored draft', attachment_ids: ['image-1'], rejected_ids: [], references: [] }, attachments: [attachment], rejected_attachments: [], missing_attachments: 0 };
test('restart draft restores once with attachments and acknowledges after applying', async ({ page }) => {
  await setup(page, { restore: restored }); await page.evaluate(() => window.restoreDraft());
  await expect(page.getByRole('textbox', { name: 'Ask agent...' })).toHaveValue('Restored draft'); await expect(page.locator('.attachment-chip img')).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.updateCalls.filter(x => x.command === 'ack_update_draft').length)).toBe(1);
  await settings(page); await page.getByRole('button', { name: 'Return to chat' }).click();
  await expect(page.getByRole('textbox', { name: 'Ask agent...' })).toHaveValue('Restored draft');
});
test('late restoration never overwrites fresh typing and offers explicit restore', async ({ page }) => {
  await setup(page, { restore: restored }); const editor = page.getByRole('textbox', { name: 'Ask agent...' }); await editor.fill('Newer draft'); await page.evaluate(() => window.restoreDraft());
  await expect(page.getByRole('button', { name: 'Restore saved draft' })).toBeVisible(); await expect(editor).toHaveValue('Newer draft');
  expect(await page.evaluate(() => window.updateCalls.filter(x => x.command === 'ack_update_draft'))).toEqual([]);
  await page.getByRole('button', { name: 'Restore saved draft' }).click(); await expect(editor).toHaveValue('Restored draft');
});

test('restoration retains explicit historical references and reports missing attachments', async ({ page }) => {
  const reference = { attachment: { ...attachment, id: 'historical-image', name: 'reference.png' }, reference: { attachment_id: 'historical-image', source_start: 2, source_end: 8 } };
  await setup(page, { restore: { ...restored, missing_attachments: 1, draft: { ...restored.draft, references: [reference] } } }); await page.evaluate(() => window.restoreDraft());
  await expect(page.getByRole('button', { name: 'Remove reference reference.png' })).toBeVisible();
  await expect(page.getByText('Some saved attachments are unavailable. Review your restored draft before sending.')).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.updateCalls.filter(x => x.command === 'ack_update_draft').length)).toBe(1);
});

test('a late initial status cannot erase a newer update progress event', async ({ page }) => {
  await setup(page, { delayedStatus: true });
  await page.evaluate((payload) => window.emitUpdate(payload), available);
  await expect(page.getByRole('button', { name: 'Open updates' })).toBeVisible();
  await page.evaluate(() => window.finishStatus());
  await expect(page.getByRole('button', { name: 'Open updates' })).toBeVisible();
});




test('saved draft load failure has an in-place retry', async ({ page }) => {
  await setup(page, { loadFail: true });
  await expect(page.getByRole('alert')).toContainText('could not be loaded');
  const before = await page.evaluate(() => window.updateCalls.filter(x => x.command === 'load_update_draft').length);
  await page.evaluate(() => { window.allowDraftLoad = true; });
  await page.getByRole('button', { name: 'Retry saved draft' }).click();
  await expect(page.getByRole('alert')).toHaveCount(0);
  expect(await page.evaluate(() => window.updateCalls.filter(x => x.command === 'load_update_draft').length)).toBe(before + 1);
});

test('failed saved draft acknowledgment retries without applying over fresh typing', async ({ page }) => {
  await setup(page, { restore: restored, ackFail: true });
  await page.evaluate(() => window.restoreDraft());
  const editor = page.getByRole('textbox', { name: 'Ask agent...' });
  await expect(editor).toHaveValue('Restored draft');
  await expect(page.getByRole('alert')).toBeVisible();
  await editor.fill('Fresh after restore');
  await page.getByRole('button', { name: 'Retry saved draft' }).click();
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(editor).toHaveValue('Fresh after restore');
  expect(await page.evaluate(() => window.updateCalls.filter(x => x.command === 'ack_update_draft').length)).toBe(2);
});
