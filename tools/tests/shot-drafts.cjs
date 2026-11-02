const { chromium } = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await context.route('http://draft.test/**', route => route.fulfill({ contentType: 'text/html', body: fs.readFileSync('frontend/index.html', 'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '') }));
    await page.goto('http://draft.test/');
    await page.evaluate(() => {
      window.$ = id => document.getElementById(id);
      window.state = { shots: [{ id: 1 }, { id: 2 }], currentShot: null, shotId: null };
      window.saved = []; window.fail = false;
      window.api = async (path, options) => {
        if (window.fail) throw new Error('offline fixture');
        const body = JSON.parse(options.body); saved.push({ path, body });
        return { id: Number(path.split('/').pop()), ...body };
      };
      window.showToast = () => {};
      window.setModeUI = mode => {
        for (const [id, value] of [['modeT2VBtn','T2V'], ['modeCutBtn','CUT'], ['modeContinueBtn','CONTINUE']]) $(id).classList.toggle('active', value === mode);
      };
      window.updateLightx2vHint = () => videoModelCatalog.renderParameters();
      window.loadEditor = id => {
        shotDrafts.capture();
        state.shotId = id; state.currentShot = { id };
        const defaults = { shotTitle: 'server ' + id, shotVideoModel: 'b15_49', shotSeconds: '5', shotMP: '0.4', shotSteps: '10', shotExtraLora: 'off', shotRatio: '16:9', shotSeed: '42', shotPrompt: '', shotTimelineStart: '0', shotTimelineEnd: '0', shotTargetDuration: '0', shotConformMode: 'auto', shotLyricText: '' };
        for (const [key, value] of Object.entries(defaults)) $(key).value = value;
        $('shotLipSyncEnabled').checked = false;
        setModeUI('T2V');
        shotDrafts.activate(id);
      };
    });
    await page.addScriptTag({ path: 'frontend/video-models.js' });
    await page.evaluate(data => videoModelCatalog.configure(data), JSON.parse(fs.readFileSync('config/video_models.json', 'utf8')));
    await page.addScriptTag({ path: 'frontend/shot-drafts.js' });
    await page.evaluate(() => { shotDrafts.init(); loadEditor(1); $('shotPrompt').value = 'first shot prompt'; $('shotTitle').value = 'edited title'; $('shotExtraLora').value = 'naughty'; $('shotRatio').value = '1:1'; $('shotSeed').value = '123'; shotDrafts.capture(); loadEditor(2); $('shotPrompt').value = 'second shot prompt'; shotDrafts.capture(); loadEditor(1); });
    assert.equal(await page.locator('#shotPrompt').inputValue(), 'first shot prompt');
    assert.equal(await page.locator('#shotTitle').inputValue(), 'edited title');
    assert.equal(await page.locator('#shotExtraLora').inputValue(), 'naughty');
    assert.equal(await page.locator('#shotRatio').inputValue(), '1:1');
    await page.evaluate(() => shotDrafts.saveCurrent());
    assert.equal(await page.evaluate(() => saved.find(item => item.path.endsWith('/1')).body.seed), 123);
    await page.evaluate(() => loadEditor(2));
    assert.equal(await page.locator('#shotPrompt').inputValue(), 'second shot prompt');
    await page.fill('#shotPrompt', 'autosaved without a button');
    await page.waitForFunction(() => saved.some(item => item.path.endsWith('/2') && item.body.prompt === 'autosaved without a button'));
    const appSource = fs.readFileSync('frontend/app.js', 'utf8');
    await page.evaluate(() => {
      window.renderShots = () => {};
      window.updateGenerationLive = () => {};
      window.fmtStatus = value => value;
      window.statusClass = () => '';
      window.renderShotError = () => {};
      window.renderRefSlots = async () => {};
      HTMLMediaElement.prototype.pause = function() {};
    });
    await page.addScriptTag({ content: appSource.slice(appSource.indexOf('function renderShotLipSyncState('), appSource.indexOf('async function selectShot(')) });
    await page.evaluate(() => {
      $('shotTimelineStart').value = '2'; $('shotTimelineEnd').value = '7';
      shotDrafts.capture();
      renderShotLipSyncState({ id: 2, timeline_start: 0, timeline_end: 0 });
    });
    assert.equal(await page.locator('#shotTimelineStart').inputValue(), '2');
    await page.evaluate(() => renderSelectedShot({ id: 2, title: 'stale title', mode: 'T2V', video_model: 'b15_49', seconds: 5, megapixels: 0.4, steps: 10, aspect_ratio: '16:9', seed: 42, prompt: 'stale prompt', status: 'RUNNING' }));
    assert.equal(await page.locator('#shotPrompt').inputValue(), 'autosaved without a button');
    assert.equal(await page.locator('#shotTimelineStart').inputValue(), '2');
    await page.evaluate(() => {
      $('videoContainer').classList.remove('hidden');
      $('mainVideoPanel').classList.remove('hidden');
      $('shotVideo').setAttribute('src', 'data:video/mp4;base64,');
      $('shotPrompt').focus();
      window.beforeOptions = [...$('shotSteps').options];
      updateLightx2vHint();
      window.optionIdentityKept = beforeOptions.every((node, i) => node === $('shotSteps').options[i]);
      renderSelectedShot({ id: 2, title: 'old', mode: 'T2V', video_model: 'b15_49', seconds: 5, megapixels: 0.4, steps: 10, aspect_ratio: '16:9', seed: 42, prompt: 'old', status: 'RUNNING' });
    });
    assert.equal(await page.evaluate(() => window.optionIdentityKept), true);
    assert.equal(await page.locator('#videoContainer').evaluate(el => el.classList.contains('hidden')), false);
    assert.equal(await page.locator('#mainVideoPanel').evaluate(el => el.classList.contains('hidden')), false);
    assert.equal(await page.locator('#shotVideo').getAttribute('src'), 'data:video/mp4;base64,');
    await page.evaluate(() => { window.fail = true; $('shotPrompt').value = 'keep on network error'; shotDrafts.capture(); });
    const failed = await page.evaluate(async () => { try { await shotDrafts.saveCurrent(); return false; } catch { return true; } });
    assert.equal(failed, true);
    assert.equal(await page.evaluate(() => JSON.parse(localStorage.getItem('anime-studio:shot-draft:v1:2')).form.shotPrompt), 'keep on network error');
    // Simulate a page restart: a second window uses the same local storage.
    const restored = await page.context().newPage();
    await restored.goto('http://draft.test/');
    assert.equal(await restored.evaluate(() => JSON.parse(localStorage.getItem('anime-studio:shot-draft:v1:2')).form.shotPrompt), 'keep on network error');
    await restored.close();
    await page.evaluate(() => { window.fail = false; loadEditor(1); });
    // A late save for Shot 1 must never overwrite the edited Shot 2 form.
    await page.evaluate(() => {
      window.release = null;
      const normal = api;
      window.api = (path, options) => new Promise(resolve => { window.release = () => normal(path, options).then(resolve); });
      $('shotPrompt').value = 'late request shot 1'; shotDrafts.capture(); window.request = shotDrafts.saveCurrent();
    });
    await page.waitForFunction(() => typeof window.release === 'function');
    await page.evaluate(() => { loadEditor(2); window.release(); });
    await page.evaluate(() => window.request);
    assert.equal(await page.locator('#shotPrompt').inputValue(), 'keep on network error');
    assert.deepEqual(errors, []);
    console.log('PASS: per-shot prompt/parameters, autosave serialization, local fallback, refresh storage, late-response isolation.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
