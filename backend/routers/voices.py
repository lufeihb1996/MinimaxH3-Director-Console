"""全局音色库 (/api/voices) 与项目配音任务 (/api/voice-jobs) 路由。"""
from __future__ import annotations

from pathlib import Path
from fastapi import APIRouter, Body, File, Form, UploadFile
from fastapi.responses import FileResponse

from backend.core.schemas import (
    UseAsProjectAudioIn,
    VoiceJobCreateIn,
    VoiceJobPatch,
    VoiceJobSpeakerIn,
    VoiceProfilePatch,
)
from backend.services.drama3 import DEFAULT_PRESET_SCRIPTS
from backend.services.voice_jobs import (
    cancel_voice_job,
    create_project_voice_job,
    create_voice_profile,
    delete_voice_job,
    delete_voice_job_speaker,
    delete_voice_profile,
    enqueue_voice_job,
    get_drama3_defaults,
    get_voice_job,
    get_voice_job_output_file,
    get_voice_profile_audio_file,
    list_project_voice_jobs,
    list_unassigned_voice_jobs,
    list_voice_profiles,
    patch_voice_job,
    patch_voice_profile,
    put_voice_job_speaker,
    use_voice_job_as_project_audio,
)

router = APIRouter()

_AUDIO_MIME_MAP = {
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}


def _audio_media_type(path: Path) -> str:
    return _AUDIO_MIME_MAP.get(path.suffix.lower(), "audio/flac")


# ============================================================================
# 2.2 音色库接口
# ============================================================================

@router.get("/api/voices")
async def voices_list():
    return list_voice_profiles()


@router.post("/api/voices")
async def voices_create(
    file: UploadFile = File(...),
    name: str = Form(default=""),
):
    return await create_voice_profile(file, name=name)


@router.patch("/api/voices/{profile_id}")
async def voices_patch(profile_id: int, payload: VoiceProfilePatch):
    return patch_voice_profile(profile_id, payload)


@router.delete("/api/voices/{profile_id}")
async def voices_delete(profile_id: int, force: bool = False):
    return delete_voice_profile(profile_id, force=force)


@router.get("/api/voices/{profile_id}/audio")
async def voices_audio(profile_id: int):
    path = get_voice_profile_audio_file(profile_id)
    return FileResponse(
        path,
        media_type=_audio_media_type(path),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/voice-presets")
async def voice_presets():
    return {
        "presets": DEFAULT_PRESET_SCRIPTS,
        "defaults": get_drama3_defaults(),
    }


# ============================================================================
# 2.3 配音任务接口
# ============================================================================

@router.get("/api/projects/{project_id}/voice-jobs")
async def project_voice_jobs_list(project_id: int):
    return list_project_voice_jobs(project_id)


@router.get("/api/voice-jobs")
async def unassigned_voice_jobs_list(unassigned: int = 1):
    if unassigned:
        return list_unassigned_voice_jobs()
    return list_unassigned_voice_jobs()


@router.post("/api/projects/{project_id}/voice-jobs")
async def project_voice_jobs_create(
    project_id: int,
    payload: VoiceJobCreateIn = Body(default_factory=VoiceJobCreateIn),
):
    return create_project_voice_job(project_id, payload)


@router.get("/api/voice-jobs/{job_id}")
async def voice_jobs_get(job_id: int):
    return get_voice_job(job_id)


@router.patch("/api/voice-jobs/{job_id}")
async def voice_jobs_patch(job_id: int, payload: VoiceJobPatch):
    return patch_voice_job(job_id, payload)


@router.put("/api/voice-jobs/{job_id}/speakers/{speaker_index}")
async def voice_jobs_put_speaker(
    job_id: int,
    speaker_index: int,
    payload: VoiceJobSpeakerIn = Body(default_factory=VoiceJobSpeakerIn),
):
    return put_voice_job_speaker(job_id, speaker_index, payload)


@router.delete("/api/voice-jobs/{job_id}/speakers/{speaker_index}")
async def voice_jobs_delete_speaker(job_id: int, speaker_index: int):
    return delete_voice_job_speaker(job_id, speaker_index)


@router.post("/api/voice-jobs/{job_id}/generate")
async def voice_jobs_generate(job_id: int):
    return await enqueue_voice_job(job_id)


@router.post("/api/voice-jobs/{job_id}/cancel")
async def voice_jobs_cancel(job_id: int):
    return await cancel_voice_job(job_id)


@router.delete("/api/voice-jobs/{job_id}")
async def voice_jobs_delete(job_id: int):
    return await delete_voice_job(job_id)


@router.get("/api/voice-jobs/{job_id}/audio")
async def voice_jobs_audio(job_id: int):
    path, _ = get_voice_job_output_file(job_id)
    return FileResponse(
        path,
        media_type=_audio_media_type(path),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/voice-jobs/{job_id}/download")
async def voice_jobs_download(job_id: int):
    path, filename = get_voice_job_output_file(job_id)
    return FileResponse(path, filename=filename, media_type=_audio_media_type(path))


@router.post("/api/voice-jobs/{job_id}/use-as-project-audio")
async def voice_jobs_use_as_project_audio(
    job_id: int,
    payload: UseAsProjectAudioIn = Body(default_factory=UseAsProjectAudioIn),
):
    return await use_voice_job_as_project_audio(job_id, payload)
