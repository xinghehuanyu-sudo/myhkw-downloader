"""去重存储：记录已下载曲目的逻辑键与内容哈希，避免同一首歌下载多个版本。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import unicodedata
from typing import Dict, Optional, Set


_VERSION_TAGS = (r"(live|dj|acoustic|remix|伴奏|现场|翻唱|cover|"
                 r"纯音乐|钢琴版|合奏|女声版|男声版|深情版)")


def norm_text(s: str, strip_versions: bool = True) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    # 去掉 (xxx) （xxx） [xxx] 【xxx】 里的版本标注
    s = re.sub(r"[\(（\[【].*?[\)）\]】]", "", s)
    if strip_versions:
        s = re.sub(_VERSION_TAGS, "", s)
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", s)
    return s


def logical_key(title: str, artist: str) -> str:
    """规范化 曲名+歌手 作为“同一首歌”的判定键（跨来源去重）。

    去空白/标点、全角转半角、英文小写、剔除括号内的版本标注(Live/DJ/伴奏等)。
    """
    return f"{norm_text(title)}|{norm_text(artist)}"


def content_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class DedupStore:
    """线程安全的持久化去重表，落在下载目录下的 .myhkw_library.json。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._keys: Set[str] = set()
        self._hashes: Set[str] = set()
        self._entries: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._keys = set(data.get("keys", []))
            self._hashes = set(data.get("hashes", []))
            self._entries = data.get("entries", {})
        except (json.JSONDecodeError, OSError):
            pass

    def has_logical(self, key: str) -> bool:
        with self._lock:
            return key in self._keys

    def has_content(self, digest: str) -> bool:
        with self._lock:
            return digest in self._hashes

    def add(self, key: str, digest: str, file: str, meta: Optional[dict] = None) -> None:
        with self._lock:
            self._keys.add(key)
            self._hashes.add(digest)
            e = dict(meta or {})
            base = os.path.dirname(os.path.abspath(self.path))
            rel = os.path.relpath(os.path.abspath(file), base)
            e["file"] = rel.replace("\\", "/")
            e["name"] = os.path.basename(file)
            e["md5"] = digest
            self._entries[key] = e
            self._save()

    def remove(self, key: str) -> bool:
        with self._lock:
            e = self._entries.pop(key, None)
            self._keys.discard(key)
            if e and e.get("md5"):
                self._hashes.discard(e["md5"])
            self._save()
            return e is not None

    def entries(self) -> Dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._entries.items()}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        payload = {
            "keys": sorted(self._keys),
            "hashes": sorted(self._hashes),
            "entries": self._entries,
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
