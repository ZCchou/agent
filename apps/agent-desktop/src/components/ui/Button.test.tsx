import { expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { Button, IconButton, ButtonGroup } from './Button';

it('uses regular, dense and composer geometry with safe default button type', () => {
  expect(renderToStaticMarkup(<Button>导入</Button>)).toContain('ui-button--regular');
  expect(renderToStaticMarkup(<Button size="dense">导入</Button>)).toContain('ui-button--dense');
  expect(renderToStaticMarkup(<Button size="composer">导入</Button>)).toContain('ui-button--composer');
  expect(renderToStaticMarkup(<Button>导入</Button>)).toContain('type="button"');
  expect(renderToStaticMarkup(<Button type="submit">保存</Button>)).toContain('type="submit"');
});

it('keeps disabled reasons accessible and exposes loading state', () => {
  const html = renderToStaticMarkup(<Button disabled disabledReason="请先配置模型" busy>加载</Button>);
  expect(html).toContain('aria-busy="true"');
  expect(html).toContain('请先配置模型');
  expect(html).toContain('role="note"');
});

it('requires an accessible label for icon actions and labels action groups', () => {
  const html = renderToStaticMarkup(<ButtonGroup label="文档操作"><IconButton label="删除">x</IconButton></ButtonGroup>);
  expect(html).toContain('aria-label="删除"');
  expect(html).toContain('role="group"');
  expect(html).toContain('aria-label="文档操作"');
});
