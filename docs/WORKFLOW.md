# Workflow — from an empty project to a finished cut

[English](WORKFLOW.md) | [简体中文](WORKFLOW.zh-CN.md)

This document follows the actual UI order. Every stage is optional except 1, 5, 6 and 8.

```
1 Project/Scene → 2 Reference images → 3 Audio (song/MV) → 4 Voice-over
→ 5 Shots → 6 Generate → 7 Review versions → 8 Merge → 9 Packaging (optional)
```

---

## 1. Project and Scene

A **Project** owns Scenes; a **Scene** owns Shots. One scene is usually one location or one
continuous action.

- `＋` in the top-left creates a Project.
- The left sidebar switches between **Video director / Image workbench / Audio workbench /
  Voice-over workbench / Packaging workbench** — all five operate on the same project.
- **New Scene** adds a scene. Shots created later inherit its reference images.

Nothing is generated yet; this only creates rows in `data/studio.db`.

## 2. Reference images (Image workbench)

Four fixed slots per scene:

| Slot | Role |
|---|---|
| Ref1 | main character |
| Ref2 | supporting character |
| Ref3 | scene A |
| Ref4 | scene B / prop |

- Pick the image model (`moss_image_5b` by default, or `krea2`), aspect ratio and prompt, then
  **Generate**. Uploads work too (**Upload / drag / Ctrl+V**).
- Every result lands in the slot's **history**; only the image you **Apply** becomes the default
  for future CUT shots.
- Ref1/Ref2 have a **scope** switch: *scene* (default) or *project*. With *project* scope, one
  character image is shared by every scene and generated only once.
- Queueing several slots at once is supported; the image queue runs independently of the video
  queue.

Skipping this stage is fine for T2V shots — they need no reference at all.

## 3. Audio (only for songs / MV / talking-head)

**Audio workbench** does two different things:

- **YuE2 song**: write style + lyrics, **Generate song**. One song per project, no version
  history.
- **Upload audio**: bring your own track (MV, narration, dialogue).

Either way, run **Analyze** afterwards. The analysis produces:

| Output | Used for |
|---|---|
| `vocals.wav` / `no_vocals.wav` | Demucs split — the clean vocal drives lip-sync for MV |
| `timeline.json` | shot boundaries, beat positions, per-line timings |
| `lyrics.srt` | subtitles burned into the final MV |

Analysis runs in the isolated `audio_analysis/` environment (see README). Without it this stage
fails and everything audio-related is unavailable — image and video work still functions.

Replacing the audio invalidates old shot timings. Set the final audio **before** planning shots.

## 4. Voice-over (Digital-human / interview)

**Voice-over workbench**:

1. `＋ Add to queue` creates a task.
2. Pick a **voice profile** per speaker (global library, reusable across projects).
3. Write the script. With several speakers, tag lines as `<|speaker:0|>`, `<|speaker:1|>` …
4. **Generate voice-over** (Fish Audio Drama 3), then **Use as project audio**.

When generation finishes and the project has no audio yet, the result is attached automatically.
If the project already has a song or an uploaded track, you must confirm the replacement.

Emotion tags such as `[sighs]`, `[laughs]`, `[whispers]` go inline in the script line.

## 5. Shots

Two ways to create shots:

- **New shot** — manual, one at a time.
- **Auto shots from audio** — reads the analyzed timeline and the vocal troughs, then proposes
  cut points. The preview is a dry run: nothing is written until you confirm. If the plan
  changes between preview and save, the API returns `409 PLAN_CHANGED` and you re-preview.

Per shot you choose:

| Field | Notes |
|---|---|
| Mode | `T2V` (prompt only), `CUT` (reference images), `CONTINUE` (previous shot's last frame) |
| Video model | `b15_49` (ComfyUI) or `pika_h3` (separate service) |
| Aspect / MP / steps / seed | constrained by `config/models.json` |
| Second pass | `b15_49` only: 720p / 1K / 1.5K / 2K refine after the 0.4 MP draft |

`CONTINUE` shots wait for the previous shot in the same scene, then extract its last frame
automatically. You can override that with a manual first frame.

## 6. Generate

Three scopes: **single shot**, **whole scene**, **whole project**.

- Enqueueing freezes the shot's parameters and inputs into
  `projects/project_NNN/scene_NNN/shot_NNNN/inputs/<id>/` — later edits do not affect a queued
  job.
- The backend serializes GPU work through a single slot and calls ComfyUI `/free` only when the
  model key changes.
- Progress arrives over WebSocket; the terminal panel shows the ComfyUI/Pika log for the
  selected shot.
- With second pass enabled, re-rendering a scene re-queues every non-running shot, including
  ones already `DONE`.

## 7. Review versions

Every generation is stored as `shot_NNNN_v1.mp4`, `v2`, … Nothing is deleted when a run fails:
the old video stays selected and the failure is recorded next to it.

Click a version to select it; the selected version is what merging uses.

## 8. Merge

- **Play this scene** merges the finished shots of the current scene. CONTINUE seams are
  smoothed (duplicate still frames, low-frequency luminance and chroma drift, static background
  micro-blending); moving regions are not cross-faded.
- **Merge full project** unifies canvas size and frame rate across scenes, concatenates them,
  muxes the original audio back and — for MV projects — burns in the lyric subtitles.

Outputs in `projects/project_NNN/`:

| File | Contents |
|---|---|
| `project_NNN_master_no_subtitles.mp4` | merged video without subtitles |
| `project_NNN_master.srt` | generated lyric subtitles |
| `project_NNN_master.mp4` | final cut |
| `scene_NNN_master.mp4` | per-scene cut |

Merging is cached: unchanged inputs reuse the previous result.

## 9. Packaging (talking-head, optional)

The **16:9 packaging workbench** turns a talking-head clip into a card-based 16:9 video: edit
card text in the browser, preview instantly, render through Remotion, export MP4.

---

## Guided tour of the main screen

```
┌───────────────┬──────────────────────────────────────┬─────────────────┐
│ Projects      │ Shot list / editor                    │ Reference slots │
│  └ Scenes     │  mode, model, prompt, dialogue        │  Ref1..Ref4     │
│     └ Shots   │  aspect, MP, steps, seed, second pass │  history        │
│               │  preview · progress · terminal log    │  first frame    │
└───────────────┴──────────────────────────────────────┴─────────────────┘
```

Top bar: workspace switch, project switcher, Settings (`comfy_url`, `pika_url`, `fps`).

## Rules worth knowing

- A queued job is frozen; edit a shot only after it finishes, or the change waits for the next run.
- Re-generating keeps the previous successful video unless the new run succeeds.
- Uploading or replacing project audio invalidates earlier shot timings — re-plan after the change.
- The app never guesses a remote capability: unsupported combinations (e.g. Pika + MV
  original-audio lock) are rejected before queueing instead of silently downgraded.
