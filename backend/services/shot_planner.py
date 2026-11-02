"""按音频强制对齐时间轴自动分镜纯算法（25fps 帧边界对齐、长句拆分、短句合并、静音谷底切点）。"""
from __future__ import annotations

import math
import unicodedata
import wave
from pathlib import Path
from typing import Any

import numpy as np

PUNCT_BREAK_CHARS = set("，、；：,;—…")


def _is_punct(s: str) -> bool:
    return not s or all(unicodedata.category(c)[0] in ("P", "Z", "S") for c in str(s))


def _load_vocals_mono(vocals_path: str | Path | None, fallback_duration: float) -> tuple[np.ndarray, int]:
    """读取人声 WAV 为单声道 float32 数组（无需依赖 librosa/soundfile）。"""
    if vocals_path:
        p = Path(vocals_path)
        if p.is_file():
            try:
                with wave.open(str(p), "rb") as wf:
                    sr = wf.getframerate() or 16000
                    nch = wf.getnchannels() or 1
                    sw = wf.getsampwidth()
                    raw = wf.readframes(wf.getnframes())
                if sw == 2:
                    y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                elif sw == 4:
                    y = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
                elif sw == 1:
                    y = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
                else:
                    y = np.zeros(int(max(1.0, fallback_duration) * 16000), dtype=np.float32)
                    return y, 16000
                if nch > 1 and len(y) >= nch:
                    y = y[: (len(y) // nch) * nch].reshape(-1, nch).mean(axis=1)
                return y, int(sr)
            except Exception:
                pass
    n_samples = max(1600, int(max(1.0, float(fallback_duration or 1.0)) * 16000))
    return np.zeros(n_samples, dtype=np.float32), 16000


SILENCE_RMS = 0.015
# 强制对齐的句首/句尾常有 0.2~0.5s 偏差，而含英文专有名词（如 OpenRouter/DeepSeek）的句内逗号插值可能有 1.0~1.5s 偏差；
# 窄窗口找不到静音时，向两侧扩展搜索真实的静音谷底（受 min_t / max_t 保护不会产生过短镜头）。
WIDEN_SEARCH_SECONDS = 0.8
WIDEN_SPLIT_SEARCH_SECONDS = 1.6


def _rms_at(y: np.ndarray, sr: int, t: float) -> float:
    s0 = max(0, int(round((t - 0.04) * sr)))
    s1 = min(len(y), int(round((t + 0.04) * sr)))
    return float(np.sqrt(np.mean(y[s0:s1] ** 2))) if s1 > s0 else 0.0


def _frame_candidates(lo_t: float, hi_t: float) -> list[float]:
    lo = math.ceil(round(lo_t * 25.0, 6)) / 25.0
    hi = math.floor(round(hi_t * 25.0, 6)) / 25.0
    if hi < lo - 1e-9:
        return []
    n_steps = int(round((hi - lo) / 0.04))
    return [round(lo + step * 0.04, 2) for step in range(max(0, n_steps) + 1)]


def snap_25fps_silence(
    t_end: float,
    t_next_start: float,
    y: np.ndarray,
    sr: int,
    *,
    is_split: bool = False,
    min_t: float | None = None,
    max_t: float | None = None,
) -> tuple[float, float]:
    """在相邻两句（或句内拆分点）气口区间内以 0.04s (25fps) 步进寻找 RMS 最低的切点。

    先在 [t_end, t_next_start] 窄窗口里找；最优点仍不是静音（RMS ≥ 0.015）时，
    向两侧各扩展 WIDEN_SEARCH_SECONDS（不越过 min_t / max_t），在所有静音帧中取离原气口中点最近的一个。
    """
    search_end = float(t_end)
    search_next = float(t_next_start)
    if is_split and (search_next - search_end) < 0.04:
        search_end = max(0.0, search_end - 0.04)
        search_next = search_next + 0.04

    mid = (float(t_end) + float(t_next_start)) / 2.0
    mid_aligned = round(mid * 25.0) / 25.0

    def best_of(candidates: list[float]) -> tuple[float, float]:
        best_t, best_rms = round(float(candidates[0]), 2), 999.0
        for c in candidates:
            rms = _rms_at(y, sr, c)
            if rms < best_rms - 1e-9:
                best_rms, best_t = rms, round(float(c), 2)
        return best_t, best_rms

    narrow = _frame_candidates(search_end, search_next) or [mid_aligned]
    best_t, best_rms = best_of(narrow)
    if best_rms < SILENCE_RMS:
        return best_t, best_rms

    widen_s = WIDEN_SPLIT_SEARCH_SECONDS if is_split else WIDEN_SEARCH_SECONDS
    lo_bound = search_end - widen_s
    hi_bound = search_next + widen_s
    if min_t is not None:
        lo_bound = max(lo_bound, float(min_t))
    if max_t is not None:
        hi_bound = min(hi_bound, float(max_t))
    wide = _frame_candidates(max(0.0, lo_bound), hi_bound)
    wide_rms = [_rms_at(y, sr, c) for c in wide]
    silent_idx = [i for i, r in enumerate(wide_rms) if r < SILENCE_RMS]
    if silent_idx:
        # 取离原气口中点最近的那一段连续静音，再在这段静音里取最安静的一帧
        nearest = min(silent_idx, key=lambda i: abs(wide[i] - mid))
        lo_i = hi_i = nearest
        while lo_i - 1 >= 0 and wide_rms[lo_i - 1] < SILENCE_RMS:
            lo_i -= 1
        while hi_i + 1 < len(wide) and wide_rms[hi_i + 1] < SILENCE_RMS:
            hi_i += 1
        pick = min(range(lo_i, hi_i + 1), key=lambda i: (wide_rms[i], abs(wide[i] - mid)))
        return round(float(wide[pick]), 2), wide_rms[pick]
    if wide:
        pick = min(range(len(wide)), key=lambda i: wide_rms[i])
        if wide_rms[pick] < best_rms - 1e-9:
            return round(float(wide[pick]), 2), wide_rms[pick]
    return best_t, best_rms


def _align_words_to_text(text: str, unit_words: list[dict[str, Any]]) -> tuple[list[int], list[int], set[int]]:
    """计算每个 word 在原文 text 中的起止字符下标，并识别标点断点位置 k（第 k 个字之后）。"""
    m = len(unit_words)
    if m == 0:
        return [], [], set()

    non_punct_indices = [i for i, ch in enumerate(text) if not _is_punct(ch)]
    char_starts: list[int] = []
    char_ends: list[int] = []

    if len(non_punct_indices) == m:
        for idx in non_punct_indices:
            char_starts.append(idx)
            char_ends.append(idx + 1)
    else:
        cursor = 0
        for idx, w in enumerate(unit_words):
            token = str(w.get("word") or "").strip()
            pos = text.find(token, cursor) if token else -1
            if pos != -1:
                char_starts.append(pos)
                char_ends.append(pos + len(token))
                cursor = pos + len(token)
            else:
                approx_s = int(round(idx * len(text) / m))
                approx_e = int(round((idx + 1) * len(text) / m))
                char_starts.append(approx_s)
                char_ends.append(max(approx_s + 1, approx_e))

    punct_breaks: set[int] = set()
    for k in range(1, m):
        between = text[char_ends[k - 1] : char_starts[k]]
        if any(ch in PUNCT_BREAK_CHARS for ch in between):
            punct_breaks.add(k)

    return char_starts, char_ends, punct_breaks


def _slice_text_by_breaks(
    text: str,
    char_starts: list[int],
    breaks: list[int],
    total_words: int,
) -> list[str]:
    """按选定的字下标断点列表切分原文，保留标点符号。"""
    if not breaks:
        return [text.strip()]
    boundaries = [0, *breaks, total_words]
    segments: list[str] = []
    for i in range(len(boundaries) - 1):
        k_start = boundaries[i]
        k_end = boundaries[i + 1]
        c_start = 0 if k_start == 0 else char_starts[k_start]
        c_end = len(text) if k_end >= total_words else char_starts[k_end]
        seg_text = text[c_start:c_end].strip()
        segments.append(seg_text)
    return segments


def _split_long_unit(
    unit: dict[str, Any],
    *,
    min_shot: float,
    target_mid: float,
    soft_max: float,
    hard_max: float,
) -> list[dict[str, Any]]:
    """步骤 2：拆分时长超过 soft_max 的长句。"""
    u_start = float(unit["start"])
    u_end = float(unit["end"])
    u_dur = u_end - u_start
    if u_dur <= soft_max + 1e-6:
        return [unit]

    u_words = unit["words"]
    m = len(u_words)
    line_no = unit["source_lines"][0]
    text = str(unit["text"] or "")

    if m < 2:
        warn = f"Line {line_no} runs {u_dur:.2f}s, over {soft_max:g}s with no break point"
        unit["warnings"].append(warn)
        return [unit]

    char_starts, _char_ends, punct_breaks = _align_words_to_text(text, u_words)
    pause_breaks: set[int] = set()
    for k in range(1, m):
        gap = float(u_words[k]["start"]) - float(u_words[k - 1]["end"])
        if gap >= 0.25 - 1e-6:
            pause_breaks.add(k)

    candidates = sorted(punct_breaks | pause_breaks)

    def seg_bounds(k_a: int, k_b: int) -> tuple[float, float]:
        s = u_start if k_a == 0 else float(u_words[k_a]["start"])
        e = u_end if k_b == m else float(u_words[k_b - 1]["end"])
        return s, e

    chosen_breaks: list[int] = []
    split_warnings: list[str] = []

    if candidates:
        nodes = [0, *candidates, m]
        n_nodes = len(nodes)
        # dp[j][splits_used_flag] -> (cost, prev_node_idx)
        INF = 1e18
        dp_cost = [[INF, INF] for _ in range(n_nodes)]
        dp_prev = [[-1, -1] for _ in range(n_nodes)]
        dp_cost[0][0] = 0.0

        for j in range(1, n_nodes):
            k_b = nodes[j]
            bp_penalty = 0.0 if (k_b == m or k_b in punct_breaks) else 0.5
            for i in range(j):
                k_a = nodes[i]
                s, e = seg_bounds(k_a, k_b)
                dur = e - s
                if min_shot - 1e-6 <= dur <= soft_max + 1e-6:
                    c_seg = (dur - target_mid) ** 2
                elif dur <= hard_max + 1e-6:
                    over = max(0.0, dur - soft_max) + max(0.0, min_shot - dur)
                    c_seg = 100.0 + over * 50.0 + (dur - target_mid) ** 2
                else:
                    c_seg = 10000.0 + (dur - hard_max) * 500.0

                used_split = 1 if (i > 0 or j < n_nodes - 1) else 0
                for prev_flag in (0, 1):
                    if dp_cost[i][prev_flag] >= INF:
                        continue
                    next_flag = 1 if (prev_flag == 1 or used_split == 1) else 0
                    total_c = dp_cost[i][prev_flag] + c_seg + bp_penalty
                    if total_c < dp_cost[j][next_flag]:
                        dp_cost[j][next_flag] = total_c
                        dp_prev[j][next_flag] = i * 2 + prev_flag

        if dp_cost[n_nodes - 1][1] < INF:
            cur_idx = n_nodes - 1
            cur_flag = 1
            rev_nodes: list[int] = []
            while cur_idx > 0:
                encoded = dp_prev[cur_idx][cur_flag]
                if encoded < 0:
                    break
                prev_idx = encoded // 2
                prev_flag = encoded % 2
                if cur_idx < n_nodes - 1:
                    rev_nodes.append(nodes[cur_idx])
                cur_idx = prev_idx
                cur_flag = prev_flag
            chosen_breaks = list(reversed(rev_nodes))

        if any(k not in punct_breaks for k in chosen_breaks):
            split_warnings.append(f"第 {line_no} 句（{u_dur:.2f}s）无标点断点，已在停顿处拆分")

    # 检查是否仍有超过 hard_max 的片段，或根本没有候选断点
    if not chosen_breaks:
        if u_dur <= hard_max + 1e-6:
            unit["warnings"].append(f"Line {line_no} runs {u_dur:.2f}s, over {soft_max:g}s with no break point")
            return [unit]

    # 对任何仍超过 hard_max 的区间，在字间最大间隔处强制切开
    final_breaks: list[int] = []
    check_bounds = [0, *chosen_breaks, m]
    forced_any = False
    for idx_b in range(len(check_bounds) - 1):
        k_start = check_bounds[idx_b]
        k_end = check_bounds[idx_b + 1]
        if idx_b > 0:
            final_breaks.append(k_start)

        # 用队列递归切分超过 hard_max 的子段
        sub_queue: list[tuple[int, int]] = [(k_start, k_end)]
        while sub_queue:
            sa, sb = sub_queue.pop(0)
            s_time, e_time = seg_bounds(sa, sb)
            if (e_time - s_time) <= hard_max + 1e-6 or (sb - sa) < 2:
                if sa > k_start:
                    final_breaks.append(sa)
                continue
            forced_any = True
            # 找 [sa+1, sb-1] 中使两侧尽量合法且字间间隔最大的 k
            best_k = (sa + sb) // 2
            best_score = (-1e9, -1e9)
            for k in range(sa + 1, sb):
                gap = float(u_words[k]["start"]) - float(u_words[k - 1]["end"])
                s1, e1 = seg_bounds(sa, k)
                s2, e2 = seg_bounds(k, sb)
                d1, d2 = e1 - s1, e2 - s2
                both_under_hard = 1 if (d1 <= hard_max + 1e-6 and d2 <= hard_max + 1e-6) else 0
                balance = -abs(d1 - d2)
                score = (both_under_hard, round(gap, 4), balance)
                if score > best_score:
                    best_score = score
                    best_k = k
            sub_queue.insert(0, (best_k, sb))
            sub_queue.insert(0, (sa, best_k))

    chosen_breaks = sorted(set(final_breaks))
    if forced_any:
        split_warnings.append(f"第 {line_no} 句（{u_dur:.2f}s）超过 {hard_max:g} 秒且无标点断点，已在最大停顿处强制切开")

    if not chosen_breaks:
        return [unit]

    text_parts = _slice_text_by_breaks(text, char_starts, chosen_breaks, m)
    boundaries = [0, *chosen_breaks, m]
    out_units: list[dict[str, Any]] = []
    for i in range(len(boundaries) - 1):
        ka, kb = boundaries[i], boundaries[i + 1]
        s_t, e_t = seg_bounds(ka, kb)
        u_warns = list(unit["warnings"]) if i == 0 else []
        if i == 0 and split_warnings:
            u_warns.extend(split_warnings)
        out_units.append({
            "source_lines": list(unit["source_lines"]),
            "text": text_parts[i] if i < len(text_parts) else "",
            "start": s_t,
            "end": e_t,
            "words": u_words[ka:kb],
            "split": True,
            "warnings": u_warns,
        })
    return out_units


def _merge_short_units(
    units: list[dict[str, Any]],
    *,
    min_shot: float,
    soft_max: float,
) -> list[dict[str, Any]]:
    """步骤 3：合并时长小于 min_shot 的短句（优先与较短的相邻单元合并，且合并后不超过 soft_max）。"""
    result = list(units)
    unmergeable_ids: set[int] = set()

    while True:
        merged_this_round = False
        for i, u in enumerate(result):
            dur = float(u["end"]) - float(u["start"])
            if dur >= min_shot - 1e-6 or id(u) in unmergeable_ids:
                continue

            neighbors: list[tuple[float, int]] = []
            if i > 0:
                prev_dur = float(result[i - 1]["end"]) - float(result[i - 1]["start"])
                neighbors.append((prev_dur, i - 1))
            if i + 1 < len(result):
                next_dur = float(result[i + 1]["end"]) - float(result[i + 1]["start"])
                neighbors.append((next_dur, i + 1))
            neighbors.sort(key=lambda item: item[0])

            did_merge = False
            for _ndur, j in neighbors:
                first_idx, second_idx = (j, i) if j < i else (i, j)
                ua, ub = result[first_idx], result[second_idx]
                combined_dur = float(ub["end"]) - float(ua["start"])
                if combined_dur <= soft_max + 1e-6:
                    src_lines = list(ua["source_lines"])
                    for ln in ub["source_lines"]:
                        if ln not in src_lines:
                            src_lines.append(ln)
                    sep = "" if (not ua["text"] or not ub["text"] or _is_punct(ua["text"][-1])) else " "
                    # 中文不额外加空格
                    if ua["text"] and ub["text"] and ord(ua["text"][-1]) > 127:
                        sep = ""
                    merged_unit = {
                        "source_lines": src_lines,
                        "text": f"{ua['text']}{sep}{ub['text']}".strip(),
                        "start": float(ua["start"]),
                        "end": float(ub["end"]),
                        "words": [*ua["words"], *ub["words"]],
                        "split": bool(ua["split"] or ub["split"]),
                        "warnings": [*ua["warnings"], *ub["warnings"]],
                    }
                    result[first_idx : second_idx + 1] = [merged_unit]
                    did_merge = True
                    merged_this_round = True
                    break

            if not did_merge:
                u["warnings"].append(f"镜头时长 {dur:.2f} 秒短于 {min_shot:g} 秒，且无法与相邻句合并")
                unmergeable_ids.add(id(u))
            else:
                break

        if not merged_this_round:
            break

    return result


def plan_shots(
    lines: list[dict[str, Any]],
    words: list[dict[str, Any]] | None,
    vocals_path: str | Path | None,
    audio_duration: float,
    cfg: dict[str, Any] | Any | None = None,
) -> dict[str, Any]:
    """根据对齐时间轴规划镜头切点与参数。"""
    if cfg is None:
        cfg_dict: dict[str, Any] = {}
    elif hasattr(cfg, "model_dump"):
        cfg_dict = cfg.model_dump()
    elif isinstance(cfg, dict):
        cfg_dict = dict(cfg)
    else:
        cfg_dict = dict(vars(cfg))

    target_min = float(cfg_dict.get("target_min", 5.0))
    target_max = float(cfg_dict.get("target_max", 6.0))
    soft_max = float(cfg_dict.get("soft_max", 7.0))
    hard_max = float(cfg_dict.get("hard_max", 9.6))
    min_shot = float(cfg_dict.get("min_shot", 3.0))
    group_size = max(1, int(cfg_dict.get("group_size", 4)))
    target_mid = (target_min + target_max) / 2.0

    if not lines:
        raise ValueError("timeline.lines 为空，无法自动分镜")

    clean_words = [
        {
            "word": str(w.get("word", "")),
            "start": float(w["start"]),
            "end": float(w["end"]),
            "score": float(w.get("score") or 0.0),
        }
        for w in (words or [])
        if isinstance(w, dict)
        and w.get("start") is not None
        and w.get("end") is not None
        and not _is_punct(str(w.get("word", "")))
    ]

    # 第 1 步：建立句子单元
    raw_units: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        line_no = int(line.get("index") or (idx + 1))
        l_start = float(line["start"])
        l_end = float(line["end"])
        text = str(line.get("text") or "").strip()
        line_words: list[dict[str, Any]] = []
        for w in clean_words:
            if w["start"] >= l_start - 1e-3 and w["start"] <= l_end + 1e-3:
                line_words.append({
                    **w,
                    "end": min(float(w["end"]), l_end) if w["end"] > l_end else float(w["end"]),
                })
        raw_units.append({
            "source_lines": [line_no],
            "text": text,
            "start": l_start,
            "end": l_end,
            "words": line_words,
            "split": False,
            "warnings": [],
        })

    # 第 2 步：拆分长句
    split_units: list[dict[str, Any]] = []
    for u in raw_units:
        split_units.extend(
            _split_long_unit(
                u,
                min_shot=min_shot,
                target_mid=target_mid,
                soft_max=soft_max,
                hard_max=hard_max,
            )
        )

    # 第 3 步：合并短句
    units = _merge_short_units(split_units, min_shot=min_shot, soft_max=soft_max)

    # 第 4 步：确定 25fps 切点
    total_dur = float(audio_duration or 0.0)
    if total_dur <= 0 and units:
        total_dur = float(units[-1]["end"])
    end_25fps = round(math.floor(round(total_dur * 25.0, 6)) / 25.0, 2)
    y, sr = _load_vocals_mono(vocals_path, total_dur)

    n_units = len(units)
    cuts: list[float] = [0.0]
    cut_rms_list: list[float] = []
    top_warnings: list[str] = []

    for i in range(n_units - 1):
        prev_u = units[i]
        next_u = units[i + 1]
        is_mid_split = (prev_u["source_lines"] == next_u["source_lines"])
        cut_t, cut_rms = snap_25fps_silence(
            prev_u["end"],
            next_u["start"],
            y,
            sr,
            is_split=is_mid_split,
            # 扩展搜索时每镜至少保留 1 秒，避免切点越过整句
            min_t=max(float(prev_u["start"]), cuts[-1]) + 1.0,
            max_t=float(next_u["end"]) - 1.0,
        )
        # 确保切点严格递增且至少留 1 帧 (0.04s)
        min_allowed = round(cuts[-1] + 0.04, 2)
        max_allowed = round(end_25fps - 0.04 * (n_units - 1 - i), 2)
        cut_t = max(min_allowed, min(max_allowed, cut_t))
        cuts.append(cut_t)
        cut_rms_list.append(cut_rms)

        if cut_rms >= SILENCE_RMS:
            if is_mid_split:
                w_msg = f"Shot {i + 1} 与 Shot {i + 2} 的切点不在静音处（rms={cut_rms:.3f}），句中切开，请人工确认"
            else:
                w_msg = f"Shot {i + 1} 与 Shot {i + 2} 的切点不在静音处（rms={cut_rms:.3f}），请人工确认"
            prev_u["warnings"].append(w_msg)

    cuts.append(end_25fps)
    cut_rms_list.append(0.0)

    # 第 5 步：生成 Shot 字段并校验不变量
    shots_out: list[dict[str, Any]] = []
    for i, u in enumerate(units):
        shot_no = i + 1
        t_start = round(cuts[i], 2)
        t_end = round(cuts[i + 1], 2)
        dur = round(t_end - t_start, 2)

        if dur <= 0:
            raise ValueError(f"Shot {shot_no} 时长非正 ({dur}s)")
        if dur > hard_max + 1e-4:
            u["warnings"].append(f"Shot {shot_no} 切点对齐后时长 {dur:.2f} 秒超过上限 {hard_max:g} 秒")

        is_group_head = (i % group_size == 0)
        mode = "CUT" if is_group_head else "CONTINUE"
        if is_group_head and i > 0:
            title = f"镜头 {shot_no} · 段首·需换景别"
        else:
            title = f"镜头 {shot_no}"

        for w_msg in u["warnings"]:
            if w_msg not in top_warnings:
                top_warnings.append(w_msg)

        shots_out.append({
            "shot_no": shot_no,
            "title": title,
            "mode": mode,
            "timeline_start": t_start,
            "timeline_end": t_end,
            "target_duration": dur,
            "seconds": dur,
            "dialogue": u["text"],
            "source_lines": u["source_lines"],
            "split": bool(u["split"]),
            "cut_rms": round(float(cut_rms_list[i]), 4),
            "warnings": list(u["warnings"]),
        })

    return {
        "audio_duration": round(total_dur, 4),
        "shots": shots_out,
        "warnings": top_warnings,
    }
