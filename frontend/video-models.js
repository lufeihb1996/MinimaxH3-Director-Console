/** Model capabilities come from the same catalog used by the backend. */
const videoModelCatalog = (() => {
  let catalog = { default_model: '', models: [] };
  function profile(id) { return catalog.models.find(model => model.id === id); }
  function configure(data) {
    if (!Array.isArray(data.models) || !data.models.some(model => model.id === data.default_model)) {
      throw new Error('Invalid video model catalog');
    }
    catalog = data;
    const selector = document.getElementById('shotVideoModel');
    const previous = selector.value;
    selector.replaceChildren(...data.models.map(model => new Option(model.label, model.id)));
    selector.value = profile(previous) ? previous : data.default_model;
  }
  function renderParameters() {
    const get = id => document.getElementById(id);
    const model = profile(get('shotVideoModel').value);
    if (!model) return;
    const steps = get('shotSteps');
    const previous = Number(steps.value);
    const secondPassEl = get('shotSecondPass');
    const secondPassResEl = get('shotSecondPassRes');
    if (secondPassEl) {
      secondPassEl.disabled = model.supports_second_pass === false;
      if (secondPassEl.disabled) secondPassEl.value = '0';
    }
    if (secondPassResEl) {
      secondPassResEl.disabled = model.supports_second_pass === false;
      if (!secondPassResEl.value) secondPassResEl.value = '1k';
    }
    const twoPassOn = !secondPassEl || String(secondPassEl.value) === '1';
    const resTier = (secondPassResEl?.value || '1k').toUpperCase();
    const desired = model.steps.map(value => {
      let lbl = model.step_labels[String(value)] || `${value} 步`;
      if (model.pipeline === 'two_pass' && !twoPassOn && Number(value) === 10) {
        lbl = 'First pass only: 10 steps (0.4MP quick draft)';
      } else if (model.id === 'pika_h3') {
        lbl = twoPassOn ? `Second-pass refine: full BF16, 3 steps (${resTier})` : 'Official fixed 6 steps (first pass)';
      }
      return [String(value), lbl];
    });
    const current = [...steps.options].map(option => [option.value, option.textContent]);
    if (JSON.stringify(current) !== JSON.stringify(desired)) {
      steps.replaceChildren(...desired.map(([value, label]) => new Option(label, value)));
    }
    steps.value = String(model.steps.includes(previous) ? previous : model.default_steps);
    steps.disabled = model.steps.length === 1;
    get('shotMP').disabled = model.fixed_megapixels !== null;
    if (model.fixed_megapixels !== null) get('shotMP').value = model.fixed_megapixels;
    const resolution = get('shotPikaResolution');
    const hasResolutions = Array.isArray(model.resolutions);
    const mpLabel = document.querySelector('label[for="shotMP"]');
    if (mpLabel) mpLabel.textContent = hasResolutions ? 'Output resolution' : 'Resolution (MP)';
    if (resolution) {
      resolution.hidden = !hasResolutions;
      get('shotMP').hidden = hasResolutions;
      if (hasResolutions) {
        const mp = Number(get('shotMP').value);
        if (!model.resolutions.some(item => item.mp === mp)) get('shotMP').value = model.default_megapixels;
        resolution.value = String(Number(get('shotMP').value));
      }
    }
    const seconds = get('shotSeconds');
    seconds.min = hasResolutions ? '5' : '0.1';
    seconds.max = hasResolutions ? '15' : '60';
    seconds.step = hasResolutions ? '1' : '0.01';
    const disabledStyle = model.style_disabled_steps.includes(Number(steps.value));
    get('shotExtraLora').disabled = disabledStyle;
    if (disabledStyle) get('shotExtraLora').value = 'off';
    const hint = model.pipeline === 'two_pass' ? '' : (model.hints[steps.value] || '');
    get('lightx2vProfileHint').textContent = hint;
    get('lightx2vProfileHint').hidden = !hint;
    if (get('shotStepsGroup')) get('shotStepsGroup').classList.toggle('hidden', model.execution_backend !== 'pika');
  }
  async function load() { configure(await api('/api/video-models')); }
  function defaults() {
    const model = profile(catalog.default_model);
    if (!model) throw new Error('Video model catalog not loaded. Reload the page');
    return { video_model: model.id, steps: model.default_steps, megapixels: model.default_megapixels, aspect_ratio: model.default_aspect_ratio, second_pass_enabled: false, second_pass_resolution: '1k' };
  }
  return { configure, profile, renderParameters, load, defaults, get defaultModel() { return catalog.default_model; } };
})();
