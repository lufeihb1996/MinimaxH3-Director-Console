"""P1 单元与集成测试：覆盖 T1～T6（全部使用独立临时库 + Mock ComfyUI，绝不触碰远端 GPU 或修改 Project #34）。"""
from __future__ import annotations

import asyncio
import json
import math
import shutil
import sqlite3
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.drama3_migrate import migrate_drama3_state
from backend.services.shot_planner import plan_shots
from backend.services.voice_jobs import extract_spoken_lyrics


# ============================================================================
# T1: extract_spoken_lyrics
# ============================================================================

def test_t1_extract_spoken_lyrics():
    script = (
        "\n"
        "  <|speaker:0|>   [sighs] 姐妹们\u3000听我一句劝，[chuckles] 千万别买智商税！  \n"
        "   \u3000  \n"
        "<|speaker:1|> [shaky breath] 抱歉抱歉，\t\t马上就忙完这一段了！\n"
        "[giggles]   <|speaker:0|> <|emotion:happy|> 第三句单人混合标签测试。 [laughs]\n"
        "   [panting]   \n"
        "第四句没有任何标签但有全角　　和半角   空格。\n"
    )
    out = extract_spoken_lyrics(script)
    out_lines = out.splitlines()
    # 第 5 行只有 [panting] 标签，剥离标签后为空行应丢弃；其余 4 行非空脚本行应完整保留
    assert len(out_lines) == 4
    assert out_lines[0] == "姐妹们 听我一句劝， 千万别买智商税！"
    assert out_lines[1] == "抱歉抱歉， 马上就忙完这一段了！"
    assert out_lines[2] == "第三句单人混合标签测试。"
    assert out_lines[3] == "第四句没有任何标签但有全角 和半角 空格。"


# ============================================================================
# T2: plan_shots 合成数据
# ============================================================================

def _make_words_for_text(text: str, start: float, end: float, pause_after_idx: dict[int, float] | None = None) -> list[dict]:
    import unicodedata
    chars = [c for c in text if not all(unicodedata.category(ch)[0] in ("P", "Z", "S") for ch in c)]
    n = len(chars)
    if n == 0:
        return []
    pause_map = pause_after_idx or {}
    total_pause = sum(pause_map.values())
    avail = max(0.1, (end - start) - total_pause)
    step = avail / n
    cur = start
    words = []
    for i, c in enumerate(chars):
        w_start = round(cur, 4)
        w_end = round(cur + step, 4)
        words.append({"word": c, "start": w_start, "end": w_end, "score": 0.99})
        cur = w_end + pause_map.get(i + 1, 0.0)
    words[-1]["end"] = round(end, 4)
    return words


def _assert_25fps_and_contiguous(res: dict, expected_end: float):
    shots = res["shots"]
    assert len(shots) >= 1
    assert shots[0]["timeline_start"] == 0.0
    assert shots[-1]["timeline_end"] == expected_end
    for idx, sh in enumerate(shots):
        s = sh["timeline_start"]
        e = sh["timeline_end"]
        d = sh["seconds"]
        assert d == sh["target_duration"]
        assert round(e - s, 2) == d
        assert d > 0
        # 0.04 的整数倍
        assert abs(round(s * 25) - s * 25) < 1e-5
        assert abs(round(e * 25) - e * 25) < 1e-5
        assert abs(round(d * 25) - d * 25) < 1e-5
        if idx > 0:
            assert s == shots[idx - 1]["timeline_end"]


def test_t2_plan_shots_synthetic():
    # ① 全部是 4～6 秒的短句：一句一镜
    lines_1 = [
        {"index": 1, "text": "第一句五秒钟的正常短句测试。", "start": 0.2, "end": 5.0, "aligned_ratio": 1.0},
        {"index": 2, "text": "第二句五秒钟的正常短句测试。", "start": 5.2, "end": 10.2, "aligned_ratio": 1.0},
        {"index": 3, "text": "第三句五秒钟的正常短句测试。", "start": 10.4, "end": 15.6, "aligned_ratio": 1.0},
    ]
    words_1 = []
    for ln in lines_1:
        words_1.extend(_make_words_for_text(ln["text"], ln["start"], ln["end"]))
    res_1 = plan_shots(lines_1, words_1, None, 16.0)
    assert len(res_1["shots"]) == 3
    assert [s["source_lines"] for s in res_1["shots"]] == [[1], [2], [3]]
    _assert_25fps_and_contiguous(res_1, 16.0)

    # ② 一句 8.6 秒且中间有逗号：在逗号处拆成两段，每段 3～7 秒
    text_2 = "上半句大约四秒钟刚好到这里，下半句也是大约四秒钟刚好结束！"
    lines_2 = [{"index": 1, "text": text_2, "start": 0.2, "end": 8.8, "aligned_ratio": 1.0}]
    words_2 = _make_words_for_text(text_2, 0.2, 8.8)
    res_2 = plan_shots(lines_2, words_2, None, 9.0)
    assert len(res_2["shots"]) == 2
    assert res_2["shots"][0]["dialogue"] == "上半句大约四秒钟刚好到这里，"
    assert res_2["shots"][1]["dialogue"] == "下半句也是大约四秒钟刚好结束！"
    for sh in res_2["shots"]:
        assert 3.0 <= sh["seconds"] <= 7.0
        assert sh["split"] is True
    _assert_25fps_and_contiguous(res_2, 9.0)

    # ③ 一句 11 秒、没有标点：在最大停顿处拆开，每段 ≤ 9.6 秒，并带 warning
    text_3 = "这是一句完全没有任何标点符号但是长达十一秒钟的超长口播句子必须在最大气口停顿处切开"
    # 在第 18 个字后放一个 0.35s 的最大停顿
    lines_3 = [{"index": 1, "text": text_3, "start": 0.0, "end": 11.0, "aligned_ratio": 1.0}]
    words_3 = _make_words_for_text(text_3, 0.0, 11.0, pause_after_idx={18: 0.35})
    res_3 = plan_shots(lines_3, words_3, None, 11.0)
    assert len(res_3["shots"]) == 2
    for sh in res_3["shots"]:
        assert sh["seconds"] <= 9.6
    assert len(res_3["warnings"]) >= 1
    assert res_3["shots"][0]["dialogue"] + res_3["shots"][1]["dialogue"] == text_3
    _assert_25fps_and_contiguous(res_3, 11.0)

    # ④ 两句都是 1.5 秒：合并成一镜
    lines_4 = [
        {"index": 1, "text": "很短的第一句。", "start": 0.2, "end": 1.7, "aligned_ratio": 1.0},
        {"index": 2, "text": "很短的第二句。", "start": 1.9, "end": 3.4, "aligned_ratio": 1.0},
        {"index": 3, "text": "正常长度的第三句有五秒钟。", "start": 3.6, "end": 8.8, "aligned_ratio": 1.0},
    ]
    words_4 = []
    for ln in lines_4:
        words_4.extend(_make_words_for_text(ln["text"], ln["start"], ln["end"]))
    res_4 = plan_shots(lines_4, words_4, None, 9.0)
    assert len(res_4["shots"]) == 2
    assert res_4["shots"][0]["source_lines"] == [1, 2]
    assert "很短的第一句。" in res_4["shots"][0]["dialogue"]
    assert "很短的第二句。" in res_4["shots"][0]["dialogue"]
    assert res_4["shots"][1]["source_lines"] == [3]
    _assert_25fps_and_contiguous(res_4, 9.0)


# ============================================================================
# T3: plan_shots 用 Project #34 真实 timeline.json + vocals.wav（只读）
# ============================================================================

def test_t3_plan_shots_project_34_readonly():
    tl_path = ROOT / "projects" / "project_034" / "mv" / "audio" / "analysis_000c86a1eb07" / "timeline.json"
    vocals_path = ROOT / "projects" / "project_034" / "mv" / "audio" / "analysis_000c86a1eb07" / "vocals.wav"
    assert tl_path.is_file(), f"Missing {tl_path}"
    assert vocals_path.is_file(), f"Missing {vocals_path}"

    tl = json.loads(tl_path.read_text(encoding="utf-8"))
    words = [w for seg in tl.get("segments", []) for w in seg.get("words", [])]
    res = plan_shots(tl["lines"], words, vocals_path, tl["duration"])

    shots = res["shots"]
    assert len(shots) == 5
    # 原来的第 2 句（6.93～15.52 秒，8.6 秒）被拆开成 Shot 2 和 Shot 3
    assert shots[1]["source_lines"] == [2] and shots[1]["split"] is True
    assert shots[2]["source_lines"] == [2] and shots[2]["split"] is True
    # 每镜时长在 3～7 秒
    for sh in shots:
        assert 3.0 <= sh["seconds"] <= 7.0, f"Shot {sh['shot_no']} duration {sh['seconds']} out of [3, 7]"
    # mode 依次是 CUT / CONTINUE / CONTINUE / CONTINUE / CUT
    assert [sh["mode"] for sh in shots] == ["CUT", "CONTINUE", "CONTINUE", "CONTINUE", "CUT"]
    assert "段首·需换景别" in shots[4]["title"]
    assert "段首·需换景别" not in shots[0]["title"]
    _assert_25fps_and_contiguous(res, 27.88)


# ============================================================================
# T4: 迁移（用 studio.db 和 state.json 的副本）
# ============================================================================

def test_t4_migrate_drama3_state_on_copy():
    backup_dbs = sorted((ROOT / "data" / "backups").glob("studio_before_voice_migration_*.db"))
    real_db = backup_dbs[0] if backup_dbs else (ROOT / "data" / "studio.db")
    real_state = ROOT / "data" / "drama3" / "state.json.bak"
    if not real_state.is_file():
        real_state = ROOT / "data" / "drama3" / "state.json.migrated"
    if not real_state.is_file():
        real_state = ROOT / "data" / "drama3" / "state.json"
    assert real_db.is_file() and real_state.is_file()

    raw_state_data = json.loads(real_state.read_text(encoding="utf-8"))
    expected_items_count = len(raw_state_data.get("items") or [])
    expected_unique_paths = {
        str(v.get("local_path") or "").strip()
        for v in [
            *(raw_state_data.get("voices") or []),
            *(vv for it in (raw_state_data.get("items") or []) for vv in (it.get("voices") or [])),
        ]
        if str(v.get("local_path") or "").strip()
    }

    with tempfile.TemporaryDirectory(prefix="koubo_t4_") as tmp:
        tmp_dir = Path(tmp)
        db_copy = tmp_dir / "studio.db"
        drama_dir = tmp_dir / "drama3"
        drama_dir.mkdir(parents=True, exist_ok=True)
        state_copy = drama_dir / "state.json"

        shutil.copy2(real_db, db_copy)
        shutil.copy2(real_state, state_copy)

        # 确保副本库中已跑过 init_db 建好新表
        with patch("backend.core.db.DB_PATH", db_copy):
            from backend.core.db import init_db
            init_db()

        res1 = migrate_drama3_state(state_path=state_copy, db_path=db_copy)
        assert res1["migrated"] is True
        assert not state_copy.exists()
        assert (drama_dir / "state.json.migrated").is_file()
        assert (drama_dir / "state.json.bak").is_file()
        assert len(list((tmp_dir / "backups").glob("studio_before_voice_migration_*.db"))) >= 1

        conn = sqlite3.connect(db_copy)
        jobs_cnt = conn.execute("SELECT COUNT(*) FROM voice_jobs").fetchone()[0]
        profiles_cnt = conn.execute("SELECT COUNT(*) FROM voice_profiles").fetchone()[0]
        unassigned_cnt = conn.execute("SELECT COUNT(*) FROM voice_jobs WHERE project_id IS NULL").fetchone()[0]
        conn.close()

        assert jobs_cnt == expected_items_count
        assert unassigned_cnt == expected_items_count
        assert profiles_cnt == len(expected_unique_paths)

        # 连续执行第二次（幂等性验证：直接调不会重复，即使把 .migrated 拷回 state.json 再跑也不会重复插入）
        res2 = migrate_drama3_state(state_path=state_copy, db_path=db_copy)
        assert res2["migrated"] is False

        shutil.copy2(drama_dir / "state.json.migrated", state_copy)
        (drama_dir / "state.json.migrated").unlink()
        res3 = migrate_drama3_state(state_path=state_copy, db_path=db_copy)
        assert res3["migrated"] is True

        conn = sqlite3.connect(db_copy)
        assert conn.execute("SELECT COUNT(*) FROM voice_jobs").fetchone()[0] == expected_items_count
        assert conn.execute("SELECT COUNT(*) FROM voice_profiles").fetchone()[0] == len(expected_unique_paths)
        conn.close()


# ============================================================================
# T5: 配音队列与重启恢复（mock ComfyUI，独立临时库）
# ============================================================================

def _write_dummy_wav(path: Path, duration_sec: float = 2.0, sr: int = 44100, channels: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = int(duration_sec * sr)
    samples = np.zeros(n_frames * channels, dtype=np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())


async def test_t5_voice_queue_and_recovery():
    with tempfile.TemporaryDirectory(prefix="koubo_t5_") as tmp:
        tmp_dir = Path(tmp)
        test_db = tmp_dir / "studio.db"
        outputs_dir = tmp_dir / "outputs"
        voices_dir = tmp_dir / "voices"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        voices_dir.mkdir(parents=True, exist_ok=True)

        with (
            patch("backend.core.db.DB_PATH", test_db),
            patch("backend.services.voice_jobs.DRAMA3_OUTPUTS_DIR", outputs_dir),
            patch("backend.services.voice_jobs.DRAMA3_VOICES_DIR", voices_dir),
        ):
            from backend.core.db import init_db
            from backend.core.runtime import rt, voice_job_queue
            from backend.core.schemas import ProjectIn, VoiceJobCreateIn
            from backend.routers.projects import projects_create
            from backend.services.voice_jobs import (
                _update_voice_job_fields,
                create_project_voice_job,
                enqueue_voice_job,
                get_voice_job,
                recover_voice_jobs,
                voice_queue_worker,
            )

            init_db()
            # 清空可能残留的队列
            while not voice_job_queue.empty():
                voice_job_queue.get_nowait()
                voice_job_queue.task_done()

            proj = await projects_create(ProjectIn(name="T5 Queue Test Project"))
            pid = int(proj["id"])

            job_a = create_project_voice_job(
                pid,
                VoiceJobCreateIn(title="Task A", script_text="<|speaker:0|> [sighs] 第一条测试配音任务。"),
            )
            job_b = create_project_voice_job(
                pid,
                VoiceJobCreateIn(title="Task B", script_text="<|speaker:0|> [excited] 第二条测试配音任务。"),
            )

            execution_order: list[int] = []

            class FakeComfyClient:
                def __init__(self, base_url: str):
                    self.base_url = base_url
                    self.ws_base = "ws://127.0.0.1:9"

                async def require_nodes(self, nodes):
                    return None

                async def upload_input_file(self, path, subfolder=""):
                    return Path(path).name

                async def submit(self, workflow, client_id):
                    # 记录执行顺序并稍微等待，确保 B 在队列里排队时不会打断 A
                    await asyncio.sleep(0.05)
                    prompt_id = f"pid_{len(execution_order) + 1}"
                    execution_order.append(len(execution_order) + 1)
                    return prompt_id

                async def history(self, prompt_id):
                    return {
                        prompt_id: {
                            "status": {"completed": True},
                            "outputs": {
                                "202": {
                                    "audio": [
                                        {"filename": f"{prompt_id}.wav", "subfolder": "", "type": "output"}
                                    ]
                                }
                            },
                        }
                    }

                async def download_view(self, meta, target_path: Path, progress_cb=None):
                    _write_dummy_wav(target_path, duration_sec=2.5, sr=44100, channels=1)
                    if progress_cb:
                        await progress_cb(1000, 1000)

            async def fake_monitor(client, client_id, prompt_id, workflow, job_id, project_id):
                await asyncio.sleep(0.05)

            with (
                patch("backend.services.voice_jobs.ComfyClient", FakeComfyClient),
                patch("backend.services.voice_jobs._monitor_voice_job_prompt", side_effect=fake_monitor),
            ):
                worker = asyncio.create_task(voice_queue_worker())
                try:
                    # 连续给 A、B 两条任务点生成：互不取消，按顺序完成
                    r_a = await enqueue_voice_job(job_a["id"])
                    r_b = await enqueue_voice_job(job_b["id"])
                    assert r_a["queued"] is True
                    assert r_b["queued"] is True

                    await asyncio.wait_for(voice_job_queue.join(), timeout=5.0)

                    done_a = get_voice_job(job_a["id"])
                    done_b = get_voice_job(job_b["id"])
                    assert done_a["status"] == "DONE"
                    assert done_b["status"] == "DONE"
                    assert done_a["has_audio"] is True
                    assert done_b["has_audio"] is True
                    assert execution_order == [1, 2]
                finally:
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass

            # 模拟重启恢复：RUNNING 变成 FAILED，QUEUED 重新放回队列
            _update_voice_job_fields(job_a["id"], status="RUNNING", error="")
            _update_voice_job_fields(job_b["id"], status="QUEUED", error="")

            await recover_voice_jobs()
            after_a = get_voice_job(job_a["id"])
            after_b = get_voice_job(job_b["id"])
            assert after_a["status"] == "FAILED"
            assert "重启" in after_a["error"]
            assert after_b["status"] == "QUEUED"
            assert voice_job_queue.qsize() == 1
            requeued_id = voice_job_queue.get_nowait()
            voice_job_queue.task_done()
            assert requeued_id == job_b["id"]


# ============================================================================
# T6: use-as-project-audio 与自动分镜落库（使用新建测试项目与独立临时库）
# ============================================================================

def test_t6_use_as_project_audio_and_from_timeline():
    with tempfile.TemporaryDirectory(prefix="koubo_t6_") as tmp:
        tmp_dir = Path(tmp)
        test_db = tmp_dir / "studio.db"
        projects_dir = tmp_dir / "projects"
        outputs_dir = tmp_dir / "outputs"
        voices_dir = tmp_dir / "voices"
        projects_dir.mkdir(parents=True, exist_ok=True)
        outputs_dir.mkdir(parents=True, exist_ok=True)
        voices_dir.mkdir(parents=True, exist_ok=True)

        with (
            patch("backend.core.db.DB_PATH", test_db),
            patch("backend.core.paths.DB_PATH", test_db),
            patch("backend.core.paths.PROJECTS_DIR", projects_dir),
            patch("backend.services.repo.PROJECTS_DIR", projects_dir),
            patch("backend.services.songs.PROJECTS_DIR", projects_dir),
            patch("backend.services.voice_jobs.DRAMA3_OUTPUTS_DIR", outputs_dir),
            patch("backend.services.voice_jobs.DRAMA3_VOICES_DIR", voices_dir),
        ):
            from backend.app import app
            from backend.core.db import init_db
            from backend.services.voice_jobs import _update_voice_job_fields

            init_db()
            client = TestClient(app)

            # 1. 创建测试项目和 Scene
            p_resp = client.post("/api/projects", json={"name": "T6 Koubo Test Project"})
            assert p_resp.status_code == 200
            pid = p_resp.json()["id"]

            s_resp = client.post(f"/api/projects/{pid}/scenes", json={"name": "Main Koubo Scene"})
            assert s_resp.status_code == 200
            scene_id = s_resp.json()["id"]

            # 2. 创建配音任务并模拟已完成输出音频
            j_resp = client.post(
                f"/api/projects/{pid}/voice-jobs",
                json={
                    "title": "口播测试 #1",
                    "script_text": (
                        "<|speaker:0|> [sighs] 姐妹们听我一句劝，千万别再花冤枉钱了！\n"
                        "<|speaker:0|> [excited] 想要的赶紧在评论区扣个一，我们下期见！"
                    ),
                },
            )
            assert j_resp.status_code == 200
            job_id = j_resp.json()["id"]

            dummy_flac = outputs_dir / f"voice_job_{job_id}.wav"
            _write_dummy_wav(dummy_flac, duration_sec=10.5, sr=44100, channels=1)
            _update_voice_job_fields(job_id, status="DONE", progress=100.0, audio_path=str(dummy_flac), duration=10.5)

            # 3. 首次调用 use-as-project-audio -> 200 OK
            use_resp = client.post(f"/api/voice-jobs/{job_id}/use-as-project-audio", json={"confirm_replace": False})
            assert use_resp.status_code == 200, use_resp.text
            use_data = use_resp.json()
            assert use_data["ok"] is True
            assert use_data["next"] == "analyze"
            assert use_data["lyrics_lines"] == [
                "姐妹们听我一句劝，千万别再花冤枉钱了！",
                "想要的赶紧在评论区扣个一，我们下期见！",
            ]

            # 校验 mv_projects 字段与转码出的 48kHz 双声道 WAV
            mv_resp = client.get(f"/api/projects/{pid}/mv")
            assert mv_resp.status_code == 200
            mv_data = mv_resp.json()
            assert mv_data["audio_source"] == "voice_job"
            assert mv_data["source_voice_job_id"] == job_id
            assert mv_data["song_status"] == "DONE"
            assert mv_data["song_node"] == f"配音台 · 任务 #{job_id}"
            assert mv_data["audio_linked"] == 1
            assert mv_data["native_audio_enabled"] == 1
            assert mv_data["analysis_status"] == "IDLE"
            assert mv_data["lyrics"] == "姐妹们听我一句劝，千万别再花冤枉钱了！\n想要的赶紧在评论区扣个一，我们下期见！"

            wav_path = Path(mv_data["audio_path"])
            assert wav_path.is_file()
            with wave.open(str(wav_path), "rb") as wf:
                assert wf.getframerate() == 48000
                assert wf.getnchannels() == 2
                assert wf.getsampwidth() == 2

            # 校验任务列表里 is_project_audio == True
            jobs_list = client.get(f"/api/projects/{pid}/voice-jobs").json()
            assert jobs_list[0]["is_project_audio"] is True

            # 4. 创建第二条已完成配音任务，测试已有音频时返回 409 AUDIO_EXISTS，confirm_replace=True 时成功
            j2_resp = client.post(
                f"/api/projects/{pid}/voice-jobs",
                json={"title": "口播测试 #2", "script_text": "<|speaker:0|> 新的一条口播台词。"},
            )
            job2_id = j2_resp.json()["id"]
            dummy_flac2 = outputs_dir / f"voice_job_{job2_id}.wav"
            _write_dummy_wav(dummy_flac2, duration_sec=6.0, sr=44100, channels=1)
            _update_voice_job_fields(job2_id, status="DONE", progress=100.0, audio_path=str(dummy_flac2), duration=6.0)

            conflict_resp = client.post(f"/api/voice-jobs/{job2_id}/use-as-project-audio", json={"confirm_replace": False})
            assert conflict_resp.status_code == 409
            conflict_body = conflict_resp.json()
            assert conflict_body["code"] == "AUDIO_EXISTS"
            assert conflict_body["audio_source"] == "voice_job"
            assert conflict_body["source_voice_job_id"] == job_id

            replace_resp = client.post(f"/api/voice-jobs/{job2_id}/use-as-project-audio", json={"confirm_replace": True})
            assert replace_resp.status_code == 200
            mv_after = client.get(f"/api/projects/{pid}/mv").json()
            assert mv_after["source_voice_job_id"] == job2_id
            assert mv_after["lyrics"] == "新的一条口播台词。"

            # 5. 测试分析接口能正常读取音频和台词（mock run_local_analysis）
            async def fake_run_local_analysis(audio, lyrics, output_dir: Path, progress_cb, *, dry_vocals: bool = False):
                # 配音台挂载的是干声：分析时应跳过 Demucs 人声分离
                assert dry_vocals is True
                assert Path(audio).is_file()
                assert lyrics == "新的一条口播台词。"
                output_dir.mkdir(parents=True, exist_ok=True)
                _write_dummy_wav(output_dir / "vocals.wav", duration_sec=6.0, sr=16000, channels=1)
                (output_dir / "lyrics.srt").write_text("1\n00:00:00,200 --> 00:00:05,800\n新的一条口播台词。\n", encoding="utf-8")
                tl_payload = {
                    "language": "zh",
                    "duration": 6.0,
                    "lines": [
                        {"index": 1, "text": "新的一条口播台词。", "start": 0.2, "end": 5.8, "aligned_ratio": 1.0}
                    ],
                    "segments": [
                        {"words": _make_words_for_text("新的一条口播台词。", 0.2, 5.8)}
                    ],
                }
                (output_dir / "timeline.json").write_text(json.dumps(tl_payload, ensure_ascii=False), encoding="utf-8")
                return tl_payload

            with patch("backend.services.songs.run_local_analysis", side_effect=fake_run_local_analysis):
                an_resp = client.post(f"/api/projects/{pid}/audio/analyze")
                assert an_resp.status_code == 200, an_resp.text

            # 6. 测试 /api/scenes/{scene_id}/shots/from-timeline 预览、建镜、绑定默认 Ref1、二次替换确认
            import struct, zlib
            def _chunk(ct: bytes, d: bytes) -> bytes:
                return struct.pack(">I", len(d)) + ct + d + struct.pack(">I", zlib.crc32(ct + d) & 0xFFFFFFFF)
            png_bytes = (
                b"\x89PNG\r\n\x1a\n"
                + _chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 6, 0, 0, 0))
                + _chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + (b"\x8b\x5c\xf6\xff" * 16) for _ in range(16))))
                + _chunk(b"IEND", b"")
            )
            up_resp = client.post(
                f"/api/scenes/{scene_id}/images/upload",
                data={"role": "ref1"},
                files={"file": ("dummy_ref1.png", png_bytes, "image/png")},
            )
            assert up_resp.status_code == 200, up_resp.text

            dry_resp = client.post(f"/api/scenes/{scene_id}/shots/from-timeline", json={"dry_run": True})
            assert dry_resp.status_code == 200
            assert dry_resp.json()["dry_run"] is True
            assert len(dry_resp.json()["shots"]) == 1

            create_resp = client.post(f"/api/scenes/{scene_id}/shots/from-timeline", json={"dry_run": False, "replace": False})
            assert create_resp.status_code == 200
            created_shots = client.get(f"/api/scenes/{scene_id}/shots").json()
            assert len(created_shots) == 1
            assert created_shots[0]["mode"] == "CUT"
            assert created_shots[0]["ref1_path"] != ""
            assert Path(created_shots[0]["ref1_path"]).read_bytes() == png_bytes
            assert created_shots[0]["seconds"] == 6.0

            # 再次请求 replace=False 应返回 409 SHOTS_EXIST
            exist_resp = client.post(f"/api/scenes/{scene_id}/shots/from-timeline", json={"dry_run": False, "replace": False})
            assert exist_resp.status_code == 409
            assert exist_resp.json()["code"] == "SHOTS_EXIST"
            assert exist_resp.json()["count"] == 1

            # 带 replace=True 重新建镜成功
            rep_shots_resp = client.post(f"/api/scenes/{scene_id}/shots/from-timeline", json={"dry_run": False, "replace": True})
            assert rep_shots_resp.status_code == 200
            assert len(client.get(f"/api/scenes/{scene_id}/shots").json()) == 1


class KouboTestSuite(unittest.IsolatedAsyncioTestCase):
    def test_t1(self):
        test_t1_extract_spoken_lyrics()

    def test_t2(self):
        test_t2_plan_shots_synthetic()

    def test_t3(self):
        test_t3_plan_shots_project_34_readonly()

    def test_t4(self):
        test_t4_migrate_drama3_state_on_copy()

    async def test_t5(self):
        await test_t5_voice_queue_and_recovery()

    def test_t6(self):
        test_t6_use_as_project_audio_and_from_timeline()


if __name__ == "__main__":
    unittest.main(verbosity=2)

