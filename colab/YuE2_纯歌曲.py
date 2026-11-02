# -*- coding: utf-8 -*-
"""
YuE2 纯歌曲 - 优化展开版（Colab / L4 24GB）

核心：
- ComfyUI + pytraveler/YuE2-ComfyUI
- 使用 staged nodes：YuE2 Plan -> YuE2 Render Plan -> Save Audio
- 不使用单个 YuE2 Generate Song 聚合节点
- attention_backend = fast（工作流里设置，无需 flash-attn）
- keep_model_loaded = true
- offload = off（L4 24GB 直接把模型留在 GPU）
- low_vram = false（默认）
- 使用 Comfy-Org 的 YuE2 3B INT8 ConvRot checkpoint
- 同一 Colab session 重跑时尽量跳过 Git / pip / 模型下载
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ===== 固定 Cloudflare Tunnel：你只需要填 Token =====
CF_TUNNEL_TOKEN = os.environ.get("CF_TUNNEL_TOKEN", "")  # paste your own token, never commit it
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://your-tunnel.example.com")  # set your own tunnel host
# ====================================================

os.environ["HF_XET_HIGH_PERFORMANCE"] = "1"
os.environ.pop("HF_HUB_ENABLE_HF_TRANSFER", None)
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
os.environ["YUE2_CONSOLE_PROGRESS"] = "1"

BASE = Path("/content")
COMFY = BASE / "ComfyUI"
CUSTOM = COMFY / "custom_nodes" / "YuE2-ComfyUI"
MODEL = COMFY / "models" / "checkpoints" / "yue2_3b_int8_convrot.safetensors"

PORT = 8188
LOG = BASE / "comfyui_yue2_optimized.log"
CF_LOG = BASE / "cloudflared_yue2.log"

YUE2_REPO = "https://github.com/pytraveler/YuE2-ComfyUI.git"
YUE2_REV = "f0d691587f2e484e6e0f591ea31b76f92c7f38a8"

CODE_MARKER = BASE / ".yue2_optimized_comfy.ok"
REQ_MARKER = BASE / ".yue2_optimized_requirements.sha256"
NODE_MARKER = BASE / ".yue2_optimized_node.ok"


def run(args, cwd=None, check=True):
    args = [str(x) for x in args]
    print("$", " ".join(args), flush=True)
    p = subprocess.run(args, cwd=str(cwd) if cwd else None, check=False)
    if check and p.returncode != 0:
        raise RuntimeError(f"命令失败 exit={p.returncode}: {' '.join(args)}")
    return p


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.7):
            return True
    except OSError:
        return False


def wait_port(timeout=120) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if port_open():
            return True
        time.sleep(0.5)
    return False


def ensure_comfyui():
    if CODE_MARKER.is_file() and (COMFY / "main.py").is_file():
        print("⚡ ComfyUI 已就绪，跳过 Git。", flush=True)
        return

    print("[1/5] 准备 ComfyUI", flush=True)
    if not (COMFY / ".git").is_dir():
        if COMFY.exists():
            import shutil
            shutil.rmtree(COMFY, ignore_errors=True)
        run([
            "git", "clone", "--depth", "1", "--filter=blob:none",
            "https://github.com/Comfy-Org/ComfyUI.git", str(COMFY)
        ])
    else:
        run(["git", "fetch", "--depth", "1", "origin"], cwd=COMFY)
        # ComfyUI 默认分支可能是 master；直接 fast-forward 当前分支即可
        run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=COMFY)

    CODE_MARKER.touch()


def install_requirements():
    print("[2/5] 准备 Python 依赖", flush=True)
    req = COMFY / "requirements.txt"
    digest = sha256(req)

    if REQ_MARKER.is_file() and REQ_MARKER.read_text(encoding="utf-8").strip() == digest:
        print("⚡ requirements 已安装，跳过。", flush=True)
        # tiktoken 是 YuE2-ComfyUI 额外需要的，快速 import 检查一下
        check = subprocess.run(
            [sys.executable, "-c", "import tiktoken, huggingface_hub"],
            capture_output=True, text=True
        )
        if check.returncode == 0:
            return

    run([
        sys.executable, "-m", "pip", "install", "-q",
        "-r", str(req),
        "tiktoken",
        "huggingface_hub",
        "hf-xet",
    ])
    REQ_MARKER.write_text(digest, encoding="utf-8")


def install_yue2_optimized():
    print("[3/5] 准备 YuE2 优化节点", flush=True)

    if NODE_MARKER.is_file() and (CUSTOM / "__init__.py").is_file():
        print("⚡ YuE2-ComfyUI 优化节点已就绪，跳过 Git。", flush=True)
        return

    CUSTOM.parent.mkdir(parents=True, exist_ok=True)

    if not (CUSTOM / ".git").is_dir():
        import shutil
        shutil.rmtree(CUSTOM, ignore_errors=True)
        run(["git", "clone", "--depth", "1", YUE2_REPO, str(CUSTOM)])

    # 固定到已检查的版本，避免下一次仓库变化导致 JSON 节点接口漂移
    run(["git", "fetch", "--depth", "1", "origin", YUE2_REV], cwd=CUSTOM)
    run(["git", "checkout", "--force", "--detach", "FETCH_HEAD"], cwd=CUSTOM)

    NODE_MARKER.touch()
    print("✅ YuE2 优化 Runtime 已安装。", flush=True)


def download_model():
    print("[4/5] 检查 YuE2 INT8 模型", flush=True)

    if MODEL.is_file() and MODEL.stat().st_size > 100 * 1024 * 1024:
        print(f"⚡ 模型已存在，跳过：{MODEL.name}", flush=True)
        return

    MODEL.parent.mkdir(parents=True, exist_ok=True)

    from huggingface_hub import hf_hub_download

    print("⬇️ 下载 yue2_3b_int8_convrot.safetensors ...", flush=True)
    downloaded = Path(hf_hub_download(
        repo_id="Comfy-Org/YuE2",
        filename="checkpoints/yue2_3b_int8_convrot.safetensors",
        local_dir=str(COMFY / "models"),
    ))

    if downloaded.resolve() != MODEL.resolve():
        if MODEL.exists():
            MODEL.unlink()
        downloaded.replace(MODEL)

    print(f"✅ 模型完成：{MODEL}", flush=True)


def stop_old_server():
    subprocess.run(["pkill", "-9", "-f", "python.*main.py"], check=False)
    subprocess.run(["pkill", "-9", "-f", "cloudflared"], check=False)
    time.sleep(0.5)


def start_comfyui():
    print("[5/5] 启动 ComfyUI", flush=True)
    stop_old_server()

    logf = LOG.open("w", encoding="utf-8")
    cmd = [
        sys.executable,
        "main.py",
        "--listen", "0.0.0.0",
        "--port", str(PORT),
        "--disable-auto-launch",
        "--enable-cors-header", "*",
        "--highvram",
    ]

    print("$", " ".join(cmd), flush=True)
    print("日志：", LOG, flush=True)

    proc = subprocess.Popen(
        cmd,
        cwd=str(COMFY),
        env=os.environ.copy(),
        stdout=logf,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    if not wait_port(120):
        logf.flush()
        print(LOG.read_text(encoding="utf-8", errors="ignore")[-16000:])
        raise RuntimeError("ComfyUI 120 秒内没有监听 8188")

    logf.flush()
    print(f"✅ ComfyUI 已启动：http://127.0.0.1:{PORT}", flush=True)
    return proc, logf


def verify_nodes():
    """确认优化版 staged nodes 已被 ComfyUI 注册。"""
    import json
    import urllib.request

    url = f"http://127.0.0.1:{PORT}/object_info"
    with urllib.request.urlopen(url, timeout=20) as resp:
        info = json.loads(resp.read().decode("utf-8"))

    required = ["YuE2Options", "YuE2Plan", "YuE2RenderPlan"]
    missing = [x for x in required if x not in info]
    if missing:
        raise RuntimeError(
            "YuE2 优化节点没有注册成功，缺少：" + ", ".join(missing) +
            f"\n日志：{LOG}"
        )
    print("✅ staged nodes：YuE2 Options / Plan / Render Plan 已注册", flush=True)


def ensure_cloudflared() -> Path:
    p = BASE / "cloudflared"
    if p.exists():
        p.chmod(0o755)
        return p

    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    print("⬇️ 下载 cloudflared...", flush=True)
    urllib.request.urlretrieve(url, p)
    p.chmod(0o755)
    return p


def start_cloudflare():
    token = CF_TUNNEL_TOKEN.strip()
    if not token:
        print("⚠️ CF_TUNNEL_TOKEN 为空：ComfyUI 已启动，但不启动固定 Tunnel。", flush=True)
        return None

    cf = ensure_cloudflared()
    logf = CF_LOG.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(cf), "tunnel", "--no-autoupdate", "run", "--token", token],
        stdout=logf,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    end = time.time() + 40
    while time.time() < end:
        if proc.poll() is not None:
            break
        logf.flush()
        txt = CF_LOG.read_text(encoding="utf-8", errors="ignore") if CF_LOG.exists() else ""
        if "Registered tunnel connection" in txt:
            print(f"✅ 固定地址：{PUBLIC_URL}", flush=True)
            return proc
        time.sleep(1)

    logf.flush()
    tail = CF_LOG.read_text(encoding="utf-8", errors="ignore")[-8000:] if CF_LOG.exists() else ""
    print(tail)
    raise RuntimeError("Cloudflare Tunnel 没有注册成功")


def main():
    print("=" * 72)
    print("🎵 YuE2 纯歌曲 | 优化展开版 | L4 24GB")
    print("   Plan -> Render Plan -> Save Audio")
    print("   Fast Attention + CUDA Graph/KV Cache Runtime")
    print("=" * 72)

    ensure_comfyui()
    install_requirements()
    install_yue2_optimized()
    download_model()

    (COMFY / "input").mkdir(parents=True, exist_ok=True)
    (COMFY / "output").mkdir(parents=True, exist_ok=True)

    _, logf = start_comfyui()
    verify_nodes()
    start_cloudflare()

    print()
    print("=" * 72)
    print("✅ 启动完成")
    print("工作流：YuE2_纯歌曲_优化展开版.json")
    print("模型：yue2_3b_int8_convrot.safetensors")
    print("Runtime：pytraveler/YuE2-ComfyUI")
    print("默认：fast attention / keep_model_loaded=ON / offload=OFF")
    print("默认测试：60 秒上限 / 24 ODE steps")
    print("正式出歌可把 ODE steps 改回 32")
    print("=" * 72)

    logf.flush()
    logf.close()


if __name__ == "__main__":
    main()
