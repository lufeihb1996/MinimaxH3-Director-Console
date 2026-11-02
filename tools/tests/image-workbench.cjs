// Isolated browser checks: no backend connection or GPU submissions.
const { chromium } = require('playwright');
const path = require('path');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setContent('<div id="imageStoryboardView"></div><div id="imageHistoryGallery"></div><input id="imagePrompt"><div id="imageInspector"></div>');
    await page.addScriptTag({ path: path.resolve('frontend/vendor/vue.global.prod.js') });
    await page.evaluate(() => {
      window.state = { imageRole: 'ref1', imageQueue: { current_job_id: 7 } };
      window.$ = id => document.getElementById(id);
      window.calls = [];
      window.sceneImageRoleLabel = role => role.toUpperCase();
      window.selectImageScene = async (...args) => calls.push(['select', ...args]);
      window.showToast = (...args) => calls.push(['toast', ...args]);
      window.openLightbox = (...args) => calls.push(['preview', ...args]);
      window.showConfirmModal = async () => true;
      window.escapeHtml = text => text;
      window.api = async (url, opts) => { calls.push(['api', url, opts]); return { applied_count: 2 }; };
      window.loadImageDashboard = async () => calls.push(['dashboard']);
      window.refreshImageQueueStatus = async () => calls.push(['queue']);
      window.getImageSceneData = () => ({ name: '测试' });
    });
    await page.addScriptTag({ path: path.resolve('frontend/image-workbench.js') });
    await page.evaluate(() => {
      window.jobs = ['ref1', 'ref2', 'ref3', 'ref4'].map((role, i) => ({
        scene_id: 1, scene_name: '<script>unsafe</script>', role, slot_index: i + 1,
        status: i === 0 ? 'RUNNING' : i === 3 ? 'DISABLED' : 'DONE',
        real_job_id: i + 7, progress: i === 0 ? 30 : 100,
        latest_image: { media_url: '/image.png' }
      }));
      imageWorkbenchView.update(jobs);
    });
    assert.equal(await page.locator('article').count(), 4);
    assert.equal(await page.locator('article.current.selected').count(), 1);
    assert.equal(await page.locator('.delete-job-btn').first().isDisabled(), true);
    assert.equal(await page.locator('.scene-name').first().textContent(), '<script>unsafe</script>');
    await page.evaluate(() => { window.originalRow = document.querySelector('article'); jobs[0].progress = 71; imageWorkbenchView.update(jobs.map(j => ({ ...j }))); });
    assert.equal(await page.locator('.image-task-progress-pct').first().textContent(), '71%');
    assert.equal(await page.evaluate(() => originalRow === document.querySelector('article')), true);
    await page.locator('.regenerate-job-btn').first().click();
    assert.equal(await page.evaluate(() => calls.filter(c => c[0] === 'select').length), 1);
    await page.locator('.preview-job-btn').first().click();
    assert.equal(await page.evaluate(() => calls.filter(c => c[0] === 'select').length), 1);
    await page.locator('.delete-job-btn').nth(1).click();
    assert.equal(await page.evaluate(() => calls.some(c => c[0] === 'api' && c[1] === '/api/image-queue/slots/1/ref2')), true);
    await page.evaluate(() => imageHistoryView.update([{ id: 12, media_url: '/image.png', seed: 42, width: 768, height: 1360, role: 'ref2' }], '', 'ref2'));
    await page.locator('.apply-overwrite-btn').click();
    const payloads = await page.evaluate(() => calls.filter(c => c[0] === 'api' && c[1].endsWith('/apply')).map(c => JSON.parse(c[2].body)));
    assert.deepEqual(payloads, [{ role: 'ref2', replace_existing: true }]);
    await page.locator('.image-history-delete-btn').click();
    assert.equal(await page.evaluate(() => calls.some(c => c[1] === '/api/scene-images/12' && c[2]?.method === 'DELETE')), true);
    await page.evaluate(() => { imageHistoryView.update([], '正在加载历史…'); imageWorkbenchView.update([]); });
    assert.equal(await page.locator('.image-history-card').count(), 0);
    assert.equal(await page.locator('.scene-image-empty').textContent(), '正在加载历史…');
    assert.equal(await page.locator('.image-queue-empty-title').textContent(), '当前没有可用 Scene');
    assert.deepEqual(errors, []);
    console.log('PASS: queue rendering, keyed updates, selection, preview, deletion, history apply/delete and empty states; no GPU requests.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
