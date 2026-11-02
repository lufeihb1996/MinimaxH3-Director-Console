/**
 * AI Anime Studio - Next-Gen Director Console Frontend
 */

const $ = (id) => document.getElementById(id);

const state = {
  projects: [],
  scenes: [],
  shots: [],
  projectId: null,
  sceneId: null,
  shotId: null,
  currentShot: null,
  ws: null,
  hoveredSlot: null,
  sceneMaster: null,
  projectMaster: null,
  imageSceneId: null,
  workspaceMode: 'video',
  imageDashboard: null,
  imageRole: 'ref1',
  imageWorkbenchFilter: 'current',
  imageQueue: { paused: false, queue_size: 0, current_job_id: null, jobs: [] },
  imageEditorKey: '',
  imageEditorDirty: false,
  imageSelectSeq: 0,
  imageDashboardSeq: 0,
  imageQueueSeq: 0,
  shotSelectSeq: 0,
  sceneLoadSeq: 0,
  mvProject: null,
  mvTimeline: null,
  drama3: null,
  voiceJobs: [],
  unassignedVoiceJobs: [],
  voiceProfiles: [],
  voicePresets: {},
  voiceWorkbenchScope: 'project',
  selectedDramaItemId: '',
  dramaFormItemId: '',
  dramaEditorDirty: false,
  dramaFormInitialized: false,
  dramaSaveTimer: null,
  pendingShotPlan: null,
};

// One submission per target across list/editor entry points, plus a short double-click window.
const submissionLocks = new Map();
const generationRequests = new Map();
function imageSubmissionKey(sceneId, role) {
  const owner = getImageSceneData(sceneId)?.roles?.[role]?.config?.source_scene_id || sceneId;
  return `image:${state.projectId}:${owner}:${role}`;
}
function submissionButtonKey(button) {
  if (button.dataset.submitKey) return button.dataset.submitKey;
  if (button.classList.contains('gen-btn')) return `shot:${button.closest('tr')?.dataset.shotId}`;
  if (button.classList.contains('mv-regenerate-btn')) return `song:${state.projectId}`;
  const keys = {
    generateShotBtn: `shot:${state.shotId}`,
    queueCurrentRefBtn: imageSubmissionKey(state.imageSceneId, state.imageRole),
    generateWholeStoryImagesBtn: `images-project:${state.projectId}`,
    queueProjectMissingImagesBtn: `images-scene:${state.imageSceneId}`,
    generateWholeStoryVideosBtn: `videos-project:${state.projectId}`,
    generateSceneBtn: `videos-scene:${state.sceneId}`,
  };
  return keys[button.id];
}
function syncSubmissionButtons() {
  const selector = '.gen-btn,.mv-regenerate-btn,[data-submit-key],#generateShotBtn,#queueCurrentRefBtn,#generateWholeStoryImagesBtn,#queueProjectMissingImagesBtn,#generateWholeStoryVideosBtn,#generateSceneBtn';
  document.querySelectorAll(selector).forEach(button => {
    const busy = submissionLocks.has(submissionButtonKey(button));
    if (busy) {
      if (!button.hasAttribute('data-submit-was-disabled')) button.dataset.submitWasDisabled = button.disabled ? '1' : '0';
      if (!button.disabled) button.disabled = true;
      button.setAttribute('aria-busy', 'true');
    } else if (button.hasAttribute('data-submit-was-disabled')) {
      button.disabled = button.dataset.submitWasDisabled === '1';
      delete button.dataset.submitWasDisabled;
      button.removeAttribute('aria-busy');
    }
  });
}
async function withSubmissionGuard(key, action) {
  if (submissionLocks.has(key)) return;
  const token = {};
  submissionLocks.set(key, token);
  syncSubmissionButtons();
  let completed = false;
  try { const result = await action(); completed = true; return result; }
  finally {
    const release = () => {
      if (submissionLocks.get(key) === token) submissionLocks.delete(key);
      syncSubmissionButtons();
    };
    if (completed) setTimeout(release, 1200); else release();
  }
}

// ==========================================
// Toast Notification System (replaces alert)
// ==========================================
function showToast(message, type = 'info', duration = 3200) {
  const container = $('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;

  const iconMap = {
    success: '✓',
    error: '✕',
    warning: '⚠',
    info: 'ℹ',
  };

  toast.innerHTML = `
    <span style="font-weight:700;font-size:15px">${iconMap[type] || 'ℹ'}</span>
    <span style="flex:1">${escapeHtml(message)}</span>
  `;

  container.appendChild(toast);

  setTimeout(() => {
    toast.classList.add('fade-out');
    setTimeout(() => toast.remove(), 250);
  }, duration);
}

// ==========================================
// In-App Modal Dialogs (replaces prompt & confirm)
// ==========================================
function showPromptModal(title, label, defaultValue = '') {
  return new Promise((resolve) => {
    const modal = $('inputModal');
    const titleEl = $('inputModalTitle');
    const labelEl = $('inputModalLabel');
    const inputEl = $('inputModalVal');
    const okBtn = $('confirmInputModalBtn');
    const cancelBtn = $('cancelInputModalBtn');
    const closeBtn = $('closeInputModalBtn');

    titleEl.textContent = title;
    labelEl.textContent = label;
    inputEl.value = defaultValue;
    modal.classList.remove('hidden');
    inputEl.focus();
    inputEl.select();

    const cleanup = () => {
      modal.classList.add('hidden');
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      closeBtn.onclick = null;
      inputEl.onkeydown = null;
    };

    okBtn.onclick = () => {
      const val = inputEl.value.trim();
      cleanup();
      resolve(val || null);
    };

    const cancel = () => {
      cleanup();
      resolve(null);
    };

    cancelBtn.onclick = cancel;
    closeBtn.onclick = cancel;
    inputEl.onkeydown = (e) => {
      if (e.key === 'Enter') okBtn.click();
      if (e.key === 'Escape') cancel();
    };
  });
}

function showConfirmModal(title, message) {
  return new Promise((resolve) => {
    const modal = $('confirmModal');
    const titleEl = $('confirmModalTitle');
    const msgEl = $('confirmModalMessage');
    const okBtn = $('okConfirmModalBtn');
    const cancelBtn = $('cancelConfirmModalBtn');
    const closeBtn = $('closeConfirmModalBtn');

    titleEl.textContent = title;
    msgEl.innerHTML = message;
    const card = modal.querySelector('.modal-card');
    card?.classList.toggle('ref-confirm-compact', title === 'Regenerate this Ref');
    modal.classList.remove('hidden');

    const cleanup = () => {
      modal.classList.add('hidden');
      card?.classList.remove('ref-confirm-compact');
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      closeBtn.onclick = null;
    };

    okBtn.onclick = () => {
      cleanup();
      resolve(true);
    };

    const cancel = () => {
      cleanup();
      resolve(false);
    };

    cancelBtn.onclick = cancel;
    closeBtn.onclick = cancel;
  });
}

// Lightbox
function openLightbox(src, title = '') {
  $('lightboxImg').src = src;
  $('lightboxTitle').textContent = title;
  $('lightboxModal').classList.remove('hidden');
}
function closeLightbox() {
  $('lightboxModal').classList.add('hidden');
  $('lightboxImg').src = '';
}
$('closeLightboxBtn').onclick = closeLightbox;
$('lightboxOverlay').onclick = closeLightbox;

// ==========================================
// Audio Preview Modal
// ==========================================
function openAudioPreview(mv) {
  const modal = $('audioPreviewModal');
  const player = $('audioPreviewPlayer');
  const audioUrl = mv?.audio_url || (mv?.audio_path ? `/api/projects/${state.projectId}/mv/audio` : '');
  if (!modal || !player || !audioUrl) return;

  const title = mv?.title || 'YuE2 song';
  const duration = Number(mv?.audio_duration || 0);
  const analysisStatus = String(state.mvTimeline?.analysis_status || mv?.analysis_status || 'IDLE').toUpperCase();
  const analysisText = analysisStatus === 'DONE' ? 'Timeline analyzed' : (analysisStatus === 'RUNNING' ? 'Analyzing' : 'Not analyzed');

  modal.dataset.projectId = String(state.projectId);
  $('audioPreviewTitle').textContent = 'Audio preview · ' + title;
  $('audioPreviewName').textContent = title;
  $('audioPreviewMeta').textContent = 'YuE2 · ' + (duration > 0 ? duration.toFixed(1) + 's' : 'Reading duration') + ' · ' + analysisText;
  $('audioPreviewPath').textContent = mv.audio_path || mv.audio_url;

  const dl = $('downloadAudioPreviewBtn');
  if (dl) {
    dl.href = mv.audio_download_url || ('/api/projects/' + state.projectId + '/mv/audio/download');
    dl.setAttribute('download', '');
  }

  player.src = audioUrl + '?t=' + Date.now();
  modal.classList.remove('hidden');
  player.play().catch(() => {});
}

function closeAudioPreview() {
  const modal = $('audioPreviewModal');
  const player = $('audioPreviewPlayer');
  if (player) {
    player.pause();
    player.removeAttribute('src');
    player.load();
  }
  if (modal) modal.classList.add('hidden');
}

// ==========================================
// Master Video Cinema Modal (成片放映厅)
// ==========================================
function openMasterModal(mediaUrl, title, filePath) {
  const modal = $('masterVideoModal');
  const player = $('masterVideoPlayer');
  const titleEl = $('masterModalTitleText');
  const pathEl = $('masterModalPath');
  const dlBtn = $('downloadMasterBtn');
  if (!modal || !player) return;
  if (titleEl) titleEl.textContent = title || 'Screening room';
  if (pathEl) pathEl.textContent = filePath || mediaUrl;
  player.src = mediaUrl + '?t=' + Date.now();
  if (dlBtn) {
    const downloadUrl = mediaUrl.endsWith('/master') ? `${mediaUrl}/download` : mediaUrl;
    dlBtn.href = downloadUrl;
    const filename = filePath ? filePath.split(/[/\\]/).pop() : 'master.mp4';
    dlBtn.setAttribute('download', filename);
  }
  modal.classList.remove('hidden');
  player.play().catch(() => {});
}

function closeMasterModal() {
  const modal = $('masterVideoModal');
  const player = $('masterVideoPlayer');
  if (player) {
    player.pause();
    player.removeAttribute('src');
    player.load();
  }
  if (modal) modal.classList.add('hidden');
}

async function checkMasterVideos() {
  try {
    if (state.sceneId) {
      const info = await api(`/api/scenes/${state.sceneId}/master/info`);
      const btn = $('playSceneMasterBtn');
      if (btn) {
        if (info.exists) {
          state.sceneMaster = info;
          btn.classList.remove('hidden');
        } else {
          state.sceneMaster = null;
          btn.classList.add('hidden');
        }
      }
    } else {
      const btn = $('playSceneMasterBtn');
      if (btn) btn.classList.add('hidden');
      state.sceneMaster = null;
    }
  } catch (e) {
    console.warn('checkSceneMaster failed:', e);
  }

  try {
    if (state.projectId) {
      const info = await api(`/api/projects/${state.projectId}/master/info`);
      const btn = $('playProjectMasterBtn');
      if (btn) {
        if (info.exists) {
          state.projectMaster = info;
          btn.classList.remove('hidden');
        } else {
          state.projectMaster = null;
          btn.classList.add('hidden');
        }
      }
    } else {
      const btn = $('playProjectMasterBtn');
      if (btn) btn.classList.add('hidden');
      state.projectMaster = null;
    }
  } catch (e) {
    console.warn('checkProjectMaster failed:', e);
  }
}

// ==========================================
// API Client
// ==========================================
function api(path, opts = {}) {
  const isGeneration = String(opts.method || 'GET').toUpperCase() === 'POST'
    && (/\/generate$/.test(path) || /\/images\/queue$/.test(path));
  if (!isGeneration) return apiRequest(path, opts);
  const key = path + ':' + String(opts.body || '');
  if (generationRequests.has(key)) return generationRequests.get(key);
  const request = apiRequest(path, opts);
  generationRequests.set(key, request);
  request.then(() => setTimeout(() => {
    if (generationRequests.get(key) === request) generationRequests.delete(key);
  }, 1200), () => { if (generationRequests.get(key) === request) generationRequests.delete(key); });
  return request;
}

async function apiRequest(path, opts = {}) {
  const res = await fetch(path, {
    headers: { ...(opts.body instanceof FormData ? {} : {'Content-Type': 'application/json'}), ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    let payload = null;
    try {
      const j = await res.json();
      payload = j;
      detail = j.detail || JSON.stringify(j);
    } catch {}
    const err = new Error(detail);
    err.status = res.status;
    err.payload = payload;
    throw err;
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

function escapeHtml(v) {
  return String(v ?? '').replace(/[&<>'"]/g, (c) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;',
  }[c]));
}

function fmtStatus(s) {
  const map = {
    DRAFT: 'Pending',
    PENDING: 'Pending',
    QUEUED: 'Queued',
    PREPARING: 'Preparing',
    RUNNING: 'Generating',
    DONE: 'Done',
    FAILED: 'Generation failed',
    CANCELLED: 'Interrupted',
  };
  return map[s] || s || 'Pending';
}

function statusClass(s) {
  if (s === 'DONE') return 'status-done';
  if (s === 'RUNNING' || s === 'PREPARING') return 'status-running';
  if (s === 'FAILED') return 'status-failed';
  if (s === 'QUEUED') return 'status-queued';
  return 'status-pending';
}

function formatElapsedSeconds(totalSeconds) {
  const seconds = Math.max(0, Math.floor(Number(totalSeconds || 0)));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  const remaining = seconds % 60;
  return hours ? `${hours}h ${minutes}m ${remaining}s` : (minutes ? `${minutes}m ${remaining}s` : `${remaining}s`);
}

function shotElapsedSeconds(shot, nowSeconds = Date.now() / 1000) {
  if (!shot) return null;
  const status = String(shot.status || '').toUpperCase();
  const started = Number(shot.generation_started_at || 0);
  const finished = Number(shot.generation_finished_at || 0);
  const running = ['PREPARING', 'RUNNING'].includes(status);
  if (!running && !['DONE', 'FAILED', 'CANCELLED'].includes(status)) return null;
  if ((!running && !finished) || (running && !started)) return null;
  const elapsed = running && !finished
    ? nowSeconds - started
    : Number(shot.generation_elapsed_seconds ?? Math.max(0, finished - started));
  return Math.max(0, Math.floor(elapsed));
}

function shotElapsedText(shot, nowSeconds = Date.now() / 1000) {
  const sec = shotElapsedSeconds(shot, nowSeconds);
  return sec === null ? '—' : formatElapsedSeconds(sec);
}

function updateSceneSummary(nowSeconds = Date.now() / 1000) {
  const el = $('sceneSummary');
  if (!el) return;
  if (!state.shots || !state.shots.length) {
    el.textContent = '0 Shots';
    return;
  }
  const totalCount = state.shots.length;
  const videoSeconds = state.shots.reduce((a, b) => a + Number(b.seconds || 0), 0);
  const allDone = state.shots.every((s) => String(s.status || '').toUpperCase() === 'DONE');
  const anyRunning = state.shots.some((s) => ['QUEUED', 'PREPARING', 'RUNNING'].includes(String(s.status || '').toUpperCase()));
  let totalGenSeconds = 0;
  let hasGenTime = false;
  for (const s of state.shots) {
    const sec = shotElapsedSeconds(s, nowSeconds);
    if (sec !== null) {
      totalGenSeconds += sec;
      hasGenTime = true;
    }
  }
  const durationLabel = `${allDone ? 'Total duration' : 'Estimated total'} ${videoSeconds.toFixed(1)} s`;
  if (hasGenTime) {
    const genPrefix = (allDone && !anyRunning) ? 'Total generation time' : 'Generation time';
    el.textContent = `${totalCount} shots · ${durationLabel} · ${genPrefix} ${formatElapsedSeconds(totalGenSeconds)}`;
  } else {
    el.textContent = `${totalCount} shots · ${durationLabel}`;
  }
}

function songElapsedText(mv, nowSeconds = Date.now() / 1000) {
  return shotElapsedText({status: mv?.song_status, generation_started_at: mv?.song_started_at,
    generation_finished_at: mv?.song_finished_at, generation_elapsed_seconds: mv?.song_elapsed_seconds}, nowSeconds);
}

function refreshShotElapsed() {
  const shots = new Map(state.shots.map(shot => [String(shot.id), shot]));
  const now = Date.now() / 1000;
  document.querySelectorAll('[data-shot-elapsed]').forEach(cell => {
    const text = shotElapsedText(shots.get(cell.dataset.shotElapsed), now);
    if (cell.textContent !== text) cell.textContent = text;
  });
  updateSceneSummary(now);
  document.querySelectorAll('[data-song-elapsed]').forEach(cell => {
    const text = songElapsedText(state.mvProject, now);
    if (cell.textContent !== text) cell.textContent = text;
  });
  document.querySelectorAll('[data-image-job-elapsed]').forEach(cell => {
    const [sceneId, role] = String(cell.dataset.imageJobElapsed || '').split(':');
    const roleData = getImageRoleData(Number(sceneId), role);
    const rawJob = (state.imageQueue?.jobs || []).find(j => Number(j.scene_id) === Number(sceneId) && j.role === role) || roleData?.latest_job;
    const text = shotElapsedText(rawJob, now);
    if (cell.textContent !== text) cell.textContent = text;
  });
}

function videoProgressDetail(shot) {
  const status = String(shot.status || '').toUpperCase();
  const pct = Math.max(0, Math.min(100, Math.round(Number(shot.progress || 0))));
  const node = String(shot.current_node || '');
  const actualSteps = node.match(/(\d+)\/(\d+)步/);
  const stepText = (completed, total) => actualSteps ? `${actualSteps[1]}/${actualSteps[2]} steps` : `~${completed}/${total} steps`;
  if (status === 'DONE') return { pct: 100, text: shot.error ? 'Previous output is still usable · this run failed, see the error' : 'Done' };
  if (status === 'FAILED') return { pct, text: 'Generation failed; check the error' };
  if (status === 'CANCELLED') return { pct: 0, text: 'Generation cancelled' };
  if (status === 'QUEUED') return { pct: 0, text: node.startsWith('Auto-continue') ? node : 'Waiting for queue' };
  if (!['PREPARING', 'RUNNING'].includes(status)) return { pct: 0, text: 'Pending' };
  if (videoModelCatalog.profile(shot.video_model)?.execution_backend === 'pika') return { pct, text: node || 'Waiting for the official Pika program' };
  if (/下载/.test(node) || (pct >= 98 && !/SaveVideo/.test(node))) return { pct, text: 'Download the final cut; 100% shows only when done' };
  if (node.includes('Cloud finished')) return { pct, text: node };
  if (/SaveVideo|CreateVideo|VAEDecode|AVDecode/.test(node) || pct >= 90) return { pct, text: /SaveVideo|CreateVideo/.test(node) ? 'Encoding and saving video' : 'Decoding video and audio' };
  const isSinglePassB15 = (shot.second_pass_enabled === 0 || shot.second_pass_enabled === false) && !/b15_refine|潜变量放大/i.test(node);
  if (isSinglePassB15 && (videoModelCatalog.profile(shot.video_model)?.pipeline === 'two_pass' || /b15|一采|首轮|Node 125/i.test(node))) {
    if (/一采|首轮|Sampler|Node 125/.test(node) || pct >= 5) {
      const completed = Math.min(10, Math.max(0, Math.round((pct - 5) / 80 * 10)));
      return { pct, text: `First pass running · ${stepText(completed, 10)} · 0.4MP` };
    }
    return { pct, text: status === 'PREPARING' ? 'Preparing shots and reference assets' : 'Loading model, preparing first pass (10 steps)' };
  }
  if (videoModelCatalog.profile(shot.video_model)?.pipeline === 'two_pass' || /b15|潜变量放大/i.test(node)) {
    if (/b15_refine|细化|精修/.test(node) || pct >= 65) {
      const completed = Math.min(3, Math.max(0, Math.round((pct - 65) / 20 * 3)));
      return { pct, text: `Open-H3 1080p refine · ${stepText(completed, 3)} · start_sigma=0.78` };
    }
    if (/b15_load_video|b15_video_comp|b15_prep_draft|b15_vae_encode|加载当前镜头一采原片|一采单视频编码/.test(node)) {
      return { pct, text: 'Second pass only · encoding this shot’s first-pass video, preparing 2.0x proportional upscale' };
    }
    if (/b15_split|b15_upscale|b15_concat|b15_regen_lora|b15_text_cond|b15_guider|b15_sampler|b15_noise|b15_schedule|潜变量放大/.test(node) || (pct >= 55 && !/首轮|Node 125/.test(node))) {
      return { pct, text: 'Latent 2.0x proportional upscale · 0.4MP → 1080p, preparing Open-H3 3-step refine' };
    }
    if (/首轮|Sampler|Node 125/.test(node) || pct >= 5) {
      const completed = Math.min(10, Math.max(0, Math.round((pct - 5) / 50 * 10)));
      return { pct, text: `First sampling round · ${stepText(completed, 10)} · 0.4MP` };
    }
    return { pct, text: status === 'PREPARING' ? 'Preparing the two-pass workflow and reference assets' : 'Loading model, preparing first sampling round (10 steps)' };
  }
  if (/Sampler/.test(node)) {
    const steps = Number(shot.steps || 4);
    const completed = Math.min(steps, Math.max(0, Math.round((pct - 5) / 80 * steps)));
    return { pct, text: pct < 5 ? 'Entered the sampling node, waiting for cloud progress' : `Video sampling · done${stepText(completed, steps)}` };
  }
  if (status === 'PREPARING') return { pct, text: 'Preparing workflow and reference assets' };
  return { pct, text: 'Submitted to the cloud; loading model and inputs' };
}

function renderShotError(shot) {
  $('shotError').classList.toggle('hidden', !shot?.error);
  $('shotErrorMsg').textContent = shot?.error || '';
}

let videoProgressPolling = false;
async function refreshVideoProgress() {
  if (videoProgressPolling || !state.sceneId || state.workspaceMode !== 'video') return;
  videoProgressPolling = true;
  const sceneId = Number(state.sceneId);
  try {
    const rows = await api(`/api/scenes/${sceneId}/shots`);
    if (Number(state.sceneId) !== sceneId || state.workspaceMode !== 'video') return;
    const fields = ['status', 'progress', 'current_node', 'error', 'prompt_id', 'output_path', 'last_frame_path', 'generation_started_at', 'generation_finished_at', 'generation_elapsed_seconds'];
    let listChanged = false;
    for (const row of rows) {
      const local = state.shots.find(x => Number(x.id) === Number(row.id));
      if (!local) continue;
      const rowChanged = fields.some(key => local[key] !== row[key]);
      if (!rowChanged) continue;
      listChanged = true;
      for (const key of fields) local[key] = row[key];
      if (Number(state.shotId) === Number(row.id)) {
        if (state.currentShot) for (const key of fields) state.currentShot[key] = row[key];
        $('editorStatusBadge').className = `badge-status ${statusClass(row.status)}`;
        $('editorStatusBadge').textContent = fmtStatus(row.status);
        renderShotError(local);
        updateGenerationLive(local);
      }
    }
    if (listChanged) {
      renderShots();
      renderTopVideoQueueBar();
    }
    if ((!$('terminalLogPanel')?.classList.contains('hidden')) || $('log-details')?.open) {
      void loadTerminalLog();
    }
  } catch (e) { console.warn('Video progress sync failed', e); }
  finally { videoProgressPolling = false; }
}

let terminalLogBusy = false;
function syncTerminalLogShotOptions() {
  const sel = $('terminalLogShotSelect');
  if (!sel) return;
  const prev = sel.value || 'auto';
  const opts = [['auto', '⚡ Auto-follows the running or selected shot']];
  for (const s of (state.shots || [])) {
    const no = `#${String(s.shot_no).padStart(3, '0')}`;
    opts.push([String(s.id), `${no} · ${s.title || s.mode} (${fmtStatus(s.status)})`]);
  }
  const cur = [...sel.options].map(o => `${o.value}:${o.textContent}`).join('|');
  const next = opts.map(([v, t]) => `${v}:${t}`).join('|');
  if (cur !== next) {
    sel.replaceChildren(...opts.map(([v, t]) => new Option(t, v)));
    sel.value = opts.some(([v]) => v === prev) ? prev : 'auto';
  }
}

async function loadTerminalLog(force = false) {
  if (terminalLogBusy && !force) return;
  syncTerminalLogShotOptions();
  const selVal = $('terminalLogShotSelect')?.value || 'auto';
  let target = null;
  if (selVal !== 'auto') {
    target = (state.shots || []).find(x => String(x.id) === String(selVal));
  }
  if (!target) {
    target = (state.shots || []).find(x => ['RUNNING', 'PREPARING'].includes(x.status))
      || state.currentShot
      || (state.shots || [])[0];
  }
  if (!target) {
    if ($('full-logs')) $('full-logs').textContent = 'No shot tasks in this Scene';
    if ($('logs')) $('logs').textContent = 'No shot tasks in this Scene';
    return;
  }
  terminalLogBusy = true;
  try {
    const data = await api(`/api/shots/${target.id}/log`);
    const text = data.log || 'No log output yet';
    for (const id of ['full-logs', 'logs']) {
      const el = $(id);
      if (!el) continue;
      const atBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 60;
      if (el.textContent !== text) {
        el.textContent = text;
        if (atBottom) el.scrollTop = el.scrollHeight;
      }
    }
    if ($('logJobIndicator')) {
      const shortJob = data.job_id ? ` · task ${String(data.job_id).slice(0, 8)}` : '';
      $('logJobIndicator').textContent = `Shot #${String(target.shot_no).padStart(3, '0')} (${data.backend === 'pika' ? 'Pika CLI' : 'ComfyUI'})${shortJob}`;
    }
  } catch (e) {
    const msg = `Loading the log failed: ${e.message}`;
    if ($('full-logs')) $('full-logs').textContent = msg;
    if ($('logs')) $('logs').textContent = msg;
  } finally {
    terminalLogBusy = false;
  }
}

function updateGenerationLive(shot) {
  const box = $('generationLive');
  if (!box || !shot) return;

  const active = ['QUEUED', 'PREPARING', 'RUNNING'].includes(shot.status);
  box.classList.toggle('hidden', !active);
  if (!active) return;

  const pct = Math.max(0, Math.min(100, Math.round(Number(shot.progress || 0))));
  $('generationLivePct').textContent = `${pct}%`;
  $('generationLiveBar').style.width = `${pct}%`;
  const twoPassEnabled = Number(shot.second_pass_enabled) === 1;
  const resLabel = String(shot.second_pass_resolution || '1k').toUpperCase();
  const isPika = videoModelCatalog.profile(shot.video_model)?.execution_backend === 'pika';
  $('generationLiveLabel').textContent = isPika
    ? (twoPassEnabled ? `Pika progress · ${resLabel} Full BF16 second-pass refine (3 steps)` : 'Pika progress · official first pass (6 steps)')
    : ((videoModelCatalog.profile(shot.video_model)?.pipeline === 'two_pass' || /b15/i.test(shot.current_node || ''))
      ? (twoPassEnabled ? `Video progress · Open-H3 ${resLabel} second-pass refine` : 'Video progress · first pass only (0.4MP / 10 steps)')
      : 'Current progress');
  $('generationLiveNode').textContent = videoProgressDetail(shot).text;
}

function renderTopVideoQueueBar(queueSizeOverride) {
  if (state.workspaceMode === 'image') return;
  const done = state.shots.filter((x) => x.status === 'DONE').length;
  const total = state.shots.length;
  const pct = total ? Math.round(state.shots.reduce((sum, x) => sum + (x.status === 'DONE' ? 100 : (['RUNNING', 'PREPARING'].includes(x.status) ? Number(x.progress || 0) : 0)), 0) / total) : 0;
  let qText = `Done ${done}/${total}`;
  if (Number(queueSizeOverride) > 0) qText += ` · queue ${queueSizeOverride}`;
  const activeShot = state.shots.find(x => ['RUNNING', 'PREPARING'].includes(x.status));
  if (activeShot) {
    const detail = videoProgressDetail(activeShot);
    const resTag = String(activeShot.second_pass_resolution || '1k').toUpperCase();
    const passTag = Number(activeShot.second_pass_enabled) === 1 ? `Second pass${resTag}` : 'First pass 0.4MP';
    qText += ` · #${String(activeShot.shot_no).padStart(3, '0')} (${passTag}) ${detail.pct}%`;
  }
  if ($('queueText')) $('queueText').textContent = qText;
  if ($('queueProgress')) $('queueProgress').style.width = `${pct}%`;
}

let statusRefreshBusy = false;
let lastStatusRefresh = 0;
async function refreshStatus() {
  if (statusRefreshBusy || Date.now() - lastStatusRefresh < 1000) return;
  statusRefreshBusy = true;
  lastStatusRefresh = Date.now();
  try {
    const s = await api('/api/status');
    const dot = $('cloudDot');
    const pikaSelected = state.workspaceMode === 'video' && videoModelCatalog.profile($('shotVideoModel').value)?.execution_backend === 'pika';
    dot.className = `status-pulse ${(pikaSelected ? s.pika?.online : s.online) ? 'online' : 'offline'}`;
    $('cloudText').textContent = `ComfyUI ${s.online ? 'Online' : 'Offline'} · Pika ${s.pika?.online ? 'Online' : 'Offline'}`;
    $('gpuText').textContent = '';
    $('gpuText').style.display = 'none';

    if (state.workspaceMode === 'image') {
      const imageQueueSize = Number(state.imageQueue?.queue_size || 0);
      const currentImageJob = state.imageQueue?.current_job_id;
      const currentImage = (state.imageQueue?.jobs || []).find((job) => Number(job.id) === Number(currentImageJob));
      const imagePct = currentImage ? Math.max(0, Math.min(100, Math.round(Number(currentImage.progress || 0)))) : 0;
      $('queueText').textContent = currentImageJob
        ? `Generating images ${imagePct}% · waiting ${imageQueueSize}`
        : (imageQueueSize ? `Image queue ${imageQueueSize}` : 'Image queue idle');
      $('queueProgress').style.width = `${imagePct}%`;
    } else {
      renderTopVideoQueueBar(s.queue_size);
    }
  } catch (e) {
    $('cloudDot').className = 'status-pulse offline';
    $('cloudText').textContent = 'Service offline';
    $('gpuText').style.display = 'none';
  } finally {
    statusRefreshBusy = false;
  }
}

// ==========================================
// Scene Image Studio (Krea 2 Turbo + 4-Step LoRA)
// ==========================================
function sceneImageRoleLabel(role) {
  const map = {
    ref1: 'Character 1 Ref1',
    ref2: 'Character 2 Ref2',
    ref3: 'Scene A Ref3',
    ref4: 'Scene B Ref4',
  };
  return map[role] || role;
}

// ==========================================
// Scene Image Workbench
// ==========================================
const IMAGE_ROLES = ['ref1', 'ref2', 'ref3', 'ref4'];

function getImageSceneData(sceneId = state.imageSceneId) {
  if (Number(state.imageDashboard?.project_id) !== Number(state.projectId)) return null;
  return state.imageDashboard?.scenes?.find((scene) => Number(scene.id) === Number(sceneId)) || null;
}

function getImageRoleData(sceneId = state.imageSceneId, role = state.imageRole) {
  const scene = getImageSceneData(sceneId);
  return scene?.roles?.[role] || null;
}

function imageJobStatus(roleData) {
  const job = roleData?.latest_job;
  if (job) {
    if (job.status === 'DRAFT') return 'PENDING';
    if (['QUEUED', 'PREPARING', 'RUNNING', 'DONE', 'FAILED'].includes(job.status)) return job.status;
  }
  if (roleData?.latest_image) return 'DONE';
  return 'PENDING';
}

async function setWorkspaceMode(mode) {
  const nextMode = ['image', 'mv', 'drama'].includes(mode) ? mode : 'video';
  // 工作台切换必须保留用户当前选中的 Scene。两个工作台只是不同视图，
  // 不能因为切换模式就回到第一个 Scene。
  const preservedSceneId = state.workspaceMode === 'image'
    ? Number(state.imageSceneId || state.sceneId || 0)
    : Number(state.sceneId || state.imageSceneId || 0);
  if (preservedSceneId) {
    state.sceneId = preservedSceneId;
    state.imageSceneId = preservedSceneId;
  }

  state.workspaceMode = nextMode;
  const imageMode = state.workspaceMode === 'image';
  const mvMode = state.workspaceMode === 'mv';
  const dramaMode = state.workspaceMode === 'drama';
  const videoMode = state.workspaceMode === 'video';
  $('videoWorkspaceBtn').classList.toggle('active', videoMode);
  $('imageWorkspaceBtn').classList.toggle('active', imageMode);
  $('mvWorkspaceBtn').classList.toggle('active', mvMode);
  $('dramaWorkspaceBtn')?.classList.toggle('active', dramaMode);
  $('imageWorkbench').classList.toggle('hidden', !imageMode);
  $('mvWorkbench').classList.toggle('hidden', !mvMode);
  $('dramaWorkbench')?.classList.toggle('hidden', !dramaMode);
  $('imageInspector').classList.toggle('hidden', !imageMode);
  $('mvInspector').classList.toggle('hidden', !mvMode);
  $('dramaInspector')?.classList.toggle('hidden', !dramaMode);
  $('videoSceneHeader')?.classList.toggle('hidden', !videoMode);
  $('mainVideoPanel')?.classList.toggle('hidden', !videoMode || !$('mainShotVideo')?.getAttribute('src'));
  $('tableView').classList.toggle('hidden', !videoMode);
  $('editorEmpty').classList.toggle('hidden', !videoMode || Boolean(state.shotId));
  $('editorBody').classList.toggle('hidden', !videoMode || !state.shotId);
  $('editorPanel')?.classList.remove('hidden');
  $('mergeProjectBtn').classList.toggle('hidden', !videoMode);
  $('playProjectMasterBtn').classList.toggle('hidden', !videoMode || !state.projectMaster);
  if (imageMode) {
    // 先刷新 Scene 列表并验证当前 Scene，再加载生图 dashboard。
    // 这样刚新建的 Scene 也不会因为 state.scenes 还是旧缓存而被回退到第一个。
    await loadScenes();
    if (state.sceneId) state.imageSceneId = state.sceneId;
    await loadImageDashboard();
    await refreshImageQueueStatus();
  } else if (mvMode) {
    await loadMVProject();
  } else if (dramaMode) {
    await loadDrama3State();
  } else {
    // 从生图工作台回来时，把当前 imageSceneId 原样带回视频工作台。
    if (state.imageSceneId) state.sceneId = state.imageSceneId;
    await loadScenes();
    if (state.sceneId) await loadShots();
  }
  refreshStatus();
}

async function loadImageDashboard() {
  const projectId = state.projectId;
  const requestSeq = ++state.imageDashboardSeq;
  if (!state.projectId) {
    state.imageDashboard = { scenes: [] };
    renderImageWorkbench();
    renderImageInspector();
    return;
  }
  try {
    const dashboard = await api(`/api/projects/${projectId}/image-dashboard`);
    if (Number(state.projectId) !== Number(projectId) || requestSeq !== state.imageDashboardSeq) return;
    state.imageDashboard = dashboard;
    const ids = (state.imageDashboard.scenes || []).map((s) => Number(s.id));
    if (!ids.includes(Number(state.imageSceneId))) {
      const currentVideoSceneId = Number(state.sceneId || 0);
      state.imageSceneId = ids.includes(currentVideoSceneId) ? currentVideoSceneId : (ids[0] || null);
    }
    if (state.workspaceMode === 'image') state.sceneId = state.imageSceneId;
    renderImageWorkbench();
    renderImageInspector();
    await loadImageWorkbenchHistory();
  } catch (e) {
    if (Number(state.projectId) !== Number(projectId) || requestSeq !== state.imageDashboardSeq) return;
    showToast(`Loading the image workbench failed: ${e.message}`, 'error', 7000);
  }
}

function renderImageWorkbench() {
  const container = $('imageStoryboardView');
  if (!container) return;

  const sceneData = getImageSceneData();
  if (!sceneData) {
    imageWorkbenchView.update([]);
    return;
  }

  const rawJobs = (Array.isArray(state.imageQueue?.jobs) ? state.imageQueue.jobs : [])
    .filter((job) => Number(job.scene_id) === Number(state.imageSceneId));
  const latestJobByRole = new Map();
  rawJobs.forEach((job) => {
    const existing = latestJobByRole.get(job.role);
    const isCurrent = Number(job.id) === Number(state.imageQueue.current_job_id);
    const existingCurrent = existing && Number(existing.id) === Number(state.imageQueue.current_job_id);
    if (!existing || isCurrent || (!existingCurrent && Number(job.id) > Number(existing.id))) {
      latestJobByRole.set(job.role, job);
    }
  });

  // 当前 Scene 永远固定显示 Ref1~Ref4 四个槽位；没有 job 也必须占一行。
  const jobs = IMAGE_ROLES.map((role, index) => {
    const roleData = sceneData.roles?.[role] || {};
    const config = roleData.config || {};
    const latestJob = latestJobByRole.get(role) || roleData.latest_job || null;
    const latestImage = roleData.latest_image || null;
    const enabled = true;
    const status = imageJobStatus({ latest_job: latestJob, latest_image: latestImage });
    return {
      ...(latestJob || {}),
      slot_index: index + 1,
      real_job_id: latestJob?.id || null,
      id: latestJob?.id || `slot-${state.imageSceneId}-${role}`,
      project_id: state.projectId,
      scene_id: state.imageSceneId,
      scene_name: sceneData.name,
      role,
      prompt: latestJob?.prompt || config.prompt || '',
      status,
      enabled,
      latest_image: latestImage,
      progress: latestJob?.progress || (status === 'DONE' ? 100 : 0),
      current_node: latestJob?.current_node || '',
      error: latestJob?.error || '',
    };
  });

  const activeJobs = jobs.filter((job) => ['QUEUED', 'PREPARING', 'RUNNING'].includes(job.status));
  if ($('imageSceneTitle')) $('imageSceneTitle').textContent = 'Image task queue';
  if ($('imageSceneSummary')) $('imageSceneSummary').textContent = `${activeJobs.length} in progress · 4 fixed Ref slots`;

  imageWorkbenchView.update(jobs);
}

function currentImageEditorKey(sceneId = state.imageSceneId, role = state.imageRole) {
  return sceneId ? `${Number(sceneId)}:${role}` : '';
}

function markImageEditorDirty() {
  const key = currentImageEditorKey();
  if (!key) return;
  state.imageEditorKey = key;
  state.imageEditorDirty = true;
}

function captureImageConfigForm() {
  return {
    ref_scope: $('imageRefScope').value,
    prompt: $('imagePrompt').value.trim(),
    aspect_ratio: $('imageRatio').value,
    seed: Number($('imageSeed').value || 42),
    steps: Number($('imageSteps').value || 0),
    cfg: Number($('imageCfg').value || 0),
    image_model: $('imageModel').value,
    lora_mode: $('imageLoraMode').value,
    enabled: true,
  };
}

async function saveImageConfigSnapshot(sceneId, role, payload) {
  try {
    await api(`/api/scenes/${sceneId}/image-configs/${role}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    const scene = getImageSceneData(sceneId);
    const config = scene?.roles?.[role]?.config;
    if (config) Object.assign(config, payload, { enabled: payload.enabled ? 1 : 0 });
    return true;
  } catch (e) {
    showToast(`Autosave ${sceneImageRoleLabel(role)} Failed: ${e.message}`, 'error', 5000);
    return false;
  }
}

function syncSceneSelectionHighlight() {
  const activeSceneId = state.workspaceMode === 'image' ? state.imageSceneId : state.sceneId;
  document.querySelectorAll('#sceneList .nav-item[data-scene-id]').forEach((item) => {
    item.classList.toggle('active', Number(item.dataset.sceneId) === Number(activeSceneId));
  });
}

async function selectImageScene(sceneId, role = state.imageRole) {
  if (state.workspaceMode !== 'image') await setWorkspaceMode('image');
  const nextRole = IMAGE_ROLES.includes(role) ? role : 'ref1';
  const nextSceneId = Number(sceneId);
  const nextKey = currentImageEditorKey(nextSceneId, nextRole);
  const previousSceneId = Number(state.imageSceneId || 0);
  const previousRole = state.imageRole;
  const previousKey = currentImageEditorKey();
  const previousDraft = state.imageEditorDirty && previousKey && previousKey !== nextKey
    ? captureImageConfigForm()
    : null;
  const changed = previousKey !== nextKey;
  const requestSeq = ++state.imageSelectSeq;

  // 先切 UI，再做任何网络请求。列表点击必须即时响应。
  state.imageSceneId = nextSceneId;
  state.sceneId = nextSceneId;
  state.imageRole = nextRole;
  if (changed) {
    state.imageEditorDirty = false;
    state.imageEditorKey = nextKey;
  }
  syncSceneSelectionHighlight();
  renderImageWorkbench();
  renderImageInspector({ forceForm: changed });
  const gallery = $('imageHistoryGallery');
  if (changed && gallery) imageHistoryView.update([], 'Loading history…');

  // 旧 Ref 的草稿在后台保存，不阻塞新 Ref 的显示。
  if (previousDraft && previousSceneId) {
    void saveImageConfigSnapshot(previousSceneId, previousRole, previousDraft);
  }

  // Scene 列表和历史记录都后台刷新；旧请求不得覆盖后来的选择。
  void loadScenes();
  void loadImageWorkbenchHistory(requestSeq);
}

function renderImageInspector({ forceForm = false } = {}) {
  const scene = getImageSceneData();

  if ($('imageInspectorRoleBadge')) {
    $('imageInspectorRoleBadge').textContent = sceneImageRoleLabel(state.imageRole);
  }

  document.querySelectorAll('.image-role-tab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.role === state.imageRole);
  });

  const data = getImageRoleData();
  const config = data?.config || {};
  const status = imageJobStatus(data);
  const img = data?.latest_image;

  const editorKey = currentImageEditorKey();
  const shouldLoadForm = forceForm || !state.imageEditorDirty || state.imageEditorKey !== editorKey;
  const isCharacterRole = ['ref1', 'ref2'].includes(state.imageRole);
  const defaultScope = isCharacterRole ? (config.ref_scope || 'project') : 'scene';
  if (shouldLoadForm) {
    $('imageRefScope').value = defaultScope;
    $('imagePrompt').value = config.prompt || '';
    $('imageRatio').value = config.aspect_ratio || '9:16';
    $('imageSeed').value = Number(config.seed ?? 42);
    $('imageModel').value = config.image_model || 'moss_image_5b';
    $('imageLoraMode').value = config.lora_mode || 'all';
    const defaults = defaultImageStepsAndCfg($('imageModel').value, $('imageLoraMode').value);
    $('imageSteps').value = Number(config.steps) > 0 ? Number(config.steps) : defaults.steps;
    $('imageCfg').value = Number(config.cfg) > 0 ? Number(config.cfg) : defaults.cfg;
    syncImageModelControls({ applyDefaults: false });
    state.imageEditorKey = editorKey;
    state.imageEditorDirty = false;
  }
  $('imageRefScope').disabled = !isCharacterRole;
  if ($('imageRefScopeGroup')) $('imageRefScopeGroup').classList.toggle('hidden', !isCharacterRole);
  $('imageRefScopeHint').textContent = config.ref_scope === 'project'
    ? `Shared character · source Scene #${config.source_scene_id || state.imageSceneId}, generated once only; edits and applies affect every Scene sharing this character.`
    : 'Used only by this Scene; identical prompts will not overwrite other Scenes.';
  $('imageHistoryCount').textContent = `History: ${Number(data?.history_count || 0)}`;

  $('imageInspectorStatus').className = `badge-status ${statusClass(status)}`;
  $('imageInspectorStatus').textContent = fmtStatus(status);

  // Update Right Stage Preview
  const stagePreview = $('imageStagePreview');
  const stagePlaceholder = $('imageStagePlaceholder');
  const stageToolbar = $('imageStageToolbar');
  const stageMeta = $('imageStageMetaText');

  if (img && stagePreview && stagePlaceholder) {
    stagePreview.src = `${img.media_url}?t=${img.id}`;
    stagePreview.classList.remove('hidden');
    stagePlaceholder.classList.add('hidden');
    if (stageToolbar) stageToolbar.classList.remove('hidden');
    if (stageMeta) stageMeta.textContent = `${img.width || 768}×${img.height || 1360} · Seed ${img.seed || config.seed || 42}`;
  } else if (stagePreview && stagePlaceholder) {
    stagePreview.classList.add('hidden');
    stagePlaceholder.classList.remove('hidden');
    if (stageToolbar) stageToolbar.classList.add('hidden');
  }

  updateImageGenerationLive(data?.latest_job);
}

function updateImageGenerationLive(job) {
  const box = $('imageGenerationLive');
  if (!box) return;
  const active = job && ['QUEUED', 'PREPARING', 'RUNNING'].includes(job.status);
  box.classList.toggle('hidden', !active);
  if (!active) return;
  const pct = Math.max(0, Math.min(100, Math.round(Number(job.progress || 0))));
  $('imageGenerationPct').textContent = `${pct}%`;
  $('imageGenerationBar').style.width = `${pct}%`;
  $('imageGenerationLabel').textContent = `${sceneImageRoleLabel(job.role)} · ${fmtStatus(job.status)}`;
  $('imageGenerationNode').textContent = job.current_node || (job.status === 'QUEUED' ? 'Waiting in queue' : 'Preparing');
}

async function saveImageConfig({ silent = false } = {}) {
  if (!state.imageSceneId) return false;
  const sceneId = Number(state.imageSceneId);
  const role = state.imageRole;
  const editorKey = currentImageEditorKey(sceneId, role);
  const payload = {
    ref_scope: $('imageRefScope').value,
    prompt: $('imagePrompt').value.trim(),
    aspect_ratio: $('imageRatio').value,
    seed: Number($('imageSeed').value || 42),
    steps: Number($('imageSteps').value || 0),
    cfg: Number($('imageCfg').value || 0),
    image_model: $('imageModel').value,
    lora_mode: $('imageLoraMode').value,
    enabled: true,
  };
  try {
    await api(`/api/scenes/${sceneId}/image-configs/${role}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    const stillSameDraft = currentImageEditorKey() === editorKey
      && $('imagePrompt').value.trim() === payload.prompt
      && $('imageRatio').value === payload.aspect_ratio
      && Number($('imageSeed').value || 42) === payload.seed
      && Number($('imageSteps').value || 0) === payload.steps
      && Number($('imageCfg').value || 0) === payload.cfg
      && $('imageLoraMode').value === payload.lora_mode
      && $('imageModel').value === payload.image_model
      ;
    if (stillSameDraft) {
      state.imageEditorKey = editorKey;
      state.imageEditorDirty = false;
    }
    if (!silent) showToast(`${sceneImageRoleLabel(role)} Parameters saved`, 'success');
    await loadImageDashboard();
    return true;
  } catch (e) {
    if (!silent) showToast(`Saving image parameters failed: ${e.message}`, 'error');
    return false;
  }
}

async function queueCurrentImageRef() {
  return withSubmissionGuard(imageSubmissionKey(state.imageSceneId,state.imageRole), () => queueCurrentImageRefImpl());
}

async function queueCurrentImageRefImpl() {
  if (!state.imageSceneId) return;
  const sceneId = state.imageSceneId;
  const role = state.imageRole;
  if (!await saveImageConfig({ silent: true })) return showToast('Saving parameters failed; nothing was submitted', 'error');
  if (Number(state.imageSceneId) !== Number(sceneId) || state.imageRole !== role) return;
  const data = getImageRoleData();
  if (!String(data?.config?.prompt || $('imagePrompt').value).trim()) {
    return showToast('This Ref has no prompt yet', 'warning');
  }
  try {
    const result = await api(`/api/scenes/${state.imageSceneId}/images/queue`, {
      method: 'POST',
      body: JSON.stringify({ roles: [state.imageRole], replace_existing: false, only_missing: false }),
    });
    const skipped = (result.skipped_roles || []).length;
    showToast(
      result.queued ? 'Added to the image queue' : (skipped ? 'This task is already queued' : 'This item cannot be generated yet'),
      'info'
    );
    await loadImageDashboard();
    await refreshImageQueueStatus();
  } catch (e) {
    showToast(`Adding to the image queue failed: ${e.message}`, 'error');
  }
}

async function queueCurrentSceneMissingImages() {
  return withSubmissionGuard(`images-scene:${state.imageSceneId}`, () => queueCurrentSceneMissingImagesImpl());
}

async function queueCurrentSceneMissingImagesImpl() {
  if (!state.imageSceneId) return showToast('Select a Scene first', 'warning');
  if (!await saveImageConfig({ silent: true })) return showToast('Saving parameters failed; nothing was submitted', 'error');
  try {
    const result = await api(`/api/scenes/${state.imageSceneId}/images/queue`, {
      method: 'POST',
      body: JSON.stringify({ roles: IMAGE_ROLES, replace_existing: false, only_missing: true }),
    });
    const sceneName = getImageSceneData()?.name || `Scene ${state.imageSceneId}`;
    const reasonCounts = {};
    Object.values(result.skipped_reasons || {}).forEach((reason) => {
      reasonCounts[reason] = (reasonCounts[reason] || 0) + 1;
    });
    const parts = [`Added ${Number(result.queued || 0)}`];
    const reasonLabels = {
      existing_image: 'Has image',
      no_prompt: 'No prompt',
      already_active: 'Queued',
      disabled: 'Disabled',
    };
    Object.entries(reasonLabels).forEach(([reason, label]) => {
      if (reasonCounts[reason]) parts.push(`${label} ${reasonCounts[reason]}`);
    });
    showToast(`${sceneName}：${parts.join(' · ')}`, Number(result.queued || 0) ? 'success' : 'info', 5000);
    await loadImageDashboard();
    await refreshImageQueueStatus();
  } catch (e) {
    showToast(`Batch image generation failed for this Scene: ${e.message}`, 'error');
  }
}

async function queueWholeStoryImages() {
  return withSubmissionGuard(`images-project:${state.projectId}`, () => queueWholeStoryImagesImpl());
}

async function queueWholeStoryImagesImpl() {
  if (!state.projectId) return showToast('Select a script first', 'warning');
  if (state.workspaceMode === 'image') {
    const saved = await saveImageConfig({ silent: true });
    if (!saved) return;
  }
  const project = state.projects.find((p) => Number(p.id) === Number(state.projectId));
  const projectName = project?.name || `Project ${state.projectId}`;
  const ok = await showConfirmModal(
    'Full-project images',
    `This adds "${escapeHtml(projectName)}" across all Scenes<b>Refs that are enabled, have a prompt, and have no image yet</b>to the image queue.<br>Refs that already have history images are skipped and will not be regenerated. Continue?`
  );
  if (!ok) return;
  try {
    const result = await api(`/api/projects/${state.projectId}/images/queue`, {
      method: 'POST',
      body: JSON.stringify({ roles: IMAGE_ROLES, replace_existing: false, only_missing: true }),
    });
    showToast(`Full-project images: added ${Number(result.queued || 0)} tasks, skipped ${Number(result.skipped || 0)}`, Number(result.queued || 0) ? 'success' : 'info', 5000);
    await loadImageDashboard();
    await refreshImageQueueStatus();
    await refreshStatus();
  } catch (e) {
    showToast(`Full-project image generation failed to submit: ${e.message}`, 'error', 7000);
  }
}

async function queueWholeStoryVideos() {
  return withSubmissionGuard(`videos-project:${state.projectId}`, () => queueWholeStoryVideosImpl());
}

async function queueWholeStoryVideosImpl() {
  if (!state.projectId) return showToast('Select a script first', 'warning');
  const project = state.projects.find((p) => Number(p.id) === Number(state.projectId));
  const projectName = project?.name || `Project ${state.projectId}`;
  const ok = await showConfirmModal(
    'Full-project generation',
    `This adds "${escapeHtml(projectName)}" across all Scenes’<b>Unfinished Shots</b>to the video queue.<br>DONE / Queued or running Shots are skipped automatically and will not be rendered twice. Continue?`
  );
  if (!ok) return;
  try {
    const result = await api(`/api/projects/${state.projectId}/generate`, { method: 'POST', body: '{}' });
    showToast(Number(result.queued || 0)
      ? `Full-project generation: added ${result.queued} Shots, skipped ${result.skipped || 0}`
      : 'No shots left to generate. When everything is done, click "Merge full project"', Number(result.queued || 0) ? 'success' : 'info', 5000);
    if (state.sceneId) await loadShots();
    await refreshStatus();
  } catch (e) {
    showToast(`Full-project generation failed to submit: ${e.message}`, 'error', 7000);
  }
}

async function loadImageWorkbenchHistory(expectedSelectSeq = state.imageSelectSeq) {
  if (!$('imageHistoryGallery')) return;
  const sceneId = Number(state.imageSceneId || 0);
  const projectId = state.projectId;
  const role = state.imageRole;
  const isCurrent = () => Number(state.projectId) === Number(projectId) && expectedSelectSeq === state.imageSelectSeq
    && Number(state.imageSceneId) === sceneId && state.imageRole === role;
  if (!sceneId) {
    imageHistoryView.update([], 'Select a Scene');
    $('imageHistoryCount').textContent = 'History: 0';
    return;
  }
  try {
    const all = await api(`/api/scenes/${sceneId}/images`);
    if (!isCurrent()) return;
    const rows = all.filter(img => img.role === role);
    $('imageHistoryCount').textContent = `History: ${rows.length}`;
    imageHistoryView.update(rows, rows.length ? '' : 'This Ref has no history images yet', role);
  } catch (error) {
    if (isCurrent()) imageHistoryView.update([], `Loading history failed: ${error.message}`, role);
  }
}

async function refreshImageQueueStatus() {
  const projectId = state.projectId;
  const requestSeq = ++state.imageQueueSeq;
  if (!projectId) return;
  try {
    const queue = await api(`/api/image-queue/status?project_id=${projectId}`);
    if (Number(state.projectId) !== Number(projectId) || requestSeq !== state.imageQueueSeq) return;
    state.imageQueue = queue;
    renderImageWorkbench();
  } catch (e) {
    if (Number(state.projectId) === Number(projectId)) console.warn('refreshImageQueueStatus failed:', e);
  }
}

async function uploadSceneImageFile(file) {
  if (!file) return;
  if (!state.imageSceneId) return showToast('Select a Scene first', 'warning');
  const fd = new FormData();
  fd.append('role', state.imageRole);
  fd.append('file', file);
  try {
    showToast('Uploading reference assets...', 'info', 2000);
    const res = await fetch(`/api/scenes/${state.imageSceneId}/images/upload`, {
      method: 'POST',
      body: fd,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Upload failed');
    }
    showToast(`${sceneImageRoleLabel(state.imageRole)} Reference asset uploaded`, 'success');
    await loadImageDashboard();
    await refreshImageQueueStatus();
  } catch (e) {
    showToast(`Upload failed: ${e.message}`, 'error');
  }
}

// ==========================================
// MV Workbench
// ==========================================
// Project-level MV mode, independent from unsaved shot and song form drafts.
let projectMergePollTimer = null;
let projectMergePollId = null;
function renderProjectMergeProgress(data) {
  const active = data.status === 'RUNNING';
  $('projectMergeProgress').hidden = !data.status || data.status === 'IDLE';
  const pct = Math.max(0, Math.min(100, Number(data.progress || 0)));
  $('projectMergeProgressBar').style.width = `${pct}%`;
  $('projectMergeProgressBar').setAttribute('aria-valuenow', String(pct));
  const seconds = data.started_at ? Math.max(0, Math.floor((Number(data.finished_at || Date.now()/1000) - data.started_at))) : 0;
  $('projectMergeProgressText').textContent = `${data.stage || 'Preparing merge'} · ${Math.floor(pct)}% · ${Math.floor(seconds/60)}分${seconds%60}s`;
  $('projectMergeProgressText').title = data.error || '';
  $('mergeProjectBtn').disabled = active;
}
function watchProjectMerge(projectId) {
  if (projectMergePollId === projectId) return;
  clearInterval(projectMergePollTimer);
  projectMergePollId = projectId;
  $('projectMergeProgress').hidden = true;
  $('mergeProjectBtn').disabled = false;
  let fetching = false;
  const poll = async () => {
    if (fetching || state.projectId !== projectId) return;
    fetching = true;
    try {
      const progress = await api(`/api/projects/${projectId}/merge/progress`);
      if (state.projectId === projectId) renderProjectMergeProgress(progress);
    } catch {} finally { fetching = false; }
  };
  void poll();
  projectMergePollTimer = setInterval(poll, 1000);
}

let videoMVModeSeq = 0;
function renderVideoMVMode(mv) {
  watchProjectMerge(state.projectId);
  $('videoMVMode').value = Number(mv?.native_audio_enabled) ? '1' : '0';
  const unsupported = videoModelCatalog.profile($('shotVideoModel').value)?.supports_audio_lock === false;
  $('videoMVMode').disabled = !state.projectId || unsupported;
  if (unsupported) {
    $('videoMVModeHint').textContent = Number(mv?.native_audio_enabled)
      ? 'This project uses MV original-audio driving, which Pika does not support yet. Use B15, or use Pika in a normal video project.'
      : 'Pika supports normal video with native generated audio; MV original-audio locking is not supported yet.';
    return;
  }
  $('videoMVModeHint').textContent = Number(mv?.native_audio_enabled)
    ? 'Applies to all shots in this project · requires H3 b15–49, song analysis and shot time binding; queued tasks keep their current settings.'
    : 'All shots in this project use the normal video pipeline; queued tasks keep their current settings.';
}
async function loadVideoMVMode() {
  const projectId = state.projectId;
  const seq = ++videoMVModeSeq;
  $('videoMVMode').disabled = true;
  $('videoMVModeHint').textContent = 'Reading project settings…';
  if (!projectId) return;
  try {
    const mv = await api(`/api/projects/${projectId}/mv`);
    if (seq !== videoMVModeSeq || state.projectId !== projectId) return;
    state.mvProject = mv;
    renderVideoMVMode(mv);
  } catch (e) {
    if (seq !== videoMVModeSeq || state.projectId !== projectId) return;
    $('videoMVModeHint').textContent = `Load failed: ${e.message}; re-select the shot to retry`;
  }
}
async function saveVideoMVMode() {
  const projectId = state.projectId;
  if (!projectId) return;
  const enabled = $('videoMVMode').value === '1';
  const seq = ++videoMVModeSeq;
  $('videoMVMode').disabled = true;
  $('videoMVModeHint').textContent = 'Saving…';
  try {
    const mv = await api(`/api/projects/${projectId}/mv`, {
      method: 'PUT', body: JSON.stringify({ native_audio_enabled: enabled }),
    });
    if (seq !== videoMVModeSeq || state.projectId !== projectId) return;
    state.mvProject = mv;
    renderVideoMVMode(mv);
    showToast(enabled ? 'This project uses MV song driving' : 'Project switched to normal video', 'success');
  } catch (e) {
    if (seq !== videoMVModeSeq || state.projectId !== projectId) return;
    showToast(`Saving MV settings failed:${e.message}`, 'error');
    await loadVideoMVMode();
  }
}

async function loadMVProject() {
  if (!state.projectId) {
    state.mvProject = null;
    state.mvTimeline = null;
    renderMVWorkbench();
    return;
  }
  try {
    state.mvProject = await api(`/api/projects/${state.projectId}/mv`);
    try {
      state.mvTimeline = await api(`/api/projects/${state.projectId}/audio/timeline`);
    } catch {
      state.mvTimeline = null;
    }
    renderMVWorkbench();
  } catch (e) {
    showToast(`Loading the MV workbench failed: ${e.message}`, 'error', 6000);
  }
}

function setMVStageProgress(barId, pctId, progress, status, node = '') {
  const pct = Math.max(0, Math.min(100, Math.round(Number(progress || 0))));
  const bar = $(barId);
  const label = $(pctId);
  if (bar) bar.style.width = `${pct}%`;
  if (label) label.textContent = `${pct}%`;
  const holder = bar?.closest('.mv-stage-progress');
  if (holder) holder.title = node || `${String(status || 'IDLE').toUpperCase()} · ${pct}%`;
}

function isVoiceOverMVRecord(mv) {
  if (!mv) return false;
  if (mv.audio_source === 'voice_job') return true;
  if (String(mv.genre_prompt || '').trim() === 'spoken word, female studio podcast') return true;
  return false;
}

function renderMVWorkbench() {
  const rawMv = state.mvProject;
  const isFromVoiceJob = isVoiceOverMVRecord(rawMv);
  const mv = isFromVoiceJob ? null : rawMv;
  const sourceNotice = $('mvAudioSourceNotice');
  if (sourceNotice) {
    sourceNotice.classList.toggle('hidden', !isFromVoiceJob);
    if (isFromVoiceJob) {
      const jobNo = Number(rawMv?.source_voice_job_id || 1);
      sourceNotice.innerHTML = `
        <span>🎙️ This project uses talking-head audio from the Voice-over workbench (not a YuE2 song)</span>
        <button type="button" class="btn btn-secondary btn-xs" id="mvJumpVoiceJobBtn">Open the Voice-over workbench</button>
      `;
      $('mvJumpVoiceJobBtn')?.addEventListener('click', () => {
        if (jobNo > 0) state.selectedDramaItemId = String(jobNo);
        setWorkspaceMode('drama');
      });
    } else {
      sourceNotice.innerHTML = '';
    }
  }
  $('mvTitle').textContent = mv?.title || 'Audio workbench';
  // 标题旁的小胶囊：保留"YuE2 style + lyrics generation"的说明，同时把实时状态接在后面。
  const mvStatusText = mv?.song_status === 'RUNNING'
    ? `Generating · ${Math.round(Number(mv.song_progress || 0))}%`
    : (mv?.audio_path ? `Song ready · ${Number(mv.audio_duration || 0).toFixed(1)} s` : 'Pending');
  $('mvSummary').textContent = `YuE2 · full song from style + lyrics · ${mvStatusText}`;
  $('mvProjectTitle').value = mv?.title || '';
  $('mvTargetDuration').value = Number(mv?.target_duration || 60);
  $('mvAudioLinked').checked = Boolean(Number(mv?.audio_linked || 0));
  $('mvGenrePrompt').value = mv?.genre_prompt || '';
  $('mvLyrics').value = mv?.lyrics || '';
  const songStatus = String(mv?.song_status || 'PENDING').toUpperCase();
  $('mvInspectorStatus').className = `badge-status ${statusClass(songStatus)}`;
  $('mvInspectorStatus').textContent = songStatus === 'RUNNING'
    ? `Generating ${Math.round(Number(mv?.song_progress || 0))}%`
    : (songStatus === 'DONE' ? 'Done' : (songStatus === 'FAILED' ? 'Failed' : 'Pending'));
  const songProgress = songStatus === 'DONE'
    ? 100
    : Number(mv?.song_progress || 0);
  setMVStageProgress('mvGenerateStageBar', 'mvGenerateStagePct', songProgress, songStatus, mv?.song_node || '');
  $('mvAudioMeta').textContent = mv?.audio_path
    ? `Song ready · ${Number(mv.audio_duration || 0).toFixed(1)} s`
    : (mv?.song_error ? `Generation failed: ${mv.song_error}` : 'No song yet');
  const mvAudioPlayer = $('mvAudioPlayer');
  const mvAudioUrl = mv?.audio_url || '';
  mvAudioPlayer.classList.toggle('hidden', !mvAudioUrl);
  if (mvAudioUrl && mvAudioPlayer.dataset.audioUrl !== mvAudioUrl) {
    mvAudioPlayer.src = `${mvAudioUrl}?t=${Date.now()}`;
    mvAudioPlayer.dataset.audioUrl = mvAudioUrl;
  }

  const analysis = isFromVoiceJob ? null : state.mvTimeline;
  const analysisStatus = String(analysis?.analysis_status || mv?.analysis_status || 'IDLE').toUpperCase();
  const analysisError = analysis?.analysis_error || mv?.analysis_error || '';
  $('mvAnalysisMeta').textContent = analysisStatus === 'RUNNING'
    ? `Analyzing song · ${Math.round(Number(analysis?.analysis_progress || mv?.analysis_progress || 0))}%`
    : (analysisStatus === 'DONE'
      ? 'Song analysis done: vocals, lyric timeline and beat are ready'
      : (analysisStatus === 'FAILED' ? `Analysis failed: ${analysisError}` : 'Song not analyzed'));
  const timeline = analysis?.timeline || {};
  const slotCount = Array.isArray(timeline.slots) && timeline.slots.length
    ? timeline.slots.length
    : (Array.isArray(timeline.lines) ? timeline.lines.length : 0);
  const boundCount = (analysis?.shots || []).filter((s) => Number(s.target_duration || 0) > 0).length;
  $('mvTimelineMeta').textContent = `${slotCount} song time slots · ${boundCount} existing Shots bound · main duration ${Number(timeline.duration || mv?.audio_duration || 0).toFixed(2)} s`;
  $('mvSrtDownload').classList.toggle('hidden', analysisStatus !== 'DONE');
  if (analysisStatus === 'DONE') $('mvSrtDownload').href = `/api/projects/${state.projectId}/audio/lyrics.srt`;
  const hasAudio = Boolean(mv?.audio_url || mv?.audio_path);
  const genButtons = [$('mvGenerateBtn'), $('mvInspectorGenerateBtn')].filter(Boolean);
  const genButtonText = songStatus === 'RUNNING'
    ? 'Regenerate song'
    : (hasAudio ? 'Regenerate song' : '🎵 Generate song');
  genButtons.forEach((button) => {
    button.textContent = genButtonText;
  });
  const analyzeButtons = [$('mvAnalyzeBtn'), $('mvAnalyzeAgainBtn')].filter(Boolean);
  const analyzeButtonText = analysisStatus === 'RUNNING'
    ? 'Analyzing…'
    : (analysisStatus === 'IDLE' ? 'Analyze song' : 'Re-analyze song');
  analyzeButtons.forEach((button) => {
    button.disabled = !mv?.audio_path || analysisStatus === 'RUNNING';
    button.textContent = analyzeButtonText;
  });
  const analysisProgress = analysisStatus === 'DONE'
    ? 100
    : Number(analysis?.analysis_progress || mv?.analysis_progress || 0);
  setMVStageProgress(
    'mvAnalyzeStageBar',
    'mvAnalyzeStagePct',
    analysisProgress,
    analysisStatus,
    analysisStatus === 'FAILED' ? analysisError : (analysis?.analysis_node || mv?.analysis_node || '')
  );

  const analysisProgressVisible = ['RUNNING', 'DONE', 'FAILED'].includes(analysisStatus);
  $('mvAnalyzeProgress').classList.toggle('hidden', !analysisProgressVisible);
  $('mvAnalyzeProgress').setAttribute('aria-valuenow', String(Math.max(0, Math.min(100, Math.round(analysisProgress)))));
  const analysisStageText = $('mvAnalyzeStageText');
  analysisStageText.classList.toggle('hidden', !analysisProgressVisible);
  analysisStageText.textContent = analysisStatus === 'DONE' ? 'Analysis done'
    : (analysisStatus === 'FAILED' ? `Analysis failed: ${analysisError}`
      : (analysis?.analysis_node || mv?.analysis_node || 'Analyzing the song'));

  renderMVAudioList(mv, analysisStatus);
}

function renderMVAudioList(mv, analysisStatus) {
  const list = $('mvAudioList');
  if (!list) return;

  const songStatus = String(mv?.song_status || 'PENDING').toUpperCase();
  const hasAudio = Boolean(mv?.audio_url || mv?.audio_path);
  const isRunning = songStatus === 'RUNNING';
  const hasConfig = Boolean(String(mv?.genre_prompt || '').trim() || String(mv?.lyrics || '').trim());
  const hasItem = hasAudio || isRunning || songStatus === 'FAILED' || hasConfig;

  if (!hasItem) {
    list.innerHTML = `
      <div class="image-queue-empty">
        <div class="image-queue-empty-icon">♫</div>
        <div class="image-queue-empty-title">No audio tasks</div>
        <div class="image-queue-empty-sub">Fill in the style and lyrics on the right, then click "Generate song" above. Only one song is kept per project; no version history.</div>
      </div>`;
    return;
  }

  const titleRaw = mv?.title || 'YuE2 song';
  const title = escapeHtml(titleRaw);
  const duration = Number(mv?.audio_duration || 0);
  const targetDuration = Number(mv?.target_duration || 60);
  const statusForUi = hasAudio && !isRunning && songStatus !== 'FAILED' ? 'DONE' : songStatus;
  const rawProgress = statusForUi === 'DONE' ? 100 : (mv?.song_progress ?? 0);
  const progress = Math.max(0, Math.min(100, Math.round(Number(rawProgress))));
  const nodeText = mv?.song_error
    ? `Failed: ${String(mv.song_error).replace(/\s+/g, ' ').trim()}`
    : (mv?.song_node || (isRunning ? 'Waiting for YuE2 node progress' : (hasAudio ? 'Generation complete' : 'Waiting to generate')));
  const statusText = fmtStatus(statusForUi);
  const analysisText = analysisStatus === 'DONE'
    ? 'Analyzed'
    : (analysisStatus === 'RUNNING' ? 'Analyzing' : (analysisStatus === 'FAILED' ? 'Analysis failed' : 'Not analyzed'));
  const isActive = ['QUEUED', 'PREPARING', 'RUNNING'].includes(songStatus);
  const durationText = duration > 0
    ? `Actual ${duration.toFixed(1)}s / target ${targetDuration.toFixed(0)}s · ${analysisText}`
    : `Target ${targetDuration.toFixed(0)}s · ${analysisText}`;

  list.innerHTML = '';
  const row = document.createElement('article');
  row.className = `image-queue-task image-queue-table-row mv-audio-table-row ${isRunning ? 'current' : ''}`;
  row.innerHTML = `
    <div class="image-queue-col task-no">
      <strong>#001</strong>
      ${isRunning ? '<span class="queue-current-chip">Current</span>' : ''}
    </div>
    <div class="image-queue-col scene-name" title="${title}">${title}</div>
    <div class="image-queue-col"><span class="badge-status ${statusClass(statusForUi)}">${statusText}</span></div>
    <div class="image-queue-col image-queue-progress-cell">
      <div class="image-queue-task-progress-row">
        <div class="mini-bar-track image-task-progress-track"><div class="mini-bar-fill" style="width:${progress}%"></div></div>
        <span class="font-mono image-task-progress-pct">${progress}%</span>
      </div>
      <div class="image-queue-task-node" title="${escapeHtml(nodeText)}">${escapeHtml(nodeText)}</div>
      <div class="mv-audio-row-meta">${durationText}${Number(mv?.audio_linked || 0) ? ' · linked final cut' : ''}</div>
    </div>
    <div class="image-queue-col font-mono" data-song-elapsed title="Live timer from start of processing; includes download, excludes GPU wait">${songElapsedText(mv)}</div>
    <div class="image-queue-col image-queue-task-actions mv-audio-row-actions">
      <button class="btn ${hasAudio || isActive ? 'btn-ghost' : 'btn-primary'} btn-xs mv-regenerate-btn">${hasAudio || isActive ? 'Regenerate' : 'Generate song'}</button>
      <button class="btn btn-ghost btn-xs mv-preview-btn" ${hasAudio ? '' : 'disabled'} title="${hasAudio ? (isRunning ? 'Preview the last successful song' : 'Preview song') : 'Preview available after the song is generated'}">Preview</button>
      <button class="btn btn-danger-outline btn-xs mv-delete-btn" ${isActive ? 'disabled' : ''}>Delete</button>
    </div>
  `;

  row.querySelector('.mv-regenerate-btn')?.addEventListener('click', async (e) => {
    e.stopPropagation();
    await generateCurrentMVSong(hasAudio || isActive);
  });
  row.querySelector('.mv-preview-btn')?.addEventListener('click', async (e) => {
    e.stopPropagation();
    const projectId = state.projectId;
    try {
      const latest = await api(`/api/projects/${projectId}/mv`);
      if (Number(state.projectId) !== Number(projectId)) return;
      state.mvProject = latest;
      openAudioPreview(latest);
    } catch (error) {
      showToast(`Loading the audio preview failed: ${error.message}`, 'error');
    }
  });
  row.querySelector('.mv-delete-btn')?.addEventListener('click', async (e) => {
    e.stopPropagation();
    const ok = await showConfirmModal(
      'Delete current song',
      'This deletes the current song file and clears song analysis, SRT, timeline, Shot lyric bindings and old lip-sync results.<br><b>No song version history is kept.</b>Delete?'
    );
    if (!ok) return;
    try {
      closeAudioPreview();
      await api(`/api/projects/${state.projectId}/mv/audio`, { method: 'DELETE' });
      await loadMVProject();
      if (state.sceneId) await loadShots();
      showToast('Song deleted', 'success');
    } catch (e2) {
      showToast(`Deleting the song failed: ${e2.message}`, 'error', 7000);
    }
  });

  list.appendChild(row);
}

async function saveMVProject() {
  if (!state.projectId) return;
  await api(`/api/projects/${state.projectId}/mv`, {
    method: 'PUT',
    body: JSON.stringify({
      title: $('mvProjectTitle').value.trim(),
      genre_prompt: $('mvGenrePrompt').value.trim(),
      lyrics: $('mvLyrics').value.trim(),
      target_duration: Number($('mvTargetDuration').value || 60),
      audio_linked: $('mvAudioLinked').checked,
    }),
  });
  showToast('Audio config saved', 'success');
  await loadMVProject();
}

async function uploadMVFile(url, file, successMessage) {
  const fd = new FormData();
  fd.append('file', file);
  const res = await fetch(url, { method: 'POST', body: fd });
  if (!res.ok) {
    const j = await res.json().catch(() => ({}));
    throw new Error(j.detail || 'Upload failed');
  }
  showToast(successMessage, 'success');
  await loadMVProject();
}

async function generateCurrentMVSong(isRegenerate = false) {
  return withSubmissionGuard(`song:${state.projectId}`, () => generateCurrentMVSongImpl(isRegenerate));
}

async function generateCurrentMVSongImpl(isRegenerate = false) {
  if (!state.projectId) return showToast('Select a project first', 'warning');
  if (!$('mvGenrePrompt').value.trim() || !$('mvLyrics').value.trim()) {
    return showToast('Fill in the style and lyrics first', 'warning');
  }
  try {
    await saveMVProject();
    showToast(isRegenerate ? 'YuE2 is regenerating the current song' : 'YuE2 is generating the song; keep Colab online until it finishes', 'info', 4000);
    if (state.mvProject) {
      state.mvProject.song_status = 'RUNNING';
      state.mvProject.song_started_at = 0;
      state.mvProject.song_finished_at = 0;
      state.mvProject.song_elapsed_seconds = 0;
      state.mvProject.song_progress = 2;
      state.mvProject.song_node = 'Preparing the YuE2 workflow';
      state.mvProject.song_error = '';
      renderMVWorkbench();
    }
    await api('/api/projects/' + state.projectId + '/mv/song/generate', { method: 'POST', body: '{}' });
    await loadMVProject();
    showToast(isRegenerate ? 'Song regenerated' : 'Song generated', 'success');
  } catch (e) {
    await loadMVProject();
    showToast('Song generation failed: ' + e.message, 'error', 8000);
  }
}

// ==========================================
// Drama 3 Voice Workbench (Database-Backed voice_jobs + voice_profiles)
// ==========================================
function voiceJobStorageKey() {
  if (state.voiceWorkbenchScope === 'unassigned') return 'studio:selectedVoiceJob:unassigned';
  return `studio:selectedVoiceJob:${state.projectId || '0'}`;
}

function getSavedVoiceJobId() {
  try {
    return localStorage.getItem(voiceJobStorageKey()) || '';
  } catch {
    return '';
  }
}

function saveSelectedVoiceJobId(jobId) {
  try {
    if (jobId) localStorage.setItem(voiceJobStorageKey(), String(jobId));
    else localStorage.removeItem(voiceJobStorageKey());
  } catch {}
}

function getDrama3Items() {
  return Array.isArray(state.voiceJobs) ? state.voiceJobs : [];
}

function findVoiceJobById(jobId) {
  const target = Number(jobId);
  return (state.voiceJobs || []).find((x) => Number(x.id) === target)
    || (state.unassignedVoiceJobs || []).find((x) => Number(x.id) === target)
    || null;
}

function replaceVoiceJobInState(updatedJob) {
  if (!updatedJob || !updatedJob.id) return;
  const jid = Number(updatedJob.id);
  state.voiceJobs = (state.voiceJobs || []).filter((x) => Number(x.id) !== jid);
  state.unassignedVoiceJobs = (state.unassignedVoiceJobs || []).filter((x) => Number(x.id) !== jid);
  if (updatedJob.project_id === null || updatedJob.project_id === undefined) {
    state.unassignedVoiceJobs.push(updatedJob);
  } else if (Number(updatedJob.project_id) === Number(state.projectId)) {
    state.voiceJobs.push(updatedJob);
    state.voiceJobs.sort((a, b) => (Number(a.sort_order || 0) - Number(b.sort_order || 0)) || (Number(a.id) - Number(b.id)));
  }
}

function getSelectedDrama3Item() {
  const items = getDrama3Items();
  if (!items.length) return null;
  const targetId = String(state.selectedDramaItemId || getSavedVoiceJobId() || '');
  return items.find((x) => String(x.id) === targetId) || items[0];
}

function captureDrama3FormPayload() {
  return {
    title: $('dramaTitleInput')?.value.trim() || 'Voice-over task',
    script_text: $('dramaScriptText')?.value || '',
    params: {
      model_name: $('dramaModelName')?.value || 'drama3_w6a8experts_bf16.safetensors',
      experts: $('dramaExpertsMode')?.value || 'in RAM',
      asr_language: $('dramaAsrLanguage')?.value || 'auto',
      temperature: Number($('dramaTemperature')?.value || 1.0),
      top_p: Number($('dramaTopP')?.value || 0.9),
      max_frames: Number($('dramaMaxFrames')?.value || 800),
      seed: Number($('dramaSeed')?.value || 42),
      speakers_mode: $('dramaSpeakersMode')?.value || 'one take per line',
      split_long_text: Boolean($('dramaSplitLongText')?.checked),
      shorten_long_pauses: Boolean($('dramaShortenPauses')?.checked),
    },
  };
}

function markDramaEditorDirty() {
  state.dramaEditorDirty = true;
  const item = getSelectedDrama3Item();
  if (item) {
    if ($('dramaTitleInput')) item.title = $('dramaTitleInput').value.trim() || item.title;
    if ($('dramaScriptText')) item.script_text = $('dramaScriptText').value;
    if (!item.params) item.params = {};
    if ($('dramaSeed')) item.params.seed = Number($('dramaSeed').value || 42);
    const row = document.querySelector(`#dramaAudioList .mv-audio-table-row[data-item-id="${CSS.escape(String(item.id))}"]`);
    if (row) {
      const titleEl = row.querySelector('[data-drama-row-title]');
      const scriptEl = row.querySelector('[data-drama-row-script]');
      const scriptPreview = String(item.script_text || '').replace(/\s+/g, ' ').trim();
      if (titleEl) titleEl.textContent = item.title || 'Voice-over task';
      if (scriptEl) scriptEl.textContent = scriptPreview || '(no voice-over lines)';
    }
    const items = getDrama3Items();
    const idx = items.findIndex((x) => String(x.id) === String(item.id));
    if ($('dramaInspectorTaskBadge') && idx >= 0) {
      $('dramaInspectorTaskBadge').textContent = `#${String(idx + 1).padStart(3, '0')} · ${item.title || 'Single-speaker voice clone'}`;
    }
    const status = String(item.status || 'PENDING').toUpperCase();
    if (!['QUEUED', 'PREPARING', 'RUNNING'].includes(status)) {
      clearTimeout(state.dramaSaveTimer);
      const targetId = item.id;
      state.dramaSaveTimer = setTimeout(() => {
        if (state.dramaEditorDirty && String(state.selectedDramaItemId) === String(targetId)) {
          void saveDrama3Config({ silent: true, itemId: targetId });
        }
      }, 650);
    }
  }
}

function populateDrama3Form(item) {
  const src = item || {};
  const params = src.params || src || {};
  if ($('dramaTitleInput')) $('dramaTitleInput').value = src.title || '';
  if ($('dramaScriptText')) $('dramaScriptText').value = src.script_text || '';
  if ($('dramaModelName')) $('dramaModelName').value = params.model_name || 'drama3_w6a8experts_bf16.safetensors';
  if ($('dramaExpertsMode')) $('dramaExpertsMode').value = params.experts || 'in RAM';
  if ($('dramaAsrLanguage')) $('dramaAsrLanguage').value = params.asr_language || 'auto';
  if ($('dramaTemperature')) $('dramaTemperature').value = Number(params.temperature ?? 1.0);
  if ($('dramaTopP')) $('dramaTopP').value = Number(params.top_p ?? 0.9);
  if ($('dramaMaxFrames')) $('dramaMaxFrames').value = Number(params.max_frames ?? 800);
  if ($('dramaSeed')) $('dramaSeed').value = Number(params.seed ?? 42);
  if ($('dramaSpeakersMode')) $('dramaSpeakersMode').value = params.speakers_mode || 'one take per line';
  if ($('dramaSplitLongText')) $('dramaSplitLongText').checked = Boolean(params.split_long_text ?? true);
  if ($('dramaShortenPauses')) $('dramaShortenPauses').checked = Boolean(params.shorten_long_pauses ?? true);
  state.dramaFormItemId = item ? String(item.id) : '';
  state.dramaEditorDirty = false;
  state.dramaFormInitialized = true;
}

function insertDramaTagAtCursor(tagText) {
  const ta = $('dramaScriptText');
  if (!ta) return;
  const start = ta.selectionStart ?? ta.value.length;
  const end = ta.selectionEnd ?? ta.value.length;
  const before = ta.value.slice(0, start);
  const after = ta.value.slice(end);
  ta.value = before + tagText + after;
  ta.focus();
  const nextPos = start + tagText.length;
  ta.setSelectionRange(nextPos, nextPos);
  markDramaEditorDirty();
}

function updateDramaScopeTabsUI() {}

async function loadDrama3State({ preserveForm = false } = {}) {
  try {
    const projectId = state.projectId;
    const [projectJobs, voices, presetsRes] = await Promise.all([
      projectId ? api(`/api/projects/${projectId}/voice-jobs`) : Promise.resolve([]),
      api('/api/voices'),
      Object.keys(state.voicePresets || {}).length
        ? Promise.resolve({ presets: state.voicePresets })
        : api('/api/voice-presets').catch(() => ({ presets: {} })),
    ]);
    if (projectId && Number(state.projectId) !== Number(projectId)) return;
    state.voiceJobs = Array.isArray(projectJobs) ? projectJobs : [];
    state.voiceProfiles = Array.isArray(voices) ? voices : [];
    if (presetsRes && presetsRes.presets) state.voicePresets = presetsRes.presets;

    if (projectId) {
      try {
        state.mvProject = await api(`/api/projects/${projectId}/mv`);
        state.mvTimeline = await api(`/api/projects/${projectId}/audio/timeline`);
      } catch {}
    }

    const savedId = getSavedVoiceJobId();
    const items = getDrama3Items();
    if (!state.selectedDramaItemId || !items.some((x) => String(x.id) === String(state.selectedDramaItemId))) {
      const matched = items.find((x) => String(x.id) === String(savedId)) || items[0];
      state.selectedDramaItemId = matched ? String(matched.id) : '';
    }
    renderDrama3Workbench({ preserveForm });
  } catch (e) {
    showToast(`Loading the voice-over workbench failed: ${e.message}`, 'error', 6000);
  }
}

function renderDrama3StatusOnly() {
  const items = getDrama3Items();
  const allItems = [...(state.voiceJobs || []), ...(state.unassignedVoiceJobs || [])];
  const activeItems = allItems.filter((x) => ['QUEUED', 'PREPARING', 'RUNNING'].includes(String(x.status || '').toUpperCase()));
  const selectedItem = getSelectedDrama3Item();
  const selIndex = selectedItem ? items.findIndex((x) => String(x.id) === String(selectedItem.id)) : -1;

  updateDramaScopeTabsUI();
  if ($('dramaQueueSummary')) {
    const activeInView = items.filter((x) => ['QUEUED', 'PREPARING', 'RUNNING'].includes(String(x.status || '').toUpperCase())).length;
    $('dramaQueueSummary').textContent = `${activeInView} in progress · ${items.length} tasks`;
  }
  if ($('dramaSummary')) {
    const runningItem = activeItems.find((x) => String(x.status || '').toUpperCase() === 'RUNNING') || activeItems[0];
    if (runningItem) {
      const rPct = Math.max(0, Math.min(100, Math.round(Number(runningItem.progress || 0))));
      const rNode = runningItem.current_node || 'Generating the voice-over…';
      $('dramaSummary').textContent = `Fish Audio Drama 3 · generating ${rPct}% · ${rNode}`;
    } else {
      $('dramaSummary').textContent = `Fish Audio Drama 3 · single-speaker voice cloning and emotional voice-over`;
    }
  }

  if ($('dramaInspectorTaskBadge')) {
    $('dramaInspectorTaskBadge').textContent = selectedItem
      ? `#${String(selIndex + 1).padStart(3, '0')} · ${selectedItem.title || 'Single-speaker voice clone'}`
      : 'Add or select a voice-over task first';
  }

  const selStatus = String(selectedItem?.status || 'PENDING').toUpperCase();
  const selBusy = ['QUEUED', 'PREPARING', 'RUNNING'].includes(selStatus);
  const selHasAudio = Boolean(selectedItem?.has_audio && selectedItem?.audio_url);
  const selPct = Math.max(0, Math.min(100, Math.round(Number(selectedItem?.progress || 0))));
  if ($('dramaInspectorStatus')) {
    const uiStatus = selStatus === 'RUNNING' ? 'RUNNING'
      : (selStatus === 'QUEUED' ? 'QUEUED'
        : (selStatus === 'FAILED' ? 'FAILED'
          : (selStatus === 'CANCELLED' ? 'CANCELLED'
            : (selHasAudio || selStatus === 'DONE' ? 'DONE' : 'PENDING'))));
    $('dramaInspectorStatus').className = `badge-status ${statusClass(uiStatus)}`;
    $('dramaInspectorStatus').textContent = selStatus === 'RUNNING'
      ? `Generating ${selPct}%`
      : fmtStatus(uiStatus);
  }

  if ($('dramaSaveBtn')) {
    $('dramaSaveBtn').disabled = !selectedItem || selBusy;
  }
  if ($('dramaInspectorGenerateBtn')) {
    $('dramaInspectorGenerateBtn').disabled = !selectedItem;
    $('dramaInspectorGenerateBtn').textContent = (selBusy || selHasAudio) ? '🔄 Regenerate voice-over' : '🎙️ Generate voice-over';
  }
  if ($('dramaInspectorApplyBtn')) {
    const isApplied = Boolean(selectedItem?.is_project_audio && selHasAudio);
    $('dramaInspectorApplyBtn').disabled = !selectedItem || !selHasAudio || selBusy || isApplied;
    $('dramaInspectorApplyBtn').textContent = isApplied ? '✓ Applied' : 'Apply';
    $('dramaInspectorApplyBtn').style.background = isApplied ? 'rgba(34,197,94,0.18)' : '';
    $('dramaInspectorApplyBtn').style.borderColor = isApplied ? 'rgba(34,197,94,0.45)' : '';
    $('dramaInspectorApplyBtn').style.color = isApplied ? '#4ade80' : '';
  }

  items.forEach((item) => {
    const row = document.querySelector(`#dramaAudioList .mv-audio-table-row[data-item-id="${CSS.escape(String(item.id))}"]`);
    if (!row) return;
    const st = String(item.status || 'PENDING').toUpperCase();
    const isRowRunning = st === 'RUNNING' || st === 'PREPARING';
    const isRowQueued = st === 'QUEUED';
    const isRowFailed = st === 'FAILED';
    const hasAudio = Boolean(item.has_audio && item.audio_url);
    const pct = Math.max(0, Math.min(100, Math.round(Number(item.progress ?? (hasAudio ? 100 : 0)))));
    const scriptPreview = String(item.script_text || '').replace(/\s+/g, ' ').trim();
    const nodeText = isRowFailed
      ? `Failed: ${String(item.error || 'unknown error').replace(/\s+/g, ' ').trim()}`
      : (isRowRunning
        ? (item.current_node || 'Synthesizing the voice-over…')
        : (isRowQueued
          ? (item.current_node || 'Waiting in queue')
          : (hasAudio ? 'Generation complete · 44.1kHz' : (scriptPreview ? 'Parameters ready, not generated' : 'Waiting for lines'))));
    const barEl = row.querySelector('[data-drama-bar]');
    const pctEl = row.querySelector('[data-drama-pct]');
    const nodeEl = row.querySelector('[data-drama-node]');
    if (barEl) barEl.style.width = `${pct}%`;
    if (pctEl) pctEl.textContent = `${pct}%`;
    if (nodeEl) {
      nodeEl.textContent = nodeText;
      nodeEl.title = nodeText;
    }
  });
}

function syncDramaInspectorPlayer(item, { autoPlay = false } = {}) {
  const player = $('dramaAudioPlayer');
  const meta = $('dramaAudioMeta');
  const dlBtn = $('dramaInspectorDownloadBtn');
  if (!player || !meta) return;

  if (!item || !item.audio_url) {
    player.pause();
    player.removeAttribute('src');
    player.classList.add('hidden');
    player.dataset.itemId = '';
    if (dlBtn) dlBtn.classList.add('hidden');
    meta.textContent = item
      ? 'This queue row has no voice-over yet. Fill in the lines below, then click "🎙️ Generate voice-over"'
      : 'Pick a task from the list on the left, or click "＋ Add to queue" to create one';
    return;
  }

  const dur = Number(item.duration || 0);
  const seed = item.params?.seed ?? item.seed ?? 42;
  meta.textContent = `${item.title || 'Drama 3 voice-over'} · ${dur > 0 ? dur.toFixed(1) + 's' : '44.1kHz'} · Seed ${seed}`;
  if (dlBtn) {
    dlBtn.href = item.download_url || item.audio_url;
    dlBtn.classList.remove('hidden');
  }
  player.classList.remove('hidden');

  const cacheKey = `${item.id}:${item.duration || 0}:${item.audio_url}`;
  if (player.dataset.cacheKey !== cacheKey) {
    player.dataset.itemId = String(item.id);
    player.dataset.cacheKey = cacheKey;
    player.src = `${item.audio_url}?t=${Date.now()}`;
    player.load();
  }
  if (autoPlay) {
    player.currentTime = 0;
    player.play().catch((err) => {
      showToast(`The browser blocked autoplay. Click play in the player on the right: ${err.message}`, 'warning');
    });
  }
}

function updateDramaPlayButtonsUI() {
  const player = $('dramaAudioPlayer');
  const activeId = player && !player.paused ? String(player.dataset.itemId || '') : '';
  document.querySelectorAll('#dramaAudioList .mv-audio-table-row[data-item-id]').forEach((row) => {
    const rowId = String(row.dataset.itemId || '');
    row.classList.toggle('selected', rowId === String(state.selectedDramaItemId || ''));
    const btn = row.querySelector('.drama-play-btn');
    if (btn && !btn.disabled) {
      const isPlayingThis = rowId && rowId === activeId;
      btn.textContent = isPlayingThis ? '⏸ Pause' : '▶ Preview';
      btn.className = `btn ${isPlayingThis ? 'btn-secondary' : 'btn-ghost'} btn-xs drama-play-btn`;
    }
  });
}

async function selectDrama3Item(itemId, { autoPlay = false } = {}) {
  const items = getDrama3Items();
  const nextItem = items.find((x) => String(x.id) === String(itemId));
  if (!nextItem) return;

  const prevId = String(state.selectedDramaItemId || '');
  const changed = prevId !== String(nextItem.id);
  if (changed && prevId && state.dramaEditorDirty) {
    clearTimeout(state.dramaSaveTimer);
    await saveDrama3Config({ silent: true, itemId: prevId });
  }

  state.selectedDramaItemId = String(nextItem.id);
  saveSelectedVoiceJobId(nextItem.id);
  if (changed || state.dramaFormItemId !== String(nextItem.id)) {
    populateDrama3Form(nextItem);
  }
  renderDrama3VoiceCards(nextItem.speakers || []);
  syncDramaInspectorPlayer(nextItem, { autoPlay });
  renderDrama3StatusOnly();
  updateDramaPlayButtonsUI();
}

function renderDrama3Workbench({ preserveForm = false } = {}) {
  const items = getDrama3Items();
  if (items.length) {
    const found = items.find((x) => String(x.id) === String(state.selectedDramaItemId || getSavedVoiceJobId() || ''));
    const activeItem = found || items[0];
    const switchedItem = String(state.dramaFormItemId || '') !== String(activeItem.id);
    state.selectedDramaItemId = String(activeItem.id);
    saveSelectedVoiceJobId(activeItem.id);
    if (!preserveForm || !state.dramaFormInitialized || switchedItem) {
      populateDrama3Form(activeItem);
    }
    syncDramaInspectorPlayer(activeItem, { autoPlay: false });
    renderDrama3VoiceCards(activeItem.speakers || []);
  } else {
    state.selectedDramaItemId = '';
    if (!preserveForm || !state.dramaFormInitialized) {
      populateDrama3Form(null);
    }
    syncDramaInspectorPlayer(null);
    renderDrama3VoiceCards([]);
  }

  renderDrama3HistoryList(items);
  renderDrama3StatusOnly();
}

function insertDramaSpeakerTag(speakerIndex) {
  const ta = $('dramaScriptText');
  if (!ta) return;
  const tag = `<|speaker:${speakerIndex}|> `;
  const start = ta.selectionStart ?? ta.value.length;
  const before = ta.value.slice(0, start);
  const needsNewline = before.length > 0 && !before.endsWith('\n');
  insertDramaTagAtCursor((needsNewline ? '\n' : '') + tag);
}

async function addDrama3VoiceSlot() {
  const selectedItem = getSelectedDrama3Item();
  if (!selectedItem) {
    return showToast('Create or select a voice-over task with "＋ Add to queue" first', 'warning');
  }
  try {
    if (state.dramaEditorDirty) {
      await saveDrama3Config({ silent: true, itemId: selectedItem.id });
    }
    const speakers = Array.isArray(selectedItem.speakers) ? selectedItem.speakers : [];
    const maxIdx = speakers.reduce((m, s) => Math.max(m, Number(s.speaker_index ?? 0)), -1);
    const nextIdx = maxIdx + 1;
    if (nextIdx > 15) {
      return showToast('Up to 16 speaker roles (0~15)', 'warning');
    }
    const updated = await api(`/api/voice-jobs/${selectedItem.id}/speakers/${nextIdx}`, {
      method: 'PUT',
      body: JSON.stringify({
        name: `Role ${nextIdx}`,
        voice_profile_id: null,
      }),
    });
    replaceVoiceJobInState(updated);
    renderDrama3Workbench({ preserveForm: true });
    showToast(`Added role ${nextIdx} (<|speaker:${nextIdx}|>)`, 'success');
  } catch (err) {
    showToast(`Adding a speaker failed: ${err.message}`, 'error');
  }
}

function renderDrama3VoiceCards(speakers) {
  const grid = $('dramaVoiceCards');
  if (!grid) return;
  grid.innerHTML = '';
  const selectedItem = getSelectedDrama3Item();
  const jobId = selectedItem?.id || null;
  const isBusy = selectedItem && ['QUEUED', 'PREPARING', 'RUNNING'].includes(String(selectedItem.status || '').toUpperCase());
  const list = Array.isArray(speakers) && speakers.length
    ? speakers
    : [{ speaker_index: 0, name: 'Role 0 (lead)', voice_profile_id: null, voice_name: '' }];
  const profiles = Array.isArray(state.voiceProfiles) ? state.voiceProfiles : [];

  list.forEach((v, idx) => {
    const sidx = Number(v.speaker_index ?? idx);
    const card = document.createElement('div');
    card.className = 'drama-voice-card';
    card.dataset.speakerIndex = String(sidx);
    const vpid = v.voice_profile_id ? Number(v.voice_profile_id) : null;
    const matchedProfile = vpid ? profiles.find((p) => Number(p.id) === vpid) : null;
    const defaultRoleName = sidx === 0 ? 'Role 0 (lead)' : `Role ${sidx}`;
    const sourceBadge = matchedProfile
      ? `Voice library: ${escapeHtml(matchedProfile.name)}${matchedProfile.duration ? ` (${Number(matchedProfile.duration).toFixed(1)}s)` : ''}`
      : 'No reference vocal bound · this role is synthesized without reference from the Seed';

    const profileOptions = [
      `<option value="">✨ No reference (random voice from Seed)</option>`,
      ...profiles.map((p) => {
        const durText = Number(p.duration || 0) > 0 ? ` (${Number(p.duration).toFixed(1)}s)` : '';
        const selectedAttr = vpid === Number(p.id) ? 'selected' : '';
        return `<option value="${p.id}" ${selectedAttr}>🎯 ${escapeHtml(p.name)}${durText}</option>`;
      }),
    ].join('');

    card.innerHTML = `
      <div class="drama-voice-top" style="flex-wrap:wrap;gap:6px">
        <span class="drama-speaker-badge" title="In the script, use <|speaker:${sidx}|> marks this speaker’s lines">&lt;|speaker:${sidx}|&gt;</span>
        <input class="form-input drama-voice-name-input" style="width:110px;height:28px;padding:2px 7px;font-size:11.5px;font-weight:600" value="${escapeHtml(v.name || defaultRoleName)}" placeholder="${defaultRoleName}" title="Speaker label" ${!jobId || isBusy ? 'disabled' : ''} />
        <select class="form-select drama-voice-profile-select" title="Pick a reference vocal from the global voice library" ${!jobId || isBusy ? 'disabled' : ''}>
          ${profileOptions}
        </select>
        <label class="btn btn-secondary btn-xs" style="cursor:pointer;margin:0" title="Upload audio (wav/mp3/flac/m4a) or video (mp4/mov/mkv/webm) to the global voice library and bind it to this role">
          <span>📁 Upload new voice</span>
          <input type="file" accept="audio/*,video/*,.mp4,.mov,.mkv,.webm,.wav,.mp3,.flac,.m4a" class="hidden drama-voice-file-input" ${ isBusy ? 'disabled' : '' } />
        </label>
        <button type="button" class="btn btn-ghost btn-xs drama-insert-speaker-btn" title="Insert at cursor in the script below <|speaker:${sidx}|>">+ Insert tag</button>
        ${list.length > 1 && sidx > 0 ? `<button type="button" class="btn btn-danger-outline btn-xs drama-remove-voice-btn" title="Delete this speaker slot" ${!jobId || isBusy ? 'disabled' : ''}>Delete role</button>` : ''}
      </div>
      <div class="drama-voice-meta" title="${sourceBadge}">${matchedProfile ? '🎯 cloneReady · ' : '✨ no reference · '}${sourceBadge}</div>
      ${matchedProfile?.audio_url ? `<audio class="drama-voice-audio" controls preload="metadata" src="${matchedProfile.audio_url}?t=${ matchedProfile.id }"></audio>` : ''}
    `;

    const nameInput = card.querySelector('.drama-voice-name-input');
    const profileSelect = card.querySelector('.drama-voice-profile-select');

    const saveSpeakerBinding = async (nextProfileId) => {
      if (!jobId) return;
      try {
        const updated = await api(`/api/voice-jobs/${jobId}/speakers/${sidx}`, {
          method: 'PUT',
          body: JSON.stringify({
            name: nameInput?.value.trim() || defaultRoleName,
            voice_profile_id: nextProfileId ? Number(nextProfileId) : null,
          }),
        });
        replaceVoiceJobInState(updated);
        renderDrama3Workbench({ preserveForm: true });
      } catch (err) {
        showToast(`Updating the speaker voice failed: ${err.message}`, 'error');
      }
    };

    nameInput?.addEventListener('change', () => {
      void saveSpeakerBinding(profileSelect?.value ? Number(profileSelect.value) : null);
    });

    profileSelect?.addEventListener('change', () => {
      const chosenId = profileSelect.value ? Number(profileSelect.value) : null;
      void saveSpeakerBinding(chosenId);
    });

    card.querySelector('.drama-insert-speaker-btn')?.addEventListener('click', () => {
      insertDramaSpeakerTag(sidx);
    });

    card.querySelector('.drama-remove-voice-btn')?.addEventListener('click', async () => {
      if (!jobId) return;
      try {
        const updated = await api(`/api/voice-jobs/${jobId}/speakers/${sidx}`, { method: 'DELETE' });
        replaceVoiceJobInState(updated);
        renderDrama3Workbench({ preserveForm: true });
        showToast(`Speaker slot deleted <|speaker:${sidx}|>`, 'info');
      } catch (err) {
        showToast(`Deleting the speaker failed: ${err.message}`, 'error');
      }
    });

    card.querySelector('.drama-voice-file-input')?.addEventListener('change', async (e) => {
      const file = e.target.files?.[0];
      e.target.value = '';
      if (!file) return;
      const uploadTask = (async () => {
        try {
          const fd = new FormData();
          fd.append('file', file);
          fd.append('name', file.name.replace(/\.[^.]+$/, ''));
          showToast(`Uploading and extracting a clean reference vocal (Demucs strips BGM + VAD trims the tail)…`, 'info', 4000);
          const createdProfile = await api('/api/voices', { method: 'POST', body: fd });
          state.voiceProfiles = await api('/api/voices');
          if (jobId) {
            const updatedJob = await api(`/api/voice-jobs/${jobId}/speakers/${sidx}`, {
              method: 'PUT',
              body: JSON.stringify({
                name: nameInput?.value.trim() || defaultRoleName,
                voice_profile_id: createdProfile.id,
              }),
            });
            replaceVoiceJobInState(updatedJob);
          }
          renderDrama3Workbench({ preserveForm: true });
          showToast(`Voice "${createdProfile.name}" saved and bound to <|speaker:${sidx}|>`, 'success');
        } catch (err) {
          showToast(`Uploading the reference voice failed: ${err.message}`, 'error');
        } finally {
          if (state.dramaVoiceUploadPromise === uploadTask) {
            state.dramaVoiceUploadPromise = null;
          }
        }
      })();
      state.dramaVoiceUploadPromise = uploadTask;
      await uploadTask;
    });

    grid.appendChild(card);
  });
}

async function applyVoiceJobToProject(itemId) {
  const items = getDrama3Items();
  const target = itemId
    ? items.find((x) => String(x.id) === String(itemId))
    : getSelectedDrama3Item();
  if (!target) return showToast('Select a voice-over task first', 'warning');
  if (!target.has_audio) return showToast('Wait for this voice-over task to finish before applying', 'warning');
  try {
    await api(`/api/voice-jobs/${target.id}/use-as-project-audio`, {
      method: 'POST',
      body: JSON.stringify({ confirm_replace: true }),
    });
    await loadMVProject();
    await loadDrama3State({ preserveForm: true });
    showToast(`Applied "${target.title || 'current voice-over'}" as this project's audio!`, 'success');
  } catch (err) {
    showToast(`Applying the voice-over failed: ${err.message}`, 'error');
  }
}

async function analyzeVoiceJobProjectAudio(item) {
  if (!state.projectId) return showToast('Select a project first', 'warning');
  try {
    if (item && item.id && !item.is_project_audio) {
      await api(`/api/voice-jobs/${item.id}/use-as-project-audio`, {
        method: 'POST',
        body: JSON.stringify({ confirm_replace: true }),
      });
      item.is_project_audio = true;
    }
    if (state.mvProject) {
      state.mvProject.analysis_status = 'RUNNING';
      state.mvProject.analysis_progress = 2;
      state.mvProject.analysis_error = '';
    }
    if (state.mvTimeline) {
      state.mvTimeline.analysis_status = 'RUNNING';
      state.mvTimeline.analysis_progress = 2;
      state.mvTimeline.analysis_node = 'Starting audio analysis…';
    }
    renderDrama3HistoryList(getDrama3Items());
    showToast('Separating vocals, aligning lines word by word and analyzing pauses…', 'info', 3500);
    await api(`/api/projects/${state.projectId}/audio/analyze`, { method: 'POST', body: '{}' });
    await loadMVProject();
    await loadDrama3State({ preserveForm: true });
    showToast('Voice-over timeline is ready! Go to the Video director and click "Auto shots from audio"', 'success', 5000);
  } catch (e) {
    await loadMVProject();
    await loadDrama3State({ preserveForm: true });
    showToast(`Analyzing the voice-over failed: ${e.message}`, 'error', 8000);
  }
}

function getVoiceJobAnalysisStatus(item) {
  const hasAudio = Boolean(item.has_audio && item.audio_url);
  if (!hasAudio || !item.is_project_audio) return 'IDLE';
  const mv = state.mvProject || {};
  const analysis = state.mvTimeline || {};
  return String(analysis.analysis_status || mv.analysis_status || 'IDLE').toUpperCase();
}

function renderProjectAudioAnalysisBlock(item) {
  const status = getVoiceJobAnalysisStatus(item);
  if (status === 'IDLE') return '';
  const mv = state.mvProject || {};
  const analysis = state.mvTimeline || {};
  const pct = status === 'DONE'
    ? 100
    : Math.max(0, Math.min(100, Math.round(Number(analysis.analysis_progress ?? mv.analysis_progress ?? 0))));
  const node = status === 'FAILED'
    ? `Analysis failed: ${analysis.analysis_error || mv.analysis_error || ''}`
    : (status === 'RUNNING' ? (analysis.analysis_node || 'Analyzing audio and aligning lines…') : '');
  const lines = Array.isArray(analysis.timeline?.lines) ? analysis.timeline.lines : [];
  if (status === 'DONE' && lines.length) {
    const chips = lines.map((ln, idx) => {
      const ratio = Number(ln.aligned_ratio ?? 1.0);
      const isOk = Math.abs(ratio - 1.0) < 1e-6;
      const lineNo = Number(ln.index || (idx + 1));
      const pctText = `${Math.round(ratio * 100)}%`;
      const textPreview = String(ln.text || '').trim();
      return `<span class="drama-align-chip ${isOk ? 'ok' : 'bad'}" title="Line ${lineNo} row (${pctText}): ${escapeHtml(textPreview)}">L${lineNo} ${pctText}: ${escapeHtml(textPreview)}</span>`;
    }).join('');
    return `<div class="drama-project-audio-analysis" data-project-audio-analysis><div class="drama-align-lines">${chips}</div></div>`;
  }
  if (status === 'RUNNING' || status === 'FAILED') {
    return `
      <div class="drama-project-audio-analysis" data-project-audio-analysis>
        <div class="drama-analysis-top">
          <div class="drama-analysis-bar-wrap">
            <div class="mini-bar-track image-task-progress-track" style="height:5px"><div class="mini-bar-fill" style="width:${pct}%"></div></div>
            <span class="font-mono" style="font-size:10px;color:#cbd5e1">${pct}%</span>
          </div>
          <span style="font-size:10px;color:${status === 'FAILED' ? '#f87171' : '#94a3b8'}">${escapeHtml(node)}</span>
        </div>
      </div>
    `;
  }
  return '';
}

function renderDrama3HistoryList(items) {
  const list = $('dramaAudioList');
  if (!list) return;

  if (!items.length) {
    list.innerHTML = `
      <div class="image-queue-empty">
        <div class="image-queue-empty-icon">🎙️</div>
        <div class="image-queue-empty-title">This project has no voice-over tasks yet</div>
        <div class="image-queue-empty-sub">Click "＋ Add to queue" above to create a voice-over task, set the reference voice and lines on the right, then generate.</div>
      </div>`;
    return;
  }

  list.innerHTML = '';

  items.forEach((item, index) => {
    const row = document.createElement('article');
    const isSelected = String(item.id) === String(state.selectedDramaItemId || '');
    const status = String(item.status || 'PENDING').toUpperCase();
    const isRunning = status === 'RUNNING' || status === 'PREPARING';
    const isQueued = status === 'QUEUED';
    const isFailed = status === 'FAILED';
    const isCancelled = status === 'CANCELLED';
    const hasAudio = Boolean(item.has_audio && item.audio_url);
    const uiStatus = isRunning ? 'RUNNING'
      : (isQueued ? 'QUEUED'
        : (isFailed ? 'FAILED'
          : (isCancelled ? 'CANCELLED'
            : (hasAudio || status === 'DONE' ? 'DONE' : 'PENDING'))));
    const statusText = fmtStatus(uiStatus);
    const pct = Math.max(0, Math.min(100, Math.round(Number(item.progress ?? (hasAudio ? 100 : 0)))));
    const scriptPreview = String(item.script_text || '').replace(/\s+/g, ' ').trim();
    const duration = Number(item.duration || 0);
    const elapsed = Number(item.elapsed_seconds || 0);
    const speakerList = Array.isArray(item.speakers) ? item.speakers : [];
    const refCount = speakerList.filter((v) => v?.voice_profile_id).length;
    const speakerLabel = speakerList.length > 1 ? `${speakerList.length}-speaker dialogue` : 'Single-speaker voice-over';
    const seed = item.params?.seed ?? item.seed ?? 42;
    const analysisStatus = getVoiceJobAnalysisStatus(item);
    const analysisLabel = analysisStatus === 'DONE'
      ? 'Analyzed'
      : (analysisStatus === 'RUNNING' ? 'Analyzing' : (analysisStatus === 'FAILED' ? 'Analysis failed' : 'Not analyzed'));
    const nodeText = isFailed
      ? `Failed: ${String(item.error || 'unknown error').replace(/\s+/g, ' ').trim()}`
      : (isRunning
        ? (item.current_node || 'Synthesizing the voice-over…')
        : (isQueued
          ? (item.current_node || 'Waiting in queue')
          : (hasAudio ? 'Generation complete · 44.1kHz' : (scriptPreview ? 'Parameters ready, not generated' : 'Waiting for lines'))));
    const metaText = hasAudio
      ? `Actual ${duration > 0 ? duration.toFixed(1) + 's' : '--'} · ${speakerLabel} · Seed ${seed} · ${analysisLabel}`
      : `${speakerLabel} · ${refCount > 0 ? `${refCount} voices selected` : 'no reference'} · Seed ${seed}`;
    const elapsedText = isRunning ? 'Running' : (elapsed > 0 ? `${elapsed.toFixed(1)}s` : '—');

    row.className = `image-queue-task image-queue-table-row mv-audio-table-row ${isRunning ? 'current' : ''} ${isSelected ? 'selected' : ''}`;
    row.dataset.itemId = String(item.id);
    row.innerHTML = `
      <div class="image-queue-col task-no">
        <strong>#${String(index + 1).padStart(3, '0')}</strong>
        ${isRunning ? '<span class="queue-current-chip">Current</span>' : ''}
      </div>
      <div class="image-queue-col scene-name" title="${escapeHtml(scriptPreview || item.title || '')}">
        <div data-drama-row-title style="font-weight:700;color:#f8fafc;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:flex;align-items:center;gap:6px">
          ${item.is_project_audio && hasAudio ? '<span style="font-size:10px;padding:1px 6px;border-radius:4px;background:rgba(34,197,94,0.18);border:1px solid rgba(34,197,94,0.45);color:#4ade80;flex-shrink:0">✓ Applied</span>' : ''}
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(item.title || `Voice-over task #${String(index + 1).padStart(3, '0')}`)}</span>
        </div>
        <div data-drama-row-script style="font-size:11px;color:#94a3b8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(scriptPreview || '(no voice-over lines)')}</div>
        ${renderProjectAudioAnalysisBlock(item)}
      </div>
      <div class="image-queue-col">
        <span class="badge-status ${statusClass(uiStatus)}">${statusText}</span>
      </div>
      <div class="image-queue-col image-queue-progress-cell">
        <div class="image-queue-task-progress-row">
          <div class="mini-bar-track image-task-progress-track"><div data-drama-bar class="mini-bar-fill" style="width:${pct}%"></div></div>
          <span data-drama-pct class="font-mono image-task-progress-pct">${pct}%</span>
        </div>
        <div data-drama-node class="image-queue-task-node" title="${escapeHtml(nodeText)}">${escapeHtml(nodeText)}</div>
        <div class="mv-audio-row-meta" title="${escapeHtml(metaText)}">${escapeHtml(metaText)}</div>
      </div>
      <div class="image-queue-col font-mono" data-drama-elapsed>${elapsedText}</div>
      <div class="image-queue-col image-queue-task-actions mv-audio-row-actions">
        ${(isRunning || isQueued)
          ? `<button class="btn btn-danger-outline btn-xs drama-row-cancel-btn">Cancel</button>`
          : `<button class="btn ${hasAudio ? 'btn-ghost' : 'btn-primary'} btn-xs drama-row-gen-btn">${hasAudio ? 'Regenerate' : 'Generate'}</button>`
        }
        <button class="btn btn-ghost btn-xs drama-play-btn" ${hasAudio ? '' : 'disabled'} title="${hasAudio ? 'Play / pause preview' : 'Generation complete available for preview'}">▶ Preview</button>
        ${hasAudio
          ? `<button type="button" class="btn ${item.is_project_audio ? 'btn-ghost' : 'btn-secondary'} btn-xs drama-apply-audio-btn" ${item.is_project_audio ? 'disabled style="color:#4ade80;border-color:rgba(34,197,94,0.4)"' : ''}>${item.is_project_audio ? '✓ Applied' : 'Apply'}</button>`
          : ''
        }
        ${hasAudio
          ? `<button type="button" class="btn btn-secondary btn-xs drama-analyze-audio-btn" ${analysisStatus === 'RUNNING' ? 'disabled' : ''}>${analysisStatus === 'RUNNING' ? 'Analyzing…' : (analysisStatus === 'DONE' ? 'Re-analyze' : 'Analyze')}</button>`
          : ''
        }
        ${hasAudio ? `<a class="btn btn-ghost btn-xs" href="${item.download_url || item.audio_url}" download onclick="event.stopPropagation()">Download</a>` : ''}
        <button class="btn btn-danger-outline btn-xs drama-del-item-btn" ${isRunning ? 'disabled' : ''}>Delete</button>
      </div>
    `;

    row.addEventListener('click', () => {
      void selectDrama3Item(item.id, { autoPlay: false });
    });

    row.querySelector('.drama-analyze-audio-btn')?.addEventListener('click', async (e) => {
      e.stopPropagation();
      await analyzeVoiceJobProjectAudio(item);
    });

    row.querySelector('.drama-apply-audio-btn')?.addEventListener('click', async (e) => {
      e.stopPropagation();
      await applyVoiceJobToProject(item.id);
    });

    row.querySelector('.drama-row-cancel-btn')?.addEventListener('click', async (e) => {
      e.stopPropagation();
      await cancelDrama3Audio(item.id);
    });

    row.querySelector('.drama-row-gen-btn')?.addEventListener('click', async (e) => {
      e.stopPropagation();
      await generateDrama3Audio(item.id);
    });

    row.querySelector('.drama-play-btn')?.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!hasAudio) return;
      const player = $('dramaAudioPlayer');
      const isSameItem = player && String(player.dataset.itemId || '') === String(item.id);
      await selectDrama3Item(item.id, { autoPlay: false });
      if (isSameItem && !player.paused) {
        player.pause();
        updateDramaPlayButtonsUI();
      } else {
        syncDramaInspectorPlayer(item, { autoPlay: true });
        updateDramaPlayButtonsUI();
      }
    });

    row.querySelector('.drama-del-item-btn')?.addEventListener('click', async (e) => {
      e.stopPropagation();
      const ok = await showConfirmModal('Delete voice-over queue row', `Delete <b>#${String(index + 1).padStart(3, '0')} ${escapeHtml(item.title || '')}</b> and its voice-over audio?`);
      if (!ok) return;
      try {
        await api(`/api/voice-jobs/${encodeURIComponent(String(item.id))}`, { method: 'DELETE' });
        state.voiceJobs = (state.voiceJobs || []).filter((x) => Number(x.id) !== Number(item.id));
        state.unassignedVoiceJobs = (state.unassignedVoiceJobs || []).filter((x) => Number(x.id) !== Number(item.id));
        if (String(state.selectedDramaItemId || '') === String(item.id)) {
          const remaining = getDrama3Items();
          state.selectedDramaItemId = remaining[0] ? String(remaining[0].id) : '';
          saveSelectedVoiceJobId(state.selectedDramaItemId);
        }
        renderDrama3Workbench({ preserveForm: false });
        showToast('Voice-over queue task deleted', 'info');
      } catch (err) {
        showToast(`Delete failed: ${err.message}`, 'error');
      }
    });

    list.appendChild(row);
  });

  updateDramaPlayButtonsUI();
}

async function addDrama3Task() {
  if (!state.projectId) {
    return showToast('Select or create a project on the left first', 'warning');
  }
  try {
    if (state.dramaEditorDirty && state.selectedDramaItemId) {
      await saveDrama3Config({ silent: true, itemId: state.selectedDramaItemId });
    }
    const prevSelected = getSelectedDrama3Item();
    state.voiceWorkbenchScope = 'project';
    const created = await api(`/api/projects/${state.projectId}/voice-jobs`, {
      method: 'POST',
      body: JSON.stringify({
        from_job_id: prevSelected ? Number(prevSelected.id) : null,
      }),
    });
    replaceVoiceJobInState(created);
    state.selectedDramaItemId = String(created.id);
    saveSelectedVoiceJobId(created.id);
    updateDramaScopeTabsUI();
    renderDrama3Workbench({ preserveForm: false });
    $('dramaScriptText')?.focus();
    $('dramaInspector')?.scrollTo({ top: 0, behavior: 'smooth' });
    showToast('Voice-over task added. Fill in the lines on the right, then generate', 'success');
  } catch (e) {
    showToast(`Adding the voice-over task failed: ${e.message}`, 'error');
  }
}

async function cancelDrama3Audio(jobId = state.selectedDramaItemId) {
  if (!jobId) return;
  try {
    const updated = await api(`/api/voice-jobs/${jobId}/cancel`, { method: 'POST', body: '{}' });
    replaceVoiceJobInState(updated);
    renderDrama3Workbench({ preserveForm: true });
    showToast('Voice-over task cancelled', 'info');
  } catch (e) {
    showToast(`Cancel failed: ${e.message}`, 'error');
  }
}

async function saveDrama3Config({ silent = false, itemId = state.selectedDramaItemId } = {}) {
  clearTimeout(state.dramaSaveTimer);
  const targetId = itemId || state.selectedDramaItemId;
  if (!targetId) {
    if (!silent) showToast('Create a voice-over task with "＋ Add to queue" first', 'warning');
    return false;
  }
  const targetJob = findVoiceJobById(targetId);
  if (targetJob && ['QUEUED', 'PREPARING', 'RUNNING'].includes(String(targetJob.status || '').toUpperCase())) {
    if (!silent) showToast('This voice-over task is queued or running; its config cannot be changed now', 'warning');
    return false;
  }
  try {
    const payload = captureDrama3FormPayload();
    const updated = await api(`/api/voice-jobs/${targetId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
    replaceVoiceJobInState(updated);
    state.dramaEditorDirty = false;
    renderDrama3Workbench({ preserveForm: true });
    if (!silent) showToast('Voice-over queue row saved', 'success');
    return true;
  } catch (e) {
    if (!silent) showToast(`Saving voice-over config failed: ${e.message}`, 'error');
    return false;
  }
}

async function generateDrama3Audio(targetItemId = null) {
  const resolvedId = (typeof targetItemId === 'string' || typeof targetItemId === 'number')
    ? String(targetItemId)
    : String(state.selectedDramaItemId || '');
  if (!resolvedId) {
    return showToast('Add or select a voice-over task first', 'warning');
  }
  return withSubmissionGuard(`drama3:generate:${resolvedId}`, async () => {
    if (state.dramaVoiceUploadPromise) {
      showToast('Waiting for the reference vocal to upload and extract, then generation starts automatically…', 'info', 3000);
      await state.dramaVoiceUploadPromise;
    }
    if (resolvedId !== String(state.selectedDramaItemId || '')) {
      await selectDrama3Item(resolvedId, { autoPlay: false });
    }
    const script = $('dramaScriptText')?.value.trim() || '';
    if (!script) {
      $('dramaScriptText')?.focus();
      return showToast('Enter the script to voice on the right first', 'warning');
    }
    const targetItem = findVoiceJobById(resolvedId);
    const isBusy = targetItem && ['QUEUED', 'PREPARING', 'RUNNING'].includes(String(targetItem.status || '').toUpperCase());
    if (!isBusy) {
      const saved = await saveDrama3Config({ silent: true, itemId: resolvedId });
      if (!saved) return;
    }
    try {
      await api(`/api/voice-jobs/${resolvedId}/generate`, {
        method: 'POST',
        body: '{}',
      });
      await loadDrama3State({ preserveForm: true });
      showToast('Added to the Fish Audio Drama 3 voice-over queue', 'info', 3500);
    } catch (e) {
      await loadDrama3State({ preserveForm: true });
      showToast(`Submitting the voice-over failed: ${e.message}`, 'error', 8000);
    }
  });
}

// ==========================================
// Auto Plan Shots From Audio Timeline (F10–F12)
// ==========================================
function closeAutoPlanShotsModal() {
  $('shotPlanModal')?.classList.add('hidden');
  state.pendingShotPlan = null;
}

async function openAutoPlanShotsModal() {
  if (!state.sceneId) return showToast('Select or create a Scene first', 'warning');
  const sceneId = Number(state.sceneId);
  try {
    const res = await api(`/api/scenes/${sceneId}/shots/from-timeline`, {
      method: 'POST',
      body: JSON.stringify({ dry_run: true, replace: false }),
    });
    state.pendingShotPlan = { sceneId, plan: res };
    const shots = Array.isArray(res.shots) ? res.shots : [];
    const warnings = Array.isArray(res.warnings) ? res.warnings : [];
    const cutCount = shots.filter((s) => s.mode === 'CUT').length;
    const contCount = shots.filter((s) => s.mode === 'CONTINUE').length;

    if ($('shotPlanSummaryBar')) {
      $('shotPlanSummaryBar').textContent = `Planned ${shots.length} shots (${cutCount} CUT · ${contCount} CONTINUE) · total audio length ${Number(res.audio_duration || 0).toFixed(2)} s · CUT shots inherit this Scene’s Ref1 base image`;
    }
    const warnBanner = $('shotPlanWarningsBanner');
    if (warnBanner) {
      warnBanner.classList.toggle('hidden', !warnings.length);
      warnBanner.innerHTML = warnings.map((w) => `<div>⚠️ ${escapeHtml(w)}</div>`).join('');
    }

    const tbody = $('shotPlanRows');
    if (tbody) {
      tbody.innerHTML = shots.map((s) => {
        const rowWarns = Array.isArray(s.warnings) ? [...s.warnings] : [];
        const needChangeAngle = s.mode === 'CUT' && Number(s.shot_no) > 1;
        const hasWarn = rowWarns.length > 0 || needChangeAngle;
        const warnBadges = [
          needChangeAngle ? `<span class="shot-plan-warn-pill">Segment start · change shot size</span>` : '',
          ...rowWarns.map((w) => `<span class="shot-plan-warn-pill">${escapeHtml(w)}</span>`),
        ].filter(Boolean).join(' ');
        return `
          <tr class="${hasWarn ? 'shot-plan-row-warn' : ''}">
            <td class="font-mono" style="font-weight:700">#${String(s.shot_no).padStart(3, '0')}</td>
            <td><span class="mode-badge ${String(s.mode || 'CUT').toLowerCase()}">${escapeHtml(s.mode)}</span></td>
            <td class="font-mono">${Number(s.timeline_start || 0).toFixed(2)}s</td>
            <td class="font-mono">${Number(s.timeline_end || 0).toFixed(2)}s</td>
            <td class="font-mono" style="font-weight:700">${Number(s.seconds || 0).toFixed(2)}s</td>
            <td>${escapeHtml(s.dialogue || '')}</td>
            <td>${escapeHtml(s.title || '')} ${warnBadges}</td>
          </tr>
        `;
      }).join('');
    }

    $('shotPlanModal')?.classList.remove('hidden');
  } catch (err) {
    showToast(`Auto shots from audio could not start: ${err.message}`, 'error', 7000);
  }
}

async function confirmAutoPlanShots(replaceExisting = false) {
  const pending = state.pendingShotPlan;
  const sceneId = Number(pending?.sceneId || state.sceneId || 0);
  if (!sceneId) return;
  const btn = $('confirmShotPlanModalBtn');
  if (btn) btn.disabled = true;
  try {
    const res = await api(`/api/scenes/${sceneId}/shots/from-timeline`, {
      method: 'POST',
      body: JSON.stringify({ dry_run: false, replace: Boolean(replaceExisting) }),
    });
    closeAutoPlanShotsModal();
    const count = Array.isArray(res.shots) ? res.shots.length : 0;
    showToast(`Automatically created from the audio timeline: ${count} shots`, 'success', 4500);
    await loadShots();
  } catch (err) {
    if (err.status === 409 && err.payload?.code === 'SHOTS_EXIST' && !replaceExisting) {
      const existingCount = Number(err.payload.count || state.shots.length || 0);
      const ok = await showConfirmModal(
        'Confirm overwriting the existing shot',
        `This will delete the Scene’s existing <b>${existingCount}</b> shots and recreate them from the audio timeline. Continue?`
      );
      if (ok) {
        await confirmAutoPlanShots(true);
      }
      return;
    }
    showToast(`Creating auto shots failed: ${err.message}`, 'error', 7000);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ==========================================
// Projects & Scenes
// ==========================================
function renderProjects() {
  const list = $('projectList');
  const selected = state.projects.find(p => p.id === state.projectId);
  if ($('activeProjectName')) $('activeProjectName').textContent = selected?.name || 'Select project';
  if ($('activeProjectNumber')) $('activeProjectNumber').textContent = selected ? 'Current project' : 'Click to switch or create a project';
  list.innerHTML = '';

  const scrollTop = list.scrollTop;
  const query = ($('projectSearch')?.value || '').trim().toLocaleLowerCase();
  const visibleProjects = state.projects.filter(p => !query || p.name.toLocaleLowerCase().includes(query) || String(p.id).includes(query));
  if ($('projectCount')) $('projectCount').textContent = `${visibleProjects.length}/${state.projects.length}`;
  visibleProjects.forEach((p) => {
    const item = document.createElement('div');
    item.className = `nav-item ${state.projectId === p.id ? 'active' : ''}`;
    item.dataset.projectId = p.id;
    item.tabIndex = 0;
    item.setAttribute('role', 'button');
    item.setAttribute('aria-label', `Switching project: ${p.name}`);
    item.setAttribute('aria-pressed', String(state.projectId === p.id));
    item.innerHTML = `
      <span class="project-item-content"><span class="nav-label" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</span>${state.projectId === p.id ? '<span class="project-item-meta">Current project</span>' : ''}</span>
      <div class="nav-item-actions">
        <button class="icon-btn danger btn-xs del-btn" title="Delete project">✕</button>
      </div>
    `;

    item.onclick = async (e) => {
      if (e.target.closest('.del-btn')) return;
      $('projectPickerDialog')?.close();
      if (state.projectId === p.id) return;
      shotDrafts.capture();
      state.projectId = p.id;
      try { localStorage.setItem('studio:lastProjectId', String(p.id)); } catch {}
      state.sceneId = null;
      state.imageSceneId = null;
      state.shotId = null;
      state.imageDashboard = null;
      state.imageQueue = { queue_size: 0, current_job_id: null, jobs: [] };
      state.imageSelectSeq++;
      state.imageDashboardSeq++;
      state.imageQueueSeq++;
      state.imageEditorDirty = false;
      state.imageEditorKey = '';
      imageHistoryView.update([], 'Loading current project assets…');
      state.scenes = [];
      $('sceneList').replaceChildren();
      renderImageWorkbench();
      renderImageInspector({ forceForm: true });
      clearEditor();
      renderProjects();
      await loadScenes();
      if (state.workspaceMode === 'image') { await loadImageDashboard(); await refreshImageQueueStatus(); }
      if (state.workspaceMode === 'mv') await loadMVProject();
      if (state.workspaceMode === 'drama') {
        state.selectedDramaItemId = getSavedVoiceJobId();
        await loadDrama3State({ preserveForm: false });
      }
    };

    item.onkeydown = (event) => {
      if (event.target !== item || !['Enter', ' '].includes(event.key)) return;
      event.preventDefault();
      item.click();
    };
    const delBtn = item.querySelector('.del-btn');
    delBtn.onclick = async (e) => {
      e.stopPropagation();
      const ok = await showConfirmModal('Delete project', `Delete project <b>${escapeHtml(p.name)}</b> and all of its Scenes and shots?`);
      if (ok) {
        try {
          await api(`/api/projects/${p.id}`, { method: 'DELETE' });
          showToast('Project deleted', 'info');
          if (state.projectId === p.id) {
            state.projectId = null;
            state.sceneId = null;
            state.imageSceneId = null;
            state.shotId = null;
            state.imageDashboard = null;
          }
          await loadProjects();
          await loadScenes();
          if (state.workspaceMode === 'image') await loadImageDashboard();
          if (state.workspaceMode === 'mv') await loadMVProject();
          if (state.workspaceMode === 'drama') await loadDrama3State({ preserveForm: false });
          clearEditor();
        } catch (e2) {
          showToast(`Deleting the project failed: ${e2.message}`, 'error');
        }
      }
    };

    list.appendChild(item);
  });

  if (!visibleProjects.length) {
    const empty = document.createElement('p');
    empty.className = 'project-empty';
    empty.textContent = state.projects.length ? 'No matching project. Try another keyword' : 'No projects yet. Click ＋ in the top right to create one';
    list.appendChild(empty);
  }
  list.scrollTop = scrollTop;
}

async function loadProjects() {
  state.projects = await api('/api/projects');
  let changed = false;
  if (!state.projects.some(p => p.id === state.projectId)) {
    let savedId = 0;
    try { savedId = Number(localStorage.getItem('studio:lastProjectId')); } catch {}
    state.projectId = state.projects.find(p => p.id === savedId)?.id || state.projects[0]?.id || null;
    changed = true;
  }
  if (state.projectId) {
    try { localStorage.setItem('studio:lastProjectId', String(state.projectId)); } catch {}
  }
  renderProjects();
  if (changed && state.projectId) {
    await loadScenes();
    if (state.workspaceMode === 'mv') await loadMVProject();
    if (state.workspaceMode === 'drama') {
      state.selectedDramaItemId = getSavedVoiceJobId();
      await loadDrama3State({ preserveForm: false });
    }
  }
}


async function loadScenes() {
  const requestSeq = ++state.sceneLoadSeq;
  const requestedProjectId = state.projectId;
  const list = $('sceneList');
  if ($('deleteSceneBtn')) $('deleteSceneBtn').classList.add('hidden');

  if (!requestedProjectId) {
    list.replaceChildren();
    state.scenes = [];
    $('sceneTitle').textContent = 'Create or select a project first';
    $('sceneSummary').textContent = '0 Shots';
    state.shots = [];
    renderShots();
    return;
  }

  const scenes = await api(`/api/projects/${requestedProjectId}/scenes`);
  if (requestSeq !== state.sceneLoadSeq || Number(state.projectId) !== Number(requestedProjectId)) return;

  state.scenes = scenes;
  let autoSelectedVideoScene = false;
  if (state.workspaceMode === 'image') {
    const validImageScene = state.scenes.some((scene) => Number(scene.id) === Number(state.imageSceneId));
    if (!validImageScene) state.imageSceneId = state.scenes[0]?.id || null;
    state.sceneId = state.imageSceneId;
  } else {
    const validVideoScene = state.scenes.some((scene) => Number(scene.id) === Number(state.sceneId));
    if (!validVideoScene) {
      state.sceneId = state.scenes[0]?.id || null;
      state.shotId = null;
      autoSelectedVideoScene = Boolean(state.sceneId);
    }
  }

  // 一定在接口返回之后再一次性清空并重绘，避免多个并发 loadScenes() 各自 append 导致 Scene 越来越多。
  list.replaceChildren();

  state.scenes.forEach((s) => {
    const item = document.createElement('div');
    const activeSceneId = state.workspaceMode === 'image' ? state.imageSceneId : state.sceneId;
    item.className = `nav-item ${Number(activeSceneId) === Number(s.id) ? 'active' : ''}`;
    item.dataset.sceneId = String(s.id);
    item.innerHTML = `
      <span class="nav-label" title="${escapeHtml(s.name)}">${escapeHtml(s.name)}</span>
      <div class="nav-item-actions">
        <button class="icon-btn btn-xs scene-image-btn" title="Open the image workbench and select this Scene">🖼</button>
        <button class="icon-btn danger btn-xs del-btn" title="Delete Scene">✕</button>
      </div>
    `;

    item.onclick = async (e) => {
      if (e.target.closest('.del-btn') || e.target.closest('.scene-image-btn')) return;
      if (state.workspaceMode === 'image') {
        await selectImageScene(s.id, state.imageRole);
        return;
      }
      state.sceneId = s.id;
      state.shotId = null;
      clearEditor();
      await loadScenes();
      await loadShots();
    };

    const imageBtn = item.querySelector('.scene-image-btn');
    imageBtn.onclick = async (e) => {
      e.stopPropagation();
      state.imageSceneId = s.id;
      state.sceneId = s.id;
      await setWorkspaceMode('image');
      await selectImageScene(s.id, state.imageRole);
    };

    const delBtn = item.querySelector('.del-btn');
    delBtn.onclick = async (e) => {
      e.stopPropagation();
      const ok = await showConfirmModal('Delete Scene', `Delete Scene <b>${escapeHtml(s.name)}</b> and all of its shots?`);
      if (ok) {
        try {
          await api(`/api/scenes/${s.id}`, { method: 'DELETE' });
          showToast('Scene deleted', 'info');
          if (state.sceneId === s.id) {
            state.sceneId = null;
            state.shotId = null;
          }
          if (state.imageSceneId === s.id) state.imageSceneId = null;
          await loadScenes();
          if (state.workspaceMode === 'image') {
            await loadImageDashboard();
            await refreshImageQueueStatus();
          } else {
            await loadShots();
          }
          clearEditor();
        } catch (e2) {
          showToast(`Deleting Scene failed: ${e2.message}`, 'error');
        }
      }
    };

    list.appendChild(item);
  });

  if (state.workspaceMode === 'image') {
    state.sceneId = state.imageSceneId;
  } else if (autoSelectedVideoScene) {
    await loadShots();
  } else if (!state.scenes.length) {
    state.shots = [];
    $('sceneTitle').textContent = 'Create a Scene first';
    $('sceneSummary').textContent = '0 Shots';
    renderShots();
  }
  await checkMasterVideos();
}

let shotListRequestSeq = 0;
async function loadShots() {
  const requestSeq = ++shotListRequestSeq;
  const sceneId = Number(state.sceneId);
  const projectId = Number(state.projectId);
  if (!sceneId) return;
  const shots = await api(`/api/scenes/${sceneId}/shots`);
  if (requestSeq !== shotListRequestSeq || Number(state.sceneId) !== sceneId || Number(state.projectId) !== projectId) return;
  state.shots = Array.from(new Map(shots.map(shot => [String(shot.id), shot])).values());

  // 进入页面/切换 Scene 时，如果当前没有有效选中的 Shot，默认选中第一个。
  // 这样右侧 Inspector 会立即显示第一个 Shot，而不是停留在空状态。
  const selectedStillExists = state.shots.some((shot) => Number(shot.id) === Number(state.shotId));
  if (!selectedStillExists) {
    state.shotId = null;
    state.currentShot = null;
  }

  renderShots();

  const scene = state.scenes.find((x) => x.id === state.sceneId);
  $('sceneTitle').textContent = scene ? scene.name : 'Scene';
  if ($('deleteSceneBtn')) $('deleteSceneBtn').classList.toggle('hidden', !scene);

  if ($('sceneSecondPassCheck')) {
    $('sceneSecondPassCheck').checked = state.shots.length > 0 && state.shots.every((x) => Number(x.second_pass_enabled) === 1);
  }
  updateSceneSummary();
  refreshStatus();
  await checkMasterVideos();

  if (requestSeq !== shotListRequestSeq || Number(state.sceneId) !== sceneId || Number(state.projectId) !== projectId) return;
  if (state.workspaceMode === 'video' && !state.shotId && state.shots.length) {
    await selectShot(state.shots[0].id);
  }
}

// ==========================================
// Storyboard & Table Renderers
// ==========================================
function renderShots() {
  renderTableView();
  updateSceneSummary();
}

function renderTableView() {
  const tbody = $('shotRows');
  state.shots = Array.from(new Map(state.shots.map(shot => [String(shot.id), shot])).values());
  const existingRows = new Map();
  const wantedIds = new Set(state.shots.map(shot => String(shot.id)));
  for (const row of Array.from(tbody.children)) {
    const id = row.dataset.shotId;
    if (!wantedIds.has(id) || existingRows.has(id)) row.remove();
    else existingRows.set(id, row);
  }

  state.shots.forEach((s) => {
    let tr = document.createElement('tr');
    tr.dataset.shotId = String(s.id);
    if (state.shotId === s.id) tr.classList.add('active');
    tr.onclick = () => selectShot(s.id);

    const pct = videoProgressDetail(s).pct;

    tr.innerHTML = `
      <td class="font-mono" style="font-weight:700">#${String(s.shot_no).padStart(3, '0')}</td>
      <td><span class="mode-badge ${s.mode.toLowerCase()}">${s.mode}</span></td>
      <td style="font-weight:600">${escapeHtml(s.title || 'Untitled shot')}</td>
      <td>
        <div style="display:flex;align-items:center;gap:8px">
          <div class="mini-bar-track video-task-progress-track" role="progressbar" aria-label="Video generation progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><div class="mini-bar-fill" style="width:${pct}%"></div></div>
          <span style="font-size:11px;color:var(--text-muted)">${pct}%</span>
        </div>
        <div style="font-size:10px;color:var(--text-secondary);margin-top:5px">${escapeHtml(videoProgressDetail(s).text)}</div>
      </td>
      <td class="font-mono" data-shot-elapsed="${s.id}" title="Live timer from start of processing; fixed to total time once downloaded. Includes preparation and download, excludes queue wait">${shotElapsedText(s)}</td>
      <td style="text-align:right">
        <div style="display:inline-flex;gap:4px">
          <button class="btn btn-ghost btn-xs gen-btn">▶ ${['QUEUED','PREPARING','RUNNING','DONE'].includes(s.status) ? 'Regenerate' : 'Generate'}</button>
          ${s.output_path ? '<button class="btn btn-ghost btn-xs play-btn">Preview</button>' : ''}
          <button class="btn btn-ghost btn-xs clone-btn">📋</button>
          <button class="btn btn-ghost btn-xs del-btn" style="color:#f87171">🗑️</button>
        </div>
      </td>
    `;

    const existing = existingRows.get(String(s.id));
    if (existing) {
      existing.classList.toggle('active', Number(state.shotId) === Number(s.id));
      existing.onclick = () => selectShot(s.id);
      for (let i = 0; i < tr.cells.length; i++) {
        const oldCell = existing.cells[i];
        const newCell = tr.cells[i];
        if (i === 3) {
          oldCell.querySelector('[role="progressbar"]').setAttribute('aria-valuenow', String(pct));
          const oldFill = oldCell.querySelector('.mini-bar-fill');
          const newFill = newCell.querySelector('.mini-bar-fill');
          if (oldFill.style.width !== newFill.style.width) oldFill.style.width = newFill.style.width;
          const oldPercent = oldCell.querySelector('span');
          const newPercent = newCell.querySelector('span');
          if (oldPercent.textContent !== newPercent.textContent) oldPercent.textContent = newPercent.textContent;
          if (oldCell.lastElementChild.textContent !== newCell.lastElementChild.textContent) {
            oldCell.lastElementChild.textContent = newCell.lastElementChild.textContent;
          }
        } else if (oldCell.innerHTML !== newCell.innerHTML) {
          oldCell.innerHTML = newCell.innerHTML;
        }
      }
      tr = existing;
    }

    tr.querySelector('.gen-btn').onclick = async (e) => {
      e.stopPropagation();
      await generateShot(s.id);
    };
    if (s.output_path) {
      tr.querySelector('.play-btn').onclick = async (e) => {
        e.stopPropagation();
        await selectShot(s.id);
        showVideo(state.currentShot);
      };
    }
    tr.querySelector('.clone-btn').onclick = async (e) => {
      e.stopPropagation();
      await cloneShot(s.id);
    };
    tr.querySelector('.del-btn').onclick = async (e) => {
      e.stopPropagation();
      await deleteShot(s.id, s.shot_no);
    };

    const index = state.shots.indexOf(s);
    if (tbody.children[index] !== tr) tbody.insertBefore(tr, tbody.children[index] || null);
  });
}

// ==========================================
// Shot Inspector & Editor
// ==========================================
function clearEditor() {
  shotDrafts.deactivate();
  const isVideo = state.workspaceMode === 'video';
  $('editorEmpty').classList.toggle('hidden', !isVideo);
  $('editorBody').classList.add('hidden');
  $('videoContainer').classList.add('hidden');
  $('mainVideoPanel').classList.add('hidden');
  $('mainShotVideo').pause();
  $('mainShotVideo').removeAttribute('src');
  state.currentShot = null;
  state.shotId = null;
}

function renderShotLipSyncState(s) {
  updateGenerationLive(s);
}

function renderSelectedShot(s, selectionSeq = state.shotSelectSeq) {
  shotDrafts.capture();
  const changedShot = Number(state.currentShot?.id) !== Number(s.id);
  state.currentShot = s;
  const idx = state.shots.findIndex((x) => Number(x.id) === Number(s.id));
  if (idx >= 0) state.shots[idx] = s;
  renderShots();

  const isVideo = state.workspaceMode === 'video';
  $('editorEmpty').classList.add('hidden');
  $('editorBody').classList.toggle('hidden', !isVideo);

  $('editorShotNo').textContent = `#${String(s.shot_no).padStart(3, '0')}`;
  $('editorStatusBadge').className = `badge-status ${statusClass(s.status)}`;
  $('editorStatusBadge').textContent = fmtStatus(s.status);
  const isActiveGeneration = ['QUEUED', 'PREPARING', 'RUNNING'].includes(s.status);
  // 「重新生成」在生成中也要可用：点它会自动清理旧的本地等待协程 / 旧状态并重新提交。
  $('generateShotBtn').querySelector('span').textContent = (isActiveGeneration || s.status === 'DONE') ? 'Regenerate' : 'Generate';
  $('generateShotBtn').disabled = false;
  $('previewOriginalBtn').classList.toggle('hidden', s.status !== 'DONE' || !s.output_path);
  updateGenerationLive(s);

  setModeUI(s.mode);
  $('shotTitle').value = s.title || '';
  $('shotVideoModel').value = s.video_model || videoModelCatalog.defaultModel;
  void loadVideoMVMode();
  $('shotSeconds').value = s.seconds;
  $('shotMP').value = s.megapixels;
  updateLightx2vHint();
  if ($('shotSecondPass')) $('shotSecondPass').value = Number(s.second_pass_enabled) ? '1' : '0';
  if ($('shotSecondPassRes')) $('shotSecondPassRes').value = s.second_pass_resolution || '1k';
  $('shotExtraLora').value = s.realism_lora_enabled ? (s.naughty_lora_enabled ? 'both' : 'realism') : (s.naughty_lora_enabled ? 'naughty' : 'off');
  $('shotSteps').value = String(s.steps);
  updateLightx2vHint();
  $('shotRatio').value = s.aspect_ratio;
  $('shotSeed').value = s.seed;
  $('shotPrompt').value = s.prompt || '';
  renderShotLipSyncState(s, { fillForm: true });
  shotDrafts.activate(s.id);

  renderShotError(s);

  if (changedShot) {
  $('videoContainer').classList.add('hidden');
  $('shotVideo').pause();
  $('shotVideo').removeAttribute('src');
  $('mainVideoPanel').classList.add('hidden');
  $('mainShotVideo').pause();
  $('mainShotVideo').removeAttribute('src');

  }

  // 参考图先用缓存立即渲染，continuity 查询在后台完成。
  void renderRefSlots(s, selectionSeq);
  if ((!$ ('terminalLogPanel')?.classList.contains('hidden')) || $('log-details')?.open) {
    void loadTerminalLog();
  }
}

async function selectShot(id) {
  shotDrafts.capture();
  if(state.shotId !== id && $('continuationProgressBox')) {$('continuationProgressBox').hidden=true;$('continuationResponseBtn').hidden=true;$('continuationResponsePanel').hidden=true;}
  const shotId = Number(id);
  const changedShot = Number(state.currentShot?.id) !== shotId;
  const requestSeq = ++state.shotSelectSeq;
  state.shotId = shotId;

  // 列表本身已有 Shot 数据，先立即显示右侧，不等待 API。
  const cached = state.shots.find((x) => Number(x.id) === shotId);
  const hadCached = Boolean(cached);
  if (cached && changedShot) renderSelectedShot(cached, requestSeq);
  else renderShots();

  try {
    const s = await api(`/api/shots/${shotId}`);
    // 用户已经点了别的 Shot 时，旧请求直接丢弃，禁止把右侧切回去。
    if (requestSeq !== state.shotSelectSeq || Number(state.shotId) !== shotId) return;
    if (!hadCached) {
      renderSelectedShot(s, requestSeq);
      return;
    }

    // 列表缓存已经把表单即时显示出来；后台详情只校准运行态和素材，
    // 不再次写 title/prompt/dialogue 等输入框，避免用户刚开始编辑就被旧响应覆盖。
    state.currentShot = s;
    const idx = state.shots.findIndex((x) => Number(x.id) === shotId);
    if (idx >= 0) state.shots[idx] = s;
    renderShots();
    $('editorStatusBadge').className = `badge-status ${statusClass(s.status)}`;
    $('editorStatusBadge').textContent = fmtStatus(s.status);
    const isActiveGeneration = ['QUEUED', 'PREPARING', 'RUNNING'].includes(s.status);
    $('generateShotBtn').querySelector('span').textContent = (isActiveGeneration || s.status === 'DONE') ? 'Regenerate' : 'Generate';
    $('generateShotBtn').disabled = false;
    $('previewOriginalBtn').classList.toggle('hidden', s.status !== 'DONE' || !s.output_path);
    updateGenerationLive(s);
    renderShotLipSyncState(s);
    void renderRefSlots(s, requestSeq);
  } catch (e) {
    if (requestSeq === state.shotSelectSeq && Number(state.shotId) === shotId) {
      showToast(`Loading Shot failed: ${e.message}`, 'error', 5000);
    }
  }
}

function setModeUI(mode) {
  const selected = mode.toUpperCase();
  const isCut = selected === 'CUT';
  const isContinue = selected === 'CONTINUE';
  const isT2V = selected === 'T2V';
  $('modeCutBtn').classList.toggle('active', isCut);
  $('modeContinueBtn').classList.toggle('active', isContinue);
  $('modeT2VBtn').classList.toggle('active', isT2V);
  $('shotReferencesSection').classList.toggle('hidden', isT2V);
  for (const rSlot of ['ref1', 'ref2', 'ref3', 'ref4']) {
    const c = document.querySelector(`.ref-slot-card[data-slot="${rSlot}"]`);
    if (c) c.classList.toggle('hidden', !isCut);
  }
  const firstFrameCard = document.querySelector('.ref-slot-card[data-slot="first_frame"]');
  if (firstFrameCard) firstFrameCard.classList.toggle('hidden', !isContinue);
  if ($('continuationPanel')) $('continuationPanel').classList.toggle('hidden', !isContinue);
  const tip = $('modeTip');
  if (isT2V) {
    tip.innerHTML = '<strong>T2V text-to-video</strong>: uses only the prompt and dialogue. No reference image, no first frame, no dependency on the previous shot. Reuses the base model and acceleration LoRA; character look comes from the text.';
  } else if (isCut) {
    tip.innerHTML = '<strong>CUT reference-image video</strong>: for standalone shots, camera changes and locking the character. Ref1 is required; Ref2~4 are optional.';
  } else {
    tip.innerHTML = '<strong>CONTINUE (continuation) video</strong>: continues only from the previous shot’s last frame (Ref1~Ref4 are not used). The backend feeds that frame into both first-frame guidance and feature reference, locking expression and pose.';
  }
}

// ==========================================
// Reference Image Cards & Upload Handling
// ==========================================
async function renderRefSlots(shot, selectionSeq = state.shotSelectSeq) {
  if (selectionSeq !== state.shotSelectSeq || Number(state.shotId) !== Number(shot.id)) return;
  const slots = ['ref1', 'ref2', 'ref3', 'ref4', 'first_frame'];

  for (const slot of slots) {
    const card = document.querySelector(`.ref-slot-card[data-slot="${slot}"]`);
    if (!card) continue;

    const emptyState = card.querySelector('.drop-zone');
    const previewWrap = card.querySelector('.preview-wrap');
    const thumbImg = card.querySelector('.slot-thumb');
    const pathField = `${slot}_path`;
    const hasImage = Boolean(shot[pathField] && shot[pathField].trim());

    if (hasImage) {
      const previewSrc = `/api/shots/${shot.id}/ref/${slot}?v=${encodeURIComponent(shot[pathField])}`;
      emptyState.classList.add('hidden');
      previewWrap.classList.remove('hidden');
      if (thumbImg.getAttribute('src') !== previewSrc) thumbImg.src = previewSrc;
      card.dataset.previewSrc = previewSrc;
    } else {
      emptyState.classList.remove('hidden');
      previewWrap.classList.add('hidden');
      if (thumbImg.hasAttribute('src')) thumbImg.removeAttribute('src');
      delete card.dataset.previewSrc;
    }

    // Special handling for First Frame in CONTINUE mode
    if (slot === 'first_frame') {
      const isContinue = shot.mode.toUpperCase() === 'CONTINUE';
      const badge = $('firstFrameBadge');
      const desc = $('firstFrameDesc');
      const text = $('firstFrameText');

      if (isContinue) {
        let meta = {};
        try { meta = JSON.parse(shot.continuation_selection || '{}'); } catch {}
        const auto = meta.mode === 'auto' || !shot.first_frame_path;
        badge.textContent = auto ? 'Auto-continue from the previous last frame' : 'Manual continuation frame (frame 0)';
        text.textContent = auto ? 'Fetches the previous shot’s latest last frame before generating; you can also upload a specific first frame' : 'First frame set manually';
        desc.textContent = auto ? 'Continues automatically once the previous shot is done and downloaded' : 'Generation will use this fixed continuation image';
        if ($('restoreAutoContinuation')) $('restoreAutoContinuation').hidden = auto;
        if ($('continuationCandidates') && $('continuationCandidates').dataset.shotId !== String(shot.id)) { $('continuationCandidates').replaceChildren(); $('continuationCandidates').dataset.shotId=String(shot.id); }
        if ($('continuationStatus')) {
          $('continuationStatus').textContent = auto
            ? (shot.status === 'QUEUED' && shot.current_node?.startsWith('Auto-continue') ? shot.current_node
              : meta.source_shot_id ? `Continuing from the previous last frame; the latest one is fetched again on the next run.` : 'Automatically continues from the previous shot’s last frame in this Scene; waits if that shot is still rendering. No manual frame picking.')
            : meta.source_shot_id ? `Pick from the previous shot ${Number(meta.time).toFixed(3)} s frame; the previous shot is trimmed accordingly on merge.`
            : 'Manual first frame: the previous shot is not trimmed.';
        }
      } else {
        badge.textContent = 'First Frame';
        desc.textContent = 'CUT mode is optional; normally driven by the Ref character image';
        text.textContent = 'Click to upload a first frame or drag a file';
      }
    }
  }
}

async function uploadRef(slot, file) {
  if (!state.shotId) {
    showToast('Select a shot before uploading assets', 'warning');
    return;
  }
  if (!file) return;

  const fd = new FormData();
  fd.append('slot', slot);
  fd.append('file', file);

  showToast(`Uploading ${slotBadgeName(slot)}...`, 'info', 1500);

  try {
    const res = await fetch(`/api/shots/${state.shotId}/upload`, { method: 'POST', body: fd });
    if (!res.ok) throw new Error(await res.text());
    showToast(`${slotBadgeName(slot)} Uploaded`, 'success');
    await selectShot(state.shotId);
  } catch (e) {
    showToast(`Upload failed: ${e.message}`, 'error', 5000);
  }
}

async function deleteRef(slot) {
  if (!state.shotId) return;
  const ok = await showConfirmModal('Delete reference image', `Clear ${slotBadgeName(slot)} ?`);
  if (!ok) return;

  try {
    await api(`/api/shots/${state.shotId}/ref/${slot}`, { method: 'DELETE' });
    showToast(`${slotBadgeName(slot)} Cleared`, 'info');
    await selectShot(state.shotId);
  } catch (e) {
    showToast(`Clear failed: ${e.message}`, 'error');
  }
}

function slotBadgeName(slot) {
  const map = {
    ref1: 'Ref 1 (character)',
    ref2: 'Ref 2 (supporting)',
    ref3: 'Ref 3 (scene)',
    ref4: 'Ref 4 (prop)',
    first_frame: 'First Frame',
  };
  return map[slot] || slot;
}

// Bind Drag & Drop, File Inputs, and Hover Actions to each slot
function initSlotInteractions() {
  document.querySelectorAll('.ref-slot-card').forEach((card) => {
    const slot = card.dataset.slot;

    // Drag and Drop
    card.addEventListener('dragover', (e) => {
      e.preventDefault();
      card.classList.add('drag-over');
      state.hoveredSlot = slot;
    });
    card.addEventListener('dragleave', () => {
      card.classList.remove('drag-over');
    });
    card.addEventListener('drop', async (e) => {
      e.preventDefault();
      card.classList.remove('drag-over');
      if (e.dataTransfer.files && e.dataTransfer.files[0]) {
        await uploadRef(slot, e.dataTransfer.files[0]);
      }
    });

    // File input changes
    card.querySelectorAll('input[type="file"]').forEach((inp) => {
      inp.onchange = async () => {
        if (inp.files && inp.files[0]) {
          await uploadRef(slot, inp.files[0]);
          inp.value = '';
        }
      };
    });

    // Hover Action Buttons
    const zoomBtn = card.querySelector('.action-btn.zoom');
    if (zoomBtn) {
      zoomBtn.onclick = (e) => {
        e.stopPropagation();
        if (state.shotId) {
          const src = card.dataset.previewSrc || `/api/shots/${state.shotId}/ref/${slot}?t=${Date.now()}`;
          openLightbox(src, slotBadgeName(slot));
        }
      };
    }

    const delBtn = card.querySelector('.action-btn.delete');
    if (delBtn) {
      delBtn.onclick = async (e) => {
        e.stopPropagation();
        await deleteRef(slot);
      };
    }
  });

  // Global Clipboard Paste (Ctrl+V) Support
  window.addEventListener('paste', async (e) => {
    if (!state.shotId) return;
    const items = e.clipboardData?.items;
    if (!items) return;

    for (let i = 0; i < items.length; i++) {
      if (items[i].type.indexOf('image') !== -1) {
        const file = items[i].getAsFile();
        if (file) {
          const targetSlot = state.hoveredSlot || 'ref1';
          showToast(`Clipboard image detected, uploading to ${slotBadgeName(targetSlot)}...`, 'info');
          await uploadRef(targetSlot, file);
          break;
        }
      }
    }
  });
}

// ==========================================
// Video Station
// ==========================================
function showVideo(shot, variant = 'auto') {
  const id = shot.id;
  const mediaBase = `/api/shots/${id}/media`;
  const downloadBase = mediaBase;
  const mediaUrl = `${mediaBase}?t=${Date.now()}`;
  const shotNo = String(shot.shot_no).padStart(3, '0');

  // 右侧编辑器内的视频预览保留。
  const container = $('videoContainer');
  const v = $('shotVideo');
  v.src = mediaUrl;
  container.classList.remove('hidden');
  v.load();

  // 中间主工作区提供一个始终容易看到的成片播放器。
  const mainPanel = $('mainVideoPanel');
  const mainVideo = $('mainShotVideo');
  mainVideo.src = mediaUrl;
  mainVideo.load();
  $('mainVideoShotNo').textContent = `#${shotNo}`;
  $('mainVideoTitle').textContent = shot.title || `Shot ${shot.shot_no}`;
  $('mainVideoMeta').textContent = `${Number(shot.seconds).toFixed(1)}s · ${Number(shot.megapixels).toFixed(2)}MP · ${shot.aspect_ratio} · ${shot.steps} steps`;
  $('videoMetaText').textContent = 'Generate final cut';
  mainPanel.classList.remove('hidden');

  const download = () => {
    const a = document.createElement('a');
    a.href = downloadBase;
    a.download = `shot_${shotNo}.mp4`;
    a.click();
  };
  const viewLastFrame = () => {
    openLightbox(`/api/shots/${id}/ref/last_frame?t=${Date.now()}`, `Shot #${shotNo} last frame`);
  };

  $('downloadVideoBtn').onclick = download;
  $('mainVideoDownloadBtn').onclick = download;
  $('shotVideoFloatingDownloadBtn').onclick = download;
  $('mainVideoFloatingDownloadBtn').onclick = download;
  $('viewLastFrameBtn').onclick = viewLastFrame;
  $('mainVideoLastFrameBtn').onclick = viewLastFrame;
  $('mainVideoCloseBtn').onclick = () => {
    mainVideo.pause();
    mainVideo.removeAttribute('src');
    mainVideo.load();
    mainPanel.classList.add('hidden');
  };
}

// ==========================================
// Shot Actions (Save, Generate, Clone, Delete)
// ==========================================
async function saveCurrentShot({ silent = false, refresh = true } = {}) {
  if (!state.shotId) return false;
  const id = Number(state.shotId);
  try {
    const saved = await shotDrafts.saveCurrent();
    if (!saved) return false;
    if (!silent) showToast('Shot saved', 'success');
    if (refresh && Number(state.shotId) === id) {
      await loadShots();
      if (Number(state.shotId) === id) await selectShot(id);
    }
    return true;
  } catch (error) {
    showToast(`Save incomplete; your edits are kept:${error.message}`, 'error');
    return false;
  }
}

/** 生图模型下拉框与模型参数联动均来自统一模型目录 config/models.json（/api/models）。 */
function defaultImageStepsAndCfg(modelId = $('imageModel')?.value, loraMode = $('imageLoraMode')?.value) {
  const models = state.imageCatalog?.models || [];
  const model = models.find((m) => m.id === modelId);
  const modelSteps = Number(model?.steps || 0);
  const steps = modelSteps > 0 ? modelSteps : (['accelerated', 'all'].includes(loraMode) ? 4 : 8);
  const cfg = Number(model?.cfg || 0) > 0 ? Number(model.cfg) : 1.0;
  return { steps, cfg, hasModelSteps: modelSteps > 0, model };
}

function syncImageModelControls({ applyDefaults = false } = {}) {
  const { steps, cfg, hasModelSteps, model } = defaultImageStepsAndCfg();
  if ($('imageLoraGroup')) {
    $('imageLoraGroup').classList.toggle('hidden', hasModelSteps);
  }
  if ($('imageCfgGroup')) {
    $('imageCfgGroup').classList.toggle('hidden', !hasModelSteps);
  }
  if ($('imageSteps')) {
    $('imageSteps').disabled = !hasModelSteps;
    if (!hasModelSteps || applyDefaults || !Number($('imageSteps').value)) {
      $('imageSteps').value = steps;
    }
  }
  if ($('imageCfg')) {
    if (!hasModelSteps || applyDefaults || !Number($('imageCfg').value)) {
      $('imageCfg').value = cfg;
    }
  }
  const ratioSelect = $('imageRatio');
  if (ratioSelect && model?.aspect_ratios) {
    const ratioTitles = { '9:16': '9:16 · portrait', '16:9': '16:9 · landscape', '1:1': '1:1 · square' };
    Array.from(ratioSelect.options).forEach((opt) => {
      const size = model.aspect_ratios[opt.value];
      const base = ratioTitles[opt.value] || opt.value;
      opt.textContent = Array.isArray(size) && size.length === 2 ? `${base} (${size[0]}×${size[1]})` : base;
    });
  }
}

async function loadImageModelOptions() {
  const catalog = await api('/api/models');
  state.imageCatalog = catalog.image;
  const selector = $('imageModel');
  const previous = selector.value;
  selector.replaceChildren(...catalog.image.models.map(model => new Option(model.label, model.id)));
  selector.value = catalog.image.models.some(model => model.id === previous) ? previous : catalog.image.default;
  syncImageModelControls();
}

async function generateShot(id) {
  return withSubmissionGuard(`shot:${id}`, () => generateShotImpl(id));
}

async function generateShotImpl(id) {
  const shot = state.shots.find((x) => x.id === id) || (state.currentShot?.id === id ? state.currentShot : null);
  const wasActive = Boolean(shot && ['QUEUED', 'PREPARING', 'RUNNING'].includes(shot.status));

  const previous = shot ? {
    status: shot.status,
    progress: shot.progress,
    current_node: shot.current_node,
  } : null;
  const wasDone = shot?.status === 'DONE';

  // 点击后先立即显示排队状态，避免重新生成时还弹确认框或等待网络返回。
  // 按钮本身保持可用：生成中再点一次等于「重新生成」，后端会自动清理旧的本地等待和旧状态。
  if (shot) {
    shot.status = 'QUEUED';
    shot.progress = 0;
    shot.current_node = 'Submitting';
    if (state.currentShot?.id === id) Object.assign(state.currentShot, shot);
    renderShots();
    if (Number(state.shotId) === Number(id)) {
      $('editorStatusBadge').className = `badge-status ${statusClass('QUEUED')}`;
      $('editorStatusBadge').textContent = fmtStatus('QUEUED');
      $('generateShotBtn').querySelector('span').textContent = 'Regenerate';
      $('generateShotBtn').disabled = false;
      updateGenerationLive(shot);
    }
  }

  if (id === state.shotId) {
    const saved = await saveCurrentShot({ silent: true, refresh: false });
    if (!saved) {
      if (shot && previous) Object.assign(shot, previous);
      renderShots();
      if (Number(state.shotId) === Number(id) && state.currentShot) renderSelectedShot(state.currentShot);
      return;
    }
  }

  try {
    await api(`/api/shots/${id}/generate`, { method: 'POST', body: '{}' });
    showToast(wasDone || wasActive ? 'Added to the regeneration queue' : 'Added to the generation queue', 'info');
    await loadShots();
  } catch (e) {
    if (shot && previous) Object.assign(shot, previous);
    renderShots();
    if (Number(state.shotId) === Number(id) && state.currentShot) renderSelectedShot(state.currentShot);
    showToast(`Submitting generation failed: ${e.message}`, 'error');
  }
}

async function cloneShot(id) {
  try {
    const copy = await api(`/api/shots/${id}/clone`, { method: 'POST' });
    showToast('Shot duplicated', 'success');
    await loadShots();
    await selectShot(copy.id);
  } catch (e) {
    showToast(`Copy failed: ${e.message}`, 'error');
  }
}

async function deleteShot(id, shotNo) {
  const ok = await showConfirmModal('Delete shot', `Delete shot <b>#${String(shotNo).padStart(3, '0')}</b> ? Queued tasks are removed from the waiting queue as well.`);
  if (!ok) return;

  try {
    await api(`/api/shots/${id}`, { method: 'DELETE' });
    shotDrafts.discard(id);
    showToast('Shot deleted', 'info');
    if (state.shotId === id) clearEditor();
    await loadShots();
  } catch (e) {
    showToast(`Delete failed: ${e.message}`, 'error');
  }
}

// ==========================================
// Settings Modal
// ==========================================
async function openSettings() {
  const s = await api('/api/settings');
  $('comfyUrl').value = s.comfy_url || '';
  $('pikaUrl').value = s.pika_url || '';
  $('fps').value = s.fps || '24';
  $('settingsModal').classList.remove('hidden');
}

async function saveSettings() {
  const payload = {
    comfy_url: $('comfyUrl').value.trim(),
    pika_url: $('pikaUrl').value.trim(),
    fps: $('fps').value || '24',
  };
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify(payload) });
    showToast('Config saved', 'success');
    $('settingsModal').classList.add('hidden');
    refreshStatus();
  } catch (e) {
    showToast(`Save failed: ${e.message}`, 'error');
  }
}

// ==========================================
// Real-time WebSocket
// ==========================================
function connectAppWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/app`);
  state.ws = ws;

  ws.onopen = async () => {
    ws.send('hello');
    // 后端 reload / Windows 应用重启后，WebSocket 可能错过恢复广播。
    // 重连时主动重新读取当前工作区，避免页面继续显示旧的“生成中”。
    try {
      if (state.workspaceMode === 'image') {
        await loadImageDashboard();
        await refreshImageQueueStatus();
      } else if (state.workspaceMode === 'drama') {
        await loadDrama3State({ preserveForm: true });
      } else if (state.sceneId) {
        await loadShots();
        if (state.shotId && state.shots.some((shot) => Number(shot.id) === Number(state.shotId))) {
          await selectShot(state.shotId);
        }
      }
    } catch (e) {
      console.warn('WebSocket reconnect refresh failed:', e);
    }
  };

  ws.onmessage = async (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if ((msg.type === 'mv_song_progress' || msg.type === 'mv_song_state') && msg.project_id) {
        if (Number(msg.project_id) === Number(state.projectId)) {
          const mv = state.mvProject || {};
          state.mvProject = {
            ...mv,
            song_status: msg.status || mv.song_status || 'RUNNING',
            song_started_at: msg.song_started_at ?? mv.song_started_at ?? 0,
            song_finished_at: msg.song_finished_at ?? mv.song_finished_at ?? 0,
            song_elapsed_seconds: msg.song_elapsed_seconds ?? mv.song_elapsed_seconds ?? 0,
            song_progress: msg.progress !== undefined ? Number(msg.progress || 0) : Number(mv.song_progress || 0),
            song_node: msg.node !== undefined ? (msg.node || '') : (mv.song_node || ''),
            song_error: msg.error !== undefined ? (msg.error || '') : (mv.song_error || ''),
            audio_url: msg.audio_url || mv.audio_url || '',
            audio_duration: msg.audio_duration !== undefined ? Number(msg.audio_duration || 0) : Number(mv.audio_duration || 0),
          };
          if (msg.deleted) {
            state.mvProject.audio_url = '';
            state.mvProject.audio_download_url = '';
            state.mvProject.audio_path = '';
            state.mvProject.audio_duration = 0;
          }
          if (msg.type === 'mv_song_state' && msg.status === 'DONE' && msg.audio_url) {
            const player = $('mvAudioPlayer');
            if (player) player.dataset.audioUrl = '';
          }
          if (state.workspaceMode === 'mv') renderMVWorkbench();
          if (msg.type === 'mv_song_state' && ['DONE', 'FAILED', 'PENDING'].includes(msg.status)) {
            await loadMVProject();
            const preview = $('audioPreviewModal');
            if (msg.status === 'DONE' && !msg.deleted && preview && !preview.classList.contains('hidden')
                && Number(preview.dataset.projectId) === Number(msg.project_id)
                && Number(state.projectId) === Number(msg.project_id)) {
              openAudioPreview(state.mvProject);
            }
          }
        }
        return;
      }
      if (msg.type === 'voice_job_progress' || msg.type === 'voice_job_state') {
        const targetId = Number(msg.job_id || 0);
        const matchesProject = msg.project_id === null
          || msg.project_id === undefined
          || Number(msg.project_id) === Number(state.projectId);
        const targetItem = findVoiceJobById(targetId);
        let statusChanged = false;
        if (targetItem) {
          if (msg.status && targetItem.status !== msg.status) statusChanged = true;
          if (msg.status) targetItem.status = msg.status;
          if (msg.progress !== undefined) targetItem.progress = Number(msg.progress || 0);
          if (msg.node !== undefined) targetItem.current_node = msg.node || '';
          if (msg.error !== undefined) targetItem.error = msg.error || '';
          if (msg.duration !== undefined) targetItem.duration = Number(msg.duration || 0);
          if (msg.elapsed_seconds !== undefined) targetItem.elapsed_seconds = Number(msg.elapsed_seconds || 0);
          if (msg.audio_url !== undefined) {
            targetItem.audio_url = msg.audio_url || '';
            targetItem.has_audio = Boolean(msg.audio_url);
          }
        }
        if (state.workspaceMode === 'drama' && (targetItem || matchesProject)) {
          if (statusChanged) {
            renderDrama3HistoryList(getDrama3Items());
          }
          renderDrama3StatusOnly();
          if (msg.type === 'voice_job_state' && ['DONE', 'FAILED', 'CANCELLED', 'QUEUED', 'PENDING'].includes(msg.status)) {
            await loadDrama3State({ preserveForm: true });
          }
        }
        return;
      }
      if (msg.type === 'scene_shots_replaced' && msg.scene_id) {
        if (Number(msg.scene_id) === Number(state.sceneId)) {
          await loadShots();
        }
        return;
      }
      if ((msg.type === 'mv_analysis_progress' || msg.type === 'mv_analysis_state') && msg.project_id) {
        if (Number(msg.project_id) === Number(state.projectId)) {
          const mv = state.mvProject || {};
          state.mvProject = {
            ...mv,
            analysis_status: msg.status || mv.analysis_status || 'RUNNING',
            analysis_progress: msg.progress !== undefined ? Number(msg.progress || 0) : Number(mv.analysis_progress || 0),
            analysis_error: msg.error !== undefined ? (msg.error || '') : (mv.analysis_error || ''),
          };
          const timelineState = state.mvTimeline || {};
          state.mvTimeline = {
            ...timelineState,
            analysis_status: state.mvProject.analysis_status,
            analysis_progress: state.mvProject.analysis_progress,
            analysis_error: state.mvProject.analysis_error,
            analysis_node: msg.node || '',
          };
          if (state.workspaceMode === 'mv') renderMVWorkbench();
          if (state.workspaceMode === 'drama') renderDrama3HistoryList(getDrama3Items());
          if (msg.type === 'mv_analysis_state' && ['DONE', 'FAILED'].includes(msg.status)) {
            await loadMVProject();
            if (state.workspaceMode === 'drama') renderDrama3HistoryList(getDrama3Items());
          }
        }
        return;
      }
      if ((msg.type === 'mv_pipeline_progress' || msg.type === 'mv_pipeline_state') && msg.project_id) {
        if (Number(msg.project_id) === Number(state.projectId)) {
          const mv = state.mvProject || {};
          state.mvProject = {
            ...mv,
            pipeline_status: msg.status || mv.pipeline_status || 'RUNNING',
            pipeline_progress: msg.progress !== undefined ? Number(msg.progress || 0) : Number(mv.pipeline_progress || 0),
            pipeline_node: msg.node !== undefined ? (msg.node || '') : (mv.pipeline_node || ''),
            pipeline_error: msg.error !== undefined ? (msg.error || '') : (mv.pipeline_error || ''),
            master_url: msg.master_url || mv.master_url || '',
            master_video_path: msg.master_video_path || mv.master_video_path || '',
          };
          if (state.workspaceMode === 'mv') renderMVWorkbench();
          if (msg.type === 'mv_pipeline_state' && ['DONE', 'FAILED'].includes(msg.status)) {
            await loadMVProject();
          }
        }
        return;
      }
      if (msg.type === 'lip_sync_state' && msg.shot_id) {
        const timelineShot = state.mvTimeline?.shots?.find((x) => Number(x.id) === Number(msg.shot_id));
        if (timelineShot) {
          timelineShot.lip_sync_status = msg.status || timelineShot.lip_sync_status || 'IDLE';
          if (msg.progress !== undefined) timelineShot.lip_sync_progress = Number(msg.progress || 0);
          if (msg.error !== undefined) timelineShot.lip_sync_error = msg.error || '';
        }
        const shot = state.shots.find((x) => Number(x.id) === Number(msg.shot_id));
        if (shot) {
          shot.lip_sync_status = msg.status || shot.lip_sync_status || 'IDLE';
          if (msg.progress !== undefined) shot.lip_sync_progress = Number(msg.progress || 0);
          if (msg.error !== undefined) shot.lip_sync_error = msg.error || '';
          if (msg.lip_sync_output_path) shot.lip_sync_output_path = msg.lip_sync_output_path;
        }
        if (state.currentShot && Number(state.currentShot.id) === Number(msg.shot_id)) {
          state.currentShot.lip_sync_status = msg.status || state.currentShot.lip_sync_status || 'IDLE';
          if (msg.progress !== undefined) state.currentShot.lip_sync_progress = Number(msg.progress || 0);
          if (msg.error !== undefined) state.currentShot.lip_sync_error = msg.error || '';
          if (msg.lip_sync_output_path) state.currentShot.lip_sync_output_path = msg.lip_sync_output_path;
          renderShotLipSyncState(state.currentShot);
          updateGenerationLive(state.currentShot);
          if (msg.status === 'DONE' && state.workspaceMode === 'video') {
            await selectShot(msg.shot_id);
            if (state.currentShot) showVideo(state.currentShot, 'auto');
          }
        }
        renderShots();
        if (state.workspaceMode === 'mv') renderMVWorkbench();
        return;
      }
      if ((msg.type === 'scene_image_progress' || msg.type === 'scene_image_state') && msg.scene_id) {
        if (!state.scenes.some(scene => Number(scene.id) === Number(msg.scene_id))) return;
        const scene = getImageSceneData(msg.scene_id);
        const roleData = scene?.roles?.[msg.role];
        if (Array.isArray(state.imageQueue?.jobs)) {
          const qJob = state.imageQueue.jobs.find(
            j => (msg.job_id && Number(j.id) === Number(msg.job_id)) || (Number(j.scene_id) === Number(msg.scene_id) && j.role === msg.role)
          );
          if (qJob) {
            if (msg.status) qJob.status = msg.status;
            if (msg.progress !== undefined) qJob.progress = Number(msg.progress || 0);
            if (msg.node !== undefined) qJob.current_node = msg.node || '';
            if (msg.error !== undefined) qJob.error = msg.error || '';
            if (msg.generation_started_at !== undefined) qJob.generation_started_at = msg.generation_started_at;
            if (msg.generation_finished_at !== undefined) qJob.generation_finished_at = msg.generation_finished_at;
            if (msg.generation_elapsed_seconds !== undefined) qJob.generation_elapsed_seconds = msg.generation_elapsed_seconds;
          }
        }
        if (roleData) {
          const existing = roleData.latest_job || {};
          roleData.latest_job = {
            ...existing,
            id: msg.job_id || existing.id,
            scene_id: msg.scene_id,
            role: msg.role,
            status: msg.status || existing.status || 'RUNNING',
            progress: msg.progress !== undefined ? Number(msg.progress || 0) : Number(existing.progress || 0),
            current_node: msg.node !== undefined ? (msg.node || '') : (existing.current_node || ''),
            error: msg.error !== undefined ? (msg.error || '') : (existing.error || ''),
            generation_started_at: msg.generation_started_at ?? existing.generation_started_at ?? 0,
            generation_finished_at: msg.generation_finished_at ?? existing.generation_finished_at ?? 0,
            generation_elapsed_seconds: msg.generation_elapsed_seconds ?? existing.generation_elapsed_seconds ?? 0,
          };
          renderImageWorkbench();
          if (Number(state.imageSceneId) === Number(msg.scene_id) && state.imageRole === msg.role) {
            renderImageInspector();
          }
        }
        if (msg.type === 'scene_image_state' && ['DONE', 'FAILED'].includes(msg.status)) {
          await loadImageDashboard();
          if (Number(state.imageSceneId) === Number(msg.scene_id) && state.imageRole === msg.role) {
            await loadImageWorkbenchHistory();
          }

          // 生图/上传成功后，后端会立即把最新图片写入对应 CUT Ref。
          // 这里同步刷新受影响 Scene 的 Shot/参考图，但不重填 Prompt/Dialogue 表单，避免覆盖用户正在编辑的文字。
          if (msg.status === 'DONE' && state.sceneId) {
            const appliedSceneIds = Array.isArray(msg.applied_scene_ids) && msg.applied_scene_ids.length
              ? msg.applied_scene_ids.map(Number)
              : [Number(msg.scene_id)];
            if (appliedSceneIds.includes(Number(state.sceneId))) {
              const syncedSceneId = Number(state.sceneId);
              const syncedShots = await api(`/api/scenes/${syncedSceneId}/shots`);
              if (Number(state.sceneId) !== syncedSceneId) return;
              state.shots = syncedShots;
              renderShots();
              if (state.shotId) {
                const refreshedShot = state.shots.find((x) => Number(x.id) === Number(state.shotId));
                if (refreshedShot) {
                  state.currentShot = refreshedShot;
                  await renderRefSlots(refreshedShot, state.shotSelectSeq);
                }
              }
            }
          }
        }
        await refreshImageQueueStatus();
        refreshStatus();
        return;
      }
      if (msg.shot_id) {
        const shot = state.shots.find((x) => x.id === msg.shot_id);
        if (shot) {
          if (msg.progress !== undefined) shot.progress = msg.progress;
          if (msg.status) shot.status = msg.status;
          if (msg.node !== undefined) shot.current_node = msg.node || '';
          if (msg.error !== undefined) shot.error = msg.error || '';
          for (const key of ['generation_started_at', 'generation_finished_at', 'generation_elapsed_seconds']) {
            if (msg[key] !== undefined) shot[key] = msg[key];
          }
          renderShots();
          renderTopVideoQueueBar();
          if (Number(state.shotId) === Number(msg.shot_id)) {
            if (state.currentShot) Object.assign(state.currentShot, { status: shot.status, progress: shot.progress, current_node: shot.current_node, error: shot.error });
            renderShotError(shot);
            $('editorStatusBadge').className = `badge-status ${statusClass(shot.status)}`;
            $('editorStatusBadge').textContent = fmtStatus(shot.status);
            updateGenerationLive(shot);
            if (['DONE', 'FAILED', 'CANCELLED'].includes(msg.status)) {
              await selectShot(msg.shot_id);
            }
          }
        }
      }
      refreshStatus();
    } catch {}
  };

  ws.onclose = () => setTimeout(connectAppWs, 2000);
}

// ==========================================
// UI Event Bindings
// ==========================================
function initEvents() {
  new MutationObserver(syncSubmissionButtons).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled'] });
  $('videoWorkspaceBtn').onclick = () => setWorkspaceMode('video');
  $('imageWorkspaceBtn').onclick = () => setWorkspaceMode('image');
  $('mvWorkspaceBtn').onclick = () => setWorkspaceMode('mv');
  if ($('dramaWorkspaceBtn')) $('dramaWorkspaceBtn').onclick = () => setWorkspaceMode('drama');
  if ($('dramaAddTaskBtn')) $('dramaAddTaskBtn').onclick = () => addDrama3Task();
  if ($('dramaAddVoiceBtn')) $('dramaAddVoiceBtn').onclick = () => addDrama3VoiceSlot();
  if ($('dramaSaveBtn')) $('dramaSaveBtn').onclick = () => saveDrama3Config();
  if ($('dramaInspectorGenerateBtn')) $('dramaInspectorGenerateBtn').onclick = () => generateDrama3Audio();
  if ($('dramaInspectorApplyBtn')) $('dramaInspectorApplyBtn').onclick = () => applyVoiceJobToProject();
  if ($('autoPlanShotsBtn')) $('autoPlanShotsBtn').onclick = () => openAutoPlanShotsModal();
  if ($('closeShotPlanModalBtn')) $('closeShotPlanModalBtn').onclick = closeAutoPlanShotsModal;
  if ($('cancelShotPlanModalBtn')) $('cancelShotPlanModalBtn').onclick = closeAutoPlanShotsModal;
  if ($('confirmShotPlanModalBtn')) $('confirmShotPlanModalBtn').onclick = () => confirmAutoPlanShots(false);
  if ($('shotPlanModal')) {
    $('shotPlanModal').onclick = (e) => {
      if (e.target === $('shotPlanModal')) closeAutoPlanShotsModal();
    };
  }
  [
    'dramaTitleInput',
    'dramaScriptText',
    'dramaModelName',
    'dramaSpeakersMode',
    'dramaExpertsMode',
    'dramaAsrLanguage',
    'dramaTemperature',
    'dramaTopP',
    'dramaMaxFrames',
    'dramaSeed',
    'dramaSplitLongText',
    'dramaShortenPauses',
  ].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener('input', markDramaEditorDirty);
    el.addEventListener('change', markDramaEditorDirty);
  });
  if ($('dramaPresetSelect')) {
    $('dramaPresetSelect').onchange = async (e) => {
      const key = e.target.value;
      e.target.value = '';
      const preset = state.voicePresets?.[key];
      if (!preset) return;
      const selectedItem = getSelectedDrama3Item();
      if (!selectedItem) {
        return showToast('Create a voice-over task with "＋ Add to queue" before loading a preset', 'warning');
      }
      if ($('dramaTitleInput')) $('dramaTitleInput').value = preset.title || '';
      if ($('dramaScriptText')) $('dramaScriptText').value = preset.script || '';
      const needSpeakers = Math.max(1, Number(preset.speakers || 1));
      await saveDrama3Config({ silent: true, itemId: selectedItem.id });
      const existingSpeakers = Array.isArray(selectedItem.speakers) ? selectedItem.speakers : [];
      for (let idx = existingSpeakers.length; idx < needSpeakers; idx++) {
        const updated = await api(`/api/voice-jobs/${selectedItem.id}/speakers/${idx}`, {
          method: 'PUT',
          body: JSON.stringify({
            name: idx === 0 ? 'Role 0 (lead)' : `Role ${idx}`,
            voice_profile_id: null,
          }),
        });
        replaceVoiceJobInState(updated);
      }
      renderDrama3Workbench({ preserveForm: true });
      showToast(`Preset script loaded: ${preset.title}（${needSpeakers}-speaker mode)`, 'info');
    };
  }
  document.querySelectorAll('#dramaEmotionTagChips .drama-chip').forEach((btn) => {
    btn.onclick = () => insertDramaTagAtCursor(btn.dataset.tag || '');
  });
  if ($('dramaSeedRandBtn')) {
    $('dramaSeedRandBtn').onclick = () => {
      $('dramaSeed').value = Math.floor(Math.random() * 100000000);
      markDramaEditorDirty();
    };
  }
  if ($('dramaSeed42Btn')) {
    $('dramaSeed42Btn').onclick = () => {
      $('dramaSeed').value = 42;
      markDramaEditorDirty();
    };
  }
  if ($('dramaAudioPlayer')) {
    ['play', 'pause', 'ended'].forEach((evtName) => {
      $('dramaAudioPlayer').addEventListener(evtName, updateDramaPlayButtonsUI);
    });
  }
  $('closeAudioPreviewBtn').onclick = closeAudioPreview;
  $('generateWholeStoryImagesBtn').onclick = queueWholeStoryImages;
  $('generateWholeStoryVideosBtn').onclick = queueWholeStoryVideos;

  $('mvSaveProjectBtn').onclick = saveMVProject;
  const triggerGenerateMVSong = () => {
    const mv = state.mvProject;
    const isRegen = Boolean(mv?.audio_path || mv?.audio_url || String(mv?.song_status || '').toUpperCase() === 'RUNNING');
    return generateCurrentMVSong(isRegen);
  };
  if ($('mvGenerateBtn')) $('mvGenerateBtn').onclick = triggerGenerateMVSong;
  if ($('mvInspectorGenerateBtn')) $('mvInspectorGenerateBtn').onclick = triggerGenerateMVSong;
  const analyzeCurrentMVSong = async () => {
    if (!state.projectId) return showToast('Select a project first', 'warning');
    try {
      await saveMVProject();
      if (state.mvProject) {
        state.mvProject.analysis_status = 'RUNNING';
        state.mvProject.analysis_progress = 2;
        state.mvProject.analysis_error = '';
        renderMVWorkbench();
      }
      showToast('Separating vocals, aligning lyrics and analyzing beats…', 'info', 3500);
      await api(`/api/projects/${state.projectId}/audio/analyze`, { method: 'POST', body: '{}' });
      await loadMVProject();
      showToast('Song analysis done: lyric timeline, beat and vocals are ready', 'success', 5000);
    } catch (e) {
      await loadMVProject();
      showToast(`Song analysis failed: ${e.message}`, 'error', 9000);
    }
  };
  $('mvAnalyzeBtn').onclick = analyzeCurrentMVSong;
  $('mvAnalyzeAgainBtn').onclick = analyzeCurrentMVSong;
  // Add Project
  $('addProjectBtn').onclick = async () => {
    const name = await showPromptModal('New project', 'Project name (e.g. Sakura Romance EP01)');
    if (!name) return;
    try {
      const p = await api('/api/projects', { method: 'POST', body: JSON.stringify({ name }) });
      showToast(`Project「${name}」created`, 'success');
      state.projectId = p.id;
      state.sceneId = null;
      await loadProjects();
      await loadScenes();
      if (state.workspaceMode === 'image') await loadImageDashboard();
    } catch (e) {
      showToast(`Creating a project failed: ${e.message}`, 'error');
    }
  };

  // Add Scene
  $('addSceneBtn').onclick = async () => {
    if (!state.projectId) return showToast('Select or create a project first', 'warning');
    const name = await showPromptModal('New Scene', 'Scene name (e.g. Confession by the river)');
    if (!name) return;
    try {
      const s = await api(`/api/projects/${state.projectId}/scenes`, { method: 'POST', body: JSON.stringify({ name }) });
      showToast(`Scene「${name}」created`, 'success');
      state.sceneId = s.id;
      state.imageSceneId = s.id;
      await loadScenes();
      if (state.workspaceMode === 'image') {
        await loadImageDashboard();
        await selectImageScene(s.id, 'ref1');
      } else {
        await loadShots();
      }
    } catch (e) {
      showToast(`Creating a Scene failed: ${e.message}`, 'error');
    }
  };

  // Add Shot
  $('addShotBtn').onclick = async () => {
    if (!state.sceneId) return showToast('Select or create a Scene first', 'warning');
    // 新镜头默认接续，首镜需要时可手动切换生成方式。
    const defaultMode = 'CONTINUE';
    const nextShotNo = state.shots.reduce((maxNo, shot) => Math.max(maxNo, Number(shot.shot_no || 0)), 0) + 1;

    try {
      const s = await api(`/api/scenes/${state.sceneId}/shots`, {
        method: 'POST',
        body: JSON.stringify({
          title: `Shot ${nextShotNo}`,
          mode: defaultMode,
          seconds: 5.0,
          ...videoModelCatalog.defaults(),
          second_pass_enabled: Boolean($('sceneSecondPassCheck')?.checked),
          seed: 42,
        }),
      });
      showToast(`Shot #${String(s.shot_no).padStart(3, '0')} created`, 'success');
      await loadShots();
      await selectShot(s.id);
    } catch (e) {
      showToast(`Creating a shot failed: ${e.message}`, 'error');
    }
  };

  // Batch Generate Scene: 开启二采时默认生成本 Scene 全部非运行中镜头；仅一采时只补齐未完成镜头。
  $('generateSceneBtn').onclick = () => withSubmissionGuard(`videos-scene:${state.sceneId}`, async () => {
    if (!state.sceneId) return showToast('Select the Scene to render', 'warning');
    if (!state.shots.length) return showToast('This Scene has no shots yet', 'warning');

    const activeStatuses = ['QUEUED', 'PREPARING', 'RUNNING'];
    const twoPassOn = Boolean($('sceneSecondPassCheck')?.checked);
    const skipStatuses = twoPassOn ? activeStatuses : ['DONE', ...activeStatuses];
    const targets = state.shots.filter((s) => !skipStatuses.includes(s.status));
    const skipped = state.shots.length - targets.length;
    if (!targets.length) {
      return showToast(
        twoPassOn ? 'All shots in this Scene are already queued' : 'All shots in this Scene are done or already queued',
        'info',
      );
    }

    const passLabel = twoPassOn ? 'Second pass (Open-H3 1080p refine)' : 'First pass only (0.4MP quick draft)';
    const msg = twoPassOn
      ? `enabled <b>${passLabel}</b> — this run batch-generates every <b>${targets.length} shots</b>${skipped ? `(skipping ${skipped} running shots)` : ''}。Add to queue?`
      : `This run uses <b>${passLabel}</b> Generate <b>${targets.length} unfinished shots</b>，${skipped ? `Skips ${skipped} finished or running shots automatically.` : ''}Add to queue?`;
    const ok = await showConfirmModal('Render Scene batch', msg);
    if (!ok) return;

    try {
      const result = await api(`/api/scenes/${state.sceneId}/generate`, {
        method: 'POST',
        body: JSON.stringify({ include_done: twoPassOn }),
      });
      showToast(`Added ${result.queued} Shots, skipped ${result.skipped}`, 'success');
      await loadShots();
    } catch (e) {
      showToast(`Batch submit failed: ${e.message}`, 'error');
    }
  });

  // Merge current Scene
  $('mergeSceneBtn').onclick = async () => {
    if (!state.sceneId) return showToast('Select a Scene first', 'warning');
    if ($('mergeSceneBtn').disabled) return;
    closeMasterModal();
    $('mergeSceneBtn').disabled = true;
    const sceneId = state.sceneId;
    const sceneTitle = $('sceneTitle').textContent || 'Current Scene';
    showToast('Merging this Scene’s output with FFmpeg...', 'info', 2200);
    try {
      const result = await api(`/api/scenes/${sceneId}/merge`, { method: 'POST', body: '{}' });
      showToast(`Scene merged: combined ${result.merged_count} Shots${result.skipped_count ? `, skipped ${result.skipped_count}` : ''}`, 'success', 5000);
      await checkMasterVideos();
      openMasterModal(result.media_url, `Scene screening:${sceneTitle}`, result.path);
    } catch (e) {
      showToast(`Merging this Scene failed: ${e.message}`, 'error', 7000);
    } finally {
      $('mergeSceneBtn').disabled = false;
    }
  };

  // Merge whole Project / episode
  $('mergeProjectBtn').onclick = async () => {
    if (!state.projectId) return showToast('Select a Project first', 'warning');
    const projectId = state.projectId;
    if ($('mergeProjectBtn').disabled) return;
    closeMasterModal();
    $('mergeProjectBtn').disabled = true;
    const isMv = Boolean(Number(state.mvProject?.native_audio_enabled)) || $('videoMVMode')?.value === '1';
    renderProjectMergeProgress({status:'RUNNING',progress:0,stage:'Preparing merge',started_at:Date.now()/1000});
    showToast(isMv ? 'Merging the full project; lyric subtitles will be added automatically for MV…' : 'Merging the full project…', 'info', 2200);
    try {
      const result = await api(`/api/projects/${projectId}/merge`, { method: 'POST', body: '{}' });
      showToast('Full project merged! Opening the screening room...', 'success', 3500);
      await checkMasterVideos();
      openMasterModal(result.media_url, 'Project full-episode screening', result.path);
    } catch (e) {
      showToast(`Full-project merge failed: ${e.message}`, 'error', 7000);
    } finally {
      $('mergeProjectBtn').disabled = false;
    }
  };

  // Master Video Cinema Modal Handlers
  $('playProjectMasterBtn').onclick = () => {
    if (state.projectMaster && state.projectMaster.media_url) {
      openMasterModal(state.projectMaster.media_url, 'Project full-episode screening', state.projectMaster.path);
    } else {
      showToast('This Project has not been merged yet. Click "Merge full episode" first', 'warning');
    }
  };

  $('closeMasterModalBtn').onclick = closeMasterModal;
  $('masterVideoModal').onclick = (e) => {
    if (e.target === $('masterVideoModal')) closeMasterModal();
  };

  // 视频导演台固定使用列表视图（没有故事板，所以不再有视图切换按钮）。
  // Mode segmented toggle
  $('modeCutBtn').onclick = () => {
    setModeUI('CUT');
    saveCurrentShot();
  };
  $('modeT2VBtn').onclick = () => {
    setModeUI('T2V');
    saveCurrentShot();
  };
  $('modeContinueBtn').onclick = () => {
    setModeUI('CONTINUE');
    saveCurrentShot();
  };

  // Seed helpers
  $('seedRandBtn').onclick = () => {
    $('shotSeed').value = Math.floor(Math.random() * 100000000);
  };
  $('seed42Btn').onclick = () => {
    $('shotSeed').value = 42;
  };

  // Topbar Controls
  $('saveShotBtn').onclick = saveCurrentShot;
  $('previewOriginalBtn').onclick = () => state.currentShot && showVideo(state.currentShot, 'original');
  $('generateShotBtn').onclick = () => state.shotId && generateShot(state.shotId);
  $('videoMVMode').addEventListener('change', saveVideoMVMode);

  // Independent Scene Image Workbench. Scene is fixed by the left navigation.
  document.querySelectorAll('.image-role-tab').forEach((btn) => {
    btn.onclick = async () => {
      if (!state.imageSceneId) return;
      await selectImageScene(state.imageSceneId, btn.dataset.role);
    };
  });
  $('saveImageConfigBtn').onclick = () => saveImageConfig();
  // 右侧面板 = 当前选中的这个参考位。列表项上的「生成 / 重新生成」现在同时承担
  // 取消旧任务并重新提交的职责，所以这里不再需要单独的中断 / 重置按钮。
  $('queueCurrentRefBtn').onclick = queueCurrentImageRef;
  $('queueProjectMissingImagesBtn').onclick = queueCurrentSceneMissingImages;

  // 右侧生图参数属于本地编辑草稿。WebSocket/队列刷新只能更新状态和预览，
  // 不能把用户正在输入的 Prompt/Seed/比例重新覆盖成数据库里的旧值。
  $('imageRefScope').addEventListener('change', async () => {
    markImageEditorDirty();
    if (await saveImageConfig({ silent: true })) await loadImageDashboard();
  });
  $('imageModel').addEventListener('change', () => {
    syncImageModelControls({ applyDefaults: true });
    markImageEditorDirty();
  });
  $('imagePrompt').addEventListener('input', markImageEditorDirty);
  $('imageRatio').addEventListener('change', markImageEditorDirty);
  $('imageSeed').addEventListener('input', markImageEditorDirty);
  $('imageSteps').addEventListener('input', markImageEditorDirty);
  $('imageCfg').addEventListener('input', markImageEditorDirty);
  $('imageLoraMode').addEventListener('change', () => {
    syncImageModelControls({ applyDefaults: true });
    markImageEditorDirty();
  });

  // New image task: queue stays in the center; Scene / Ref / prompt are configured on the right.
  $('imageAddRefBtn').onclick = async () => {
    const scenes = state.imageDashboard?.scenes || [];
    if (!scenes.length) return showToast('Create a Scene first', 'warning');
    const scene = getImageSceneData() || scenes[0];
    const existingRoles = new Set(
      (state.imageQueue?.jobs || [])
        .filter((job) => Number(job.scene_id) === Number(scene.id))
        .map((job) => job.role)
    );
    const emptyRole = IMAGE_ROLES.find((role) => !existingRoles.has(role));
    if (!emptyRole) {
      return showToast(`${scene.name} ’s Ref1~Ref4 are already all in the list`, 'info');
    }
    try {
      await api(`/api/image-queue/slots/${scene.id}/${emptyRole}`, { method: 'POST', body: '{}' });
      await refreshImageQueueStatus();
      await selectImageScene(scene.id, emptyRole);
      $('imagePrompt')?.focus();
      $('imageInspector')?.scrollTo({ top: 0, behavior: 'smooth' });
      showToast(`Created ${scene.name} / ${sceneImageRoleLabel(emptyRole)}; a row was added to the list`, 'success');
    } catch (e) {
      showToast(`Creating the image task failed: ${e.message}`, 'error');
    }
  };

  // Seed controls for image inspector
  $('imageSeedRandBtn').onclick = () => {
    $('imageSeed').value = Math.floor(Math.random() * 100000000);
    markImageEditorDirty();
  };
  $('imageSeed42Btn').onclick = () => {
    $('imageSeed').value = 42;
    markImageEditorDirty();
  };

  // Stage interactions (Zoom, Download, Upload)
  $('imageStageZoomBtn').onclick = () => {
    const img = getImageRoleData()?.latest_image;
    if (img) openLightbox(`${img.media_url}?t=${Date.now()}`, `${getImageSceneData()?.name || 'Scene'} · ${sceneImageRoleLabel(state.imageRole)}`);
  };
  $('imageStageDownloadBtn').onclick = () => {
    const img = getImageRoleData()?.latest_image;
    if (img) {
      const a = document.createElement('a');
      a.href = img.media_url;
      a.download = `${getImageSceneData()?.name || 'scene'}_${state.imageRole}.png`;
      a.click();
    }
  };
  $('imageUploadInput').onchange = (e) => {
    if (e.target.files?.[0]) uploadSceneImageFile(e.target.files[0]);
    e.target.value = '';
  };
  $('imageStageReplaceInput').onchange = (e) => {
    if (e.target.files?.[0]) uploadSceneImageFile(e.target.files[0]);
    e.target.value = '';
  };

  $('settingsBtn').onclick = openSettings;
  $('closeSettingsBtn').onclick = () => $('settingsModal').classList.add('hidden');
  $('saveSettingsBtn').onclick = saveSettings;
  $('testConnectionBtn').onclick = async () => {
    showToast('Testing the connection to ComfyUI...', 'info');
    await refreshStatus();
    showToast('Connectivity test finished', 'success');
  };

  $('refreshStatusBtn').onclick = async () => {
    showToast('Re-checking...', 'info', 1000);
    await refreshStatus();
  };
  // Error copy
  $('copyErrorBtn').onclick = () => {
    navigator.clipboard.writeText($('shotErrorMsg').textContent);
    showToast('Error copied to clipboard', 'success');
  };

  // Init slot dropzones
  initSlotInteractions();
}

// ==========================================
// Initialization
// ==========================================
(async function init() {
  initEvents();
  shotDrafts.init();
  try {
    await videoModelCatalog.load();
    await loadImageModelOptions();
  }
  catch (error) {
    showToast(`Loading the model catalog failed: ${error.message}; please reload the page`, 'error', 10000);
    return;
  }
  await loadProjects();
  // 视频导演台固定使用列表视图：首屏直接展开，不需要再点击「列表」。
  await setWorkspaceMode('video');
  await refreshStatus();
  connectAppWs();
  setInterval(refreshStatus, 8000);
  setInterval(refreshVideoProgress, 3000);
  setInterval(refreshShotElapsed, 1000);
})();

// Continuation selection uses existing generation/reference UI; no second prompt workflow.
function compactModelResult(data, zeroBased=false) {
  const names=['First is better','Second is better','Both similar','Neither fits','Undecided'];
  return {
    status:data.status, recommended_image:data.recommended==null?null:'#'+(Number(data.recommended)+(zeroBased?1:0))+'',
    rounds_completed:data.completed, rounds_total:data.total, elapsed_seconds:data.elapsed_seconds,
    comparisons:(data.comparisons||[]).map(c=>({
      image:c.first+' vs '+c.second,
      result:c.winner===0?'#'+c.first+'  is better':c.winner===1?'#'+c.second+'  is better':names[c.winner],
      probabilities:Object.fromEntries(names.map((name,i)=>[name,((c.probabilities[i]||0)*100).toFixed(2)+'%'])),
      elapsed_seconds:c.elapsed_seconds??null,
      input_tokens:c.usage?.prompt_tokens??null,
      cached_tokens:c.usage?.prompt_tokens_details?.cached_tokens??c.timings?.cache_n??null
    }))
  };
}

let continuationRequest = 0;
$('continuationResponseBtn').onclick=()=>{$('continuationResponsePanel').hidden=!$('continuationResponsePanel').hidden;};
$('copyContinuationResponse').onclick=async()=>{try{await navigator.clipboard.writeText($('continuationResponseJson').value);showToast('Model JSON copied','success');}catch{$('continuationResponseJson').select();showToast('Press Ctrl+C to copy','info');}};
function updateContinuationProgress(label, completed=null) {
  const box=$('continuationProgressBox'), bar=$('continuationProgress');
  box.hidden=false;$('continuationProgressLabel').textContent=label;
  if(completed===null)bar.removeAttribute('value');else bar.value=completed;
}
async function continuationAction(action) {
  const requestId = ++continuationRequest;
  $('continuationResponseBtn').hidden=true;$('continuationResponsePanel').hidden=true;$('continuationResponseJson').value='';
  const shotId = state.shotId;
  if (!shotId) return;
  const button = $('extractContinuationFrames');
  button.disabled = true;
  try {
    const body = new FormData();
    const custom = '';
    try {
      const inherited = await api(`/api/shots/${shotId}/continuation/inherit-refs`, {method:'POST'});
      if(state.shotId !== shotId) return;
      state.currentShot=inherited;
      const index=state.shots.findIndex(s=>Number(s.id)===Number(shotId));
      if(index>=0)state.shots[index]=inherited;
      await renderRefSlots(inherited,state.shotSelectSeq);
    } catch(e) {
      if(!e.message.includes('The previous shot has no reusable image, or this reference slot is already filled')) throw e;
    }
    updateContinuationProgress('Extracting candidate frames from the end…');
    $('continuationStatus').textContent = 'Extracting frames locally…';
    const result = await api(`/api/shots/${shotId}/continuation/candidates`, {method:'POST',body});
    if (state.shotId !== shotId) return;
    const list = $('continuationCandidates'); list.replaceChildren();list.dataset.shotId=String(shotId);
    for (const item of result.candidates) {
      const card = document.createElement('div');card.className='continuation-candidate';card.dataset.index=item.index;
      const image = document.createElement('img');image.src=item.url;image.alt=`${item.time}s continuation frame`;
      const label = document.createElement('span');
      label.textContent = `${Number(item.time).toFixed(3)} s${item.index === result.recommended ? ' · model pick' : (item.usable === false ? ' · too dark/too bright/blurry, please check' : '')}`;
      const zoom=document.createElement('button');zoom.type='button';zoom.className='btn btn-secondary';zoom.textContent='Zoom in';
      zoom.onclick=()=>openLightbox(item.url,`Previous shot ${Number(item.time).toFixed(3)} s candidate frame`);
      image.style.cursor='zoom-in';image.onclick=zoom.onclick;
      const use=document.createElement('button');use.type='button';use.className='btn btn-secondary';use.textContent='Use this frame';
      const actions=document.createElement('div');actions.className='continuation-card-actions';actions.append(zoom,use);
      card.append(image,label,actions);
      use.onclick = async () => {
        if (state.shotId !== shotId) return;
        continuationRequest++;
        updateContinuationProgress('Applying the selected frame…');
        use.disabled=true;
        try {
          const selected = new FormData();selected.append('batch',result.batch);selected.append('index',item.index);
          await api(`/api/shots/${shotId}/continuation/select`,{method:'POST',body:selected});
          updateContinuationProgress('✓ Selected frame applied',3);
          if (state.shotId === shotId) {
            await selectShot(shotId);
            for(const candidate of list.children){const chosen=Number(candidate.dataset.index)===item.index;candidate.classList.toggle('continuation-selected',chosen);candidate.querySelector('.continuation-card-actions button:last-child').textContent=chosen?'✓ In use':'Use this frame';}
          }
        } catch (e) { $('continuationStatus').textContent=e.message; }
        finally {use.disabled=false;}
      };
      list.append(card);
    }
    $('continuationStatus').textContent=custom ? 'Frame extracted; click to use it.' : 'Extracted the last four frames, starting imajev comparison; you can also click to pick manually.';
    if (custom) updateContinuationProgress('✓ Frame extracted; click the image to use it',3);
    if (!custom && result.candidates.length === 4) {
      updateContinuationProgress('Candidate frames ready; preparing the model…');
      try {
        const recommendBody = new FormData();recommendBody.append('batch',result.batch);
        const job = await api(`/api/shots/${shotId}/continuation/recommend`,{method:'POST',body:recommendBody});
        while(state.shotId === shotId && requestId === continuationRequest) {
          const progress = await api(`/api/shots/${shotId}/continuation/recommend/${job.id}`);
          if(state.shotId !== shotId || requestId !== continuationRequest) return;
          $('continuationResponseBtn').hidden=false;$('continuationResponseJson').value=JSON.stringify(compactModelResult(progress,true),null,2);
          $('continuationStatus').textContent=`${progress.stage} · ${progress.completed}/3`;
          updateContinuationProgress(`${progress.stage} · done ${progress.completed}/3 rounds`,progress.completed);
          if(progress.status === 'error') throw Error(progress.stage);
          if(progress.status === 'done') {
            updateContinuationProgress('✓ Comparison done · 3/3 rounds'+(progress.elapsed_seconds?' · '+progress.elapsed_seconds+'s':''),3);
            for(const card of list.children) {
              const index=Number(card.dataset.index),item=result.candidates.find(c=>c.index===index);
              card.classList.toggle('model-recommended',index===progress.recommended);
              card.querySelector('span').textContent=`${Number(item.time).toFixed(3)} s${index===progress.recommended?' · model pick':''}`;
            }
            $('continuationStatus').textContent=progress.recommended != null ? 'Green outline is the model recommendation. All four are clickable; nothing is applied to this Shot until you confirm.' : 'No reliable model recommendation. Review the four candidates and pick manually.';
            break;
          }
          await new Promise(resolve=>setTimeout(resolve,1500));
        }
      } catch(e) {if(state.shotId===shotId && requestId===continuationRequest){updateContinuationProgress('Recommendation unfinished; pick manually',0);$('continuationStatus').textContent=e.message+'; the four candidates can still be picked manually.';}}
    }
  } catch (e) { if (state.shotId === shotId) {updateContinuationProgress('Operation incomplete',0);$('continuationStatus').textContent=e.message;} }
  finally {button.disabled=false;}
}
document.getElementById('extractContinuationFrames').onclick=()=>continuationAction('extract');


function updateLightx2vHint() {
  videoModelCatalog.renderParameters();
}
$('shotSteps').addEventListener('change', updateLightx2vHint);
$('shotVideoModel').addEventListener('change', () => {
  const model = videoModelCatalog.profile($('shotVideoModel').value);
  if (model?.execution_backend === 'pika') {
    const seconds = Number($('shotSeconds').value);
    if (!Number.isInteger(seconds) || seconds < 5 || seconds > 15) $('shotSeconds').value = 5;
  }
  updateLightx2vHint();
  if (state.mvProject) renderVideoMVMode(state.mvProject);
  void refreshStatus();
});
if ($('shotPikaResolution')) $('shotPikaResolution').addEventListener('change', () => {
  $('shotMP').value = $('shotPikaResolution').value;
  $('shotMP').dispatchEvent(new Event('input', { bubbles: true }));
});
if ($('shotSecondPass')) $('shotSecondPass').addEventListener('change', updateLightx2vHint);
if ($('shotSecondPassRes')) $('shotSecondPassRes').addEventListener('change', updateLightx2vHint);
if ($('openTerminalLogBtn')) {
  $('openTerminalLogBtn').addEventListener('click', () => {
    const panel = $('terminalLogPanel');
    if (!panel) return;
    const willOpen = panel.classList.contains('hidden');
    panel.classList.toggle('hidden', !willOpen);
    $('openTerminalLogBtn').classList.toggle('btn-primary', willOpen);
    $('openTerminalLogBtn').classList.toggle('btn-secondary', !willOpen);
    if (willOpen) void loadTerminalLog(true);
  });
}
if ($('closeTerminalLogBtn')) {
  $('closeTerminalLogBtn').addEventListener('click', () => {
    $('terminalLogPanel')?.classList.add('hidden');
    if ($('openTerminalLogBtn')) {
      $('openTerminalLogBtn').classList.remove('btn-primary');
      $('openTerminalLogBtn').classList.add('btn-secondary');
    }
  });
}
if ($('refreshFullLogBtn')) {
  $('refreshFullLogBtn').addEventListener('click', () => void loadTerminalLog(true));
}
if ($('terminalLogShotSelect')) {
  $('terminalLogShotSelect').addEventListener('change', () => void loadTerminalLog(true));
}
if ($('copyFullLogBtn')) {
  $('copyFullLogBtn').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('full-logs')?.textContent || '');
      showToast('Terminal log copied to clipboard', 'success');
    } catch {
      showToast('Copy failed; select and copy manually', 'error');
    }
  });
}
if ($('log-details')) {
  $('log-details').addEventListener('toggle', () => {
    if ($('log-details').open) void loadTerminalLog(true);
  });
}
setInterval(() => {
  if (state.workspaceMode === 'video' && ((!$('terminalLogPanel')?.classList.contains('hidden')) || $('log-details')?.open)) {
    void loadTerminalLog();
  }
}, 2500);
if ($('sceneSecondPassCheck')) {
  $('sceneSecondPassCheck').addEventListener('change', async (e) => {
    if (!state.sceneId) return;
    const enabled = Boolean(e.target.checked);
    try {
      await api(`/api/scenes/${state.sceneId}/second-pass`, {
        method: 'POST',
        body: JSON.stringify({ second_pass_enabled: enabled }),
      });
      for (const s of state.shots) {
        s.second_pass_enabled = enabled && videoModelCatalog.profile(s.video_model)?.supports_second_pass !== false ? 1 : 0;
      }
      if (state.currentShot) {
        const currentEnabled = enabled && videoModelCatalog.profile(state.currentShot.video_model)?.supports_second_pass !== false;
        state.currentShot.second_pass_enabled = currentEnabled ? 1 : 0;
        if ($('shotSecondPass')) $('shotSecondPass').value = currentEnabled ? '1' : '0';
        updateLightx2vHint();
        shotDrafts.capture();
      }
      renderShots();
      showToast(enabled ? 'Shots in this Scene that support second pass now use Open-H3 1080p refine' : 'Second pass disabled for this Scene', 'success');
    } catch (err) {
      e.target.checked = !enabled;
      showToast(`Toggling second pass failed: ${err.message}`, 'error');
    }
  });
}

$('restoreAutoContinuation').onclick = async () => {
  if (!state.shotId) return;
  const shotId = state.shotId;
  try {
    await saveCurrentShot();
    await api(`/api/shots/${shotId}/continuation/auto`, {method:'POST'});
    if (state.shotId === shotId) {
      if ($('continuationCandidates')) $('continuationCandidates').replaceChildren();
      await selectShot(shotId);
    }
    showToast('Back to auto-continuation from the previous last frame', 'success');
  } catch (e) { showToast(e.message, 'error'); }
};

$('projectSearch').addEventListener('input', renderProjects);

$('openProjectPicker').onclick = () => {
  $('projectSearch').value = '';
  renderProjects();
  $('projectPickerDialog').showModal();
  $('projectSearch').focus();
  $('projectList').querySelector('.active')?.scrollIntoView({block:'nearest'});
};
$('projectPickerDialog').addEventListener('click', event => {
  if (event.target !== event.currentTarget) return;
  const rect = event.currentTarget.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.currentTarget.close();
});

// ==========================================
// ✨ 16:9 极简口播包装工作台 (0s实时交互预览 + 可视化卡片编辑 + 一键导出 MP4)
// ==========================================
const kouboState = {
  sceneId: null,
  config: null,
  timeline: null,
  lines: [],
  activeSlideIdx: 0,
  lastRenderedSlideKey: '',
  pollTimer: null,
  tab: 'live',
};

function escHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function buildKouboLines(tl, highlights) {
  const out = [];
  const segs = tl?.segs || [];
  segs.forEach((s, si) => {
    const chars = s.chars || {};
    const text = s.text || '';
    const tAt = (i) => {
      let k = i;
      while (chars[String(k)] === undefined && k < text.length) k++;
      return chars[String(k)] ?? s.end;
    };
    const parts = [];
    let buf = '';
    let b0 = 0;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if ('，。、：；！？'.includes(c)) {
        if (buf.trim()) parts.push([buf, b0]);
        buf = '';
        b0 = i + 1;
        continue;
      }
      if (!buf) b0 = i;
      buf += c;
    }
    if (buf.trim()) parts.push([buf, b0]);
    parts.forEach(([p, o], k) => {
      const nx = parts[k + 1];
      out.push({
        text: p.trim(),
        start: k === 0 && si === 0 ? 0.0 : tAt(o),
        end: nx ? tAt(nx[1]) - 0.03 : s.end,
      });
    });
  });
  return out;
}

function fitKouboLiveStage() {
  const outer = $('kouboLiveStageOuter');
  const stage = $('kouboLiveStage');
  if (!outer || !stage) return;
  const r = outer.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const scale = Math.min((r.width - 12) / 1920, (r.height - 12) / 1080);
  stage.style.transform = `translate(-50%, -50%) scale(${Math.max(0.15, scale)})`;
}

window.addEventListener('resize', fitKouboLiveStage);

function renderKouboSlideDom(s) {
  const CMAP = { orange: '#F7931E', green: '#1FA05B', red: '#E5484D', blue: '#3366EE', ink: '#1E1D1A' };
  const INK = '#1E1D1A';
  const MUTE = '#7A776E';
  const GREEN = '#1FA05B';
  const RED = '#E5484D';
  const OR = '#F7931E';
  const LINE = 'rgba(30,29,26,.09)';
  const chip = (txt, bg = 'rgba(30,29,26,.08)', col = INK, sz = 21) =>
    `<span style="display:inline-block;font-weight:700;font-size:${sz}px;padding:6px 16px;border-radius:10px;background:${bg};color:${col};white-space:nowrap">${escHtml(txt)}</span>`;

  if (s.type === 'strike-flow') {
    return `
      <div style="position:absolute;left:680px;top:150px;width:1120px">
        <div>${chip(s.tag, 'rgba(30,29,26,.08)', INK, 22)}</div>
        <div style="font-weight:900;font-size:74px;color:${INK};margin-top:18px;letter-spacing:-0.02em">${escHtml(s.title)}</div>
      </div>
      <div style="position:absolute;left:680px;top:340px;width:1100px;height:220px;padding:32px 48px;background:#fff;border-radius:28px;border:1px solid ${LINE};box-shadow:0 20px 55px rgba(30,30,22,.08);display:flex;align-items:center;justify-content:space-between;box-sizing:border-box">
        <div style="display:flex;flex-direction:column;align-items:flex-start">
          ${chip(s.oldLabel, 'rgba(30,29,26,.06)', MUTE, 20)}
          <div style="position:relative;display:inline-block;margin-top:14px">
            <div style="font-weight:900;font-size:64px;color:${MUTE}">${escHtml(s.oldText)}</div>
            <div style="position:absolute;left:-10px;top:52%;width:104%;height:10px;background:${RED};border-radius:99px;transform:rotate(-2deg);box-shadow:0 4px 14px rgba(229,72,77,.4)"></div>
          </div>
        </div>
        <div style="transform:rotate(-8deg);border:5px solid ${RED};color:${RED};font-weight:900;font-size:48px;padding:8px 28px;border-radius:16px;background:rgba(229,72,77,.08);letter-spacing:0.06em">× ${escHtml(s.stamp)}</div>
      </div>
      <div style="position:absolute;left:680px;top:600px;width:1100px;height:245px;padding:34px 48px;background:#181816;border-radius:28px;border:2px solid ${GREEN};box-shadow:0 20px 55px rgba(30,30,22,.14);display:flex;align-items:center;justify-content:space-between;box-sizing:border-box">
        <div style="display:flex;flex-direction:column;align-items:flex-start">
          ${chip(s.newLabel, 'rgba(31,160,91,.22)', '#4ADE80', 20)}
          <div style="font-weight:900;font-size:68px;color:#fff;margin-top:14px;display:flex;align-items:center;gap:22px">
            <span style="color:#4ADE80">➔</span><span>${escHtml(s.newText)}</span>
          </div>
        </div>
        <div style="font-weight:700;font-size:28px;color:#4ADE80;background:rgba(31,160,91,.14);padding:14px 26px;border-radius:14px">${escHtml(s.newSub)}</div>
      </div>
    `;
  }

  if (s.type === 'spotlight-shot') {
    const imgSrc = `/api/koubo-pack/public/${encodeURIComponent(s.image || '')}?t=${Date.now()}`;
    const statsHtml = (s.stats || []).map((st) => {
      const isAcc = Boolean(st.accent);
      const c = CMAP[st.color] || INK;
      const numCol = isAcc ? (st.color === 'red' ? '#FF6B6B' : '#4ADE80') : c;
      return `
        <div style="background:${isAcc ? '#181816' : '#F6F7F2'};border-radius:22px;padding:24px 34px;border:${isAcc ? `2px solid ${c}` : `1px solid ${LINE}`};display:flex;flex-direction:column;justify-content:center;height:170px;box-sizing:border-box">
          <div style="font-weight:900;font-size:60px;color:${numCol};line-height:1.05;white-space:nowrap">${escHtml(st.num)}</div>
          <div style="font-weight:700;font-size:25px;color:${isAcc ? 'rgba(255,255,255,.85)' : MUTE};margin-top:10px;white-space:nowrap">${escHtml(st.label)}</div>
        </div>
      `;
    }).join('');
    const stampHtml = s.stamp
      ? `<div style="position:absolute;right:28px;bottom:28px;transform:rotate(-6deg);background:${RED};color:#fff;font-weight:900;font-size:38px;padding:10px 28px;border-radius:14px;box-shadow:0 12px 30px rgba(229,72,77,.45)">${escHtml(s.stamp)}</div>`
      : '';
    return `
      <div style="position:absolute;left:70px;top:145px;width:1340px;height:735px;padding:38px 46px;background:#fff;border-radius:28px;border:1px solid ${LINE};box-shadow:0 20px 55px rgba(30,30,22,.08);box-sizing:border-box">
        <div style="display:flex;align-items:center;justify-content:space-between">
          <div style="display:flex;align-items:center;gap:16px">
            ${chip(s.tag, 'rgba(30,29,26,.08)', INK, 21)}
            <span style="font-weight:900;font-size:44px;color:${INK}">${escHtml(s.title)}</span>
          </div>
          ${s.spotlightTag ? `<span style="font-weight:700;font-size:18px;color:${MUTE};letter-spacing:0.08em">${escHtml(s.spotlightTag)}</span>` : ''}
        </div>
        <div style="display:flex;gap:34px;margin-top:28px;height:560px">
          <div style="flex:1.45;position:relative;height:100%;border-radius:22px;overflow:hidden;background:${s.imageBg || '#0B0C0E'};border:2px solid ${LINE}">
            <img src="${imgSrc}" style="width:100%;height:100%;object-fit:${s.imageFit || 'cover'}" />
            ${stampHtml}
          </div>
          <div style="flex:0.95;display:flex;flex-direction:column;justify-content:space-between">${statsHtml}</div>
        </div>
      </div>
    `;
  }

  if (s.type === 'scale-compare') {
    const itemsHtml = (s.items || []).map((it) => {
      const isAcc = Boolean(it.accent);
      const c = CMAP[it.color] || INK;
      return `
        <div style="background:${isAcc ? '#181816' : '#F6F7F2'};border-radius:22px;padding:24px 36px;border:${isAcc ? `2px solid ${RED}` : `1px solid ${LINE}`};box-sizing:border-box">
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:16px">
            <div style="display:flex;align-items:center;gap:14px">
              <span style="font-weight:700;font-size:27px;color:${isAcc ? '#fff' : INK}">${escHtml(it.tag)}</span>
              <span style="font-weight:600;font-size:23px;color:${isAcc ? '#FF8A8A' : MUTE}">· ${escHtml(it.sub)}</span>
            </div>
            <span style="font-weight:900;font-size:54px;color:${isAcc ? '#FF6B6B' : c};line-height:1">${escHtml(it.big)}</span>
          </div>
          <div style="width:100%;height:24px;background:${isAcc ? 'rgba(255,255,255,.12)' : '#E6E5DE'};border-radius:999px;overflow:hidden">
            <div style="width:${Math.max(2, Number(it.pct) || 10)}%;height:100%;background:${isAcc ? RED : c};border-radius:999px"></div>
          </div>
        </div>
      `;
    }).join('');
    return `
      <div style="position:absolute;left:70px;top:145px;width:1340px;height:735px;padding:42px 52px;background:#fff;border-radius:28px;border:1px solid ${LINE};box-shadow:0 20px 55px rgba(30,30,22,.08);box-sizing:border-box">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div style="display:flex;align-items:center;gap:16px">
            ${chip(s.tag, 'rgba(30,29,26,.08)', INK, 21)}
            <span style="font-weight:900;font-size:44px;color:${INK}">${escHtml(s.title)}</span>
          </div>
          <div style="font-weight:900;font-size:44px;color:#fff;background:${RED};padding:10px 30px;border-radius:16px;box-shadow:0 10px 28px rgba(229,72,77,.35)">${escHtml(s.badge)}</div>
        </div>
        <div style="margin-top:38px;display:flex;flex-direction:column;justify-content:space-between;height:530px">${itemsHtml}</div>
      </div>
    `;
  }

  return `
    <div style="position:absolute;left:680px;top:150px;width:1120px">
      <div>${chip(s.tag, 'rgba(30,29,26,.08)', INK, 22)}</div>
      <div style="font-weight:900;font-size:68px;color:${INK};margin-top:18px">${escHtml(s.title)}</div>
    </div>
    <div style="position:absolute;left:680px;top:340px;width:1100px;height:340px;display:flex;align-items:center;gap:28px">
      <div style="flex:0.9;height:100%;padding:42px 40px;background:#fff;border-radius:28px;border:1px solid ${LINE};display:flex;flex-direction:column;justify-content:center;align-items:flex-start;box-sizing:border-box">
        ${chip(s.fromTag, 'rgba(30,29,26,.06)', MUTE, 20)}
        <div style="position:relative;display:inline-block;margin-top:24px">
          <div style="font-weight:900;font-size:54px;color:${MUTE}">${escHtml(s.fromBig)}</div>
          <div style="position:absolute;left:-6px;top:52%;width:102%;height:8px;background:${RED};border-radius:99px"></div>
        </div>
        <div style="font-weight:600;font-size:26px;color:${MUTE};margin-top:16px">${escHtml(s.fromSub)}</div>
      </div>
      <div style="font-weight:900;font-size:56px;color:${GREEN}">➔</div>
      <div style="flex:1.15;height:100%;padding:42px 44px;background:#181816;border-radius:28px;border:3px solid ${GREEN};display:flex;flex-direction:column;justify-content:center;align-items:flex-start;box-sizing:border-box">
        ${chip(s.toTag, 'rgba(31,160,91,.22)', '#4ADE80', 20)}
        <div style="font-weight:900;font-size:60px;color:#fff;margin-top:24px">${escHtml(s.toBig)}</div>
        <div style="font-weight:700;font-size:26px;color:#4ADE80;margin-top:16px">${escHtml(s.toSub)}</div>
      </div>
    </div>
    <div style="position:absolute;left:680px;top:725px;width:1100px;height:130px;padding:0 46px;background:#fff;border-radius:28px;border:1px solid ${LINE};display:flex;align-items:center;justify-content:space-between;box-sizing:border-box">
      <div style="font-weight:900;font-size:40px;color:${INK}">${escHtml(s.ctaText)}</div>
      <div style="font-weight:800;font-size:30px;color:#fff;background:${OR};padding:16px 38px;border-radius:999px">${escHtml(s.ctaBtn)}</div>
    </div>
  `;
}

function updateKouboStageAtTime(t, forceDom = false) {
  const cfg = kouboState.config;
  if (!cfg || !Array.isArray(cfg.slides) || !cfg.slides.length) return;
  const slides = cfg.slides;
  let idx = slides.findIndex((s) => t >= s.start && t < s.end);
  if (idx < 0) idx = slides.length - 1;
  const s = slides[idx];

  if (idx !== kouboState.activeSlideIdx) {
    kouboState.activeSlideIdx = idx;
    renderKouboSlideTabs();
    renderKouboEditorForm();
  }

  // TopBar
  const topBar = $('kouboStageTopBar');
  if (topBar) {
    const stepsHtml = (cfg.steps || []).map((st, k) => {
      const on = k === s.step;
      const done = s.step > k;
      return `<div style="font-weight:600;font-size:16px;padding:6px 14px;border-radius:999px;color:${on ? '#fff' : done ? '#1FA05B' : '#7A776E'};background:${on ? '#1E1D1A' : done ? 'rgba(31,160,91,.10)' : 'rgba(30,29,26,.05)'}">${escHtml(st)}</div>`;
    }).join('');
    topBar.innerHTML = `
      <div style="display:flex;align-items:center;gap:14px">
        <span style="font-weight:800;font-size:25px;color:#1E1D1A">${escHtml(cfg.brand)}</span>
        <span style="font-weight:700;font-size:18px;color:#fff;background:#1E1D1A;padding:4px 14px;border-radius:8px">${escHtml(cfg.badge)}</span>
      </div>
      <div style="display:flex;gap:8px">${stepsHtml}</div>
    `;
  }

  // Host Box Position (FULL <-> PIP)
  const hostEl = $('kouboStageHost');
  if (hostEl) {
    if (s.shot === 'pip') {
      hostEl.style.left = '1468px';
      hostEl.style.top = '230px';
      hostEl.style.width = '390px';
      hostEl.style.height = '556px';
    } else {
      hostEl.style.left = '130px';
      hostEl.style.top = '185px';
      hostEl.style.width = '480px';
      hostEl.style.height = '685px';
    }
  }
  if ($('kouboStageHostLabel')) $('kouboStageHostLabel').textContent = cfg.hostLabel || 'nana · compute watch';

  // Slide Card DOM
  const slideKey = `${idx}:${JSON.stringify(s)}`;
  if (forceDom || slideKey !== kouboState.lastRenderedSlideKey) {
    kouboState.lastRenderedSlideKey = slideKey;
    if ($('kouboStageSlide')) $('kouboStageSlide').innerHTML = renderKouboSlideDom(s);
  }

  // Subtitles
  const subsWrap = $('kouboStageSubsWrap');
  const subsEl = $('kouboStageSubs');
  if (subsWrap && subsEl) {
    subsWrap.style.left = s.shot === 'pip' ? '70px' : '0px';
    subsWrap.style.width = s.shot === 'pip' ? '1340px' : '1920px';
    const ln = kouboState.lines.find((l) => t >= l.start - 0.08 && t < l.end + 0.25);
    if (!ln) {
      subsEl.style.display = 'none';
    } else {
      subsEl.style.display = 'block';
      let rest = ln.text;
      const segs = [];
      const hl = cfg.highlights || [];
      while (rest) {
        let best = -1;
        let w = '';
        for (const h of hl) {
          const i = rest.indexOf(h);
          if (i >= 0 && (best < 0 || i < best || (i === best && h.length > w.length))) {
            best = i;
            w = h;
          }
        }
        if (best < 0) {
          segs.push({ s: rest, h: false });
          break;
        }
        if (best > 0) segs.push({ s: rest.slice(0, best), h: false });
        segs.push({ s: w, h: true });
        rest = rest.slice(best + w.length);
      }
      subsEl.innerHTML = segs.map((x) => `<span style="color:${x.h ? '#FFB347' : '#fff'}">${escHtml(x.s)}</span>`).join('');
    }
  }

  const tot = Number(kouboState.timeline?.total || 80);
  if ($('kouboTimelineScrub')) $('kouboTimelineScrub').value = String(t);
  if ($('kouboTimeReadout')) $('kouboTimeReadout').textContent = `${t.toFixed(1)}s / ${tot.toFixed(1)}s`;
}

function renderKouboSlideTabs() {
  const wrap = $('kouboSlideTabs');
  if (!wrap || !kouboState.config) return;
  const slides = kouboState.config.slides || [];
  const steps = kouboState.config.steps || [];
  wrap.innerHTML = slides.map((s, i) => {
    const active = i === kouboState.activeSlideIdx;
    const label = steps[i] || `Act ${i + 1}`;
    return `<button type="button" data-slide-idx="${i}" class="btn btn-xs" style="background:${active ? '#f59e0b' : '#1e293b'};color:${active ? '#111' : '#cbd5e1'};font-weight:${active ? '700' : '500'};border:none;padding:5px 12px;border-radius:6px;cursor:pointer">${escHtml(label)} (${s.start}s)</button>`;
  }).join('');
  wrap.querySelectorAll('button[data-slide-idx]').forEach((btn) => {
    btn.onclick = () => {
      const idx = Number(btn.dataset.slideIdx);
      const sl = kouboState.config.slides[idx];
      if (!sl) return;
      kouboState.activeSlideIdx = idx;
      const vid = $('kouboStageHostVideo');
      const targetT = Number(sl.start) + 0.5;
      if (vid) vid.currentTime = targetT;
      updateKouboStageAtTime(targetT, true);
      renderKouboSlideTabs();
      renderKouboEditorForm();
    };
  });
}

function renderKouboEditorForm() {
  const form = $('kouboEditorForm');
  if (!form || !kouboState.config) return;
  const idx = kouboState.activeSlideIdx;
  const s = kouboState.config.slides?.[idx];
  if (!s) return;
  if ($('kouboActiveSlideBadge')) {
    $('kouboActiveSlideBadge').textContent = `Act ${idx + 1} · ${s.type} (${s.shot === 'pip' ? 'Right small window' : 'Left large window'})`;
  }

  const field = (label, key, val) => `
    <div style="display:flex;flex-direction:column;gap:4px">
      <label style="font-size:11px;color:#94a3b8">${escHtml(label)}</label>
      <input data-k-field="${key}" class="form-input" style="padding:6px 10px;font-size:13px" value="${escHtml(val ?? '')}" />
    </div>
  `;

  let html = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      ${field('Top-left label (tag)', 'tag', s.tag)}
      <div style="display:flex;flex-direction:column;gap:4px">
        <label style="font-size:11px;color:#94a3b8">Character window position (shot)</label>
        <select data-k-field="shot" class="form-input" style="padding:6px 10px;font-size:13px">
          <option value="full" ${s.shot === 'full' ? 'selected' : ''}>Left large window (full)</option>
          <option value="pip" ${s.shot === 'pip' ? 'selected' : ''}>Right small window (pip)</option>
        </select>
      </div>
    </div>
    ${field('Main title (title · ≤8 chars recommended)', 'title', s.title)}
  `;

  if (s.type === 'strike-flow') {
    html += `
      <div style="border-top:1px solid #263044;padding-top:8px;margin-top:4px;display:grid;grid-template-columns:1fr 1fr;gap:8px">
        ${field('Top card label (oldLabel)', 'oldLabel', s.oldLabel)}
        ${field('Red stamp text (stamp)', 'stamp', s.stamp)}
      </div>
      ${field('Crossed-out statement (oldText)', 'oldText', s.oldText)}
      <div style="border-top:1px solid #263044;padding-top:8px;margin-top:4px;display:grid;grid-template-columns:1fr 1fr;gap:8px">
        ${field('Bottom card label (newLabel)', 'newLabel', s.newLabel)}
        ${field('Green sub-label on the right (newSub)', 'newSub', s.newSub)}
      </div>
      ${field('Arrow statement (newText)', 'newText', s.newText)}
    `;
  } else if (s.type === 'spotlight-shot') {
    html += `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
        ${field('Evidence image filename (public/)', 'image', s.image)}
        ${field('Image stamp (stamp · optional)', 'stamp', s.stamp || '')}
      </div>
      <div style="font-size:12px;font-weight:600;color:#cbd5e1;margin-top:6px">Three key jump-text pills on the right:</div>
    `;
    (s.stats || []).forEach((st, si) => {
      html += `
        <div style="display:grid;grid-template-columns:1fr 1.3fr 0.8fr;gap:8px;background:#0e131d;padding:8px;border-radius:8px;border:1px solid #222b3d">
          ${field(`#${si + 1} huge number`, `stats.${si}.num`, st.num)}
          ${field(`#${si + 1} Minimal label (≤8 chars)`, `stats.${si}.label`, st.label)}
          ${field(`Cue word`, `stats.${si}.cue`, st.cue)}
        </div>
      `;
    });
  } else if (s.type === 'scale-compare') {
    html += field('Top-right red badge', 'badge', s.badge);
    html += `<div style="font-size:12px;font-weight:600;color:#cbd5e1;margin-top:6px">Three-tier contrast bars:</div>`;
    (s.items || []).forEach((it, ii) => {
      html += `
        <div style="display:grid;grid-template-columns:1.2fr 1fr 1fr 0.6fr;gap:6px;background:#0e131d;padding:8px;border-radius:8px;border:1px solid #222b3d">
          ${field(`#${ii + 1} name`, `items.${ii}.tag`, it.tag)}
          ${field(`#${ii + 1} sub-note`, `items.${ii}.sub`, it.sub)}
          ${field(`#${ii + 1} large number`, `items.${ii}.big`, it.big)}
          ${field(`bar %`, `items.${ii}.pct`, it.pct)}
        </div>
      `;
    });
  } else if (s.type === 'kinetic-flow') {
    html += `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
        ${field('Left card label (fromTag)', 'fromTag', s.fromTag)}
        ${field('Left card crossed-out text (fromBig)', 'fromBig', s.fromBig)}
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
        ${field('Right card label (toTag)', 'toTag', s.toTag)}
        ${field('Right card winner text (toBig)', 'toBig', s.toBig)}
      </div>
      <div style="display:grid;grid-template-columns:1.4fr 1fr;gap:8px">
        ${field('Closing copy (ctaText)', 'ctaText', s.ctaText)}
        ${field('Button text (ctaBtn)', 'ctaBtn', s.ctaBtn)}
      </div>
    `;
  }

  form.innerHTML = html;
  form.querySelectorAll('[data-k-field]').forEach((inp) => {
    inp.addEventListener('input', () => {
      const path = inp.dataset.kField.split('.');
      let cur = s;
      for (let i = 0; i < path.length - 1; i++) {
        cur = cur[path[i]];
      }
      const last = path[path.length - 1];
      cur[last] = last === 'pct' ? Number(inp.value) || 0 : inp.value;
      const vid = $('kouboStageHostVideo');
      updateKouboStageAtTime(vid ? vid.currentTime : s.start + 0.5, true);
    });
  });
}

function switchKouboTab(tab) {
  kouboState.tab = tab;
  const liveBtn = $('kouboTabLiveBtn');
  const videoBtn = $('kouboTabVideoBtn');
  const livePanel = $('kouboLivePanel');
  const videoPanel = $('kouboVideoPanel');
  if (tab === 'video') {
    if (liveBtn) { liveBtn.style.background = 'transparent'; liveBtn.style.color = '#94a3b8'; }
    if (videoBtn) { videoBtn.style.background = '#2563eb'; videoBtn.style.color = '#fff'; }
    if (livePanel) livePanel.classList.add('hidden');
    if (videoPanel) videoPanel.classList.remove('hidden');
    if ($('kouboStageHostVideo')) $('kouboStageHostVideo').pause();
    const rv = $('kouboRenderedVideo');
    if (rv && kouboState.sceneId) {
      rv.src = `/api/scenes/${kouboState.sceneId}/koubo-pack/video?t=${Date.now()}`;
    }
  } else {
    if (videoBtn) { videoBtn.style.background = 'transparent'; videoBtn.style.color = '#94a3b8'; }
    if (liveBtn) { liveBtn.style.background = '#2563eb'; liveBtn.style.color = '#fff'; }
    if (videoPanel) videoPanel.classList.add('hidden');
    if (livePanel) livePanel.classList.remove('hidden');
    if ($('kouboRenderedVideo')) $('kouboRenderedVideo').pause();
    setTimeout(fitKouboLiveStage, 30);
  }
}

async function openKouboPackModal() {
  if (!state.sceneId) {
    showToast('Select a scene first', 'warning');
    return;
  }
  try {
    const data = await api(`/api/scenes/${state.sceneId}/koubo-pack`);
    kouboState.sceneId = state.sceneId;
    kouboState.config = data.config;
    kouboState.timeline = data.timeline;
    kouboState.lines = buildKouboLines(data.timeline, data.config?.highlights || []);
    kouboState.activeSlideIdx = 0;
    kouboState.lastRenderedSlideKey = '';

    $('kouboPackModal').classList.remove('hidden');
    switchKouboTab('live');

    const tot = Number(data.timeline?.total || 80);
    if ($('kouboTimelineScrub')) {
      $('kouboTimelineScrub').max = String(tot);
      $('kouboTimelineScrub').value = '0';
    }
    const vid = $('kouboStageHostVideo');
    if (vid) {
      vid.src = `${data.master_url}?t=${Date.now()}`;
      vid.currentTime = 0;
    }
    if ($('kouboDownloadMp4Btn')) {
      $('kouboDownloadMp4Btn').href = data.download_url;
    }

    renderKouboSlideTabs();
    renderKouboEditorForm();
    setTimeout(() => {
      fitKouboLiveStage();
      updateKouboStageAtTime(0.5, true);
    }, 50);

    updateKouboRenderStatusUI(data.render_status);
    if (data.render_status?.status === 'RUNNING') {
      startKouboProgressPoll();
    }
  } catch (err) {
    showToast(`Loading the talking-head packaging workbench failed: ${err.message}`, 'error');
  }
}

function updateKouboRenderStatusUI(st) {
  if (!st) return;
  if ($('kouboRenderBar')) $('kouboRenderBar').style.width = `${st.progress || 0}%`;
  if ($('kouboRenderStatusText')) $('kouboRenderStatusText').textContent = st.stage || 'Ready';
  if ($('kouboRenderMp4Btn')) $('kouboRenderMp4Btn').disabled = st.status === 'RUNNING';
}

function startKouboProgressPoll() {
  if (kouboState.pollTimer) clearInterval(kouboState.pollTimer);
  kouboState.pollTimer = setInterval(async () => {
    if (!kouboState.sceneId || $('kouboPackModal').classList.contains('hidden')) {
      clearInterval(kouboState.pollTimer);
      return;
    }
    try {
      const st = await api(`/api/scenes/${kouboState.sceneId}/koubo-pack/progress`);
      updateKouboRenderStatusUI(st);
      if (st.status === 'DONE') {
        clearInterval(kouboState.pollTimer);
        showToast('16:9 talking-head packaging rendered!', 'success');
        switchKouboTab('video');
      } else if (st.status === 'FAILED') {
        clearInterval(kouboState.pollTimer);
        showToast(st.stage || 'Render failed', 'error');
      }
    } catch (_) {}
  }, 1500);
}

if ($('openKouboPackBtn')) $('openKouboPackBtn').onclick = openKouboPackModal;
if ($('closeKouboPackModalBtn')) {
  $('closeKouboPackModalBtn').onclick = () => {
    $('kouboPackModal').classList.add('hidden');
    if ($('kouboStageHostVideo')) $('kouboStageHostVideo').pause();
    if ($('kouboRenderedVideo')) $('kouboRenderedVideo').pause();
    if (kouboState.pollTimer) clearInterval(kouboState.pollTimer);
  };
}
if ($('kouboTabLiveBtn')) $('kouboTabLiveBtn').onclick = () => switchKouboTab('live');
if ($('kouboTabVideoBtn')) $('kouboTabVideoBtn').onclick = () => switchKouboTab('video');

if ($('kouboStageHostVideo')) {
  const vid = $('kouboStageHostVideo');
  vid.addEventListener('timeupdate', () => updateKouboStageAtTime(vid.currentTime));
  vid.addEventListener('play', () => { if ($('kouboPlayToggleBtn')) $('kouboPlayToggleBtn').textContent = '⏸ Pause'; });
  vid.addEventListener('pause', () => { if ($('kouboPlayToggleBtn')) $('kouboPlayToggleBtn').textContent = '▶ Play'; });
}
if ($('kouboPlayToggleBtn')) {
  $('kouboPlayToggleBtn').onclick = () => {
    const vid = $('kouboStageHostVideo');
    if (!vid) return;
    if (vid.paused) vid.play();
    else vid.pause();
  };
}
if ($('kouboTimelineScrub')) {
  $('kouboTimelineScrub').addEventListener('input', (e) => {
    const t = Number(e.target.value) || 0;
    const vid = $('kouboStageHostVideo');
    if (vid) vid.currentTime = t;
    updateKouboStageAtTime(t);
  });
}
if ($('kouboSaveConfigBtn')) {
  $('kouboSaveConfigBtn').onclick = async () => {
    if (!kouboState.sceneId || !kouboState.config) return;
    try {
      await api(`/api/scenes/${kouboState.sceneId}/koubo-pack`, {
        method: 'PUT',
        body: JSON.stringify({ config: kouboState.config }),
      });
      showToast('Card config saved to cards169.json', 'success');
    } catch (err) {
      showToast(`Save failed: ${err.message}`, 'error');
    }
  };
}
if ($('kouboRenderMp4Btn')) {
  $('kouboRenderMp4Btn').onclick = async () => {
    if (!kouboState.sceneId || !kouboState.config) return;
    try {
      await api(`/api/scenes/${kouboState.sceneId}/koubo-pack/render`, {
        method: 'POST',
        body: JSON.stringify({ config: kouboState.config }),
      });
      showToast('Background Remotion accelerated rendering started!', 'info');
      startKouboProgressPoll();
    } catch (err) {
      showToast(`Starting the render failed: ${err.message}`, 'error');
    }
  };
}

