"""SillyDream Prompt Reverse — ComfyUI 节点

两个节点：
  * 图像反推 (SillyDream)  IMAGE  -> STRING
  * 文本扩写 (SillyDream)  STRING -> STRING

两者都通过任意 OpenAI 兼容的 Chat Completions 接口工作
（New-API / One-API / ModelScope / 官方 OpenAI 等均可）。
"""

from __future__ import annotations

import base64
import io
import json
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import numpy as np
import requests
from PIL import Image

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

CATEGORY = "Image/Prompt"

DEFAULT_ENDPOINT = "http://127.0.0.1:3000"
CHAT_PATH = "/v1/chat/completions"

# 单张图片编码后的体积上限。超过会先降质再降分辨率，仍超则直接报错，
# 避免把一个几十 MB 的 base64 串甩给网关换来一个 413。
MAX_IMAGE_BYTES = 10 * 1024 * 1024

JPEG_QUALITY_LADDER = (92, 85, 75, 65, 55)
DOWNSCALE_LADDER = (1.0, 0.8, 0.6, 0.45, 0.3)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
BACKOFF_BASE = 1.6

LANGUAGE_CHOICES = ("zh", "en")

LANGUAGE_CLAUSE = {
    "zh": "请使用中文作答。",
    "en": "Respond in English.",
}

DEFAULT_SYSTEM_PROMPT = (
    "你是一位资深的图像分析师。观察用户给出的图片，写出一段可以直接用于"
    "文生图模型的提示词：覆盖主体、外观细节、动作与姿态、服装、场景与环境、"
    "光线、色调、镜头与构图、整体画面风格。只输出提示词正文，不要解释，"
    "不要加标题、编号或 Markdown 标记。"
)

DEFAULT_USER_PROMPT = "请反推这张图片的提示词。"

DEFAULT_EXPAND_SYSTEM_PROMPT = (
    "你是一位提示词润色专家。把用户给出的简短描述扩写成信息更丰富、"
    "细节更具体的文生图提示词，补足材质、光线、环境、镜头与风格等要素，"
    "但不得改变原意、不得添加原文没有暗示的主体。只输出扩写后的正文。"
)


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------


def build_chat_url(raw: str) -> str:
    """把用户填的地址补全成完整的 chat/completions 地址。

    允许只填 ``host:port``、``http://host:port``、``http://host/v1``
    或已经写全的 ``http://host/v1/chat/completions``。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("api_url 不能为空")

    if "://" not in text:
        text = "http://" + text

    parts = urlparse(text)
    if not parts.netloc:
        raise ValueError(f"无法解析 api_url：{raw!r}")

    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        final_path = path
    elif path.endswith("/v1"):
        final_path = path + "/chat/completions"
    else:
        final_path = path + CHAT_PATH

    return urlunparse((parts.scheme, parts.netloc, final_path, "", "", ""))


def tensor_to_pil(image: Any) -> Image.Image:
    """ComfyUI 的 IMAGE 是 [B,H,W,C] 的 float 张量，取第一帧转成 PIL。"""
    array = image
    if hasattr(array, "detach"):  # torch.Tensor
        array = array.detach().cpu().numpy()
    array = np.asarray(array)

    if array.ndim == 4:
        array = array[0]
    if array.ndim != 3:
        raise ValueError(f"预期 [B,H,W,C] 或 [H,W,C] 的图像张量，实际维度为 {array.ndim}")

    if array.dtype != np.uint8:
        array = np.clip(array * 255.0, 0, 255).astype(np.uint8)

    channels = array.shape[2]
    if channels == 1:
        return Image.fromarray(array[:, :, 0], mode="L").convert("RGB")
    if channels >= 3:
        return Image.fromarray(array[:, :, :3], mode="RGB")
    raise ValueError(f"不支持的通道数：{channels}")


def encode_image_data_url(pil: Image.Image) -> str:
    """把图片编码成 data URL，必要时逐级压缩以满足体积上限。"""
    last_size = 0
    for scale in DOWNSCALE_LADDER:
        if scale == 1.0:
            candidate = pil
        else:
            width = max(1, int(pil.width * scale))
            height = max(1, int(pil.height * scale))
            candidate = pil.resize((width, height), Image.LANCZOS)

        for quality in JPEG_QUALITY_LADDER:
            buffer = io.BytesIO()
            candidate.save(buffer, format="JPEG", quality=quality, optimize=True)
            payload = buffer.getvalue()
            last_size = len(payload)
            if last_size <= MAX_IMAGE_BYTES:
                encoded = base64.b64encode(payload).decode("ascii")
                return f"data:image/jpeg;base64,{encoded}"

    raise ValueError(
        f"图片压缩到最低档后仍有 {last_size / 1048576:.1f} MB，"
        f"超过 {MAX_IMAGE_BYTES / 1048576:.0f} MB 上限，请先缩小分辨率"
    )


def build_headers(api_key: str) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = (api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def flatten_content(content: Any) -> str:
    """content 可能是字符串，也可能是 OpenAI 的分段数组。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces: List[str] = []
        for item in content:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content", "output_text"):
                    value = item.get(key)
                    if isinstance(value, str):
                        pieces.append(value)
                        break
        return "".join(pieces).strip()
    return ""


def read_reply_text(payload: Dict[str, Any]) -> str:
    """从 Chat Completions 响应里取出正文。

    不同网关的返回差异不小，按「标准 -> 常见变体」的顺序依次尝试。
    """
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] or {}

        message = first.get("message")
        if isinstance(message, dict):
            text = flatten_content(message.get("content"))
            if text:
                return text
            # 部分推理模型只填了思维链字段
            for key in ("reasoning_content", "reasoning"):
                text = flatten_content(message.get(key))
                if text:
                    return text

        # 旧版 completions 风格
        text = flatten_content(first.get("text"))
        if text:
            return text

        delta = first.get("delta")
        if isinstance(delta, dict):
            text = flatten_content(delta.get("content"))
            if text:
                return text

    # 少数网关直接把结果放在顶层
    for key in ("output_text", "content", "result"):
        text = flatten_content(payload.get(key))
        if text:
            return text

    return ""


def describe_http_error(status: int, url: str, body: str) -> str:
    hints = {
        401: "鉴权失败，检查 api_key 是否正确",
        403: "该 Key 没有访问此模型的权限",
        404: "地址或模型名不存在，检查 api_url 与 model_name",
        413: "请求体过大，图片太大",
        429: "请求过于频繁或额度不足，稍后再试",
        500: "网关内部错误",
        502: "上游模型服务异常",
        503: "上游模型服务暂时不可用",
        504: "上游模型服务响应超时",
    }
    hint = hints.get(status, "")
    snippet = (body or "").strip().replace("\n", " ")
    if len(snippet) > 400:
        snippet = snippet[:400] + " …"
    parts = [f"HTTP {status}"]
    if hint:
        parts.append(hint)
    parts.append(f"请求地址：{url}")
    if snippet:
        parts.append(f"响应：{snippet}")
    return " | ".join(parts)


def request_chat(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float) -> str:
    """发起一次带重试的 Chat Completions 调用，返回正文文本。"""
    last_error: Optional[str] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.exceptions.Timeout:
            last_error = f"请求超时（{timeout:.0f} 秒） | 请求地址：{url}"
        except requests.exceptions.RequestException as exc:
            last_error = f"网络请求失败：{exc} | 请求地址：{url}"
        else:
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    preview = response.text.strip()[:400]
                    raise RuntimeError(
                        "响应不是合法 JSON，通常是 api_url 指向了网页而不是 API 接口。"
                        f" 请求地址：{url} | 响应开头：{preview}"
                    )

                if isinstance(payload, dict) and payload.get("error"):
                    detail = payload["error"]
                    message = detail.get("message") if isinstance(detail, dict) else detail
                    raise RuntimeError(f"网关返回错误：{message} | 请求地址：{url}")

                text = read_reply_text(payload if isinstance(payload, dict) else {})
                if text:
                    return text

                preview = json.dumps(payload, ensure_ascii=False)[:400]
                raise RuntimeError(
                    f"响应中没有找到正文内容 | 请求地址：{url} | 原始响应：{preview}"
                )

            last_error = describe_http_error(response.status_code, url, response.text)
            if response.status_code not in RETRYABLE_STATUS:
                raise RuntimeError(last_error)

        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_BASE ** (attempt - 1))

    raise RuntimeError(f"重试 {MAX_ATTEMPTS} 次后仍然失败：{last_error}")


def sampling_fields(temperature: float, top_p: float, max_tokens: int, seed: int) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "temperature": round(float(temperature), 3),
        "top_p": round(float(top_p), 3),
    }
    if max_tokens > 0:
        fields["max_tokens"] = int(max_tokens)
    if seed > 0:
        fields["seed"] = int(seed)
    return fields


# --------------------------------------------------------------------------
# 节点
# --------------------------------------------------------------------------


class SillyDreamImagePromptReverse:
    """把图像反推为可直接使用的文生图提示词。"""

    CATEGORY = CATEGORY
    FUNCTION = "reverse"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("prompt",)
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE",),
                "api_url": ("STRING", {"default": DEFAULT_ENDPOINT, "multiline": False}),
                "model_name": ("STRING", {"default": "gpt-4o-mini", "multiline": False}),
                "api_key": ("STRING", {"default": "", "multiline": False}),
                "language": (list(LANGUAGE_CHOICES), {"default": LANGUAGE_CHOICES[0]}),
                "system_prompt": ("STRING", {"default": DEFAULT_SYSTEM_PROMPT, "multiline": True}),
                "user_prompt": ("STRING", {"default": DEFAULT_USER_PROMPT, "multiline": True}),
                "top_p": ("FLOAT", {"default": 0.9, "min": 0.0, "max": 1.0, "step": 0.05}),
                "temperature": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 2.0, "step": 0.05}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFF}),
                "max_tokens": ("INT", {"default": 2048, "min": 0, "max": 32768, "step": 64}),
                "timeout": ("INT", {"default": 120, "min": 5, "max": 1800, "step": 5}),
            }
        }

    def reverse(self, image, api_url, model_name, api_key, language, system_prompt,
                user_prompt, top_p, temperature, seed, max_tokens, timeout) -> Tuple[str]:
        model = (model_name or "").strip()
        if not model:
            raise ValueError("model_name 不能为空")

        url = build_chat_url(api_url)
        data_url = encode_image_data_url(tensor_to_pil(image))

        system_text = (system_prompt or DEFAULT_SYSTEM_PROMPT).strip()
        clause = LANGUAGE_CLAUSE.get(language, "")
        if clause and clause not in system_text:
            system_text = f"{system_text}\n{clause}"

        messages = [
            {"role": "system", "content": system_text},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": (user_prompt or DEFAULT_USER_PROMPT).strip()},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]

        body: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        body.update(sampling_fields(temperature, top_p, max_tokens, seed))

        return (request_chat(url, build_headers(api_key), body, float(timeout)),)


class SillyDreamTextExpand:
    """把简短描述扩写成更详细的提示词。"""

    CATEGORY = CATEGORY
    FUNCTION = "expand"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("expanded_text",)
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "text": ("STRING", {"default": "", "multiline": True}),
                "api_url": ("STRING", {"default": DEFAULT_ENDPOINT, "multiline": False}),
                "model_name": ("STRING", {"default": "gpt-4o-mini", "multiline": False}),
                "api_key": ("STRING", {"default": "", "multiline": False}),
                "max_tokens": ("INT", {"default": 2048, "min": 0, "max": 32768, "step": 64}),
                "temperature": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 2.0, "step": 0.05}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFF}),
                "system_prompt": ("STRING", {"default": DEFAULT_EXPAND_SYSTEM_PROMPT, "multiline": True}),
                "top_p": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05}),
                "timeout": ("INT", {"default": 120, "min": 5, "max": 1800, "step": 5}),
            }
        }

    def expand(self, text, api_url, model_name, api_key, max_tokens, temperature,
               seed, system_prompt, top_p, timeout) -> Tuple[str]:
        source = (text or "").strip()
        if not source:
            raise ValueError("text 不能为空")

        model = (model_name or "").strip()
        if not model:
            raise ValueError("model_name 不能为空")

        url = build_chat_url(api_url)
        messages = [
            {"role": "system", "content": (system_prompt or DEFAULT_EXPAND_SYSTEM_PROMPT).strip()},
            {"role": "user", "content": source},
        ]

        body: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        body.update(sampling_fields(temperature, top_p, max_tokens, seed))

        return (request_chat(url, build_headers(api_key), body, float(timeout)),)


# 注册键沿用既有的中文名，这样用户此前保存的工作流可以直接打开；
# 另外提供一组英文别名，方便在脚本 / API 里引用。
NODE_CLASS_MAPPINGS = {
    "图像反推": SillyDreamImagePromptReverse,
    "文本扩写": SillyDreamTextExpand,
    "SillyDreamImagePromptReverse": SillyDreamImagePromptReverse,
    "SillyDreamTextExpand": SillyDreamTextExpand,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "图像反推": "图像反推 (SillyDream)",
    "文本扩写": "文本扩写 (SillyDream)",
    "SillyDreamImagePromptReverse": "图像反推 (SillyDream)",
    "SillyDreamTextExpand": "文本扩写 (SillyDream)",
}
