import { expect, test } from '@playwright/test';
import { installTauriMock } from './tauri-mock.mjs';

async function setup(page, options = {}) {
  await installTauriMock(page);
  await page.addInitScript(({ options }) => {
    const invoke = window.__TAURI_INTERNALS__.invoke;
    window.migrationCalls = [];
    window.migrationState = options.recovery ? { status: 'interrupted', phase: 'publishing', error: { code: 'migration_restart_required' } } : null;
    window.__TAURI_INTERNALS__.invoke = async (command, args) => {
      if (command !== 'profile_migration_request') return invoke(command, args);
      window.migrationCalls.push(args);
      if (args.action === 'discover') return { profiles: [{ id: 'document', label: 'Document Preview', root: 'C:/profiles/document' },
        { id: 'ocr', label: 'OCR Preview', root: 'C:/profiles/ocr' }], model_root: null,
        latest_job_id: options.recovery ? 'job-1' : null, recovery_required: !!options.recovery };
      if (args.action === 'choose_storage') return { model_root: 'F:/agent-models/airworthiness' };
      if (args.action === 'prepare') {
        window.migrationState = { status: 'preview_ready', phase: 'preview_ready', preview: {
          conflicting_groups: 1, can_prepare: !options.insufficient, storage: [
            { purpose: 'profile', path: 'C:/profile-snapshots', required_bytes: 2 * 1024 ** 3, available_bytes: options.insufficient ? 1 : 4 * 1024 ** 3, sufficient: !options.insufficient },
            { purpose: 'models', path: 'F:/agent-models/airworthiness', required_bytes: 6 * 1024 ** 3, available_bytes: 100 * 1024 ** 3, sufficient: true },
          ],
        } };
        return { job_id: 'job-1' };
      }
      if (args.action === 'confirm_prepare') {
        window.migrationState = options.processing ? { status: 'running', phase: 'copying_files', current_file: 'model.onnx', completed_bytes: 50, total_bytes: 100 }
          : { status: 'prepared', phase: 'prepared' };
        return { job_id: 'job-1' };
      }
      if (args.action === 'status') return { job_id: 'job-1', state: window.migrationState };
      if (args.action === 'activate') window.migrationState = { status: 'awaiting_restart', phase: 'awaiting_restart' };
      if (args.action === 'cancel') window.migrationState = { status: 'cancelled', phase: 'cancelled' };
      if (args.action === 'rollback') window.migrationState = { status: 'rolled_back', phase: 'rolled_back' };
      return {};
    };
  }, { options });
  await page.goto('http://127.0.0.1:5173');
  if (!options.recovery) {
    const settings = page.getByRole('navigation').getByRole('button', { name: 'Settings', exact: true });
    if (!await settings.isVisible()) await page.getByRole('button', { name: 'System features' }).click();
    await settings.click();
  }
  await expect(page.getByRole('heading', { name: 'Import preview data' })).toBeVisible();
}

test('migration requires a native-selected model directory and preserves both source selections', async ({ page }) => {
  await setup(page);
  const prepare = page.getByRole('button', { name: 'Check migration', exact: true });
  await expect(prepare).toBeDisabled();
  await expect(page.getByText('Choose a dedicated agent model directory first', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Choose model directory' }).click();
  await expect(page.getByText('F:/agent-models/airworthiness')).toBeVisible();
  await prepare.click();
  await expect(page.getByText('C:/profile-snapshots', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Prepare verified copy', exact: true }).click();
  await expect(page.getByText('Verified copy ready to activate')).toBeVisible();
  expect(await page.evaluate(() => window.migrationCalls.find(call => call.action === 'prepare').sourceIds)).toEqual(['document', 'ocr']);
  page.on('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Switch and restart' }).click();
  await expect.poll(() => page.evaluate(() => window.migrationCalls.filter(call => call.action === 'restart').length)).toBe(1);
});

test('copy progress remains stage-local and cancellation is explicit', async ({ page }) => {
  await setup(page, { processing: true });
  await page.getByRole('button', { name: 'Choose model directory' }).click();
  await page.getByRole('button', { name: 'Check migration', exact: true }).click();
  await page.getByRole('button', { name: 'Prepare verified copy', exact: true }).click();
  await expect(page.getByText('model.onnx')).toBeVisible();
  await expect(page.getByRole('progressbar', { name: 'Current file copy progress' })).toHaveAttribute('value', '50');
  page.on('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Cancel preparation' }).click();
  await expect(page.getByText('Cancelled', { exact: true })).toBeVisible();
});

test('insufficient profile space prevents copying despite ample model space', async ({ page }) => {
  await setup(page, { insufficient: true });
  await page.getByRole('button', { name: 'Choose model directory' }).click();
  await page.getByRole('button', { name: 'Check migration', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Prepare verified copy', exact: true })).toBeDisabled();
  await expect(page.getByText('C:/profile-snapshots', { exact: false })).toBeVisible();
  expect(await page.evaluate(() => window.migrationCalls.some(call => call.action === 'confirm_prepare'))).toBe(false);
});

test('interrupted activation opens migration recovery with an available restore action', async ({ page }) => {
  await setup(page, { recovery: true });
  await expect(page.getByRole('button', { name: 'Choose model directory' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Restore previous data' })).toBeEnabled();
  page.on('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Restore previous data' }).click();
  await expect(page.getByText('Previous data restored')).toBeVisible();
});
