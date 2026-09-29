import { test, expect, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { getMetrics, type Snapshot } from '../src/homeData';

let errors: string[];
let externalRequests: string[];
test.beforeEach(async ({ page }) => {
  errors = []; externalRequests = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (/^https?:/.test(request.url()) && new URL(request.url()).hostname !== '127.0.0.1') externalRequests.push(request.url()); });
});
test.afterEach(() => { expect(errors).toEqual([]); expect(externalRequests).toEqual([]); });

async function ready(page: Page, animated = false) {
  await page.goto(animated ? '/' : '/?visual-test');
  await expect(page.locator('.metric strong').first()).toHaveText('4');
  await expect(page.locator('.globe-stage')).toHaveAttribute('data-state', /ready|poster/);
  await page.evaluate(async () => { await document.fonts.ready; await Promise.all([...document.images].map(i => i.decode().catch(() => {}))); });
}
const capture = (page: Page, name: string, fullPage = false) => page.screenshot({ path: `verification/${name}.png`, fullPage });

test('statistics preserve scope, time boundaries, unknown and successful empty', async () => {
  const snapshot = JSON.parse(await readFile('public/demo-snapshot.json', 'utf8')) as Snapshot;
  expect(getMetrics(snapshot).map(m => m.value)).toEqual([4, 4, 4, 3]);
  expect(getMetrics(null).map(m => m.value)).toEqual([null, null, null, null]);
  expect(getMetrics({ ...snapshot, entries: [] }).map(m => m.value)).toEqual([0, 0, 0, 0]);
  const observation = snapshot.entries.find(e => e.kind === 'activity')!;
  expect(getMetrics({ ...snapshot, entries: [
    { ...observation, id: 'start', observedAt: '2026-09-27T09:30:00Z' },
    { ...observation, id: 'end', observedAt: snapshot.asOf },
    { ...observation, id: 'future', observedAt: '2026-09-28T09:30:01Z' },
  ] })[2].value).toBe(1);
});

for (const [width, height] of [[1536, 1024], [1440, 900], [1280, 800], [768, 1024], [390, 844], [360, 800]]) {
  test(`layout ${width}x${height}, local assets and reproducible frame`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await ready(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(page.getByRole('heading', { level: 1 })).toHaveCount(1);
    await expect(page.locator('.feature-card')).toHaveCount(3);
    await expect(page.getByRole('button', { name: 'Explore Smart Money', exact: true })).toBeInViewport();
    await expect(page.locator('.globe-canvas canvas')).toHaveCount(width < 768 ? 0 : 1);
    const image = await capture(page, `home-${width}x${height}`, width < 1280);
    if (width === 1536) {
      await page.waitForTimeout(180);
      expect((await page.screenshot()).equals(image)).toBe(true);
      const title = await page.locator('h1').boundingBox();
      const cta = await page.locator('.primary-cta').boundingBox();
      expect(title!.y + title!.height).toBeLessThan(cta!.y);
      expect((await page.locator('.feature-grid').boundingBox())!.y).toBeLessThan(750);
    }
  });
}

test('navigation, every feature, filters, empty results, research and API', async ({ page }) => {
  await ready(page);
  for (const [name, tab] of [['Markets', 'Markets'], ['Traders', 'Sector Wallets'], ['Insights', 'Evidence & Research']]) {
    await page.getByRole('navigation').getByRole('button', { name, exact: true }).click();
    await expect(page.getByRole('tab', { name: tab, exact: true })).toHaveAttribute('aria-selected', 'true');
  }
  await page.getByRole('button', { name: 'API', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('The read-only API is not connected.');
  await page.keyboard.press('Escape');
  for (const title of ['Sector Wallets', 'Trade Activity', 'Evidence & Research']) {
    await page.locator('.feature-card').filter({ has: page.getByRole('heading', { name: title, exact: true }) }).click();
    await expect(page.getByRole('tab', { name: title, exact: true })).toHaveAttribute('aria-selected', 'true');
    await expect(page.locator('.collection-row').first()).toBeVisible();
  }
  await page.locator('.collection-row').first().click();
  await expect(page.getByRole('dialog')).toContainText('Not published');
  await expect(page.getByRole('dialog')).toContainText('Illustrative source');
  await capture(page, 'research-detail');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Explore Smart Money', exact: true }).click();
  await expect(page.locator('.collection-row')).toHaveCount(5);
  await page.getByLabel('Filter by action').selectOption('EXIT');
  await expect(page.locator('.collection-row')).toHaveCount(2);
  await page.getByLabel('Filter by sector').selectOption('Crypto');
  await expect(page.getByText('No matching cases', { exact: true })).toBeVisible();
  await expect(page.locator('.collection-row')).toHaveCount(0);
  await page.getByLabel('Filter by action').selectOption('REDUCE');
  await expect(page.locator('.collection-row')).toHaveCount(1);
  await page.locator('.collection-row').click();
  await expect(page.getByRole('dialog')).toContainText('REDUCE / SELL / YES');
  await expect(page.getByRole('dialog')).toContainText('50,000 → 30,000 YES shares');
});

test('search keyboard, stale query cancellation, clear, no results and focus restoration', async ({ page }) => {
  await ready(page);
  const trigger = page.getByRole('button', { name: 'Search Smart Money', exact: true });
  await trigger.focus();
  await page.keyboard.press('Control+k');
  const input = page.getByRole('combobox', { name: 'Search demo collection' });
  await expect(input).toBeFocused();
  await input.fill('Atlas'); await input.fill('protocol');
  await expect(page.getByRole('option')).toHaveCount(2);
  await expect(page.getByRole('option').first()).toContainText('protocol');
  await capture(page, 'search');
  await input.press('ArrowDown'); await input.press('Enter');
  await expect(page.getByRole('dialog')).toContainText('Source trade amount');
  await capture(page, 'observation-detail');
  for (let i = 0; i < 6; i++) { await page.keyboard.press('Tab'); expect(await page.evaluate(() => !!document.activeElement?.closest('dialog'))).toBe(true); }
  await page.keyboard.press('Escape'); await expect(trigger).toBeFocused();
  await trigger.click(); await input.fill('nothing-matches-this');
  await expect(page.getByText('No matching cases', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Clear search' }).click();
  await expect(input).toHaveValue(''); await expect(input).toBeFocused();
  await expect(page.getByRole('option')).toHaveCount(16);
  await input.fill('demo:vector'); await expect(page.getByRole('option')).toHaveCount(2);
  await page.keyboard.press('Escape');
});

test('demo asset error, retry and successful empty remain distinct', async ({ page }) => {
  await page.route('**/demo-snapshot.json', route => route.fulfill({ status: 503, body: 'unavailable' }));
  await page.goto('/?visual-test');
  await expect(page.getByText('Demo unavailable', { exact: false }).first()).toBeVisible();
  await expect(page.locator('.metric strong')).toHaveText(['—', '—', '—', '—']);
  await expect(page.locator('.observation-overlay')).toHaveCount(0);
  await page.getByRole('button', { name: 'Search Smart Money', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('Demo collection unavailable');
  await expect(page.getByRole('option')).toHaveCount(0);
  await capture(page, 'data-error');
  await page.unroute('**/demo-snapshot.json');
  await page.getByRole('button', { name: 'Retry loading', exact: true }).last().click();
  await expect(page.getByRole('option')).toHaveCount(16);
  await page.keyboard.press('Escape');
  await page.route('**/demo-snapshot.json', route => route.fulfill({ json: { mode: 'demo', asOf: '2026-09-28T09:30:00Z', entries: [] } }));
  await page.reload();
  await expect(page.locator('.metric strong')).toHaveText(['0', '0', '0', '0']);
});

test('unsupported data is rejected, never replaced with the demo', async ({ page }) => {
  await page.route('**/demo-snapshot.json', route => route.fulfill({ json: { mode: 'live', entries: [] } }));
  await page.goto('/?visual-test');
  await expect(page.getByText('Demo unavailable', { exact: false }).first()).toBeVisible();
  await expect(page.locator('.metric strong')).toHaveText(['—', '—', '—', '—']);
});

test('animation advances, pause freezes, modal and offscreen suspend rendering', async ({ page }) => {
  await ready(page, true);
  const stats = () => page.evaluate(() => window.__SMART_MONEY_GLOBE__!.stats());
  const first = await stats();
  await expect.poll(async () => (await stats()).time).toBeGreaterThan(first.time);
  await page.getByRole('button', { name: 'Pause animation', exact: true }).click();
  const stopped = await stats(); await page.waitForTimeout(200);
  expect(await stats()).toEqual(stopped);
  await page.getByRole('button', { name: 'Resume animation', exact: true }).click();
  await expect.poll(async () => (await stats()).time).toBeGreaterThan(stopped.time);
  await page.getByRole('button', { name: 'Search Smart Money', exact: true }).click();
  await expect.poll(async () => (await stats()).running).toBe(false);
  await page.keyboard.press('Escape');
  await expect.poll(async () => (await stats()).running).toBe(true);
  await page.getByRole('button', { name: 'Explore Smart Money', exact: true }).click();
  await page.locator('.collection-row').last().scrollIntoViewIfNeeded();
  await expect.poll(async () => (await stats()).running).toBe(false);
  await page.evaluate(() => scrollTo(0, 0));
  await expect.poll(async () => (await stats()).running).toBe(true);
  // Exercise the actual visibility handler with a controlled hidden document.
  await page.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => true }); document.dispatchEvent(new Event('visibilitychange')); });
  await expect.poll(async () => (await stats()).running).toBe(false);
  await page.evaluate(() => { delete (document as unknown as Record<string, unknown>).hidden; document.dispatchEvent(new Event('visibilitychange')); });
  await expect.poll(async () => (await stats()).running).toBe(true);
  await expect(page.locator('canvas')).toHaveCount(1);
});

test('reduced motion changes dispose and rebuild only one scene', async ({ page }) => {
  await ready(page);
  for (let i = 0; i < 2; i++) {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await expect(page.locator('.globe-stage')).toHaveAttribute('data-state', 'poster');
    await expect(page.locator('canvas')).toHaveCount(0);
    await expect(page.locator('.globe-poster')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Enable animation' })).toHaveCount(0);
    if (i === 0) await capture(page, 'reduced-motion');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await expect(page.locator('.globe-stage')).toHaveAttribute('data-state', 'ready');
    await expect(page.locator('canvas')).toHaveCount(1);
  }
});

test('WebGL unavailable, context lost, and mask failure keep the homepage usable', async ({ page }) => {
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, kind: string, ...args: unknown[]) { return kind.startsWith('webgl') ? null : original.apply(this, [kind, ...args] as Parameters<typeof original>); } as typeof original;
  });
  await ready(page);
  await expect(page.locator('.globe-stage')).toHaveAttribute('data-state', 'poster');
  await expect(page.locator('canvas')).toHaveCount(0);
  await capture(page, 'webgl-fallback');
  await page.getByRole('button', { name: 'Explore Smart Money', exact: true }).click();
  await expect(page.locator('.collection-row')).toHaveCount(5);
});

test('context loss and land-mask failure dispose the scene without breaking search', async ({ page }) => {
  await ready(page);
  await page.locator('canvas').evaluate(canvas => (canvas as HTMLCanvasElement).getContext('webgl2')!.getExtension('WEBGL_lose_context')!.loseContext());
  await expect(page.locator('.globe-stage')).toHaveAttribute('data-state', 'poster');
  await expect(page.locator('canvas')).toHaveCount(0);
  await page.getByRole('button', { name: 'Search Smart Money', exact: true }).click();
  await expect(page.getByRole('option')).toHaveCount(16);
  await page.route('**/earth-land-mask.png', route => route.abort());
  await page.reload();
  await expect(page.locator('.globe-stage')).toHaveAttribute('data-state', 'poster');
  await expect(page.locator('.globe-poster')).toBeVisible();
  await capture(page, 'mask-fallback');
});

test('mobile menu, filtering, drawer, touch controls and animation opt-in', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await ready(page);
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('button', { name: 'Traders', exact: true }).click();
  await page.getByLabel('Filter by sector').selectOption('Crypto');
  await expect(page.locator('.collection-row')).toHaveCount(1);
  await page.locator('.collection-row').click();
  await expect(page.getByRole('dialog')).toContainText('Follow eligibility');
  await capture(page, 'mobile-detail');
  await page.getByRole('button', { name: 'Close dialog' }).click();
  await page.getByRole('link', { name: 'Smart Money', exact: true }).click();
  await page.getByRole('button', { name: 'Enable animation' }).click();
  await expect(page.locator('canvas')).toHaveCount(1);
  await expect(page.locator('.globe-stage')).toHaveAttribute('data-state', 'ready');
  expect((await page.evaluate(() => window.__SMART_MONEY_GLOBE__!.stats())).dpr).toBe(1);
});
