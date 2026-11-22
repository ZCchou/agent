import { test, expect } from '@playwright/test';

test('frontend renders without the desktop window bridge', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));

  await page.goto('http://127.0.0.1:5173');
  await expect(page.locator('.window-root')).toBeVisible();
  await expect(page.locator('.window-titlebar')).toBeVisible();
  expect(errors).toEqual([]);
});
