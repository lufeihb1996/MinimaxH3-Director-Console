// Offline DOM contract for switching between existing B15 and Pika controls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../..');
const controls = {};
for (const id of ['shotVideoModel', 'shotSteps', 'shotSecondPass', 'shotMP', 'shotPikaResolution',
  'shotSeconds', 'shotExtraLora', 'lightx2vProfileHint', 'shotStepsGroup']) {
  controls[id] = { value: '', options: [], disabled: false, hidden: false,
    replaceChildren(...options) { this.options = options; },
    classList: { toggle(name, value) { this[name] = value; } } };
}
controls.shotVideoModel.value = 'b15_49';
controls.shotSteps.value = '10';
controls.shotSecondPass.value = '1';
controls.shotMP.value = '0.4';
const label = {};
const ctx = vm.createContext({
  document: { getElementById: id => controls[id], querySelector: () => label },
  Option: function(text, value) { this.textContent = text; this.value = value; },
});
vm.runInContext(fs.readFileSync(path.join(root, 'frontend/video-models.js'), 'utf8') + '\nglobalThis.models = videoModelCatalog;', ctx);
const catalog = JSON.parse(fs.readFileSync(path.join(root, 'config/models.json'), 'utf8')).video;
ctx.models.configure({ default_model: catalog.default, models: catalog.models });
ctx.models.renderParameters();
assert.equal(controls.shotSecondPass.disabled, false);
assert.equal(controls.shotSeconds.step, '0.01');

controls.shotVideoModel.value = 'pika_h3';
ctx.models.renderParameters();
assert.equal(controls.shotSteps.value, '6');
assert.equal(controls.shotSecondPass.disabled, true);
assert.equal(controls.shotSecondPass.value, '0');
assert.equal(controls.shotExtraLora.disabled, true);
assert.equal(controls.shotMP.hidden, true);
assert.equal(controls.shotPikaResolution.hidden, false);
assert.equal(controls.shotSeconds.step, '1');
assert.equal(controls.shotSeconds.min, '5');
assert.equal(controls.shotSeconds.max, '15');
assert.equal(label.textContent, '输出分辨率');
controls.shotMP.value = '1';
ctx.models.renderParameters();
assert.equal(controls.shotPikaResolution.value, '1');

controls.shotVideoModel.value = 'b15_49';
ctx.models.renderParameters();
assert.equal(controls.shotSecondPass.disabled, false);
assert.equal(controls.shotPikaResolution.hidden, true);
assert.equal(controls.shotMP.hidden, false);
assert.equal(controls.shotSteps.value, '10');
assert.equal(controls.shotSeconds.step, '0.01');
console.log('PASS: model switch, resolution choices, integer seconds, six steps and B15 control restoration.');
