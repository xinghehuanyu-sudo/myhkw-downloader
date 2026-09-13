"""音频时长探测：优先远程 ffprobe（服务器支持 Range，只需拉几 KB），
探测失败则回退到本地文件探测。"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Optional

_FFPROBE = None


def ffprobe_exe(configured: Optional[str] = None) -> Optional[str]:
    global _FFPROBE
    if _FFPROBE is None or configured:
        path = configured or shutil.which("ffprobe") or shutil.which("ffprobe.exe")
        _FFPROBE = path
    return _FFPROBE


def _probe(ffprobe: str, target: str, timeout: int) -> Optional[float]:
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", target]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if out.returncode != 0:
            return None
        info = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
        dur = info.get("format", {}).get("duration")
        return float(dur) if dur else None
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None


def probe_remote(url: str, ffprobe: Optional[str], timeout: int = 25) -> Optional[float]:
    """对播放地址直接探测时长。返回 None 表示无法确定。"""
    if not ffprobe:
        return None
    return _probe(ffprobe, url, timeout)


def probe_file(path: str, ffprobe: Optional[str], timeout: int = 25) -> Optional[float]:
    if not ffprobe:
        return None
    return _probe(ffprobe, str(path), timeout)
