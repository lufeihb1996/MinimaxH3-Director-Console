const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
 const browser = await chromium.launch({headless:true});
 try {
  const page = await browser.newPage({viewport:{width:1920,height:1080}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://nav.test/**', route=>{
   const url=new URL(route.request().url());
   if (url.pathname.startsWith('/static/')) {
    const file=path.join('frontend',url.pathname.slice('/static/'.length));
    return route.fulfill({body:fs.existsSync(file)?fs.readFileSync(file):'',contentType:file.endsWith('.css')?'text/css':'application/octet-stream'});
   }
   return route.fulfill({contentType:'text/html',body:fs.readFileSync('frontend/index.html','utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'')});
  });
  await page.goto('http://nav.test/');
  await page.evaluate(()=>{
   window.$=id=>document.getElementById(id);
   window.escapeHtml=text=>{const d=document.createElement('div');d.textContent=text;return d.innerHTML.replace(/"/g,'&quot;');};
   window.fixtureProjects=Array.from({length:18},(_,i)=>({id:22+i,name:i===0?'遗迹夺宝 · 30秒动漫冒险':i===1?'咖啡馆里的半分钟 · 自动接续测试':`测试项目 ${i+1} · 森林冒险与人物动作`}));
   window.state={projects:[],projectId:null,workspaceMode:'video'};
   window.captured=0;window.sceneLoads=[];
   window.shotDrafts={capture(){captured++}};
   window.api=async()=>fixtureProjects;
   window.clearEditor=()=>{};
   window.loadScenes=async()=>{sceneLoads.push(state.projectId)};
   localStorage.setItem('studio:lastProjectId','23');
  });
  const js=fs.readFileSync('frontend/app.js','utf8');
  const defs=js.slice(js.indexOf('function renderProjects()'),js.indexOf('async function loadScenes()'));
  await page.addScriptTag({content:defs+"\n$('projectSearch').addEventListener('input',renderProjects);"});
  await page.evaluate(()=>loadProjects());
  await page.evaluate(()=>document.getElementById('projectPickerDialog').showModal());
  assert.equal(await page.evaluate(()=>state.projectId),23);
  assert.equal(await page.locator('#projectList .nav-item').count(),18);
  assert.equal(await page.locator('#projectCount').textContent(),'18/18');
  assert.equal(await page.locator('#projectList .nav-label').first().evaluate(el=>getComputedStyle(el).whiteSpace),'normal');
  const dimensions=await page.locator('#projectList').evaluate(el=>({height:el.clientHeight,row:el.firstElementChild.offsetHeight}));
  assert(dimensions.height/dimensions.row>=3,JSON.stringify(dimensions));
  await page.locator('#projectSearch').fill('遗迹');
  assert.equal(await page.locator('#projectList .nav-item').count(),1);
  await page.locator('#projectList .nav-item').press('Enter');
  assert.equal(await page.evaluate(()=>state.projectId),22);
  assert.equal(await page.evaluate(()=>captured),1);
  assert.equal(await page.evaluate(()=>localStorage.getItem('studio:lastProjectId')),'22');
  assert.equal(await page.locator('#projectPickerDialog').isVisible(),false);
  assert.equal(await page.locator('#activeProjectName').textContent(),'遗迹夺宝 · 30秒动漫冒险');
  await page.evaluate(()=>document.getElementById('projectPickerDialog').showModal());
  await page.locator('#projectSearch').fill('does not exist');
  assert.equal(await page.locator('.project-empty').count(),1);
  assert.equal(await page.evaluate(()=>state.projectId),22);
  await page.locator('#projectSearch').fill('');
  await page.evaluate(()=>{
    document.getElementById('projectPickerDialog').close();
    document.getElementById('sceneList').innerHTML=Array.from({length:12},(_,i)=>`<div class="nav-item"><span class="nav-label">场景 ${i+1} · 遗迹夺宝与逃生</span></div>`).join('');
  });
  await page.locator('.sidebar-panel').screenshot({path:path.join(process.env.TEMP,'studio-project-nav.png')});
  await page.setViewportSize({width:1280,height:720});
  assert(await page.locator('#openProjectPicker').isVisible());
  const bounds=await page.locator('.projects-panel').boundingBox();
  const scenes=await page.locator('.sidebar-panel > .flex-grow').boundingBox();
  assert(bounds.height<200,JSON.stringify(bounds));
  assert(scenes.height>bounds.height,JSON.stringify({bounds,scenes}));
  await page.evaluate(()=>document.getElementById('projectPickerDialog').showModal());
  assert(await page.locator('#projectSearch').isVisible());
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#projectPickerDialog').isVisible(),false);
  assert.deepEqual(errors,[]);
  console.log('PASS: readable scrolling list, search, keyboard switching, saved selection, draft capture, empty search, compact sidebar, Scene space, dialog and Escape, desktop sizes.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
