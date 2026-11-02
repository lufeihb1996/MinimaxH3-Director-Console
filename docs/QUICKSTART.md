# Quick start — first project in about 15 minutes

[English](QUICKSTART.md) | [简体中文](QUICKSTART.zh-CN.md)

Assumes you already have a reachable ComfyUI instance (see the main README for the Colab
notebooks). If you do not, you can still finish steps 1–4 — generation will simply report
`offline`.

## 0. Start the app

```bat
start.bat
```

The browser opens <http://127.0.0.1:18081>. Then open **Settings** (top right):

| Field | Value |
|---|---|
| `comfy_url` | your tunnel hostname, e.g. `https://my-gpu.example.com` |
| `pika_url` | leave empty unless you run the separate Pika service |
| `fps` | `24` is fine |

Click **Test connection** — the badge should turn online.

## 1. Create a project

1. Click `＋` next to **Projects** (top left).
2. Name it, e.g. `My first short`.
3. Click **New Scene**, name it `Scene 1`.

## 2. Generate one reference image

1. Switch to **Image workbench** (top bar).
2. Select `Scene 1`, slot **Ref1**.
3. Model `moss_image_5b`, aspect `9:16`, and a prompt such as
   *a young woman standing by a river at dusk, cinematic lighting*.
4. **Generate** → wait → click the result → **Apply**.

Ref1 now becomes the default character image for CUT shots in this scene.

## 3. Create a shot

1. Switch back to **Video director**.
2. **New shot**.
3. Set:

| Field | Value |
|---|---|
| Mode | `CUT` (uses the reference image) |
| Video model | `b15_49` |
| Seconds | `5` |
| Prompt | *she turns and looks at the camera, hair moving in the wind* |
| Second pass | off for the first try |

## 4. Generate

Click **Generate** on the shot. You should see:

- the shot move to `QUEUED` → `PREPARING` → `RUNNING`
- a progress bar driven by the ComfyUI node stream
- the terminal panel showing the remote log

First run downloads nothing on your machine; it only uploads references and polls ComfyUI.

## 5. Review

When it finishes, the shot is `DONE` and a version (`shot_0001_v1.mp4`) appears. Play it inline.
Unsatisfied? Change the prompt and **Generate** again — a `v2` is created and `v1` is kept.

## 6. Add audio (optional)

- **Audio workbench → Generate song** (YuE2) or **Upload audio**, then **Analyze**.
- Analysis needs the `audio_analysis/` environment; without it this step errors out and you can
  skip audio entirely.

## 7. Merge

- **Play this scene** merges the finished shots of the current scene.
- **Merge full project** produces `project_NNN_master.mp4` in `projects/project_NNN/`.

Both live in the top bar / scene toolbar.

---

## If something goes wrong

| Symptom | Fix |
|---|---|
| Badge stays `offline` | `comfy_url` wrong, tunnel down, or Colab runtime recycled — restart the notebook |
| `FFmpeg not found` | install FFmpeg, add `bin` to `PATH`, reopen the terminal |
| Shot stuck at `QUEUED` | another job holds the GPU slot; check the queue bar and the terminal panel |
| `Generation failed` but no message | open the shot's terminal panel; the last remote log line is usually the cause |
| Second pass checkbox disabled | the model does not support it (`pika_h3` has its own path) |

## Where things are stored

| What | Where |
|---|---|
| Project data | `data/studio.db` |
| Images / videos / audio | `projects/project_NNN/` |
| Frozen inputs per job | `projects/project_NNN/scene_NNN/shot_NNNN/inputs/<id>/` |
| Logs of the local server | your terminal (uvicorn output) |

Backing up is copying `data/` and `projects/` together — they reference each other by absolute path.
