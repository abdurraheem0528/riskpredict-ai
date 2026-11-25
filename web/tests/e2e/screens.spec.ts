import { test, expect, mapSettled, shot } from './fixtures';
import { PENDING, risks } from '../fixtures/data';

test.describe('signed out', () => {
  test.use({ signedIn: false });
  test('sign in', async ({ page }, info) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await mapSettled(page);
    await shot(page, '0-sign-in', info.project.name);
  });
});

test.describe('new workspace', () => {
  test.use({ state: 'new' });
  test('empty', async ({ page }, info) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Nothing on this map is yours yet' })).toBeVisible();
    await expect(page.getByText('9 of 10 feeds live')).toBeAttached();
    await mapSettled(page);
    await shot(page, '1-empty', info.project.name);
  });
});

test.describe('active workspace', () => {
  test('connect', async ({ page }, info) => {
    await page.goto('/connect');
    await expect(page.getByRole('heading', { name: 'Connect a source' })).toBeVisible();
    await expect(page.getByText('Connected · live')).toBeVisible();
    await mapSettled(page);
    await shot(page, '2-connect', info.project.name);
  });

  test('confirm', async ({ page }, info) => {
    await page.goto(`/sources/${PENDING}/confirm`);
    await expect(page.getByRole('heading', { name: 'Check how we read your sheet' })).toBeVisible();
    await expect(page.getByText('6 of 7 columns matched. One needs your choice.')).toBeVisible();
    await expect(page.getByText(/suppliers from 42 rows/).first()).toBeAttached();
    await mapSettled(page);
    await shot(page, '3-confirm', info.project.name);
  });

  test('main', async ({ page }, info) => {
    await page.goto('/');
    await expect(page.getByRole('button', { name: /Name matches a sanctions entry/ })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open risk' })).toBeVisible();
    await mapSettled(page);
    await shot(page, '4-main', info.project.name);
  });

  test('register', async ({ page }, info) => {
    await page.goto('/register');
    await expect(page.getByText('6 open risks')).toBeVisible();
    await mapSettled(page);
    await shot(page, '5-register', info.project.name);
  });

  test('risk', async ({ page }, info) => {
    const r = risks.find((x) => x.category === 'logistics')!;
    await page.goto(`/risks/${r.id}`);
    await expect(page.getByRole('heading', { name: r.title })).toBeVisible();
    await expect(page.getByText('What to do')).toBeVisible();
    await mapSettled(page);
    await shot(page, '6-risk', info.project.name);
  });

  test('scenario', async ({ page }, info) => {
    await page.goto('/scenarios');
    await expect(page.getByRole('heading', { name: 'Bab el-Mandeb closes' })).toBeVisible();
    await page.getByRole('button', { name: 'Run what-if' }).click();
    await expect(page.getByText('Who feels it first')).toBeVisible();
    await mapSettled(page);
    await shot(page, '7-scenario', info.project.name);
  });

  test('jump to', async ({ page }, info) => {
    test.skip(info.project.name === 'phone', 'Jump to opens from the sections menu on phones');
    await page.goto('/');
    await expect(page.getByRole('link', { name: 'Open risk' })).toBeVisible();
    await page.keyboard.press('ControlOrMeta+k');
    await expect(page.getByRole('dialog', { name: 'Jump to' })).toBeVisible();
    await mapSettled(page);
    await shot(page, '8-jump-to', info.project.name);
  });
});
