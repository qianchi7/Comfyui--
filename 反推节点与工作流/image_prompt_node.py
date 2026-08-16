"""
图像提示词处理节点
==============================================
包含两个节点：

1. 图像反推 (Image Prompt Reverse)
   - 通过任意 HTTP API 将 ComfyUI 中的图像反推为详细提示词
   - 系统提示词 + 指令提示词 分离
   - top_p、temperature、seed、max_tokens 参数

2. 文本扩写 (Text Expand)
   - 对输入的文本进行扩写，使其更加详细
   - 内置扩写提示词："保持文本大致信息不变，扩写文本，使文本表述的内容更加详细"

参考 Muluo server/src/services/extract.ts 的实现方式：
- 使用 requests 库直发 HTTP POST（不用 OpenAI SDK）
- URL = {baseUrl}/chat/completions
- model 放在请求 body 里
- 支持 New-API / ModelScope / One-API 等 OpenAI 兼容视觉模型接口
"""

import base64
import io
import json
from typing import Optional, Tuple
from urllib.parse import urlparse, urlunparse

from PIL import Image
import numpy as np

try:
    import requests
except ImportError:
    requests = None


MAX_IMAGE_MB = 10


def normalize_api_base_for_chat_completions(url: str) -> str:
    """
    与 New-API / One-API / Muluo 一致：实际请求为 {base}/v1/chat/completions。

    若只填 http://IP:端口（无路径），会自动补上 /v1；
    若误填 …/v1/chat/completions，会规范为 …/v1。
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return u
    if not u.startswith("http"):
        u = "http://" + u

    low = u.lower()
    if low.endswith("/v1/chat/completions"):
        return u[: -len("/chat/completions")]
    if low.endswith("/v1"):
        return u
    if low.endswith("/chat/completions"):
        base = u[: -len("/chat/completions")].rstrip("/")
        if base.lower().endswith("/v1"):
            return base
        return base + "/v1"

    parsed = urlparse(u)
    path = (parsed.path or "").strip("/").lower()
    if path == "":
        return urlunparse((parsed.scheme, parsed.netloc, "/v1", "", "", "")).rstrip("/")

    return u


def _looks_like_html(s: str) -> bool:
    """判断字符串是否像 HTML 网页（而非 API JSON）"""
    if not s or not isinstance(s, str):
        return False
    head = s.lstrip()[:1200].lower()
    return (
        head.startswith("<!doctype html")
        or head.startswith("<html")
        or "<meta name=\"generator\" content=\"new-api\"" in head
        or ("<html" in head and "<head>" in head)
    )


def tensor_to_pil(image_tensor) -> Image.Image:
    """将 ComfyUI 的 IMAGE tensor 转换为 PIL Image"""
    image = image_tensor[0].cpu().numpy()
    image = (image * 255).clip(0, 255).astype(np.uint8)
    return Image.fromarray(image)


def pil_to_jpeg_base64(image: Image.Image, quality: int = 85) -> Tuple[str, int]:
    """将 PIL Image 转换为 JPEG base64"""
    if image.mode != "RGB":
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("utf-8"), buffer.tell()


def extract_prompt_from_response(data: dict) -> Optional[str]:
    """
    从 ModelScope / OpenAI 兼容响应中提取提示词。
    优先级：choices[0].message.content > prompt > data.prompt
    参考 Muluo extract.ts 的提取逻辑。
    """
    if not isinstance(data, dict):
        return None

    # OpenAI 标准格式：choices[0].message.content
    try:
        choices = data.get("choices")
        if choices and len(choices) > 0:
            choice = choices[0]
            if isinstance(choice, dict):
                message = choice.get("message", {})
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()
                    # 部分网关返回 content 为 [{type,text}, ...]
                    if isinstance(content, list):
                        parts = []
                        for item in content:
                            if isinstance(item, dict) and item.get("text"):
                                parts.append(str(item["text"]))
                            elif isinstance(item, str):
                                parts.append(item)
                        joined = "".join(parts).strip()
                        if joined:
                            return joined
    except (KeyError, TypeError, IndexError):
        pass

    # ModelScope 可能返回的字段
    for field in ["prompt", "data.prompt", "result", "text", "description"]:
        if "." in field:
            parts = field.split(".")
            obj = data
            found = True
            for p in parts:
                if isinstance(obj, dict) and p in obj:
                    obj = obj[p]
                else:
                    found = False
                    break
            if found and obj and isinstance(obj, str) and obj.strip():
                return obj.strip()
        elif field in data and data[field] and isinstance(data[field], str) and data[field].strip():
            return data[field].strip()

    return None


def _classify_http_error(status_code: int, body_str: str) -> Tuple[int, str]:
    """
    根据 HTTP 状态码和响应体，分类错误并给出友好提示。
    参考 Muluo extract.ts 的错误处理逻辑。
    """
    # 尝试从 body 里解析出错误消息
    err_detail = ""
    try:
        body_obj = json.loads(body_str)
        if isinstance(body_obj, dict):
            err_detail = (
                body_obj.get("error", {})
                or body_obj.get("message", {})
                or body_obj.get("error_message", "")
            )
            if isinstance(err_detail, dict):
                err_detail = err_detail.get("message", "") or str(err_detail)
            err_detail = str(err_detail).strip()
    except (json.JSONDecodeError, TypeError):
        err_detail = body_str.strip()[:300]

    if status_code == 401:
        return 401, f"认证失败: {err_detail or '请检查 API Key 是否正确'}"
    if status_code == 403:
        return 403, f"权限拒绝: {err_detail or '请检查 API Key 是否有权限访问该模型'}"
    if status_code == 404:
        return 404, f"模型不存在: {err_detail or '请检查模型名称是否正确'}"
    if status_code == 429:
        return 429, f"请求过于频繁 (429): {err_detail or '请稍后再试'}"
    if status_code == 502 or status_code == 503:
        return status_code, f"网关错误 ({status_code}): {err_detail or '上游服务不可用，请稍后重试'}"
    if status_code and status_code >= 400:
        return status_code, f"HTTP {status_code}: {err_detail}"

    return None, err_detail


class ImagePromptReverseNode:
    """图像提示词反推节点"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # API 配置
                "api_url": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "http://64.186.244.43:12001",
                        "tooltip": (
                            "OpenAI 兼容基址，须含 /v1（可只填 http://IP:端口，节点会自动补 /v1）。\n"
                            "示例：http://64.186.244.43:12001 或 …/v1\n"
                            "错误：请求会变成 …/chat/completions 而拿不到 JSON。"
                        ),
                    },
                ),
                "model_name": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "Qwen/Qwen2.5-VL-7B-Instruct",
                        "tooltip": (
                            "须为支持「看图」的视觉模型（VL）。纯文本模型（如部分 DeepSeek）无法读图。\n"
                            "New-API 中选 grok-4 / Qwen-VL / GPT-4o 等带 vision 的模型。"
                        ),
                    },
                ),
                "api_key": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "",
                    },
                ),
                # 中英切换
                "language": (
                    ["zh", "en"],
                    {
                        "default": "zh",
                        "label_on": "中文",
                        "label_off": "English",
                        "tooltip": "选择提示词语言 / Select prompt language",
                    },
                ),
                # 系统提示词
                "system_prompt": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "placeholder": "设置AI角色和行为...",
                    },
                ),
                # 用户指令提示词
                "user_prompt": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "placeholder": "描述图片分析要求...",
                    },
                ),
                # 生成参数（须放在 required 中，ComfyUI 才会显示；顺序在提示词框下方）
                "top_p": (
                    "FLOAT",
                    {
                        "default": 0.9,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.05,
                        "round": 0.01,
                        "display": "number",
                    },
                ),
                "temperature": (
                    "FLOAT",
                    {
                        "default": 0.7,
                        "min": 0.0,
                        "max": 2.0,
                        "step": 0.1,
                        "round": 0.01,
                        "display": "number",
                    },
                ),
                "seed": (
                    "INT",
                    {
                        "default": 42,
                        "min": 0,
                        "max": 2147483647,
                        "step": 1,
                        "round": True,
                        "display": "number",
                    },
                ),
                "max_tokens": (
                    "INT",
                    {
                        "default": 1024,
                        "min": 64,
                        "max": 8192,
                        "step": 64,
                        "round": True,
                        "display": "number",
                    },
                ),
            },
            "optional": {
                "image": ("IMAGE",),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("prompt",)
    FUNCTION = "run"
    CATEGORY = "Image/Prompt"
    OUTPUT_NODE = False

    def run(
        self,
        api_url: str,
        model_name: str,
        api_key: str,
        language: str,
        system_prompt: str,
        user_prompt: str,
        top_p: float,
        temperature: float,
        seed: int,
        max_tokens: int,
        image=None,
    ):
        if requests is None:
            return (
                "❌ 缺少 requests 库，请运行: pip install requests",
            )

        if image is None:
            return ("⚠️ 请连接图像输入 (IMAGE)",)

        raw_api = (api_url or "").strip()
        if not raw_api:
            return ("❌ api_url 不能为空",)

        base_url = normalize_api_base_for_chat_completions(raw_api)
        if not base_url or not base_url.startswith("http"):
            return ("❌ api_url 格式无效",)

        # 检查 model_name
        model = (model_name or "").strip()
        if not model:
            return ("❌ model_name 不能为空",)

        # 处理图像
        try:
            pil_image = tensor_to_pil(image)
            img_b64, size_bytes = pil_to_jpeg_base64(pil_image)
        except Exception as e:
            return (f"❌ 图像转换失败: {e}",)

        size_mb = size_bytes / (1024 * 1024)
        if size_mb > MAX_IMAGE_MB:
            return (f"❌ 图片过大: {size_mb:.2f}MB > {MAX_IMAGE_MB}MB",)

        # 构造图片 data URL（参考 Muluo extract.ts）
        image_url = f"data:image/jpeg;base64,{img_b64}"

        actual_system_prompt = (system_prompt or "").strip()
        actual_user_prompt = (user_prompt or "").strip()
        if not actual_system_prompt:
            return ("❌ 系统提示词不能为空，请在 system_prompt 中填写",)
        if not actual_user_prompt:
            return ("❌ 指令提示词不能为空，请在 user_prompt 中填写",)

        # 构造请求 body
        # 分离系统提示词和用户指令
        request_body = {
            "model": model,
            "messages": [
                # 系统消息（设置AI角色）
                {
                    "role": "system",
                    "content": actual_system_prompt,
                },
                # 用户消息（包含图像和指令）
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": actual_user_prompt,
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_url,
                            },
                        },
                    ],
                }
            ],
            # 生成参数
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "seed": seed,
        }

        # 构造请求头
        headers = {
            "Content-Type": "application/json",
        }
        api_key_stripped = (api_key or "").strip()
        if api_key_stripped:
            headers["Authorization"] = f"Bearer {api_key_stripped}"

        # 实际请求 URL（与 Muluo extract.ts: `${baseUrl}/chat/completions` 一致）
        chat_url = f"{base_url.rstrip('/')}/chat/completions"

        try:
            response = requests.post(
                chat_url,
                json=request_body,
                headers=headers,
                timeout=120,
            )
            status_code = response.status_code
            body_bytes = response.content
            body_str = body_bytes.decode("utf-8", errors="replace") if body_bytes else ""

        except requests.exceptions.Timeout:
            return (
                "❌ 请求超时（120秒）\n"
                "可能原因：图片过大、模型响应慢、网络连接不稳定。\n"
                f"【调试】POST 地址: {chat_url}"
            )

        except requests.exceptions.ConnectionError as exc:
            return (
                "❌ 无法连接到服务器\n"
                f"原因: {exc}\n\n"
                "请检查：\n"
                f"1. {chat_url} 是否可访问\n"
                "2. 服务器是否已启动\n"
                "3. 防火墙 / 网络是否放行"
            )

        except Exception as exc:
            return (
                f"❌ 请求异常 [{type(exc).__name__}]\n"
                f"{exc}\n\n"
                f"【调试】POST 地址: {chat_url}"
            )

        # 检查是否返回了 HTML（配置错误的常见表现）
        if _looks_like_html(body_str):
            return (
                "❌ 服务端返回了 HTML 网页（而非 JSON）\n"
                "网关把请求转到了 Web 管理页，而不是 API。\n"
                f"【调试】POST 地址: {chat_url}\n"
                "请确认 New-API 监听路径是否为标准 /v1；若网关挂在子路径需写全基址。"
            )

        # HTTP 状态码异常
        if status_code < 200 or status_code >= 400:
            err_code, err_msg = _classify_http_error(status_code, body_str)
            hint = ""
            if status_code == 401:
                hint = (
                    "\n\n【401 认证失败的常见原因】\n"
                    "1. New-API/One-API 密钥未在网关中生成或已失效\n"
                    "2. ModelScope API Key 填写错误\n"
                    "3. 模型在网关中未分配给当前密钥"
                )
            elif status_code == 404:
                hint = (
                    "\n\n【404 模型不存在的常见原因】\n"
                    "1. model_name 与网关中配置的模型名称不一致\n"
                    "2. 该模型未在网关中添加\n"
                    f"3. 当前填写的 model_name: '{model}'"
                )
            elif status_code == 502 or status_code == 503:
                hint = (
                    "\n\n【502/503 网关错误的排查提示】\n"
                    "1. 上游模型服务（Gemini / ModelScope 等）是否正常运行\n"
                    "2. New-API/One-API 服务端日志中是否有报错\n"
                    "3. 尝试在浏览器直接访问 /v1/models 查看支持的模型"
                )
            return (f"❌ HTTP {status_code}\n{err_msg}{hint}",)

        # 正常响应：解析 JSON，提取提示词
        try:
            data = json.loads(body_str)
        except json.JSONDecodeError:
            return (
                f"❌ 响应不是有效的 JSON\n"
                f"响应内容: {body_str[:500]}…\n\n"
                f"【调试】POST 地址: {chat_url}\n"
                "请确认该接口返回的是 OpenAI Chat Completions JSON 格式。"
            )

        extracted = extract_prompt_from_response(data)
        if extracted:
            return (extracted,)

        # 有 choices 但无内容的边界情况
        choices = data.get("choices") if isinstance(data, dict) else None
        if choices:
            return ("⚠️ 模型返回了空内容，请尝试更换模型或检查提示词",)

        return (
            f"❌ 无法从响应中提取提示词\n"
            f"响应内容: {body_str[:800]}…\n\n"
            f"【调试】POST 地址: {chat_url}\n"
            "请确认该接口支持 OpenAI Chat Completions 格式。"
        )


# ============================================================
# 文本扩写节点
# ============================================================

class TextExpandNode:
    """文本扩写节点 - 对输入的文本进行扩写"""

    EXPAND_PROMPT = "保持文本大致信息不变，扩写文本，使文本表述的内容更加详细"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "placeholder": "在此输入要扩写的文本...",
                    },
                ),
                # API 配置
                "api_url": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "http://64.186.244.43:12001",
                        "tooltip": (
                            "OpenAI 兼容基址，须含 /v1（可只填 http://IP:端口，节点会自动补 /v1）。\n"
                            "示例：http://64.186.244.43:12001 或 …/v1"
                        ),
                    },
                ),
                "model_name": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "Rim/grok-4.1-fast",
                        "tooltip": "纯文本模型即可，如 Qwen、DeepSeek 等",
                    },
                ),
                "api_key": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "",
                    },
                ),
                # 生成参数
                "max_tokens": (
                    "INT",
                    {
                        "default": 1024,
                        "min": 64,
                        "max": 8192,
                        "step": 64,
                        "round": True,
                        "display": "number",
                    },
                ),
                "temperature": (
                    "FLOAT",
                    {
                        "default": 0.7,
                        "min": 0.0,
                        "max": 2.0,
                        "step": 0.1,
                        "round": 0.01,
                        "display": "number",
                    },
                ),
                "seed": (
                    "INT",
                    {
                        "default": 42,
                        "min": 0,
                        "max": 2147483647,
                        "step": 1,
                        "round": True,
                        "display": "number",
                        "tooltip": "随机种子，用于控制模型输出的随机性。相同种子可获得可复现的结果。",
                    },
                ),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("expanded_text",)
    FUNCTION = "run"
    CATEGORY = "Image/Prompt"
    OUTPUT_NODE = False

    def run(
        self,
        text: str,
        api_url: str,
        model_name: str,
        api_key: str,
        max_tokens: int,
        temperature: float,
        seed: int,
    ):
        if requests is None:
            return ("❌ 缺少 requests 库，请运行: pip install requests",)

        input_text = (text or "").strip()
        if not input_text:
            return ("⚠️ 请输入要扩写的文本",)

        raw_api = (api_url or "").strip()
        if not raw_api:
            return ("❌ api_url 不能为空",)

        base_url = normalize_api_base_for_chat_completions(raw_api)
        if not base_url or not base_url.startswith("http"):
            return ("❌ api_url 格式无效",)

        model = (model_name or "").strip()
        if not model:
            return ("❌ model_name 不能为空",)

        # 构造请求 body
        request_body = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": self.EXPAND_PROMPT,
                },
                {
                    "role": "user",
                    "content": f"请扩写以下文本：\n\n{input_text}",
                },
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "seed": seed,
        }

        # 构造请求头
        headers = {
            "Content-Type": "application/json",
        }
        api_key_stripped = (api_key or "").strip()
        if api_key_stripped:
            headers["Authorization"] = f"Bearer {api_key_stripped}"

        chat_url = f"{base_url.rstrip('/')}/chat/completions"

        try:
            response = requests.post(
                chat_url,
                json=request_body,
                headers=headers,
                timeout=120,
            )
            status_code = response.status_code
            body_bytes = response.content
            body_str = body_bytes.decode("utf-8", errors="replace") if body_bytes else ""

        except requests.exceptions.Timeout:
            return (
                "❌ 请求超时（120秒）\n"
                "可能原因：文本过长、模型响应慢、网络不稳定。\n"
                f"【调试】POST 地址: {chat_url}"
            )

        except requests.exceptions.ConnectionError as exc:
            return (
                "❌ 无法连接到服务器\n"
                f"原因: {exc}\n\n"
                "请检查：\n"
                f"1. {chat_url} 是否可访问\n"
                "2. 服务器是否已启动\n"
                "3. 防火墙 / 网络是否放行"
            )

        except Exception as exc:
            return (f"❌ 请求异常 [{type(exc).__name__}]\n{exc}",)

        if _looks_like_html(body_str):
            return (
                "❌ 服务端返回了 HTML 网页（而非 JSON）\n"
                "网关把请求转到了 Web 管理页，而不是 API。\n"
                f"【调试】POST 地址: {chat_url}"
            )

        if status_code < 200 or status_code >= 400:
            err_code, err_msg = _classify_http_error(status_code, body_str)
            return (f"❌ HTTP {status_code}\n{err_msg}",)

        try:
            data = json.loads(body_str)
        except json.JSONDecodeError:
            return (f"❌ 响应不是有效的 JSON\n响应内容: {body_str[:500]}…",)

        extracted = extract_prompt_from_response(data)
        if extracted:
            return (extracted,)

        choices = data.get("choices") if isinstance(data, dict) else None
        if choices:
            return ("⚠️ 模型返回了空内容，请尝试更换模型或检查提示词",)

        return (f"❌ 无法从响应中提取结果\n响应内容: {body_str[:800]}…",)


NODE_CLASS_MAPPINGS = {
    "图像反推": ImagePromptReverseNode,
    "文本扩写": TextExpandNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "图像反推": "图像反推",
    "文本扩写": "文本扩写",
}
