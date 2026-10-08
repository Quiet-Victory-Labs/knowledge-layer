import { expect, test } from '@playwright/test';

test('developer sign-in, source discovery, review and knowledge map work', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('response', response => {
    if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`);
  });
  page.on('console', message => {
    if (message.type() === 'error' || message.type() === 'warning') errors.push(message.text());
  });

  const auth = await page.request.get('/auth/status', { headers: { Accept: 'application/json' } });
  expect(auth.ok()).toBe(true);
  expect(await auth.json()).toMatchObject({ mode: 'demo', authenticated: true });
  const setup = await page.request.get('/api/setup');
  expect(setup.ok()).toBe(true);
  expect((await setup.json()).agent.endpoint).toBe(`${new URL(auth.url()).origin}/agent/mcp/`);
  await page.goto('/');
  await expect(page).toHaveTitle(/Knowledge layer/i);
  await page.getByRole('button', { name: 'Setup', exact: true }).click();
  await page.getByRole('tab', { name: '2. Sources', exact: true }).click();
  await page.getByRole('button', { name: 'Connect source', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('textbox', { name: 'Connection name' }).fill(`Smoke ${info.project.name}`);
  await dialog.getByRole('button', { name: 'Connect source', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Build knowledge', exact: true })).toBeVisible();
  await expect(page.getByText('Connection saved and initial discovery finished.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Review drafts', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Review', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Evidence and details', exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Inspect knowledge', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Knowledge map', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Knowledge map', exact: true })).toBeVisible();
  await expect(page.getByText(/sources loaded.*items in view/)).toBeVisible();

  if (info.project.name === 'mobile') {
    await expect(page.getByRole('tab', { name: 'Records', exact: true })).toHaveAttribute('aria-selected', 'true');
    const dimensions = await page.evaluate(() => ({
      width: document.documentElement.clientWidth,
      content: document.documentElement.scrollWidth,
    }));
    expect(dimensions.content).toBeLessThanOrEqual(dimensions.width);
  } else {
    await expect(page.getByRole('tab', { name: 'Graph', exact: true })).toHaveAttribute('aria-selected', 'true');
    await page.getByRole('button', { name: new RegExp(`Smoke ${info.project.name}.*items.*need review`) }).click();
    await expect(page.locator('[aria-label="Knowledge graph"]')).toBeVisible();
  }
  expect(errors).toEqual([]);
});
