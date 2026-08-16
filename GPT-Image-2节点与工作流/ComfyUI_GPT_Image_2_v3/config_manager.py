"""
gpt_image_2 — 持久化配置管理器
================================

负责保存/读取 base_url 和最后一次使用的参数（不含 API key 明文落盘）。

存储位置：
    Linux/macOS:  ~/.config/gpt_image_2/config.json   (XDG 规范)
    Windows:      %APPDATA%\\gpt_image_2\\config.json
    回退:         <ComfyUI 自定义节点目录>/.gpt_image_2_config.json

注意：API key **永远不**自动落盘。每次启动都从节点 input 读取。
      base_url 和默认参数会保存，方便下次直接用。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Optional

_LOCK = threading.Lock()

# 节点目录（用于回退位置）
_NODE_DIR = os.path.dirname(os.path.abspath(__file__))


def _default_config_path() -> str:
    """跨平台配置文件路径。"""
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(appdata, "gpt_image_2", "config.json")
    # XDG
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(xdg, "gpt_image_2", "config.json")


CONFIG_PATH = _default_config_path()
FALLBACK_PATH = os.path.join(_NODE_DIR, ".gpt_image_2_config.json")

# 允许保存的非敏感字段（白名单，**绝不**保存 api_key / password）
SAVABLE_KEYS = {
    "base_url",
    "default_model",
    "default_size",
    "default_quality",
    "default_endpoint",
    "default_output_format",
    "disable_proxy",
}


def _ensure_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass


def load() -> dict[str, Any]:
    """读配置。优先 XDG/平台路径，读不到再回退到节点目录。都不存在返回空 dict。"""
    for path in (CONFIG_PATH, FALLBACK_PATH):
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def save(partial: dict[str, Any]) -> None:
    """增量保存。只写入白名单字段（**绝不**写 api_key）。

    如果两个位置都不可写（比如只读文件系统），安静失败 — 配置是便利功能，
    不该阻塞主流程。
    """
    safe: dict[str, Any] = {}
    for k, v in partial.items():
        if k in SAVABLE_KEYS and v is not None:
            safe[k] = v

    if not safe:
        return

    with _LOCK:
        # 合并已有配置
        existing: dict[str, Any] = {}
        for path in (CONFIG_PATH, FALLBACK_PATH):
            try:
                if os.path.isfile(path):
                    with open(path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    break
            except (OSError, json.JSONDecodeError):
                continue

        existing.update(safe)

        for path in (CONFIG_PATH, FALLBACK_PATH):
            try:
                _ensure_dir(path)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(existing, f, indent=2, ensure_ascii=False)
                try:
                    os.chmod(path, 0o600)  # 限制只读 owner
                except OSError:
                    pass
                return  # 写成功就退出
            except OSError:
                continue


def get(key: str, default: Any = None) -> Any:
    """读单个配置项。"""
    return load().get(key, default)


def set_value(key: str, value: Any) -> None:
    """写单个配置项。"""
    save({key: value})


def clear() -> bool:
    """清空所有配置文件。返回是否清掉了至少一个。"""
    cleared = False
    for path in (CONFIG_PATH, FALLBACK_PATH):
        try:
            if os.path.isfile(path):
                os.remove(path)
                cleared = True
        except OSError:
            pass
    return cleared


def config_location() -> dict[str, Optional[str]]:
    """返回配置路径信息（用于诊断显示）。"""
    return {
        "primary": CONFIG_PATH,
        "fallback": FALLBACK_PATH,
        "primary_exists": os.path.isfile(CONFIG_PATH),
        "fallback_exists": os.path.isfile(FALLBACK_PATH),
    }
