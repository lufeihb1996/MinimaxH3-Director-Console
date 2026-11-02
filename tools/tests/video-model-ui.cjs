const { chromium } = require('playwright');
const { readFileSync } = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const html = readFileSync('frontend/index.html', 'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');
    await page.setContent(html);
    const source = readFileSync('frontend/app.js', 'utf8');
    await page.addScriptTag({ path: 'frontend/video-models.js' });
    await page.evaluate(data => videoModelCatalog.configure(data), JSON.parse(readFileSync('config/video_models.json', 'utf8')));
    await page.addScriptTag({ content: 'const $ = id => document.getElementById(id);\n' + source.slice(source.indexOf('function updateLightx2vHint()')) });
    assert.equal(await page.locator('#shotVideoModel').inputValue(), 'b15_49');
    await page.selectOption('#shotExtraLora', 'both');
    await page.evaluate(() => updateLightx2vHint());
    assert.equal(await page.locator('#shotSteps').inputValue(), '10');
    assert.equal(await page.locator('#shotSteps').isDisabled(), true);
    assert.equal(await page.locator('#shotMP').inputValue(), '0.4');
    assert.equal(await page.locator('#shotMP').isDisabled(), true);
    assert.equal(await page.locator('#shotExtraLora').isEnabled(), true);
    assert.equal(await page.locator('#shotExtraLora').inputValue(), 'both');
    assert.match(await page.locator('#lightx2vProfileHint').textContent(), /1MP.*5步.*0.35/);
    await page.selectOption('#shotVideoModel', 'fused_h3');
    assert.equal(await page.locator('#shotSteps').inputValue(), '4');
    assert.equal(await page.locator('#shotSteps').isEnabled(), true);
    assert.equal(await page.locator('#shotMP').isEnabled(), true);
    await page.selectOption('#shotSteps', '8');
    assert.match(await page.locator('#lightx2vProfileHint').textContent(), /8步/);
    await page.selectOption('#shotSteps', '20');
    assert.equal(await page.locator('#shotExtraLora').isDisabled(), true);
    await page.selectOption('#shotVideoModel', 'b15_49');
    assert.equal(await page.locator('#shotExtraLora').isEnabled(), true);
    assert.equal(await page.locator('#shotSteps').inputValue(), '10');
    await page.addScriptTag({ content: source.slice(source.indexOf('function videoProgressDetail('), source.indexOf('let videoProgressPolling')) });
    const details = await page.evaluate(() => {
      const shot = { video_model: 'b15_49', status: 'RUNNING', steps: 10 };
      return [
        [30, 'b15–49 首轮 · 0.4MP / 10步 · Node 125'],
        [55, 'b15–49 首轮 · 0.4MP / 10步 · Node 125'],
        [60, '潜变量放大至 1MP · Node b15_upscale'],
        [65, 'b15–49 细化 · 5步 / 降噪0.35 · Node b15_refine'],
        [77, 'b15–49 细化 · 5步 / 降噪0.35 · Node b15_refine'],
        [85, 'b15–49 细化 · 5步 / 降噪0.35 · Node b15_refine'],
        [90, 'VAEDecode · Node 122'],
        [98, 'SaveVideo · Node 92'],
        [98, '下载成片到 Windows'],
      ].map(([progress, current_node]) => videoProgressDetail({ ...shot, progress, current_node }));
    });
    assert.match(details[0].text, /第一轮.*5\/10/);
    assert.match(details[1].text, /第一轮.*10\/10/);
    assert.match(details[2].text, /潜变量放大.*1MP/);
    assert.match(details[3].text, /第二轮.*0\/5/);
    assert.match(details[4].text, /第二轮.*3\/5/);
    assert.match(details[5].text, /第二轮.*5\/5/);
    assert.match(details[6].text, /解码/);
    assert.match(details[7].text, /编码并保存/);
    assert.match(details[8].text, /下载成片/);
    assert.equal(await page.evaluate(() => videoProgressDetail({ video_model: 'b15_49', status: 'DONE', progress: 98 }).pct), 100);
    const exactSteps = await page.evaluate(() => videoProgressDetail({
      status: 'RUNNING', progress: 30, video_model: 'b15_49', current_node: 'b15–49 首轮 · Node 125 · 4/10步'
    }));
    assert.match(exactSteps.text, /第一轮.*4\/10 步/);
    assert.doesNotMatch(exactSteps.text, /约|Node|处理节点/);
    assert.equal(await page.locator('.prompt-toolbar, .tag-chip').count(), 0);
    assert.equal(await page.locator('#shotRatio').inputValue(), '16:9');
    assert.equal(await page.evaluate(() => videoModelCatalog.defaults().aspect_ratio), '16:9');
    const regenerationError = await page.evaluate(() => videoProgressDetail({ status: 'DONE', progress: 100, error: 'missing model' }));
    assert.match(regenerationError.text, /旧成片可用.*失败/);
    await page.evaluate(() => {
      const extra = { ...videoModelCatalog.profile('fused_h3'), id: 'future_model', label: '未来测试模型', steps: [8], default_steps: 8 };
      videoModelCatalog.configure({ default_model: 'future_model', models: [extra] });
      updateLightx2vHint();
    });
    assert.equal(await page.locator('#shotVideoModel').inputValue(), 'future_model');
    assert.equal(await page.locator('#shotVideoModel option').textContent(), '未来测试模型');
    assert.equal(await page.locator('#shotSteps').inputValue(), '8');
    assert.equal(await page.locator('#shotSteps').isDisabled(), true);
    console.log('PASS: b15 default, fixed two-pass parameters, style controls, legacy model switching and two-pass progress text. No backend or GPU requests.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
