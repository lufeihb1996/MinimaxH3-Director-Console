"""API 请求体（Pydantic）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from backend.models.video import DEFAULT_VIDEO_MODEL, video_model


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class SceneIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class SceneImageApplyIn(BaseModel):
    role: str = "ref1"
    replace_existing: bool = False


class SceneImageConfigIn(BaseModel):
    ref_scope: str = "scene"
    role_name: str = Field(default="", max_length=120)
    prompt: str = ""
    aspect_ratio: str = "9:16"
    seed: int = 42
    steps: int = 0
    cfg: float = 0.0
    image_model: str = "moss_image_5b"
    lora_mode: str = "all"
    enabled: bool = True


class SceneImageQueueIn(BaseModel):
    roles: list[str] | None = None
    replace_existing: bool = False
    only_missing: bool = False


class ShotIn(BaseModel):
    title: str = ""
    mode: str = "CONTINUE"
    prompt: str = ""
    dialogue: str = ""
    seconds: float = 5.0
    megapixels: float = video_model(DEFAULT_VIDEO_MODEL)['default_megapixels']
    aspect_ratio: str = video_model(DEFAULT_VIDEO_MODEL)['default_aspect_ratio']
    steps: int = video_model(DEFAULT_VIDEO_MODEL)['default_steps']
    second_pass_enabled: bool = False
    second_pass_resolution: str = "1k"
    realism_lora_enabled: bool = False
    naughty_lora_enabled: bool = False
    seed: int = 0
    video_model: str = DEFAULT_VIDEO_MODEL
    timeline_start: float = 0.0
    timeline_end: float = 0.0
    target_duration: float = 0.0
    conform_mode: str = "auto"
    lip_sync_enabled: bool = False
    lyric_text: str = ""


class ShotPatch(BaseModel):
    title: str | None = None
    mode: str | None = None
    prompt: str | None = None
    dialogue: str | None = None
    seconds: float | None = None
    megapixels: float | None = None
    aspect_ratio: str | None = None
    steps: int | None = None
    second_pass_enabled: bool | None = None
    second_pass_resolution: str | None = None
    realism_lora_enabled: bool | None = None
    naughty_lora_enabled: bool | None = None
    seed: int | None = None
    video_model: str | None = None
    timeline_start: float | None = None
    timeline_end: float | None = None
    target_duration: float | None = None
    conform_mode: str | None = None
    lip_sync_enabled: bool | None = None
    lyric_text: str | None = None
    status: str | None = None


class MVProjectPatch(BaseModel):
    title: str | None = None
    genre_prompt: str | None = None
    lyrics: str | None = None
    target_duration: int | None = None
    audio_linked: bool | None = None
    native_audio_enabled: bool | None = None
    song_model: str | None = None


class SettingsIn(BaseModel):
    comfy_url: str
    pika_url: str = ""
    fps: str = "24"


class VoiceProfilePatch(BaseModel):
    name: str | None = None
    transcript: str | None = None
    remote_name: str | None = None


class VoiceJobCreateIn(BaseModel):
    title: str | None = None
    script_text: str | None = None
    from_job_id: int | None = None
    params: dict | None = None
    speakers: list[dict] | None = None


class VoiceJobPatch(BaseModel):
    title: str | None = None
    script_text: str | None = None
    params: dict | None = None
    project_id: int | None = None


class VoiceJobSpeakerIn(BaseModel):
    name: str | None = None
    voice_profile_id: int | None = None


class UseAsProjectAudioIn(BaseModel):
    confirm_replace: bool = False


class ShotPlanIn(BaseModel):
    target_min: float = 5.0
    target_max: float = 6.0
    soft_max: float = 7.0
    hard_max: float = 9.6
    min_shot: float = 3.0
    group_size: int = 4
    dry_run: bool = True
    replace: bool = False
    initial_shots: list[dict] | None = None
