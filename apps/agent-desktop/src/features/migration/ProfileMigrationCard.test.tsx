import { expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { MigrationProgress } from './ProfileMigrationCard';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

it('shows stage-local file progress without an overall percentage', () => {
  const html = renderToStaticMarkup(<MigrationProgress state={{ status: 'running', phase: 'copying_files',
    current_file: 'model.onnx', completed_bytes: 50, total_bytes: 100 }} />);
  expect(html).toContain('settings.migration.phases.copying_files');
  expect(html).toContain('model.onnx');
  expect(html).toContain('value="50"');
  expect(html).toContain('max="100"');
  expect(html).not.toContain('50%');
});

it('shows a concrete error and preserves the prepared recovery status', () => {
  const html = renderToStaticMarkup(<MigrationProgress state={{ status: 'failed', phase: 'publishing',
    error: { code: 'migration_source_changed', message: '原资料已更改，请重新准备。' } }} />);
  expect(html).toContain('role="alert"');
  expect(html).toContain('原资料已更改，请重新准备。');
});

it('shows profile and model capacity before preparation', () => {
  const html = renderToStaticMarkup(<MigrationProgress state={{ status: 'preview_ready', preview: {
    conflicting_groups: 2, can_prepare: false, storage: [
      { purpose: 'profile', path: 'C:/profile', required_bytes: 200, available_bytes: 100, sufficient: false },
      { purpose: 'models', path: 'F:/models', required_bytes: 500, available_bytes: 1000, sufficient: true },
    ],
  } }} />);
  expect(html).toContain('C:/profile');
  expect(html).toContain('F:/models');
  expect(html).toContain('settings.migration.spaceInsufficient');
  expect(html).toContain('settings.migration.conflictEstimate');
});
