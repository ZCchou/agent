import { expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { AppUpdateCard } from './AppUpdateCard';

const { update } = vi.hoisted(() => ({ update: {
  view: { state: 'idle', current_version: '0.16.0-preview.1', version: null, notes: null, downloaded_bytes: 0, total_bytes: null },
  working: false, error: null, check: vi.fn(), download: vi.fn(), install: vi.fn(), retry: vi.fn(),
} }));
vi.mock('./AppUpdateProvider', () => ({ useAppUpdate: () => update }));
vi.mock('../../contexts/AppContext', () => ({ useApp: () => ({ setCurrentPage: vi.fn() }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string, values?: { version?: string }) => ({
  'settings.updater.checkProgram': '检查程序更新', 'settings.updater.downloadProgram': '下载程序更新',
  'settings.updater.install': '安装并重启', 'settings.updater.programHint': '模型权重使用本地导入',
  'settings.updater.current': values?.version,
}[key] || key) }) }));

it('offers an explicit manual program update check and identifies excluded large files', () => {
  const html = renderToStaticMarkup(<AppUpdateCard />);
  expect(html).toContain('检查程序更新');
  expect(html).toContain('0.16.0-preview.1');
  expect(html).toContain('模型权重');
  expect(html).not.toContain('type="checkbox"');
});

it('shows verified download and install as distinct actions', () => {
  update.view.state = 'available';
  expect(renderToStaticMarkup(<AppUpdateCard />)).toContain('下载程序更新');
  update.view.state = 'ready';
  expect(renderToStaticMarkup(<AppUpdateCard />)).toContain('安装并重启');
  update.view.state = 'idle';
});
