/** Per-shot drafts survive navigation; saves are serialized and never repaint editors. */
const shotDrafts = (() => {
  const ids = ['shotTitle', 'shotVideoModel', 'shotSeconds', 'shotMP', 'shotSteps', 'shotExtraLora',
    'shotSecondPass', 'shotSecondPassRes', 'shotRatio', 'shotSeed', 'shotPrompt'];
  const drafts = new Map(), timers = new Map(), pending = new Map();
  let activeId = null;
  const key = id => `anime-studio:shot-draft:v1:${id}`;
  function read() {
    const form = Object.fromEntries(ids.filter(id => $(id)).map(id => [id, $(id).type === 'checkbox' ? $(id).checked : $(id).value]));
    form.mode = $('modeT2VBtn').classList.contains('active') ? 'T2V' : $('modeCutBtn').classList.contains('active') ? 'CUT' : 'CONTINUE';
    return form;
  }
  function remember(id, draft) {
    try { localStorage.setItem(key(id), JSON.stringify(draft)); }
    catch (error) { console.warn('Local draft write failed; still trying to auto-sync', error); }
  }
  function capture() {
    if (!activeId) return;
    const form = read(), old = drafts.get(activeId);
    if (old && JSON.stringify(old.form) === JSON.stringify(form)) return;
    const draft = { form, revision: (old?.revision || 0) + 1, dirty: true };
    drafts.set(activeId, draft);
    remember(activeId, draft);
    clearTimeout(timers.get(activeId));
    const id = activeId;
    timers.set(id, setTimeout(() => { void persist(id).catch(error => showToast(`Autosave incomplete; your edits are kept:${error.message}`, 'error', 5000)); }, 700));
  }
  function apply(form) {
    for (const id of ids) {
      if (!$(id)) continue;
      if ($(id).type === 'checkbox') $(id).checked = Boolean(form[id]);
      else $(id).value = form[id] ?? (id === 'shotSecondPassRes' ? '1k' : '');
    }
    setModeUI(form.mode);
    updateLightx2vHint();
    // Updating the model control regenerates options, so restore the chosen step last.
    if ([...$('shotSteps').options].some(option => option.value === form.shotSteps)) $('shotSteps').value = form.shotSteps;
    updateLightx2vHint();
  }
  function activate(id) {
    activeId = Number(id);
    const existing = drafts.get(activeId);
    if (!existing || !existing.dirty) {
      let saved;
      try { saved = JSON.parse(localStorage.getItem(key(activeId)) || 'null'); } catch {}
      drafts.set(activeId, saved?.form && saved?.dirty ? saved : { form: read(), revision: existing?.revision || 0, dirty: false });
    }
    const draft = drafts.get(activeId);
    apply(draft.form);
    if (draft.dirty) {
      clearTimeout(timers.get(activeId));
      const id = activeId;
      timers.set(id, setTimeout(() => { void persist(id).catch(() => {}); }, 700));
    }
  }
  function payload(form) {
    const number = id => {
      const value = Number(form[id]);
      if (form[id] === '' || !Number.isFinite(value)) throw new Error('Parameters are incomplete; the draft is kept');
      return value;
    };
    return { title: form.shotTitle, mode: form.mode, video_model: form.shotVideoModel,
      seconds: number('shotSeconds'), megapixels: number('shotMP'), steps: number('shotSteps'),
      second_pass_enabled: String(form.shotSecondPass) === '1',
      second_pass_resolution: String(form.shotSecondPassRes || '1k'),
      realism_lora_enabled: ['realism', 'both'].includes(form.shotExtraLora),
      naughty_lora_enabled: ['naughty', 'both'].includes(form.shotExtraLora),
      aspect_ratio: form.shotRatio, seed: number('shotSeed'), prompt: form.shotPrompt };
  }
  function persist(id) {
    clearTimeout(timers.get(id));
    const request = (pending.get(id) || Promise.resolve()).catch(() => {}).then(async () => {
      const draft = drafts.get(id);
      if (!draft?.dirty) return;
      const revision = draft.revision;
      const result = await api(`/api/shots/${id}`, { method: 'PATCH', body: JSON.stringify(payload(draft.form)) });
      // Update cached data only; late responses never alter the currently visible form.
      const row = state.shots.find(shot => Number(shot.id) === id);
      if (row) Object.assign(row, result);
      if (Number(state.currentShot?.id) === id) Object.assign(state.currentShot, result);
      if ($('sceneSecondPassCheck')) {
        $('sceneSecondPassCheck').checked = state.shots.length > 0 && state.shots.every(x => Number(x.second_pass_enabled) === 1);
      }
      renderShots();
      if (drafts.get(id)?.revision === revision) {
        draft.dirty = false;
        try { localStorage.removeItem(key(id)); } catch {}
      }
    });
    pending.set(id, request);
    return request;
  }
  async function saveCurrent() {
    capture();
    const id = activeId;
    if (!id) return false;
    // Include edits made while an earlier request was still in flight.
    do { await persist(id); } while (drafts.get(id)?.dirty);
    return true;
  }
  function deactivate() { capture(); activeId = null; }
  function discard(id) {
    clearTimeout(timers.get(Number(id)));
    drafts.delete(Number(id));
    try { localStorage.removeItem(key(id)); } catch {}
    if (activeId === Number(id)) activeId = null;
  }
  function init() {
    for (const type of ['input', 'change']) document.addEventListener(type, event => {
      if (ids.includes(event.target.id)) capture();
    });
    document.addEventListener('click', event => {
      if (event.target.closest('#modeCutBtn, #modeContinueBtn, #modeT2VBtn, #seedRandBtn, #seed42Btn')) capture();
    });
    window.addEventListener('pagehide', capture);
  }
  return { capture, activate, deactivate, discard, saveCurrent, init };
})();
