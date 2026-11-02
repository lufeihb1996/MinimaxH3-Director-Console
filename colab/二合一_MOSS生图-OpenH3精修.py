# -*- coding: utf-8 -*-
"""
H3 b15-49（视频一采） + 英伟达 Open-H3 1080p/2K 精修（视频二采） + MOSS-Image 5B（SFT 满血生图）
====================================================================================================================
Colab A100 二合一满血极速一键启动脚本（无音频模型版）
--------------------------------------------------------------------------------------------------------------------
本脚本从「三合一.py」抽离并升级，去掉了 Fish Audio Drama 3 与 Qwen3-ASR 音频全家桶（省去 ~36GB 音频模型下载），
专为「MOSS-Image 5B 生图 + H3 b15-49 视频生成（含英伟达 Open-H3 1080p/2K 精修二采）」打造：
生图与视频切换时由后端自动通过 /free 调度显存，无需重启 Colab、无需切换环境。

核心包含：
1. 【生图】MOSS-Image 5B SFT INT8 ConvRot + Qwen3.5-9B INT8 ConvRot + FLUX.2 VAE + SkipLayerGuidanceDiTSimple (SLG=22)
2. 【视频一采】H3 b15-49 主模型 + Qwen3-VL 32B INT8 ConvRot + 视频/音频 VAE + 写实/NaughtyTimes LoRA
3. 【视频二采】NVIDIA Open-H3-Regenerate 1080p/2K 精修栈：
   - 3D 潜变量放大器 (MinimaxH3LatentUpscaler3D: 0.4MP -> 1080p 2.07MP / 2K 3.69MP)
   - OpenH3RegenLora (open_h3_regenerate_2k_lora + sharp_curve_branches 50层高频锐化分支)
   - OpenH3RegenSigmas (3步 Euler 极速精修，start_sigma=0.78 锁动作防慢放)
   - 32KB ref2va adaln_t_table HTTP Range 毫秒级提取（免下载 27.3GB ref2va 底模！）
   - 已内置 regen_core.invert_table 3D 多维时间步去重补丁，完美兼容 CUT 参考图 / CONTINUE 首帧引导 / MV 锁原声
4. 【下载】热门文件走 hf-xet 4 路极速并发；b15-49 与 qwen3.5_9b 冷门分块文件自动切普通 HTTP 直下（彻底避开 429 与 72% 卡死）
5. 【加速】官方 comfy-kitchen (0.2.39/0.2.38) CUDA 算子预检 + Cloudflare HTTP/2 固定隧道 + 自动重连看门狗
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = Path("/content") if Path("/content").exists() else Path.cwd()
COMFY = BASE / "ComfyUI"
PORT = 8188
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://your-tunnel.example.com")  # set your own tunnel host

# ===== 固定且配套的源码与模型 Git/HF 提交号（完整 40 位，不跟随 main 漂移）=====
COMFY_REV = "b1a247c9cf9dbfda4b2c0a0c1d50950667eeaa0c"
KJNODES_REV = "17195fcfa8a05f601b766a0f09c9733284f47416"
UPSCALER_PLUGIN_REV = "40316cf008b2fd8663263270669eb4da23f89d2c"
UPSCALER_MODEL_REV = "3f941d5d182014dd5c0a5e16330420ee2d4aa0c6"
OPENH3_REGEN_PLUGIN_REV = "9b686c5ffd380e9d1351f9b8beef6d20d703ca3e"
OPENH3_REGEN_HF_REV = "main"
KITCHEN_VERSIONS = ("0.2.39", "0.2.38")

# 32KB ref2va adaln_t_table 提取目标与官方 SHA256 校验值
REF2VA_ADALN_TABLE_NAME = "minimax_h3_ref2va_adaln_t_table.safetensors"
REF2VA_FULL_NAME = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
REF2VA_ADALN_SHA256 = "c02a6c11888297688c1e6278185ea1f947023acfc69f9003bbcdcec9a229a8e7"

# 内联 01_MOSS_Image_工作流.json (Gzip + Base64)
MOSS_WORKFLOW_GZ_B64 = (
    "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"
    "BYZTXziOZzMahTguEMmiFjn28X8BfSBqw8EqAAA="
)

# ===== Optional extra LoRAs =====
# The original adult-oriented LoRA used during development is published on Civitai:
#   https://civitai.com/models/2836176   ("SexGod's NaughtyTimes MM H3 LoRA v3.0")
# It is not bundled or mirrored here. Download it yourself from Civitai and place the
# .safetensors under ComfyUI/models/loras/, or pass extra direct URLs (comma separated)
# through the EXTRA_LORA_URLS environment variable. Nothing is fetched by default.
BUCKET_LORAS = [
    (url.strip(), url.strip().rsplit("/", 1)[-1].split("?")[0])
    for url in os.environ.get("EXTRA_LORA_URLS", "").split(",")
    if url.strip()
]

# ===== H3 b15-49 + MOSS-Image 5B 固定模型清单 =====
BASE_MODELS = [
    # --- 1. H3 b15-49 两段式视频资产（A100 满血版：INT8 ConvRot + 写实人物 LoRA）---
    (
        "smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models",
        "main",
        "minimax_h3_hybrid_fl2va_ref2va_b15-49-int8.safetensors",
        "diffusion_models",
    ),
    (
        "Comfy-Org/MiniMax-H3",
        "main",
        "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
        "text_encoders",
    ),
    (
        "Comfy-Org/MiniMax-H3",
        "main",
        "vae/minimax_h3_video_vae_int8_convrot.safetensors",
        "vae",
    ),
    (
        "Comfy-Org/MiniMax-H3",
        "0543966fbdce5ba05709a8f2031c94bdba629b4a",
        "vae/minimax_h3_audio_vae_fp32.safetensors",
        "vae",
    ),
    (
        "fal/MiniMax-H3-Realism-People-LoRA",
        "main",
        "h3-realism-people-t2v-i2v-r2v.safetensors",
        "loras",
    ),
    # --- 2. OpenMOSS MOSS-Image 5B SFT 生图资产 ---
    (
        "Comfy-Org/MOSS-Image",
        "main",
        "diffusion_models/moss_image_sft_int8_convrot.safetensors",
        "diffusion_models",
    ),
    (
        "Comfy-Org/MOSS-Image",
        "main",
        "text_encoders/qwen3.5_9b_int8_convrot.safetensors",
        "text_encoders",
    ),
    (
        "Comfy-Org/MOSS-Image",
        "main",
        "vae/flux2-vae.safetensors",
        "vae",
    ),
    # --- 3. 英伟达 Open-H3-Regenerate 1080p/2K 二采精修权重 ---
    (
        "Efficient-Large-Model/Open-H3-Regenerate-2K",
        OPENH3_REGEN_HF_REV,
        "lora/open_h3_regenerate_2k_lora.safetensors",
        "loras",
    ),
    (
        "Efficient-Large-Model/Open-H3-Regenerate-2K",
        OPENH3_REGEN_HF_REV,
        "lora/sharp_curve_branches.safetensors",
        "loras",
    ),
]

# ===== 固定走普通 HTTP 的冷门分块文件白名单（彻底绕开 Xet CAS 重建 72% 卡死与 429 限流）=====
FORCE_HTTP_FILES = {
    "minimax_h3_hybrid_fl2va_ref2va_b15-49-int8.safetensors",
    "qwen3.5_9b_int8_convrot.safetensors",
}


def get_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    try:
        from google.colab import userdata
        return (userdata.get(name) or "").strip()
    except Exception:
        return ""


def kill_legacy_daemons():
    """彻底清理旧版遗留的 _drama3_bg_boot.py 后台死循环看门狗，防止它在后台 pkill -9 main.py。"""
    subprocess.run(["pkill", "-9", "-f", "_drama3_bg_boot.py"], check=False)
    for legacy_file in (BASE / "_drama3_bg_boot.py", BASE / "drama3_boot_daemon.log"):
        try:
            legacy_file.unlink(missing_ok=True)
        except Exception:
            pass
    for marker in BASE.glob("*.deps.ok"):
        try:
            marker.unlink(missing_ok=True)
        except Exception:
            pass


def run(args, cwd=None, check=True):
    if isinstance(args, str):
        print("$", args, flush=True)
        p = subprocess.run(args, cwd=cwd, shell=True, check=False)
    else:
        print("$", " ".join(map(str, args)), flush=True)
        p = subprocess.run(list(map(str, args)), cwd=cwd, check=False)

    if check and p.returncode != 0:
        raise RuntimeError("命令失败: " + (args if isinstance(args, str) else " ".join(map(str, args))))
    return p.returncode


def clone_pinned(url: str, path: Path, revision: str):
    """Pin to an exact 40-char commit. Preserve tracked local edits before changing commits."""
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError(f"必须提供完整40位Git提交号: {revision}")
    fresh = not path.exists()
    if fresh:
        run(["git", "clone", "--filter=blob:none", "--depth", "1", "--no-checkout", url, str(path)])
    if not (path / ".git").is_dir():
        shutil.rmtree(path, ignore_errors=True)
        run(["git", "clone", "--filter=blob:none", "--depth", "1", "--no-checkout", url, str(path)])
        fresh = True

    current = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        text=True, capture_output=True, check=False,
    ).stdout.strip()
    if current == revision:
        ready = subprocess.run(
            ["git", "-C", str(path), "ls-files"],
            text=True, capture_output=True, check=False,
        ).stdout.strip()
        if ready:
            print(f"⏩ 固定版本已就绪：{path.name} @ {revision[:10]}", flush=True)
            return
    elif not fresh:
        dirty = subprocess.run(
            ["git", "-C", str(path), "diff", "--name-only", "HEAD"],
            text=True, capture_output=True, check=False,
        ).stdout.strip()
        if dirty:
            run(["git", "-C", str(path), "stash", "push", "-m", "trinity-before-version-pin"], check=False)

    run(["git", "-C", str(path), "remote", "set-url", "origin", url])
    run(["git", "-C", str(path), "fetch", "--depth", "1", "origin", revision])
    run(["git", "-C", str(path), "checkout", "-f", "--detach", revision])
    print(f"✅ 固定版本：{path.name} @ {revision[:10]}", flush=True)


def ensure_cuda():
    code = (
        "import torch;"
        "print('Torch:', torch.__version__);"
        "print('CUDA:', torch.version.cuda);"
        "print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE');"
        "raise SystemExit(0 if torch.cuda.is_available() else 2)"
    )
    p = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True)
    print(p.stdout, flush=True)

    if p.returncode == 0:
        return

    print("⚠️ 当前 Torch 没有 CUDA，安装官方 cu128...", flush=True)
    run([sys.executable, "-m", "pip", "uninstall", "-y", "torch", "torchvision", "torchaudio"], check=False)
    run([
        sys.executable, "-m", "pip", "install", "-U",
        "torch", "torchvision", "torchaudio",
        "--index-url", "https://download.pytorch.org/whl/cu128",
    ])


def install_if_needed(requirements, req_file=None):
    from importlib.metadata import PackageNotFoundError, version
    try:
        try:
            from packaging.requirements import Requirement
        except ImportError:
            from pip._vendor.packaging.requirements import Requirement
        ready = True
        for raw in requirements:
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            req = Requirement(raw.split(" #", 1)[0].strip())
            if req.marker and not req.marker.evaluate():
                continue
            if req.url or req.extras:
                ready = False
                break
            try:
                installed = version(req.name)
            except PackageNotFoundError:
                ready = False
                break
            if req.specifier and not req.specifier.contains(installed, prereleases=True):
                ready = False
                break
        if ready:
            print(f"⏩ 依赖版本已满足: {req_file or '基础依赖'}", flush=True)
            return
    except Exception:
        pass
    args = [sys.executable, "-m", "pip", "install", "-q", "--prefer-binary", "--disable-pip-version-check"]
    args += ["-r", str(req_file)] if req_file else list(requirements)
    run(args)


def install_base_dependencies():
    install_if_needed([
        "huggingface_hub<2.0.0",
        "hf-xet",
        "transformers>=4.45.0",
        "safetensors>=0.4.3",
        "tokenizers>=0.19.0",
        "triton",
        "einops",
        "typing_extensions",
        "wheel",
        "setuptools",
    ])


def install_comfy_requirements():
    req = COMFY / "requirements.txt"
    if not req.exists():
        raise RuntimeError("ComfyUI requirements.txt 不存在")

    # 仅跳过 torch 家族与 comfy-kitchen，完整保留 comfyui-workflow-templates 等前端包，消除启动日志 ERROR
    skip = {
        "torch",
        "torchvision",
        "torchaudio",
        "triton",
        "comfy-kitchen",
    }
    kept = []

    for raw in req.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            kept.append(raw)
            continue

        name = (
            s.split("=", 1)[0]
            .split("<", 1)[0]
            .split(">", 1)[0]
            .split("[", 1)[0]
            .strip()
            .lower()
            .replace("_", "-")
        )

        if name in skip:
            print("⏩ 跳过 ComfyUI requirements:", s)
        else:
            kept.append(raw)

    tmp = BASE / "comfy_requirements_no_torch.txt"
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    install_if_needed(kept, tmp)


def ensure_vsa_kitchen() -> str:
    """优先安装官方预编译 comfy-kitchen 0.2.39（兼容 0.2.38）。"""
    from importlib.metadata import version
    try:
        cur = version("comfy-kitchen")
        if cur in KITCHEN_VERSIONS:
            print(f"⏩ 官方 comfy-kitchen {cur} 已安装", flush=True)
            return cur
    except Exception:
        pass

    for ver in KITCHEN_VERSIONS:
        print(f"\n⚡ 安装官方预编译 comfy-kitchen {ver}（不编译、不更新 Torch）", flush=True)
        rc = run([
            sys.executable, "-m", "pip", "install",
            "--index-url", "https://pypi.org/simple",
            "--only-binary=:all:", "--no-deps", "--force-reinstall",
            "--no-cache-dir", f"comfy-kitchen=={ver}",
        ], check=False)
        if rc == 0:
            return ver
    raise RuntimeError("comfy-kitchen 官方预编译包安装失败")


def verify_comfy_kitchen():
    """在新进程中核对版本，并实际运行 CUDA 反量化与 INT8 Attention 算子。"""
    code = r"""
import sys
import torch
import comfy_kitchen as ck
from importlib.metadata import version

v = version("comfy-kitchen")
assert v in ("0.2.38", "0.2.39"), f"安装版本错误：{v}"
assert torch.cuda.is_available(), "没有可用 CUDA GPU"
props = torch.cuda.get_device_properties(0)
print("Python:", sys.executable)
print("Kitchen:", v, ck.__file__)
print("Torch:", torch.__version__, "CUDA:", torch.version.cuda)
print("GPU:", props.name, "VRAM GiB:", round(props.total_memory / 1024**3, 2),
      "SM:", torch.cuda.get_device_capability(0))
assert ck.list_backends().get("cuda", {}).get("available"), "CUDA backend 不可用"

x = torch.randn(64, 128, device="cuda", dtype=torch.bfloat16)
scale = (x.abs().max().float() / (448 * 6))
with ck.use_backend("eager"):
    q, blocks = ck.quantize_nvfp4(x, scale)
    expected = ck.dequantize_nvfp4(q, scale, blocks, output_type=torch.float16)
with ck.use_backend("cuda"):
    actual = ck.dequantize_nvfp4(q, scale, blocks, output_type=torch.float16)
torch.cuda.synchronize()
assert actual.shape == expected.shape
assert torch.isfinite(actual).all().item(), "反量化输出含非有限值"
assert torch.allclose(actual, expected, rtol=0.01, atol=0.01), "CUDA 反量化结果异常"
print("CUDA_NVFP4_DEQUANT_OK：实际反量化运行通过")
assert ck.int8_attention_is_available(), "Kitchen INT8 Attention 在当前设备不可用"
print("KITCHEN_ATTENTION_AVAILABLE")
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(COMFY),
        env=os.environ.copy(), text=True, capture_output=True,
    )
    if result.stdout:
        print(result.stdout, flush=True)
    if result.returncode:
        if result.stderr:
            print(result.stderr, flush=True)
        raise RuntimeError("官方 Kitchen 的 GPU 实测未通过；停止启动，请发送上方日志。")


def verify_all_core_modules():
    """同时预检 H3 quant_ops + MOSS-Image 5B 核心模块。"""
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(COMFY)!r});"
        "import comfy.quant_ops;"
        "import comfy.model_prefetch;"
        "import comfy.text_encoders.llama;"
        "from comfy.ldm.moss_image.model import MOSSImage;"
        "from comfy.text_encoders.moss_image import MOSSImageTEModel, MOSSImageTokenizer;"
        "from comfy_extras.nodes_moss_image import MOSSImageLayoutPrompt, MOSSImageControl;"
        "from comfy.text_encoders.qwen35 import GatedAttention, GatedDeltaNet, LinearKV, Qwen35Transformer, Qwen35TransformerBlock;"
        "from comfy_kitchen.int8_decode import Int8DecodeCache;"
        "import comfy_kitchen as ck;"
        "print('COMFY_H3_MOSS_OPENH3_ALL_OK');"
        "print(ck.list_backends())"
    )
    p = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(COMFY),
        env=os.environ.copy(),
        text=True,
        capture_output=True,
    )
    if p.stdout:
        print(p.stdout, flush=True)
    if p.returncode != 0 or "COMFY_H3_MOSS_OPENH3_ALL_OK" not in p.stdout:
        if p.stderr:
            print(p.stderr[-12000:], flush=True)
        raise RuntimeError("ComfyUI H3 / MOSS-Image 5B 模块预检失败，请查看上方错误日志。")
    print("✅ H3 b15-49 + Open-H3 精修 + MOSS-Image 5B 核心模块全部兼容就绪", flush=True)


def patch_openh3_custom_nodes():
    """给 ComfyUI-Open-H3-Regenerate 打 3D 多维时间步去重补丁，
    使其在 CUT (MiniMaxH3ReferenceToVideo)、CONTINUE (MiniMaxH3AddGuide) 与 MV 原声锁死下
    支持 [B, num_tokens, 8] 逐 Token AdaLN 输入并极速去重。
    """
    regen_core_py = COMFY / "custom_nodes" / "ComfyUI-Open-H3-Regenerate" / "regen_core.py"
    if not regen_core_py.exists():
        return
    txt = regen_core_py.read_text(encoding="utf-8")
    if "torch.unique(flat" in txt or "def invert_table(table, coords):" not in txt:
        return
    old_fn = (
        "def invert_table(table, coords):\n"
        "    \"\"\"Model time t of each row of `coords`, points on the piecewise-linear curve `table` (as ComfyUI's curve-form\n"
        "    H3 computes its AdaLN input).  Returns (t, max distance from the curve).\"\"\"\n"
        "    table = table.to(coords.device, torch.float32)\n"
        "    coords = coords.to(torch.float32)\n"
        "    p0, d = table[:-1], table[1:] - table[:-1]                       # segments [S, C]\n"
        "    rel = coords[:, None, :] - p0[None]                               # [M, S, C]\n"
        "    s = ((rel * d[None]).sum(-1) / (d * d).sum(-1).clamp(min=1e-20)[None]).clamp(0.0, 1.0)\n"
        "    dist = (rel - s[..., None] * d[None]).norm(dim=-1)               # [M, S]\n"
        "    best = dist.argmin(dim=1)\n"
        "    rows = torch.arange(coords.shape[0], device=coords.device)\n"
        "    t = (best.to(torch.float32) + s[rows, best]) / (table.shape[0] - 1)\n"
        "    return t, float(dist[rows, best].max()) if coords.shape[0] else 0.0"
    )
    new_fn = (
        "def invert_table(table, coords):\n"
        "    \"\"\"Model time t of each row of `coords` (supports 2D [M, C] and 3D [B, M, C] with deduplication).\"\"\"\n"
        "    table = table.to(coords.device, torch.float32)\n"
        "    orig_shape = coords.shape[:-1]\n"
        "    flat = coords.to(torch.float32).reshape(-1, coords.shape[-1])\n"
        "    if flat.shape[0] == 0:\n"
        "        return torch.empty(orig_shape, device=coords.device, dtype=torch.float32), 0.0\n"
        "    uniq, inv = torch.unique(flat, dim=0, return_inverse=True)\n"
        "    p0, d = table[:-1], table[1:] - table[:-1]\n"
        "    d_norm = (d * d).sum(-1).clamp(min=1e-20)[None]\n"
        "    t_chunks, max_err = [], 0.0\n"
        "    for start in range(0, uniq.shape[0], 2048):\n"
        "        u = uniq[start:start + 2048]\n"
        "        rel = u[:, None, :] - p0[None]\n"
        "        s = ((rel * d[None]).sum(-1) / d_norm).clamp(0.0, 1.0)\n"
        "        dist = (rel - s[..., None] * d[None]).norm(dim=-1)\n"
        "        best = dist.argmin(dim=1)\n"
        "        rows = torch.arange(u.shape[0], device=coords.device)\n"
        "        t_u = (best.to(torch.float32) + s[rows, best]) / (table.shape[0] - 1)\n"
        "        err_u = float(dist[rows, best].max())\n"
        "        if err_u > max_err:\n"
        "            max_err = err_u\n"
        "        t_chunks.append(t_u)\n"
        "    t = torch.cat(t_chunks, dim=0)[inv].reshape(orig_shape)\n"
        "    return t, max_err"
    )
    if old_fn in txt:
        regen_core_py.write_text(txt.replace(old_fn, new_fn), encoding="utf-8")
        print("✅ 已修补 ComfyUI-Open-H3-Regenerate：支持 3D 逐 Token 时间步去重 + Sharp 50 曲线", flush=True)


def deploy_workflows():
    """部署 MOSS-Image 5B 与 H3 01_两次采样.json 到 ComfyUI 工作流目录。"""
    wf_dir = COMFY / "user" / "default" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)

    # 1. MOSS-Image 5B 工作流
    try:
        raw_bytes = gzip.decompress(base64.b64decode(MOSS_WORKFLOW_GZ_B64))
        wf_obj = json.loads(raw_bytes.decode("utf-8"))
        wf_pretty = json.dumps(wf_obj, ensure_ascii=False, indent=2)
        (wf_dir / "01_MOSS_Image_工作流.json").write_text(wf_pretty, encoding="utf-8")
        (BASE / "01_MOSS_Image_工作流.json").write_text(wf_pretty, encoding="utf-8")
    except Exception as exc:
        print(f"⚠️ 写入 MOSS-Image 默认工作流警告: {exc}", flush=True)

    # 2. H3 01_两次采样.json（如本地存在）
    root = Path(__file__).resolve().parent if globals().get("__file__") else Path.cwd()
    h3_wf = root / "01_两次采样.json"
    if h3_wf.is_file():
        h3_dest = wf_dir / "H3_b15-49"
        h3_dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(h3_wf, h3_dest / h3_wf.name)
        print(f"✅ H3 工作流已就位：{h3_dest / h3_wf.name}", flush=True)


def install_custom_nodes():
    custom = COMFY / "custom_nodes"
    custom.mkdir(parents=True, exist_ok=True)

    # 1. KJNodes
    kj_path = custom / "ComfyUI-KJNodes"
    clone_pinned("https://github.com/kijai/ComfyUI-KJNodes.git", kj_path, KJNODES_REV)
    req = kj_path / "requirements.txt"
    if req.exists():
        lines = [
            raw for raw in req.read_text(encoding="utf-8").splitlines()
            if not raw.strip().lower().startswith("torch")
        ]
        if any(x.strip() and not x.strip().startswith("#") for x in lines):
            tmp = BASE / f"req_{kj_path.name}_no_torch.txt"
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            install_if_needed(lines, tmp)

    # 2. Minimax H3 3D Latent Upscaler (两轮采样必需)
    upscaler_target = custom / "Comfyui_Minimax_h3_latent_Upscaler"
    clone_pinned(
        "https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler.git",
        upscaler_target,
        UPSCALER_PLUGIN_REV,
    )

    # 3. NVIDIA Open-H3-Regenerate 1080p/2K 精修节点插件
    regen_target = custom / "ComfyUI-Open-H3-Regenerate"
    clone_pinned(
        "https://github.com/rdh9616/ComfyUI-Open-H3-Regenerate.git",
        regen_target,
        OPENH3_REGEN_PLUGIN_REV,
    )
    patch_openh3_custom_nodes()

    install_if_needed(["einops", "safetensors", "typing_extensions"])
    deploy_workflows()


_EXPECTED_SIZE_CACHE = {}


def expected_size(repo, rev, filename):
    key = (repo, rev, filename)
    if key in _EXPECTED_SIZE_CACHE:
        return _EXPECTED_SIZE_CACHE[key]

    size = None
    try:
        from huggingface_hub import HfApi
        info = HfApi().get_paths_info(
            repo_id=repo, paths=[filename], revision=rev,
            token=get_secret("HF_TOKEN") or None,
        )
        if info:
            size = getattr(info[0], "size", None)
    except Exception as exc:
        print(f"ℹ️ 取不到 {filename} 的声明体积，跳过体积校验（{type(exc).__name__}）", flush=True)

    _EXPECTED_SIZE_CACHE[key] = size
    return size


def download_one(item):
    from huggingface_hub import hf_hub_download

    repo, rev, filename, category = item
    dst_dir = COMFY / "models" / category
    dst_dir.mkdir(parents=True, exist_ok=True)

    target = dst_dir / Path(filename).name
    expect = expected_size(repo, rev, filename)

    if target.is_file() and target.stat().st_size > 1024 * 1024:
        got = target.stat().st_size
        if expect is None or got == expect:
            print(f"⏩ 已存在: {target.name} ({got / 1024 ** 3:.2f} GB)", flush=True)
            return target
        print(
            f"⚠️ 体积不符，删除重下: {target.name}\n"
            f"   本地 {got} 字节 / 应为 {expect} 字节",
            flush=True,
        )
        try:
            target.unlink()
        except Exception:
            pass

    print(f"⬇️ 下载: {target.name}", flush=True)

    downloaded = Path(
        hf_hub_download(
            repo_id=repo,
            revision=rev,
            filename=filename,
            local_dir=str(dst_dir),
            token=get_secret("HF_TOKEN") or None,
        )
    )

    if downloaded.resolve() != target.resolve():
        if target.exists() or target.is_symlink():
            target.unlink()
        try:
            target.symlink_to(downloaded)
        except Exception:
            shutil.copy2(downloaded, target)

    if not target.exists():
        raise RuntimeError(f"模型下载后未找到: {target}")

    got = target.stat().st_size
    if expect is not None and got != expect:
        raise RuntimeError(
            f"{target.name} 下载后体积不符：{got} 字节 / 应为 {expect} 字节。"
            "文件不完整，请重跑本脚本（已完成的文件会自动跳过）。"
        )

    print(f"✅ {target.name} ({got / 1024 ** 3:.2f} GB)", flush=True)
    return target


def _disable_hf_xet_runtime():
    """冷门分块 / 429 兜底：当前进程立即关闭 hf-xet，切回普通 HTTP 直下。"""
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ.pop("HF_XET_HIGH_PERFORMANCE", None)
    try:
        import huggingface_hub.constants as hf_consts
        hf_consts.HF_HUB_DISABLE_XET = True
    except Exception:
        pass
    try:
        import huggingface_hub.utils._runtime as hf_runtime
        hf_runtime._is_xet_available = False
        hf_runtime.is_xet_available = lambda: False
    except Exception:
        pass
    try:
        import huggingface_hub.file_download as hf_fd
        hf_fd.is_xet_available = lambda: False
    except Exception:
        pass


def download_bucket_lora(url: str, filename: str):
    dst_dir = COMFY / "models" / "loras"
    dst_dir.mkdir(parents=True, exist_ok=True)
    target = dst_dir / filename

    if target.is_file() and target.stat().st_size > 1024 * 1024:
        size_gb = target.stat().st_size / (1024 ** 3)
        print(f"⏩ 已存在: {target.name} ({size_gb:.2f} GB)", flush=True)
        return target

    token = get_secret("HF_TOKEN")
    if not token:
        print(
            f"⚠️ 跳过私有 bucket LoRA（未配置 HF_TOKEN）: {filename}\n"
            f"   需要时请设置 HF_TOKEN 后重跑本脚本。",
            flush=True,
        )
        return None

    print(f"⬇️ 下载(私有 bucket): {filename}", flush=True)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp, open(partial, "wb") as fp:
            shutil.copyfileobj(resp, fp, length=1024 * 1024)
            expected = resp.headers.get("Content-Length")
        if expected and partial.stat().st_size != int(expected):
            raise RuntimeError("下载长度与服务器声明不符")
        if partial.stat().st_size <= 1024 * 1024:
            raise RuntimeError("下载内容异常偏小")
        partial.replace(target)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        print(
            f"⚠️ 私有 bucket LoRA 下载失败: {filename}\n"
            f"   {type(exc).__name__}: {exc}\n"
            f"   （不影响其他模型，继续启动）",
            flush=True,
        )
        return None

    print(f"✅ {target.name}", flush=True)
    return target


def download_latent_upscaler():
    from huggingface_hub import hf_hub_download
    dest = COMFY / "models" / "latent_upscale_models"
    dest.mkdir(parents=True, exist_ok=True)
    folder = "minimax_h3_latent_upscaler_3d_conv_v1"
    for filename in ("config.json", "minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors"):
        hf_hub_download(
            repo_id="LBH-123-AI/Minimax_h3_latent_Upscaler",
            revision=UPSCALER_MODEL_REV,
            filename=f"{folder}/{filename}",
            local_dir=str(dest),
            token=get_secret("HF_TOKEN") or None,
        )
    print("✅ 3D 潜变量放大模型已就绪", flush=True)


def download_ref2va_adaln_table():
    """通过 HTTP Range 仅读取 ref2va 前 200KB，精准切片生成 32KB adaln_t_table safetensors，
    免去下载 27.3GB 的 minimax_h3_ref2va_pruned_int8_convrot.safetensors 全量底模！
    """
    diff_dir = COMFY / "models" / "diffusion_models"
    diff_dir.mkdir(parents=True, exist_ok=True)
    target_path = diff_dir / REF2VA_ADALN_TABLE_NAME
    alias_path = diff_dir / REF2VA_FULL_NAME

    def _valid_table_file(p: Path) -> bool:
        if not p.is_file() or p.stat().st_size < 32800:
            return False
        try:
            with p.open("rb") as f:
                hlen = struct.unpack("<Q", f.read(8))[0]
                hdr = json.loads(f.read(hlen).decode("utf-8"))
                info = hdr.get("adaln_t_table")
                if not info or info.get("shape") != [1025, 8]:
                    return False
                start, end = info["data_offsets"]
                f.seek(8 + hlen + start)
                raw = f.read(end - start)
                return hashlib.sha256(raw).hexdigest() == REF2VA_ADALN_SHA256
        except Exception:
            return False

    if not _valid_table_file(target_path):
        url = (
            "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/"
            "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors"
        )
        print("⚡ 正在通过 HTTP Range (前 200KB) 提取 32KB ref2va adaln_t_table（免下载 27.3GB 底模）...", flush=True)
        headers = {"Range": "bytes=0-199999", "User-Agent": "Mozilla/5.0"}
        token = get_secret("HF_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            chunk = resp.read()
        hlen = struct.unpack("<Q", chunk[:8])[0]
        hdr = json.loads(chunk[8 : 8 + hlen].decode("utf-8"))
        info = hdr["adaln_t_table"]
        start, end = info["data_offsets"]
        raw_tensor = chunk[8 + hlen + start : 8 + hlen + end]
        digest = hashlib.sha256(raw_tensor).hexdigest()
        if digest != REF2VA_ADALN_SHA256:
            raise RuntimeError(f"adaln_t_table SHA256 校验失败: {digest}")

        new_hdr = {
            "__metadata__": {"format": "pt", "source": "Comfy-Org/MiniMax-H3/ref2va_adaln_t_table"},
            "adaln_t_table": {
                "dtype": info["dtype"],
                "shape": info["shape"],
                "data_offsets": [0, len(raw_tensor)],
            },
        }
        new_hdr_bytes = json.dumps(new_hdr, separators=(",", ":")).encode("utf-8")
        pad = (8 - (len(new_hdr_bytes) % 8)) % 8
        if pad:
            new_hdr_bytes += b" " * pad
        target_path.write_bytes(struct.pack("<Q", len(new_hdr_bytes)) + new_hdr_bytes + raw_tensor)

    # 若未落盘 27.3GB 全量 ref2va，则把 minimax_h3_ref2va_pruned_int8_convrot.safetensors 指向该 32KB 表文件
    if not alias_path.exists():
        try:
            alias_path.symlink_to(target_path.name)
        except Exception:
            shutil.copy2(target_path, alias_path)
    print(f"✅ Open-H3 50层锐化曲线参考表已就绪: {target_path.name} (32.8 KB)", flush=True)


def _download_hf_models(models_list):
    http_items = [it for it in models_list if Path(it[2]).name in FORCE_HTTP_FILES]
    xet_items = [it for it in models_list if Path(it[2]).name not in FORCE_HTTP_FILES]

    print("\n⬇️ 分通道下载/检查 H3 b15-49 + Open-H3 精修 + MOSS-Image 5B 全部模型...", flush=True)
    print(
        f"⚡ 通道分配：{len(xet_items)} 个热门文件走 hf-xet（4路极速） | "
        f"{len(http_items)} 个冷门分块文件走普通 HTTP（避开 429 与 72% 卡死）",
        flush=True,
    )
    for it in http_items:
        print(f"      HTTP 直下 → {Path(it[2]).name}", flush=True)

    failed = []
    if xet_items:
        with ThreadPoolExecutor(max_workers=4) as pool:
            future_map = {pool.submit(download_one, item): item for item in xet_items}
            for f in as_completed(future_map):
                item = future_map[f]
                try:
                    f.result()
                except Exception as exc:
                    print(
                        f"⚠️ Xet 下载失败，稍后并入 HTTP 兜底: {Path(item[2]).name}\n"
                        f"   {type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    failed.append(item)

    todo = http_items + failed
    if not todo:
        return

    print(
        f"\n🛡️ 关闭 hf-xet，{len(todo)} 个文件改用普通 HTTP 直下（串行稳跑，不卡 72% / 429）...",
        flush=True,
    )
    _disable_hf_xet_runtime()

    for item in todo:
        name = Path(item[2]).name
        last_exc = None
        for attempt, wait_s in enumerate((0, 12, 25, 45), start=1):
            if wait_s:
                print(f"⏳ {name}：等待 {wait_s}s 后重试 ({attempt}/4)...", flush=True)
                time.sleep(wait_s)
            try:
                download_one(item)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                print(
                    f"⚠️ HTTP 重试 {attempt}/4 失败: {name}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
        if last_exc is not None:
            raise RuntimeError(
                f"{name} 下载连续失败。已保留缓存，稍后重跑脚本会继续检查/续传。"
            ) from last_exc


def download_all_models(models_list):
    with ThreadPoolExecutor(max_workers=3) as pool:
        bucket_futures = [
            pool.submit(download_bucket_lora, url, name)
            for url, name in BUCKET_LORAS
        ]
        upscaler_future = pool.submit(download_latent_upscaler)
        adaln_future = pool.submit(download_ref2va_adaln_table)
        _download_hf_models(models_list)
        upscaler_future.result()
        adaln_future.result()
        for future in bucket_futures:
            future.result()
    # 全部下载线程结束后统一清理各目录 .cache，避免并发下载中途误删同目录其他文件的临时缓存
    models_root = COMFY / "models"
    if models_root.is_dir():
        for cache_dir in models_root.rglob(".cache"):
            if cache_dir.is_dir():
                shutil.rmtree(cache_dir, ignore_errors=True)


def wait_port(proc, log_path: Path, timeout=180):
    start = time.time()
    print(f"⏳ 等待 ComfyUI :{PORT}", end="", flush=True)

    while time.time() - start < timeout:
        if proc.poll() is not None:
            print("\n❌ ComfyUI 提前退出", flush=True)
            if log_path.exists():
                print(log_path.read_text(encoding="utf-8", errors="ignore")[-10000:])
            raise RuntimeError(f"ComfyUI exit={proc.returncode}")

        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                print(f" ✅ ({int(time.time() - start)}s)", flush=True)
                return
        except OSError:
            print(".", end="", flush=True)
            time.sleep(1)

    if log_path.exists():
        print(log_path.read_text(encoding="utf-8", errors="ignore")[-10000:])
    raise RuntimeError("ComfyUI 启动超时")


def verify_all_nodes():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/object_info", timeout=30) as response:
        info = json.load(response)

    required = [
        # 1. 通用 + H3 视频核心节点
        "UNETLoader",
        "CLIPLoader",
        "VAELoader",
        "LoraLoaderModelOnly",
        "MiniMaxH3ReferenceToVideo",
        "MiniMaxH3AddGuide",
        "MiniMaxH3ImageToVideo",
        "LoadImage",
        "PrimitiveStringMultiline",
        "MiniMaxH3SigmaShift",
        "RandomNoise",
        "BasicGuider",
        "KSamplerSelect",
        "BasicScheduler",
        "SamplerCustomAdvanced",
        "VAEDecode",
        "VAEDecodeAudio",
        "CreateVideo",
        "SaveVideo",
        "CLIPTextEncode",
        "ConditioningZeroOut",
        "EmptyLatentImage",
        "KSampler",
        "SaveImage",
        # 2. H3 两轮采样 3D 放大节点
        "MinimaxH3LatentUpscaler3D",
        "LTXVSeparateAVLatent",
        "LTXVConcatAVLatent",
        # 3. MOSS-Image 5B SFT (SLG=22) 核心节点
        "SkipLayerGuidanceDiTSimple",
        "EmptyFlux2LatentImage",
        "MOSSImageLayoutPrompt",
        "MOSSImageControl",
        # 4. 英伟达 Open-H3-Regenerate 1080p/2K 二采精修核心节点
        "OpenH3RegenLora",
        "OpenH3RegenSigmas",
        "OpenH3RegenUpscale",
    ]

    missing = [name for name in required if name not in info]
    if missing:
        raise RuntimeError("缺少节点: " + ", ".join(missing) + "。请核对上方固定提交与插件导入日志。")

    guide = info["MiniMaxH3AddGuide"].get("input", {})
    guide_inputs = set(guide.get("required", {})) | set(guide.get("optional", {}))
    expected = {"positive", "latent", "frame_idx", "image", "vae", "audio_vae"}
    if not expected <= guide_inputs:
        raise RuntimeError(
            "MiniMaxH3AddGuide 接口不兼容，缺少：" + ", ".join(sorted(expected - guide_inputs))
        )
    print(f"✅ H3 b15-49 + Open-H3 1080p/2K 精修 + MOSS-Image 5B (SLG=22) 全部 {len(required)} 个核心节点校验通过！", flush=True)


def launch_tunnel():
    token = get_secret("CF_TUNNEL_TOKEN")
    if not token:
        print("ℹ️ 未配置 CF_TUNNEL_TOKEN，只启动本地/Colab 8188", flush=True)
        print(f"🌐 固定域名未连接：{PUBLIC_URL}", flush=True)
        return None

    binary = shutil.which("cloudflared")
    if not binary:
        local = BASE / "cloudflared"
        if not local.is_file() or local.stat().st_size < 1024 * 1024:
            partial = local.with_suffix(".part")
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
            rc = subprocess.run(
                ["curl", "-fL", "--retry", "3", "--connect-timeout", "15", "-o", str(partial), url]
            ).returncode
            if rc != 0:
                urllib.request.urlretrieve(url, str(partial))
            partial.replace(local)
        local.chmod(0o755)
        binary = str(local)

    cf_log = open(BASE / "trinity_cloudflared.log", "w", encoding="utf-8")
    subprocess.Popen(
        [binary, "tunnel", "--protocol", "http2", "run", "--token", token],
        stdout=cf_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(3)
    print("✅ Cloudflare HTTP/2 固定隧道已启动", flush=True)
    print(f"🌐 打开：{PUBLIC_URL}", flush=True)
    return PUBLIC_URL


def main():
    if sys.platform != "linux":
        raise RuntimeError("这个启动脚本是给 Colab/Linux NVIDIA GPU 环境使用的")

    total_start = time.time()
    boot_id = f"trinity_a100_{int(total_start)}"

    # 第一时间清理旧版遗留的后台看门狗进程
    kill_legacy_daemons()

    _hf_token = get_secret("HF_TOKEN")
    if _hf_token:
        os.environ["HF_TOKEN"] = _hf_token

    FORCE_HTTP = (
        os.getenv("REF2VA_FORCE_HTTP", "0").strip() == "1"
        or os.getenv("DRAMA3_FORCE_HTTP", "0").strip() == "1"
    )
    if FORCE_HTTP:
        _disable_hf_xet_runtime()
    else:
        os.environ["HF_XET_CHUNK_CACHE_SIZE_BYTES"] = "0"
        os.environ["HF_XET_HIGH_PERFORMANCE"] = "1"
        os.environ.pop("HF_HUB_DISABLE_XET", None)

    os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
    os.environ["CUDA_MODULE_LOADING"] = "LAZY"

    if _hf_token:
        print("🔑 HF_TOKEN 已加载", flush=True)
    else:
        print(
            "⚠️ 未检测到 HF_TOKEN —— 私有 bucket 的 NaughtyTimes v3 LoRA 会被跳过，其余公开模型正常下载。",
            flush=True,
        )

    models_list = list(BASE_MODELS)

    print("=" * 84, flush=True)
    print("🚀 H3 b15-49（视频一采） + Open-H3 1080p/2K（精修二采） + MOSS-Image 5B（生图）二合一满血启动", flush=True)
    print(
        f"固定版本：ComfyUI @ {COMFY_REV[:10]} | OpenH3-Regen @ {OPENH3_REGEN_PLUGIN_REV[:10]} | comfy-kitchen {KITCHEN_VERSIONS[0]}",
        flush=True,
    )
    print("二采精修：OpenH3RegenLora (Refiner + LightX2V Turbo + Sharp 50曲线) + OpenH3RegenSigmas (3步 Euler, start_sigma=0.78)", flush=True)
    print("下载通道：热门文件走 hf-xet 4路极速 ｜ b15-49 & qwen3.5_9b 走 HTTP 直下（防 429/72%卡死）", flush=True)
    print("=" * 84, flush=True)

    print("\n[1/8] 本地 SSD 模式：不挂载 Google Drive（避免网盘 I/O 拖慢下载与 mmap）", flush=True)
    print("模型保存在 /content/ComfyUI/models，输出保存在 /content/ComfyUI/output_local", flush=True)

    t = time.time()
    print("\n[2/8] 检出固定 ComfyUI 版本（原生支持 H3 + MOSS-Image 5B）", flush=True)
    clone_pinned("https://github.com/Comfy-Org/ComfyUI.git", COMFY, COMFY_REV)
    print(f"✅ ComfyUI 就绪 ({time.time()-t:.1f}s)", flush=True)

    install_if_needed(["huggingface_hub<2.0.0", "hf-xet"])

    print("\n[3/8] 后台并行启动【H3 b15-49 + Open-H3 精修 + MOSS-Image 5B】全套模型下载", flush=True)
    model_pool = ThreadPoolExecutor(max_workers=1)
    model_future = model_pool.submit(download_all_models, models_list)

    try:
        t = time.time()
        print("\n[4/8] CUDA / Python 依赖（与模型下载并行）", flush=True)
        ensure_cuda()
        install_base_dependencies()
        install_comfy_requirements()
        print(f"✅ Python 依赖完成 ({time.time()-t:.1f}s)", flush=True)

        t = time.time()
        print("\n[5/8] 官方 comfy-kitchen + GPU 算子 & H3/MOSS 模块实测", flush=True)
        installed_ck = ensure_vsa_kitchen()
        verify_comfy_kitchen()
        verify_all_core_modules()
        print(f"✅ comfy-kitchen ({installed_ck}) 完成 ({time.time()-t:.1f}s)", flush=True)

        t = time.time()
        print("\n[6/8] 自定义节点（KJNodes + 3D Latent Upscaler + ComfyUI-Open-H3-Regenerate）", flush=True)
        install_custom_nodes()
        print(f"✅ 自定义节点完成 ({time.time()-t:.1f}s)", flush=True)

        print("\n[7/8] 等待后台模型下载收尾", flush=True)
        t = time.time()
        model_future.result()
        print(f"✅ 所有生图与视频（含 Open-H3 精修）模型就绪（等待收尾 {time.time()-t:.1f}s）", flush=True)
    finally:
        model_pool.shutdown(wait=False)

    (COMFY / "input").mkdir(exist_ok=True)
    (COMFY / "output").mkdir(exist_ok=True)
    local_output = COMFY / "output_local"
    if local_output.is_symlink():
        raise RuntimeError("output_local 是软链接，请使用新的 Colab 会话进行本地测试。")
    local_output.mkdir(exist_ok=True)

    print("\n[8/8] 启动 ComfyUI 与 Cloudflare HTTP/2 隧道", flush=True)
    kill_legacy_daemons()
    subprocess.run(["pkill", "-9", "-f", "main.py"], check=False)
    subprocess.run(["pkill", "-9", "-f", "cloudflared"], check=False)
    time.sleep(1)

    log_path = BASE / "h3_moss_drama3_comfyui.log"
    log_fp = open(log_path, "w", encoding="utf-8")
    # 同步软链到历史日志文件名，方便任意通用诊断脚本读取
    for alias_name in ("h3_moss_comfyui.log", "drama3_comfyui.log", "ref2va_vsa_comfyui.log"):
        compat_log = BASE / alias_name
        if compat_log != log_path:
            try:
                if compat_log.exists() or compat_log.is_symlink():
                    compat_log.unlink()
                compat_log.symlink_to(log_path)
            except Exception:
                pass

    cmd = [
        sys.executable,
        "main.py",
        "--listen", "0.0.0.0",
        "--port", str(PORT),
        "--output-directory", str(local_output),
        "--disable-auto-launch",
        "--enable-cors-header", "*",
        "--async-offload", "2",
        "--reserve-vram", "2.0",
        "--use-ck-attention",
        "--fast", "fp16_accumulation",
    ]

    print("$", " ".join(cmd), flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=str(COMFY),
        env=os.environ.copy(),
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    wait_port(proc, log_path, timeout=180)
    startup_log = log_path.read_text(encoding="utf-8", errors="replace")
    if "Using Comfy Kitchen attention" not in startup_log:
        raise RuntimeError("尚未确认 Kitchen Attention 启用，请检查启动日志：" + str(log_path))
    print("✅ 已确认 Using Comfy Kitchen attention", flush=True)
    verify_all_nodes()

    tunnel_url = launch_tunnel()

    print("\n" + "=" * 84, flush=True)
    print(f"🎉 H3 b15-49 + Open-H3 1080p/2K 精修 + MOSS-Image 5B 二合一环境完成 | 总耗时 {time.time()-total_start:.1f}s", flush=True)
    print(f"ComfyUI 固定提交：{COMFY_REV[:10]} | OpenH3-Regen：{OPENH3_REGEN_PLUGIN_REV[:10]} | Kitchen：{installed_ck}", flush=True)
    print("🖼️ 生图就绪：MOSS-Image 5B SFT (SLG=22 防人体折叠 · Qwen3.5-9B · FLUX.2 VAE)", flush=True)
    print("🎬 视频就绪：H3 b15-49 一采 (0.4MP/10步) → 3D潜变量放大至 1080p (2.07MP) → Open-H3 精修二采 (3步 Euler / start_sigma=0.78)", flush=True)
    if tunnel_url:
        print(f"🌐 固定公网地址（HTTP/2 加速）：{tunnel_url}", flush=True)
    else:
        print(f"🌐 固定公网地址未连接：{PUBLIC_URL}", flush=True)
    print("=" * 84, flush=True)

    log_fp.close()
    if os.getenv("EXIT_AFTER_BOOT", "0").strip() == "1":
        print("🟢 启动完成，ComfyUI 与 Cloudflare 隧道在后台运行；此单元格现在结束。", flush=True)
        return

    print("🟢 启动完成！已进入常驻防断连 + 隧道/ComfyUI 自动重连看门狗模式（随时点停止按钮可切后台运行）...", flush=True)
    heartbeat_count = 0
    while True:
        try:
            time.sleep(30)
            heartbeat_count += 1
            comfy_alive = (proc.poll() is None)
            if comfy_alive:
                try:
                    with socket.create_connection(("127.0.0.1", PORT), timeout=3):
                        pass
                except OSError:
                    comfy_alive = False
            if not comfy_alive:
                print("⚠️ 检测到 ComfyUI 异常退出，正在自动重启...", flush=True)
                subprocess.run(["pkill", "-9", "-f", "main.py"], check=False)
                time.sleep(1.0)
                log_fp = open(log_path, "a", encoding="utf-8")
                proc = subprocess.Popen(cmd, cwd=str(COMFY), env=os.environ.copy(), stdout=log_fp, stderr=subprocess.STDOUT, start_new_session=True)
                wait_port(proc, log_path, timeout=180)
                log_fp.close()
                print("✅ ComfyUI 已自动恢复在线！", flush=True)
            if get_secret("CF_TUNNEL_TOKEN"):
                cf_check = subprocess.run(["pgrep", "-f", "cloudflared"], capture_output=True)
                if cf_check.returncode != 0:
                    print("⚠️ 检测到 Cloudflare 隧道断开，正在自动重连...", flush=True)
                    launch_tunnel()
            if heartbeat_count % 20 == 0:
                up_min = int((time.time() - total_start) / 60)
                print(f"💓 [守护心跳] 已稳定运行 {up_min} 分钟 | 隧道与双引擎（MOSS-Image + H3 Open-H3 精修）在线", flush=True)
        except KeyboardInterrupt:
            print("ℹ️ 收到前端切后台信号，ComfyUI 与 Cloudflare 隧道仍在后台继续运行！", flush=True)
            break


if __name__ == "__main__":
    main()
