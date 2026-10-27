/* Optional browser regression checks. Uses an existing Playwright installation:
 * SORTLY_PLAYWRIGHT_MODULE=<module directory> node frontend/tests/workspace.browser.cjs
 * Start Vite first (SORTLY_UI_URL defaults to http://127.0.0.1:5178).
 * Every API request is intercepted. No actual files, providers, or credentials are changed.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.SORTLY_PLAYWRIGHT_MODULE || 'playwright');
const output = path.resolve(__dirname, '../../build/ui-refactor');
fs.mkdirSync(output, { recursive: true });
const source = 'C:\\UI fixtures\\Library';
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function until(check, label, timeout = 6000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    if (await check()) return;
    await pause(30);
  }
  throw new Error(`Timed out: ${label}`);
}

function plan(id, sourcePath = source) {
  const file = { current_path: source, selected: true, confidence: 'high', evidence_excerpt: 'Orchid habitat observations', evidence_sources: ['extracted_content'], warnings: [], filename_conflict: 'none', evidence_sufficient: true, detected_type: '.txt', extraction_status: 'sufficient', rename_reason: 'The document identifies an orchid habitat survey.', sensitive: false };
  return { plan_id: id, source: sourcePath, analysis_seconds: 1, already_organized: 0, plan_warnings: [], requires_confirmation: false,
    scan_summary: { files_seen: 2, skipped: 0, reasons: {}, truncated: false }, files: [
      { ...file, id: '0', name: 'draft.txt', destination_name: 'orchid_survey_notes.txt', target_path: 'Orchid Fieldwork/Habitat Surveys', category: 'Orchid Fieldwork/Habitat Surveys', reasoning: 'Extracted content describes an orchid habitat survey.', renamed: true },
      { ...file, id: '1', name: 'receipt.pdf', destination_name: 'receipt.pdf', target_path: 'Household/Invoices', category: 'Household/Invoices', reasoning: 'An ordinary household receipt.', renamed: false },
    ] };
}

async function setup(browser) {
  const context = await browser.newContext({ viewport: { width: 1180, height: 820 }, colorScheme: 'dark' });
  const page = await context.newPage();
  const state = { calls: [], sequence: 0, plans: new Map(), previewMode: 'ok', chatMode: 'ok', browseCancel: false, applied: 0, errors: [] };
  page.on('pageerror', (error) => state.errors.push(error.message));
  await page.route('**/*', async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (!url.pathname.startsWith('/api/') && !url.pathname.startsWith('/history')) return route.continue();
    const body = req.method() === 'POST' ? req.postDataJSON() : undefined;
    state.calls.push({ route: url.pathname, body });
    const reply = async (json, status = 200) => {
      try { await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(json) }); } catch { /* A cancelled fetch can already be closed. */ }
    };
    if (url.pathname === '/api/choose-folder') return state.browseCancel ? reply({ detail: 'No folder was selected.' }, 400) : reply({ source: state.browseSource ?? source });
    if (url.pathname === '/api/preview') {
      const mode = state.previewMode;
      if (mode === 'slow') await pause(state.previewDelay ?? 1300);
      if (mode === 'error') return reply({ detail: 'Provider unavailable for analysis.' }, 502);
      const next = plan(`plan-${++state.sequence}`, body.source);
      if (state.extensionChange) {
        Object.assign(next.files[0], { name: 'report.html', destination_name: 'report.csv', detected_type: '.csv', filename_conflict: 'extension_mismatch' });
        Object.assign(next.files[1], { destination_name: 'household_receipt.pdf', renamed: true });
      }
      if (state.compactReview) {
        next.files.push({ ...next.files[1], id: '2', name: 'credentials.env', destination_name: 'credentials.env',
          sensitive: true, selected: false, warnings: ['Potential sensitive content detected.'],
          target_path: 'Misc/Sensitive Review', category: 'Misc/Sensitive Review' });
        next.scan_summary.files_seen = 3;
      }
      state.plans.set(next.plan_id, structuredClone(next));
      return reply(next);
    }
    if (url.pathname === '/api/chat') {
      if (state.chatMode === 'slow') await pause(400);
      if (state.chatMode === 'error') return reply({ detail: 'Chat provider unavailable.' }, 502);
      if (/college/i.test(body.message)) return reply({ result: { reply: 'Which college subjects?', needs_clarification: 'Which college subjects?', rules: [] }, preview: null });
      const next = structuredClone(state.plans.get(body.plan_id));
      assert.ok(next, `Unknown plan sent to chat: ${body.plan_id}`);
      if (body.files) for (const edit of body.files) {
        const item = next.files.find((file) => file.id === edit.id);
        Object.assign(item, { selected: edit.accepted, target_path: edit.target_path || item.target_path, destination_name: edit.destination_name || item.destination_name });
      }
      let rule;
      if (/receipts/i.test(body.message)) {
        next.files[1].selected = false;
        rule = { action: 'exclude', match: { extension: '.pdf' }, target: null, reason: 'user request' };
      } else {
        rule = { action: 'move', match: { topic: 'orchid habitat surveys' }, target: next.files[0].target_path, reason: 'user request' };
      }
      next.plan_id = `plan-${++state.sequence}`;
      state.plans.set(next.plan_id, structuredClone(next));
      return reply({ result: { reply: 'The preview reflects your instruction.', needs_clarification: null, rules: [rule] }, preview: next });
    }
    if (url.pathname === '/api/apply') {
      state.applied++;
      return reply({ status: 'success', moved: body.files.filter((file) => file.accepted).length, requested: 1, failed: 0, failures: [], kept_in_root: 1, undo_available: true,
        hierarchy: { name: 'Library', files: [], folders: [{ name: 'Orchid Fieldwork', files: [], folders: [{ name: 'Habitat Surveys', files: ['orchid_survey_notes.txt'], folders: [] }] }] } });
    }
    if (url.pathname === '/api/undo') return reply({ status: 'success', restored: 1, skipped: [] });
    if (url.pathname === '/api/providers/config') return reply({ configured: true, primary: 'ollama', fallbacks: [], connections: {
      ollama: { provider: 'ollama', model: 'test-model', base_url: 'http://localhost:11434/v1', has_api_key: false, api_key_source: 'missing' },
    }, providers: [{ id: 'ollama', label: 'Ollama', default_base_url: 'http://localhost:11434/v1', requires_key: false }] });
    if (url.pathname.startsWith('/history')) return reply([]);
    return reply({ status: 'ok' });
  });
  await page.goto(process.env.SORTLY_UI_URL || 'http://127.0.0.1:5178');
  return { context, page, state };
}

async function queue(page, text) {
  await page.getByRole('textbox', { name: 'Instructions for Sortly' }).fill(text);
  await page.getByRole('button', { name: 'Send message' }).click();
}

async function main() {
  const browser = await chromium.launch({ channel: process.env.SORTLY_BROWSER_CHANNEL || 'msedge', headless: true });
  const passed = [];
  async function scenario(name, run) {
    const session = await setup(browser);
    try { await run(session); assert.deepEqual(session.state.errors, []); passed.push(name); console.log(`PASS ${name}`); }
    catch (error) { await session.page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }); throw error; }
    finally { await session.context.close(); }
  }
  try {
    await scenario('folder selection, settings mappings, multi-message instructions, tree, navigation, approval', async ({ page, state }) => {
      assert.equal(await page.locator('aside').count(), 0);
      assert.deepEqual(await page.getByRole('navigation', { name: 'Workspace' }).getByRole('button').allTextContents(), ['Preview', 'Settings', 'Providers', 'History']);
      assert.equal(await page.getByRole('button', { name: 'Preview', exact: true }).isEnabled(), true);
      assert.equal(await page.getByRole('button', { name: 'Analyze folder', exact: true }).isDisabled(), true);
      await page.screenshot({ path: path.join(output, 'initial.png'), fullPage: true });
      await page.getByRole('button', { name: 'Browse', exact: true }).click();
      await until(async () => await page.getByRole('textbox', { name: 'Folder directory' }).inputValue() === source, 'folder selection');
      await page.getByRole('button', { name: 'Settings', exact: true }).click();
      assert.equal(await page.getByRole('button', { name: 'Undo last move', exact: true }).count(), 0);
      assert.equal(await page.getByRole('switch', { name: 'Include subfolders' }).isChecked(), false);
      assert.equal(await page.getByRole('switch', { name: 'Suggest cleaner file names' }).isChecked(), true);
      assert.equal(await page.getByRole('switch', { name: 'Group similar files locally' }).count(), 0);
      await page.getByText('Include subfolders', { exact: true }).click();
      await page.getByText('Reorganize already sorted files', { exact: true }).click();
      await page.getByText('Allow redacted cloud analysis for sensitive files', { exact: true }).click();
      await page.screenshot({ path: path.join(output, 'settings.png'), fullPage: true });
      await page.getByRole('button', { name: 'Back', exact: true }).click();
      const bar = page.getByRole('region', { name: 'Ask Sortly' });
      const initialHeight = (await bar.boundingBox()).height;
      await page.getByRole('button', { name: /Ask Sortly/ }).click();
      await until(async () => (await bar.boundingBox()).height > initialHeight + 50, 'same-bar expansion');
      await queue(page, 'Keep botanical notes together');
      await queue(page, 'Leave receipts untouched');
      assert.equal(state.calls.filter((call) => call.route === '/api/chat').length, 0);
      await page.getByRole('textbox', { name: 'Instructions for Sortly' }).fill('Reuse my existing folders');
      await page.getByRole('button', { name: 'Settings', exact: true }).click();
      await page.getByRole('button', { name: 'Back', exact: true }).click();
      assert.equal(await page.getByRole('textbox', { name: 'Instructions for Sortly' }).inputValue(), 'Reuse my existing folders');
      await page.getByRole('button', { name: 'Analyze folder', exact: true }).click();
      await until(async () => await page.getByRole('heading', { name: 'Review files by destination', exact: true }).isVisible(), 'automatic preview navigation');
      assert.equal(await page.locator('.sortly-preview .ui-tree').count(), 0);
      assert.equal(await page.getByText('Confirmed destinations', { exact: true }).count(), 0);
      assert.deepEqual(state.calls.find((call) => call.route === '/api/preview').body, { source, recursive: true, rename: true, reorganize: true, allow_sensitive_cloud: true });
      const chats = state.calls.filter((call) => call.route === '/api/chat');
      assert.deepEqual(chats.map((call) => call.body.message), ['Keep botanical notes together', 'Leave receipts untouched', 'Reuse my existing folders']);
      assert.notEqual(chats[0].body.plan_id, chats[1].body.plan_id);
      const review = page.locator('.review-groups');
      assert.match(await review.innerText(), /Orchid Fieldwork/);
      assert.match(await review.innerText(), /Habitat Surveys/);
      assert.match(await review.innerText(), /orchid_survey_notes.txt/);
      assert.match(await review.innerText(), /receipt.pdf/);
      const before = await review.innerText();
      await page.getByRole('button', { name: 'Settings', exact: true }).click();
      await page.getByRole('button', { name: 'Preview', exact: true }).click();
      assert.equal(await review.innerText(), before);
      await page.locator('.review-groups').scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(output, 'preview.png'), fullPage: true });
      assert.equal(state.applied, 0);
      await page.getByRole('button', { name: 'Review & organize', exact: true }).click();
      await page.getByRole('button', { name: 'Cancel', exact: true }).click();
      assert.equal(state.applied, 0);
      await page.getByRole('button', { name: 'Review & organize', exact: true }).click();
      await page.getByRole('button', { name: 'Confirm moves', exact: true }).click();
      await until(() => state.applied === 1, 'explicit approval');
      await until(async () => await page.getByText('Confirmed destinations', { exact: true }).isVisible(), 'hierarchy appears only after confirmation');
      assert.match(await page.getByText('Confirmed destinations', { exact: true }).locator('..').locator('..').innerText(), /Orchid Fieldwork/);
      await until(async () => await page.getByRole('button', { name: 'Review & organize', exact: true }).count() === 0, 'consumed plan invalidated');
      assert.equal(state.calls.find((call) => call.route === '/api/apply').body.files.filter((file) => file.accepted).length, 1);
      assert.match(await page.getByRole('log').innerText(), /Keep botanical notes together/);
      await page.getByRole('button', { name: 'History', exact: true }).click();
      const undo = page.getByRole('button', { name: 'Undo last move', exact: true });
      await until(async () => await undo.isEnabled(), 'undo available in History after Apply');
      await undo.click();
      await until(() => state.calls.some((call) => call.route === '/api/undo'), 'existing undo endpoint');
      assert.deepEqual(state.calls.find((call) => call.route === '/api/undo').body, { source });
      await until(async () => await undo.isDisabled(), 'undo availability cleared after success');
    });

    await scenario('optional chat, settings defaults, appearance persistence, providers and history', async ({ page, state }) => {
      await page.getByRole('textbox', { name: 'Folder directory' }).fill(source);
      await page.getByRole('button', { name: 'Analyze folder', exact: true }).click();
      await until(async () => await page.getByRole('heading', { name: 'Review files by destination', exact: true }).isVisible(), 'plan without instructions');
      assert.equal(state.calls.filter((call) => call.route === '/api/chat').length, 0);
      assert.deepEqual(state.calls.find((call) => call.route === '/api/preview').body, { source, recursive: false, rename: true, reorganize: false, allow_sensitive_cloud: false });
      await page.getByRole('button', { name: 'Settings', exact: true }).click();
      await page.getByText('Appearance', { exact: true }).first().click();
      await page.getByText('Light', { exact: true }).click();
      assert.equal(await page.evaluate(() => localStorage.getItem('sortly.theme')), 'light');
      await page.getByRole('button', { name: 'Providers', exact: true }).click();
      await until(() => state.calls.some((call) => call.route === '/api/providers/config'), 'existing provider API');
      assert.equal(await page.getByRole('heading', { name: 'Providers', exact: true }).isVisible(), true);
      assert.equal(new URL(page.url()).hash, '#/providers');
      await page.getByRole('button', { name: 'History', exact: true }).click();
      await until(() => state.calls.some((call) => call.route === '/history'), 'existing history API');
      assert.equal(new URL(page.url()).hash, '#/history');
      assert.equal(await page.getByRole('heading', { name: 'History', exact: true }).isVisible(), true);
      await page.getByRole('button', { name: 'Preview', exact: true }).click();
      assert.equal(await page.locator('html').getAttribute('data-theme'), 'light');
      await page.setViewportSize({ width: 390, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      await page.screenshot({ path: path.join(output, 'mobile-light.png'), fullPage: true });
      await page.setViewportSize({ width: 320, height: 740 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      await page.getByRole('button', { name: 'History', exact: true }).click();
      await page.reload();
      assert.equal(await page.locator('html').getAttribute('data-theme'), 'light');
      assert.equal(await page.getByRole('button', { name: 'Preview', exact: true }).isEnabled(), true);
      assert.equal(await page.getByRole('heading', { name: 'History', exact: true }).isVisible(), true);
    });

    await scenario('analysis errors and cancellation cannot enable stale previews', async ({ page, state }) => {
      state.browseCancel = true;
      await page.getByRole('button', { name: 'Browse', exact: true }).click();
      await until(async () => await page.getByRole('button', { name: 'Browse', exact: true }).isEnabled(), 'cancelled folder picker');
      assert.equal(await page.getByRole('textbox', { name: 'Folder directory' }).inputValue(), '');
      assert.equal(await page.getByRole('alert').count(), 0);
      await page.getByRole('textbox', { name: 'Folder directory' }).fill(source);
      state.previewMode = 'error';
      await page.getByRole('button', { name: 'Analyze folder', exact: true }).click();
      await until(async () => (await page.locator('body').innerText()).includes('Provider unavailable for analysis.'), 'analysis error');
      assert.equal(await page.getByRole('heading', { name: 'Review files by destination', exact: true }).count(), 0);
      assert.equal(await page.getByRole('button', { name: 'Preview', exact: true }).isEnabled(), true);
      state.previewMode = 'slow';
      await page.getByRole('button', { name: 'Analyze folder', exact: true }).click();
      await page.getByRole('button', { name: 'Cancel analysis' }).click();
      await pause(1500);
      assert.equal(await page.getByRole('heading', { name: 'Review files by destination', exact: true }).count(), 0);
      assert.equal(state.applied, 0);
      state.previewMode = 'ok';
      await page.getByRole('button', { name: 'Analyze folder', exact: true }).click();
      await until(async () => await page.getByRole('heading', { name: 'Review files by destination', exact: true }).isVisible(), 'analysis retry after cancellation');
    });

    await scenario('real clarification and chat failure retain the conversation and prevent accidental approval', async ({ page, state }) => {
      await page.getByRole('textbox', { name: 'Folder directory' }).fill(source);
      await page.getByRole('button', { name: /Ask Sortly/ }).click();
      await queue(page, 'Organize my college material');
      await page.getByRole('button', { name: 'Analyze folder', exact: true }).click();
      await until(async () => await page.getByRole('heading', { name: 'Review files by destination', exact: true }).isVisible(), 'clarification preview');
      assert.match(await page.getByRole('log').innerText(), /Which college subjects/);
      assert.equal(await page.getByRole('button', { name: 'Review & organize', exact: true }).isDisabled(), true);
      await queue(page, 'Botany habitat surveys');
      await until(async () => await page.getByRole('button', { name: 'Review & organize', exact: true }).isEnabled(), 'clarification resolved');
      state.chatMode = 'error';
      await queue(page, 'Group receipts elsewhere');
      await until(async () => await page.getByRole('alert').count() > 0, 'chat error');
      assert.equal(await page.getByRole('textbox', { name: 'Instructions for Sortly' }).inputValue(), 'Group receipts elsewhere');
      assert.equal(state.applied, 0);
    });
    await scenario('Preview stays accessible without a plan and Undo belongs only to History', async ({ page, state }) => {
      await page.getByRole('button', { name: 'Settings', exact: true }).click();
      assert.equal(await page.getByRole('button', { name: 'Undo last move', exact: true }).count(), 0);
      for (const tab of ['Preview', 'Providers', 'Preview', 'History', 'Preview']) {
        await page.getByRole('button', { name: tab, exact: true }).click();
        if (tab === 'Preview') {
          assert.equal(new URL(page.url()).hash, '#/preview');
          assert.equal(await page.getByRole('textbox', { name: 'Folder directory' }).isVisible(), true);
          assert.equal(await page.getByText('Choose a folder and analyze it to generate a preview.', { exact: true }).isVisible(), true);
          assert.equal(await page.getByRole('heading', { name: 'Review files by destination', exact: true }).count(), 0);
          assert.equal(await page.getByRole('button', { name: 'Review & organize', exact: true }).count(), 0);
        } else if (tab === 'History') {
          assert.equal(await page.getByRole('button', { name: 'Undo last move', exact: true }).isDisabled(), true);
        }
      }
      await page.reload();
      assert.equal(new URL(page.url()).hash, '#/preview');
      assert.equal(await page.getByRole('textbox', { name: 'Folder directory' }).isVisible(), true);
      assert.equal(state.calls.some((call) => call.route === '/api/preview' || call.route === '/api/apply'), false);
    });
    await scenario('chat composer sizing, typing, real reply animation and reduced motion', async ({ page, state }) => {
      await page.setViewportSize({ width: 870, height: 668 });
      assert.equal(await page.getByRole('heading', { name: 'Organize your files' }).count(), 0);
      const mark = await page.locator('.sortly-home img').boundingBox();
      assert.ok(mark.width >= 40 && mark.x < 40, 'larger brand mark sits at upper left');
      const folder = await page.locator('.source-folder-control').boundingBox();
      const action = await page.getByRole('button', { name: 'Analyze folder', exact: true }).boundingBox();
      assert.ok(action.x > folder.x + folder.width && Math.abs(action.y - folder.y) <= 1, 'Analyze sits beside the folder picker');
      const gradientStart = await page.getByRole('button', { name: 'Analyze folder', exact: true }).evaluate((node) => getComputedStyle(node).backgroundPosition);
      await pause(900);
      const gradientLater = await page.getByRole('button', { name: 'Analyze folder', exact: true }).evaluate((node) => getComputedStyle(node).backgroundPosition);
      assert.notEqual(gradientStart, gradientLater, 'gradient visibly shifts over time');
      await page.screenshot({ path: path.join(output, 'compact-workspace.png'), fullPage: true });
      await page.setViewportSize({ width: 714, height: 668 });
      const analyze = page.getByRole('button', { name: 'Analyze folder', exact: true });
      assert.equal(await analyze.isDisabled(), true);
      assert.equal(await analyze.evaluate((node) => getComputedStyle(node).animationName), 'sortly-analyze-colors');
      await page.getByRole('button', { name: /Ask Sortly/ }).click();
      const input = page.getByRole('textbox', { name: 'Instructions for Sortly' });
      await input.fill('Keep botanical notes together');
      assert.equal(await page.locator('.ask-sortly-form').getAttribute('data-writing'), 'true');
      assert.equal(await page.getByText('Writing', { exact: true }).isVisible(), true);
      await until(async () => await page.locator('.ask-sortly-form').getAttribute('data-writing') === 'false', 'typing indicator settles');
      const form = await page.locator('.ask-sortly-form').boundingBox();
      const editor = await input.boundingBox();
      const send = await page.getByRole('button', { name: 'Send message' }).boundingBox();
      assert.equal(send.width, send.height);
      assert.ok(editor.width >= form.width - 1);
      assert.ok(send.x + send.width <= form.x + form.width + 1);
      assert.ok(send.y >= editor.y + editor.height);
      await input.press('Shift+Enter');
      assert.match(await input.inputValue(), /\n$/);
      await page.screenshot({ path: path.join(output, 'chat-composer-dark.png'), fullPage: true });
      await input.press('Enter');
      assert.equal(state.calls.some((call) => call.route === '/api/chat'), false);
      await page.getByRole('textbox', { name: 'Folder directory' }).fill(source);
      state.chatMode = 'slow';
      await analyze.click();
      await until(async () => await page.locator('.chat-thinking').isVisible(), 'real request indicator');
      await until(async () => await page.locator('.chat-reply-text').innerText() === 'The preview reflects your instruction.', 'actual reply finishes typing');
      assert.equal(await page.locator('.chat-text-caret').count(), 0);
      assert.equal(state.applied, 0);
      for (const width of [390, 320]) {
        await page.setViewportSize({ width, height: 740 });
        await input.fill('A longer instruction\nwith multiple lines\nfor the next analysis');
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        const mobileSend = await page.getByRole('button', { name: 'Send message' }).boundingBox();
        assert.equal(mobileSend.width, mobileSend.height);
      }
      await page.screenshot({ path: path.join(output, 'chat-composer-mobile.png'), fullPage: true });
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await until(async () => await analyze.evaluate((node) => getComputedStyle(node).animationName) === 'none', 'gradient respects reduced motion');
      await input.fill('Keep this instruction ready for the next analysis');
      assert.equal(await page.locator('.chat-dots i').first().evaluate((node) => getComputedStyle(node).animationName), 'none');
      await page.setViewportSize({ width: 714, height: 668 });
      await page.getByRole('button', { name: 'Settings', exact: true }).click();
      await page.getByText('Appearance', { exact: true }).first().click();
      await page.getByText('Light', { exact: true }).click();
      await page.getByRole('button', { name: 'Preview', exact: true }).click();
      await page.screenshot({ path: path.join(output, 'chat-composer-light.png'), fullPage: true });
    });
    await scenario('compact analysis visual, cancellation, reduced motion and immediate completion', async ({ page, state }) => {
      await page.setViewportSize({ width: 390, height: 740 });
      await page.getByRole('textbox', { name: 'Folder directory' }).fill(source);
      state.previewMode = 'slow';
      state.previewDelay = 3000;
      await page.getByRole('button', { name: 'Analyze folder', exact: true }).click();
      const activity = page.getByRole('status', { name: 'Analyzing folder', exact: true });
      await activity.waitFor({ state: 'visible' });
      assert.notEqual(await page.getByRole('button', { name: 'Cancel analysis', exact: true }).evaluate((node) => getComputedStyle(node).backgroundImage), 'none');
      assert.equal(await page.getByText('Scanning and analyzing…', { exact: true }).count(), 0);
      const lightBar = page.locator('.analysis-light-bar');
      const barBox = await lightBar.boundingBox();
      const askBox = await page.getByRole('region', { name: 'Ask Sortly', exact: true }).boundingBox();
      assert.equal(barBox.width, 160);
      assert.equal(barBox.height, 16);
      assert.ok(barBox.y > askBox.y + askBox.height && barBox.y - (askBox.y + askBox.height) < 24);
      const wave = page.locator('.analysis-light-wave');
      assert.equal(await wave.evaluate((node) => getComputedStyle(node).animationName), 'analysis-light-wave');
      await page.screenshot({ path: path.join(output, 'analysis-activity.png'), fullPage: true });
      await page.emulateMedia({ reducedMotion: 'reduce' });
      assert.equal(await wave.evaluate((node) => getComputedStyle(node).animationName), 'none');
      await page.getByRole('button', { name: 'Cancel analysis', exact: true }).click();
      assert.equal(await activity.count(), 0);
      assert.equal(state.applied, 0);
      state.previewMode = 'ok';
      await page.getByRole('button', { name: 'Analyze folder', exact: true }).click();
      await page.getByRole('heading', { name: 'Review files by destination', exact: true }).waitFor();
      assert.equal(await activity.count(), 0);
      assert.equal(await page.getByText('Confirmed destinations', { exact: true }).count(), 0);
    });
    await scenario('extension changes are explicit in review and counted only for selected files', async ({ page, state }) => {
      state.extensionChange = true;
      await page.getByRole('textbox', { name: 'Folder directory' }).fill(source);
      await page.getByRole('button', { name: 'Analyze folder', exact: true }).click();
      await page.getByRole('heading', { name: 'Review files by destination', exact: true }).waitFor();
      const row = page.locator('[data-file-id="0"]');
      const extension = row.locator('.review-extension-change');
      assert.match(await extension.innerText(), /Planned extension:.*\.html.*\.csv/s);
      assert.equal(await page.locator('.review-extension-change').count(), 1);
      assert.equal(await row.getByRole('button', { name: 'Edit or view details', exact: true }).getAttribute('aria-expanded'), 'false');
      await row.getByRole('button', { name: 'Edit or view details', exact: true }).click();
      await row.getByLabel('Proposed filename', { exact: true }).fill('Regional_report.csv');
      assert.match(await extension.innerText(), /\.html.*\.csv/s);
      await page.getByRole('button', { name: 'Review & organize', exact: true }).click();
      assert.equal(await page.getByText('1 selected file will have its extension changed. File contents will not be converted.', { exact: true }).isVisible(), true);
      assert.equal(state.applied, 0);
      await page.getByRole('button', { name: 'Cancel', exact: true }).click();
      await row.locator('.review-include input').uncheck();
      await page.getByRole('button', { name: 'Review & organize', exact: true }).click();
      assert.equal(await page.getByText('1 selected file will have its extension changed. File contents will not be converted.', { exact: true }).count(), 0);
      await page.getByRole('button', { name: 'Cancel', exact: true }).click();
      await extension.scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(output, 'extension-change.png'), fullPage: true });
    });
    await scenario('compact file rows keep edits, warnings and approval behavior accessible', async ({ page, state }) => {
      state.compactReview = true;
      await page.getByRole('textbox', { name: 'Folder directory' }).fill(source);
      await page.getByRole('button', { name: 'Analyze folder', exact: true }).click();
      await page.getByRole('heading', { name: 'Review files by destination', exact: true }).waitFor();
      const row = page.locator('[data-file-id="0"]');
      const plain = page.locator('[data-file-id="1"]');
      const sensitive = page.locator('[data-file-id="2"]');
      assert.ok((await row.boundingBox()).height <= 104, 'rename fits a compact row');
      assert.ok((await plain.boundingBox()).height <= 72, 'unchanged file needs one line');
      assert.equal(await page.locator('.review-file-plan').count(), 0);
      assert.equal(await row.locator('.review-include strong').innerText(), 'draft.txt');
      assert.equal(await row.locator('.review-rename-summary code').innerText(), 'orchid_survey_notes.txt');
      assert.equal(await sensitive.locator('.review-status-chip').innerText(), 'Sensitive');
      assert.equal(await sensitive.locator('.review-warning-summary').isVisible(), true);
      await row.getByRole('button', { name: 'Edit or view details', exact: true }).click();
      assert.equal(await row.locator('.review-original-name code').innerText(), 'draft.txt');
      await row.getByLabel('Destination folder', { exact: true }).fill('Research/Updated');
      await row.getByLabel('Proposed filename', { exact: true }).fill('Updated_survey.txt');
      assert.match(await row.locator('.review-edited-destination').innerText(), /Research.*Updated/);
      await row.getByLabel('Proposed filename', { exact: true }).fill('');
      assert.equal(await page.getByRole('button', { name: 'Fix required fields', exact: true }).isDisabled(), true);
      await row.getByLabel('Proposed filename', { exact: true }).fill('Updated_survey.txt');
      await row.getByRole('button', { name: 'Hide details', exact: true }).click();
      await page.getByRole('button', { name: 'Settings', exact: true }).click();
      await page.getByRole('button', { name: 'Preview', exact: true }).click();
      assert.equal(await row.locator('.review-rename-summary code').innerText(), 'Updated_survey.txt');
      await row.scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(output, 'compact-review.png'), fullPage: true });
      for (const width of [390, 320]) {
        await page.setViewportSize({ width, height: 740 });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        assert.equal(await sensitive.locator('.review-warning-summary').isVisible(), true);
      }
      await page.screenshot({ path: path.join(output, 'compact-review-mobile.png'), fullPage: true });
      await row.getByRole('checkbox', { name: 'Include draft.txt', exact: true }).uncheck();
      assert.equal(await row.getByText('Stays in source', { exact: true }).isVisible(), true);
      assert.equal(state.applied, 0);
    });
    await scenario('a different folder starts a fresh chat without replaying old instructions', async ({ page, state }) => {
      await page.getByRole('button', { name: 'Browse', exact: true }).click();
      await page.getByRole('button', { name: /Ask Sortly/ }).click();
      await queue(page, 'Keep botanical notes together');
      const input = page.getByRole('textbox', { name: 'Instructions for Sortly' });
      await input.fill('An unfinished instruction for this folder');
      state.browseCancel = true;
      await page.getByRole('button', { name: 'Browse', exact: true }).click();
      await until(async () => await page.getByRole('button', { name: 'Browse', exact: true }).isEnabled(), 'cancelled picker retains chat');
      assert.match(await page.getByRole('log').innerText(), /Keep botanical notes together/);
      assert.equal(await input.inputValue(), 'An unfinished instruction for this folder');
      state.browseCancel = false;
      state.browseSource = source.toLowerCase().replaceAll('\\', '/');
      await page.getByRole('button', { name: 'Browse', exact: true }).click();
      await until(async () => await page.getByRole('button', { name: 'Browse', exact: true }).isEnabled(), 'same folder selected');
      assert.match(await page.getByRole('log').innerText(), /Keep botanical notes together/);
      await input.fill('');
      await page.getByRole('button', { name: 'Analyze folder', exact: true }).click();
      await page.getByRole('heading', { name: 'Review files by destination', exact: true }).waitFor();
      assert.equal(state.calls.filter((call) => call.route === '/api/chat').length, 1);
      await input.fill('Old folder draft');
      state.browseSource = 'C:\\UI fixtures\\OtherLibrary';
      await page.getByRole('button', { name: 'Browse', exact: true }).click();
      await until(async () => await page.getByRole('textbox', { name: 'Folder directory' }).inputValue() === state.browseSource, 'new folder selected');
      assert.equal(await page.locator('.chat-log').innerText(), '');
      assert.equal(await input.inputValue(), '');
      assert.equal(await page.getByRole('heading', { name: 'Review files by destination', exact: true }).count(), 0);
      await page.getByRole('button', { name: 'Analyze folder', exact: true }).click();
      await page.getByRole('heading', { name: 'Review files by destination', exact: true }).waitFor();
      assert.equal(state.calls.filter((call) => call.route === '/api/chat').length, 1, 'old instructions are not replayed');
      await queue(page, 'A rule for this folder only');
      await input.fill('Another draft');
      await page.getByRole('textbox', { name: 'Folder directory' }).fill('C:\\UI fixtures\\ThirdLibrary');
      assert.equal(await page.locator('.chat-log').innerText(), '');
      assert.equal(await input.inputValue(), '');
      assert.equal(state.applied, 0);
    });
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ passed, backend: 'intercepted fixtures; no filesystem effects' }, null, 2));
    console.log(`All ${passed.length} browser scenarios passed.`);
  } finally { await browser.close(); }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
