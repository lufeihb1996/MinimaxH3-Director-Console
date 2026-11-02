"""Pika official API parameters; no ComfyUI graph or inference imports."""
from backend.models.base import VideoAdapter
from backend.models.registry import register
from backend.models.video import SECOND_PASS_RESOLUTIONS


@register("video", "pika_official")
class PikaOfficial(VideoAdapter):
    def prepare_template(self, workflow, shot, model):
        raise ValueError("Pika 使用官方 API 请求，不使用 ComfyUI 工作流")

    def normalize_parameters(self, values, current, model):
        values = dict(values)
        seconds = float(values.get("seconds", current.get("seconds", 5)))
        if not seconds.is_integer() or not 5 <= seconds <= 15:
            raise ValueError("Pika 时长请选择 5–15 的整数秒")
        mp = float(values.get("megapixels", current.get("megapixels", 0.4)))
        if mp not in (0.4, 1.0):
            raise ValueError("Pika 分辨率请选择 480p 或 768p")
        if values.get("lip_sync_enabled", current.get("lip_sync_enabled", False)):
            raise ValueError("Pika 暂不支持 MV 原声锁定口型，请使用 B15")
        second_pass = bool(values.get("second_pass_enabled", current.get("second_pass_enabled", False)))
        values.update(second_pass_enabled=second_pass, realism_lora_enabled=False, naughty_lora_enabled=False)
        return values

    def snapshot_files(self, shot, model, settings, read_json):
        self.normalize_parameters({}, shot, model)
        return {"request_template.json": read_json(model["request_template"])}

    def snapshot_context(self, settings):
        endpoint = str(settings.get("pika_url") or "").strip().rstrip("/")
        if not endpoint:
            raise ValueError("请先在设置中填写 Pika 服务地址")
        return {"pika_endpoint": endpoint}

    def wants_realism_trigger(self, shot):
        return False

    def build_request(self, shot, template):
        profile = shot.get("video_profile") or {}
        self.normalize_parameters({}, shot, profile)
        prompt = str(shot.get("prompt") or "").strip()
        if not 1 <= len(prompt) <= 8000:
            raise ValueError("Pika 提示词需要 1–8000 字符")
        seed = int(shot.get("seed") or 0)
        if not 0 <= seed <= 2**53 - 1:
            raise ValueError("Pika 种子须为 0–9007199254740991")
        second_pass = bool(int(shot.get("second_pass_enabled") or 0))
        res_tier = str(shot.get("second_pass_resolution") or "1k").strip().lower()
        upscale_factor = SECOND_PASS_RESOLUTIONS.get(res_tier, 2.0)
        pass1_video = str(shot.get("pass1_source_video_path") or "").strip()
        if second_pass and pass1_video:
            from backend.models.adapters.video_b15 import _ensure_picture1_prompt
            if shot.get("ref1_path"):
                task = "ref2va"
                prompt = _ensure_picture1_prompt(prompt)
                files = [("source_video", pass1_video), ("ref1_path", shot["ref1_path"])]
            else:
                task = "t2va"
                files = [("source_video", pass1_video)]
            body = dict(task=task, prompt=prompt, resolution=template["resolutions"][str(float(shot["megapixels"]))],
                        duration=int(shot["seconds"]), aspect=shot["aspect_ratio"],
                        seed=None if seed == 0 else seed, first_image="", last_image="", references=[],
                        source_video="", second_pass=True, start_sigma=0.78, upscale_factor=upscale_factor)
            return body, files
        mode = shot["mode"].upper()
        task = template["tasks"][mode]
        # Empty slots are omitted in the same order as the existing Ref UI.
        fields = [f"ref{i}_path" for i in range(1, 5)] if mode == "CUT" else (["first_frame_path"] if mode == "CONTINUE" else [])
        files = [(field, shot[field]) for field in fields if shot.get(field)]
        if mode == "CUT" and not files:
            raise ValueError("Pika CUT 至少需要一张参考图")
        if mode == "CONTINUE" and not files:
            raise ValueError("Pika CONTINUE 需要上一镜末帧或手动首帧")
        body = dict(task=task, prompt=prompt, resolution=template["resolutions"][str(float(shot["megapixels"]))],
                    duration=int(shot["seconds"]), aspect=shot["aspect_ratio"],
                    seed=None if seed == 0 else seed, first_image="", last_image="", references=[])
        if second_pass:
            body.update(second_pass=True, start_sigma=0.78, upscale_factor=upscale_factor)
        return body, files
