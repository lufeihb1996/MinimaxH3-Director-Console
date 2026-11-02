/** Vue owns only the queue root; the existing editor and API remain authoritative. */
const imageWorkbenchView = (() => {
  let model;
  return {
    update(jobs) {
      if (!model) {
        model = Vue.reactive({ jobs: [], selectedRole: '', currentJobId: null });
        Vue.createApp({
          setup() {
            const active = job => ['QUEUED', 'PREPARING', 'RUNNING'].includes(job.status);
            const current = job => Boolean(job.real_job_id) && Number(job.real_job_id) === Number(model.currentJobId);
            const percent = job => Math.max(0, Math.min(100, Math.round(Number(job.progress || (job.status === 'DONE' ? 100 : 0)))));
            const detail = job => job.error ? `Failed: ${String(job.error).replace(/\s+/g, ' ').trim()}`
              : job.current_node || ({ DRAFT: job.prompt ? 'Parameters ready, not generated' : 'Waiting for parameters', PENDING: job.prompt ? 'Parameters ready, not generated' : 'Waiting for parameters', QUEUED: 'Waiting in queue', DONE: 'Generation complete' }[job.status] || 'Waiting to run');
            const select = job => selectImageScene(job.scene_id, job.role);
            async function edit(job) {
              return withSubmissionGuard(imageSubmissionKey(job.scene_id,job.role), () => submit(job));
            }
            async function submit(job) {
              const projectId = state.projectId;
              const sourceSceneId = getImageSceneData(job.scene_id)?.roles?.[job.role]?.config?.source_scene_id || job.scene_id;
              const editorSourceSceneId = getImageSceneData(state.imageSceneId)?.roles?.[state.imageRole]?.config?.source_scene_id || state.imageSceneId;
              const editingThisSlot = state.workspaceMode === 'image'
                && state.imageRole === job.role
                && Number(editorSourceSceneId) === Number(sourceSceneId);
              if (editingThisSlot) {
                // The visible draft wins, including shared references displayed in another scene.
                if (!await saveImageConfig({ silent: true })) {
                  return showToast('Saving parameters failed; nothing was submitted', 'error');
                }
                if (Number(state.projectId) !== Number(projectId)) return;
              } else if (!String(job.prompt || '').trim()) {

                await select(job);
                $('imagePrompt')?.focus();
                return showToast('Fill in a prompt and enable this slot first', 'warning');
              }
              try {
                const result = await api(`/api/scenes/${sourceSceneId}/images/queue`, {
                  method: 'POST', body: JSON.stringify({ roles: [job.role], replace_existing: false, only_missing: false }),
                });
                if (Number(state.projectId) !== Number(projectId)) return;
                const reason = result.skipped_reasons?.[job.role];
                const message = result.queued ? 'Submitted with the latest parameters; previous images are kept'
                  : reason === 'already_active' ? 'This asset is queued or generating'
                  : reason === 'disabled' ? 'This asset is not enabled; enable it on the right'
                  : reason === 'no_prompt' ? 'This asset has no saved prompt'
                  : reason === 'existing_image' ? 'Image already exists; skipped regeneration'
                  : 'This asset was not submitted; check the task status';
                showToast(message, result.queued ? 'success' : (reason === 'already_active' || reason === 'existing_image' ? 'info' : 'warning'));
                await loadImageDashboard();
                await refreshImageQueueStatus();
              } catch (error) {
                showToast(`Submitting image generation failed: ${error.message}`, 'error');
              }
            }
            function preview(job) {
              openLightbox(`${job.latest_image.media_url}?t=${Date.now()}`, `${job.scene_name || 'Scene'} · ${sceneImageRoleLabel(job.role)} · latest image`);
            }
            async function remove(job) {
              const ok = await showConfirmModal('Delete queue row', `Only delete <b>${escapeHtml(job.scene_name || `Scene ${job.scene_id}`)} / ${escapeHtml(sceneImageRoleLabel(job.role))}</b> queue record.<br>History images will not be deleted. Confirm?`);
              if (!ok) return;
              try {
                await api(`/api/image-queue/slots/${job.scene_id}/${job.role}`, { method: 'DELETE' });
                showToast('Task record deleted; fixed Ref slots and history images are kept', 'success');
                await loadImageDashboard();
                await refreshImageQueueStatus();
              } catch (error) { showToast(`Delete failed: ${error.message}`, 'error'); }
            }
            const elapsed = job => shotElapsedText(job);
            return { model, active, current, percent, detail, elapsed, select, edit, preview, remove, label: sceneImageRoleLabel, submitKey: job => imageSubmissionKey(job.scene_id,job.role) };
          },
          template: `
            <div v-if="!model.jobs.length" class="image-queue-empty">
              <div class="image-queue-empty-icon">▧</div>
              <div class="image-queue-empty-title">No Scene available</div>
              <div class="image-queue-empty-sub">Create or select a Scene first.</div>
            </div>
            <article v-for="job in model.jobs" :key="job.scene_id + ':' + job.role"
              class="image-queue-task image-queue-table-row"
              :class="{ current: current(job), selected: job.role === model.selectedRole, 'image-slot-disabled': job.status === 'DISABLED' }"
              :data-job-id="job.real_job_id || undefined" :data-slot-key="job.scene_id + ':' + job.role" @click="select(job)">
              <div class="image-queue-col task-no"><strong>R{{ job.slot_index }}</strong><span v-if="current(job)" class="queue-current-chip">Current</span></div>
              <div class="image-queue-col scene-name" :title="job.scene_name || ''">{{ job.scene_name || 'Scene ' + job.scene_id }}</div>
              <div class="image-queue-col"><span class="image-role-chip">{{ label(job.role) }}</span></div>
              <div class="image-queue-col image-queue-progress-cell">
                <div class="image-queue-task-progress-row">
                  <div class="mini-bar-track image-task-progress-track"><div class="mini-bar-fill" :style="{ width: percent(job) + '%' }"></div></div>
                  <span class="font-mono image-task-progress-pct">{{ percent(job) }}%</span>
                </div>
                <div class="image-queue-task-node" :title="detail(job)">{{ detail(job) }}</div>
              </div>
              <div class="image-queue-col font-mono" :data-image-job-elapsed="job.scene_id + ':' + job.role" title="Live timer from start of execution; fixed to total time when finished">{{ elapsed(job) }}</div>
              <div class="image-queue-col image-queue-task-actions">
                <button class="btn btn-ghost btn-xs regenerate-job-btn" :data-submit-key="submitKey(job)" @click.stop="edit(job)">{{ (job.real_job_id || job.latest_image ? 'Regenerate' : 'Generate') }}</button>
                <button v-if="job.latest_image" class="btn btn-ghost btn-xs preview-job-btn" @click.stop="preview(job)">Preview</button>
                <button v-if="job.real_job_id" class="btn btn-danger-outline btn-xs delete-job-btn" :disabled="active(job)" @click.stop="remove(job)">Delete</button>
                <span v-else class="image-queue-no-action">—</span>
              </div>
            </article>`
        }).mount('#imageStoryboardView');
      }
      model.jobs = jobs;
      model.selectedRole = state.imageRole;
      model.currentJobId = state.imageQueue.current_job_id;
    }
  };
})();
/** History is an independent Vue island, so editor drafts survive queue refreshes. */
const imageHistoryView = (() => {
  let model;
  return {
    update(rows, message = '', role = state.imageRole) {
      if (!model) {
        model = Vue.reactive({ rows: [], message: '', role: '', scope: 'scene' });
        Vue.createApp({
          setup() {
            function preview(img) {
              openLightbox(`${img.media_url}?t=${Date.now()}`, `${getImageSceneData()?.name || 'Scene'} · ${sceneImageRoleLabel(img.role)} · history #${img.id}`);
            }
            async function remove(img) {
              try {
                await api(`/api/scene-images/${img.id}`, { method: 'DELETE' });
                showToast('History image deleted', 'success');
                await loadImageDashboard();
              } catch (error) { showToast(`Deleting the image failed: ${error.message}`, 'error'); }
            }
            async function apply(img, replace) {
              // Capture the displayed role before opening an asynchronous confirmation.
              const role = model.role;
              try {
                const result = await api(`/api/scene-images/${img.id}/apply`, {
                  method: 'POST', body: JSON.stringify({ role, replace_existing: replace }),
                });
                const selectedId = state.shotId;
                const sceneId = state.sceneId;
                const affectedScenes = (result.applied_scene_ids || []).map(Number);
                if (sceneId && affectedScenes.includes(Number(sceneId))) {
                  const refreshedShots = await api(`/api/scenes/${sceneId}/shots`);
                  if (Number(state.sceneId) === Number(sceneId)) {
                    for (const refreshed of refreshedShots) {
                      const fields = {};
                      for (const slot of ['ref1', 'ref2', 'ref3', 'ref4']) fields[`${slot}_path`] = refreshed[`${slot}_path`];
                      const listed = state.shots.find(item => Number(item.id) === Number(refreshed.id));
                      if (listed) Object.assign(listed, fields);
                      if (selectedId && Number(state.shotId) === Number(selectedId) && Number(refreshed.id) === Number(selectedId)) {
                        if (state.currentShot) Object.assign(state.currentShot, fields);
                        await renderRefSlots(refreshed, state.shotSelectSeq);
                      }
                    }
                  }
                }
                await loadImageDashboard();
                showToast(`${replace ? 'Overwrote' : 'Filled'} ${result.applied_count} ${replace ? '' : 'empty '}CUT Shot Refs`, 'success');
              } catch (error) { showToast(`Applying the image failed: ${error.message}`, 'error'); }
            }
            return { model, preview, remove, apply };
          },
          template: `
            <div v-if="model.message" class="scene-image-empty">{{ model.message }}</div>
            <div v-for="img in model.rows" :key="img.id" class="image-history-card">
              <div class="image-history-preview-wrap">
                <button class="image-history-preview" @click="preview(img)"><img :src="img.media_url + '?t=' + img.id" alt="History image" /></button>
                <button class="image-history-delete-btn" title="Delete this history image" aria-label="Delete this history image" @click.stop="remove(img)">×</button>
              </div>
              <div class="image-history-meta">Seed {{ img.seed }} · {{ img.width }}×{{ img.height }}<template v-if="Number(img.elapsed_seconds) > 0"> · {{ Math.round(Number(img.elapsed_seconds)) }}s</template></div>
              <div class="image-history-actions">
                <button class="btn btn-secondary btn-xs apply-overwrite-btn" style="width:100%" @click="apply(img, true)">Use as current</button>

              </div>
            </div>`
        }).mount('#imageHistoryGallery');
      }
      model.rows = rows;
      model.message = message;
      model.role = role;
      model.scope = getImageRoleData()?.config?.ref_scope || 'scene';
    }
  };
})();
