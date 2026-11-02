"""一键极速建剧脚本（JSON 驱动，默认 Ref1 项目共享人物 + Ref3 分幕场景 + H3 b15-49 一采 9:16）。

用法：
  python tools/quick_seed_project.py <spec.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_BASE_URL = "http://127.0.0.1:18080"


def seed_project(spec: dict[str, Any], base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
    # 只连本地后端：不走系统代理，避免 Windows 上 NO_PROXY 含 [::1] 时 httpx 解析端口报错
    client = httpx.Client(base_url=base_url, timeout=30.0, trust_env=False)

    project_id = spec.get("project_id")
    project_name = str(spec["name"]).strip()
    if project_id:
        project_id = int(project_id)
        if spec.get("replace_scenes", True):
            for sc in client.get(f"/api/projects/{project_id}/scenes").json():
                client.delete(f"/api/scenes/{sc['id']}")
    else:
        res = client.post("/api/projects", json={"name": project_name})
        res.raise_for_status()
        project_id = int(res.json()["id"])

    aspect_ratio = str(spec.get("aspect_ratio") or "9:16")
    image_model = str(spec.get("image_model") or "moss_image_5b")
    lora_mode = str(spec.get("lora_mode") or "all")
    video_model = str(spec.get("video_model") or "b15_49")
    second_pass_enabled = 1 if bool(spec.get("second_pass_enabled", False)) else 0
    base_seed = int(spec.get("seed") or 20261024)

    ref1_prompt = str(spec.get("ref1_prompt") or "").strip()
    ref2_prompt = str(spec.get("ref2_prompt") or "").strip()

    scenes_spec = spec.get("scenes") or []
    total_duration = sum(
        float(sh.get("seconds") or 5.0)
        for sc in scenes_spec
        for sh in (sc.get("shots") or [])
    ) or 60.0

    mv_payload = {
        "title": str(spec.get("mv_title") or project_name),
        "native_audio_enabled": bool(spec.get("native_audio_enabled", False)),
        "audio_linked": bool(spec.get("audio_linked", False)),
        "target_duration": int(round(total_duration)),
    }
    if "genre_prompt" in spec:
        mv_payload["genre_prompt"] = str(spec["genre_prompt"])
    if "lyrics" in spec:
        mv_payload["lyrics"] = str(spec["lyrics"])
    client.put(f"/api/projects/{project_id}/mv", json=mv_payload).raise_for_status()

    cursor_time = 0.0
    created_scenes = []

    for sc_idx, sc_data in enumerate(scenes_spec):
        sc_res = client.post(
            f"/api/projects/{project_id}/scenes",
            json={"name": str(sc_data["name"]).strip()},
        )
        sc_res.raise_for_status()
        scene_id = int(sc_res.json()["id"])

        sc_ref1 = str(sc_data.get("ref1_prompt") or ref1_prompt).strip()
        sc_ref1_scope = str(sc_data.get("ref1_scope") or ("project" if ref1_prompt else "scene"))
        sc_ref2 = str(sc_data.get("ref2_prompt") or ref2_prompt).strip()
        sc_ref2_scope = str(sc_data.get("ref2_scope") or ("project" if ref2_prompt else "scene"))
        sc_ref3 = str(sc_data.get("ref3_prompt") or "").strip()
        sc_ref4 = str(sc_data.get("ref4_prompt") or "").strip()

        slot_configs = {
            "ref1": {
                "prompt": sc_ref1,
                "aspect_ratio": aspect_ratio,
                "seed": base_seed,
                "image_model": image_model,
                "lora_mode": lora_mode,
                "enabled": bool(sc_ref1),
                "ref_scope": sc_ref1_scope,
            },
            "ref2": {
                "prompt": sc_ref2,
                "aspect_ratio": aspect_ratio,
                "seed": base_seed + 100,
                "image_model": image_model,
                "lora_mode": lora_mode,
                "enabled": bool(sc_ref2),
                "ref_scope": sc_ref2_scope,
            },
            "ref3": {
                "prompt": sc_ref3,
                "aspect_ratio": aspect_ratio,
                "seed": base_seed + sc_idx + 1,
                "image_model": image_model,
                "lora_mode": lora_mode,
                "enabled": bool(sc_ref3),
                "ref_scope": "scene",
            },
            "ref4": {
                "prompt": sc_ref4,
                "aspect_ratio": aspect_ratio,
                "seed": base_seed + 200 + sc_idx + 1,
                "image_model": image_model,
                "lora_mode": lora_mode,
                "enabled": bool(sc_ref4),
                "ref_scope": "scene",
            },
        }
        for role, cfg in slot_configs.items():
            r = client.put(f"/api/scenes/{scene_id}/image-configs/{role}", json=cfg)
            r.raise_for_status()

        created_shots = []
        for sh_idx, sh_data in enumerate(sc_data.get("shots") or []):
            dur = float(sh_data.get("seconds") or 5.0)
            t_start = float(sh_data.get("timeline_start", cursor_time))
            t_end = float(sh_data.get("timeline_end", round(t_start + dur, 4)))
            cursor_time = t_end
            mode = str(sh_data.get("mode") or ("CUT" if sh_idx == 0 else "CONTINUE")).upper()
            sh_payload = {
                "title": str(sh_data.get("title") or f"镜头 #{sh_idx + 1:03d}"),
                "mode": mode,
                "prompt": str(sh_data.get("prompt") or ""),
                "dialogue": str(sh_data.get("dialogue") or ""),
                "lyric_text": str(sh_data.get("lyric_text") or ""),
                "seconds": dur,
                "aspect_ratio": str(sh_data.get("aspect_ratio") or aspect_ratio),
                "video_model": video_model,
                "second_pass_enabled": second_pass_enabled,
                "seed": int(sh_data.get("seed") or (1000 + sc_idx * 10 + sh_idx + 1)),
                "timeline_start": t_start,
                "timeline_end": t_end,
                "target_duration": round(t_end - t_start, 4),
            }
            sh_res = client.post(f"/api/scenes/{scene_id}/shots", json=sh_payload)
            sh_res.raise_for_status()
            shot = sh_res.json()
            if float(shot.get("timeline_end") or 0) != t_end:
                patch_res = client.patch(
                    f"/api/shots/{shot['id']}",
                    json={
                        "timeline_start": t_start,
                        "timeline_end": t_end,
                        "target_duration": round(t_end - t_start, 4),
                        "second_pass_enabled": second_pass_enabled,
                    },
                )
                patch_res.raise_for_status()
                shot = patch_res.json()
            created_shots.append(shot)

        created_scenes.append(
            {
                "scene_id": scene_id,
                "name": sc_data["name"],
                "shot_count": len(created_shots),
            }
        )

    summary = {
        "ok": True,
        "project_id": project_id,
        "project_name": project_name,
        "total_duration": round(cursor_time, 2),
        "scenes": created_scenes,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("用法: python tools/quick_seed_project.py <spec.json>")
    spec_path = Path(sys.argv[1])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    seed_project(spec)


if __name__ == "__main__":
    main()
