from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
import time
import warnings
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
import urllib3
from urllib3.exceptions import InsecureRequestWarning

try:
    from requests.packages import urllib3 as requests_urllib3  # type: ignore
except Exception:  # pragma: no cover - 兼容精简 Python 发行版
    requests_urllib3 = None

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from logger import logger  # type: ignore


class GeminiApiClient:
    """封装与 Gemini 兼容图像接口交互的 HTTP 客户端。"""

    _DEFAULT_CONNECT_TIMEOUT = 15.0
    _DEFAULT_READ_TIMEOUT = None
    _MAX_RETRIES = 2
    _BASE_BACKOFF = 2.0
    _RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
    _ASPECT_RATIO_ALIASES: Dict[str, str] = {
        "1:1": "1:1",
        "1:4": "1:4",
        "1:8": "1:8",
        "2:3": "2:3",
        "3:2": "3:2",
        "3:4": "3:4",
        "4:1": "4:1",
        "4:3": "4:3",
        "4:5": "4:5",
        "5:4": "5:4",
        "8:1": "8:1",
        "9:16": "9:16",
        "16:9": "16:9",
        "21:9": "21:9",
    }
    _INSECURE_WARNING_SUPPRESSED = False

    def __init__(self, config_manager, logger_instance=logger, interrupt_checker=None) -> None:
        self.config_manager = config_manager
        self.logger = logger_instance
        self.interrupt_checker = interrupt_checker
        self._thread_local = threading.local()

    # --- request 构造逻辑 -------------------------------------------------
    def _normalize_aspect_ratio(self, aspect_ratio: Optional[str]) -> Optional[str]:
        if not aspect_ratio or aspect_ratio.lower() == "auto":
            return None
        normalized = aspect_ratio.strip()
        return self._ASPECT_RATIO_ALIASES.get(normalized, normalized)

    def _normalize_model_id(self, model_type: Optional[str]) -> str:
        model = (model_type or "").strip()
        if "/models/" in model:
            model = model.split("/models/", 1)[1]
        for prefix in ("models/", "v1beta/"):
            if model.startswith(prefix):
                model = model.split("/", 1)[1]
        return model

    def create_request_data(
        self,
        prompt: str,
        seed: int,
        aspect_ratio: str,
        top_p: float,
        input_images_b64: Optional[List[str]] = None,
        model_type: Optional[str] = None,
        image_size: Optional[str] = None,
    ) -> Dict[str, Any]:
        prompt_text = (prompt or "").strip()
        if not prompt_text and not input_images_b64:
            raise ValueError("请输入提示词或提供至少一张参考图像")

        # 构建参数后缀：将分辨率和比例附加到提示词末尾
        param_suffix_parts: List[str] = []

        # 处理分辨率
        if image_size:
            normalized_size = (image_size or "").strip().upper()
            if normalized_size in {"1K", "2K", "4K"}:
                param_suffix_parts.append(f"分辨率: {normalized_size}")

        # 处理比例
        aspect = self._normalize_aspect_ratio(aspect_ratio)
        if aspect:
            param_suffix_parts.append(f"比例: {aspect}")

        # 将参数后缀附加到提示词
        if prompt_text and param_suffix_parts:
            param_suffix = " [" + ", ".join(param_suffix_parts) + "]"
            prompt_text = prompt_text + param_suffix

        parts: List[Dict[str, Any]] = []
        if prompt_text:
            parts.append({"text": prompt_text})

        for encoded in input_images_b64 or []:
            if not encoded:
                continue
            parts.append({
                "inlineData": {
                    "mimeType": "image/png",
                    "data": encoded,
                }
            })

        content = {"role": "user", "parts": parts}
        generation_config: Dict[str, Any] = {
            "topP": float(top_p),
            "responseModalities": ["IMAGE"],
        }
        if isinstance(seed, int) and seed >= 0:
            generation_config["seed"] = seed

        image_config: Dict[str, Any] = {}
        
        # 处理 aspectRatio：非 Auto 时添加到 imageConfig
        aspect = self._normalize_aspect_ratio(aspect_ratio)
        if aspect:
            image_config["aspectRatio"] = aspect

        # 始终支持 imageSize 参数（1K、2K、4K），不再限制模型名称
        # 适用于所有支持图像生成的模型
        # 注意：使用大写格式以匹配 API 要求
        if image_size:
            normalized_size = (image_size or "2K").strip().upper()
            valid_sizes = {"1K", "2K", "4K"}
            if normalized_size in valid_sizes:
                image_config["imageSize"] = normalized_size
            elif normalized_size == "无":
                pass
            else:
                # 默认使用 2K
                image_config["imageSize"] = "2K"
        else:
            # 如果没有指定 image_size，默认使用 2K
            image_config["imageSize"] = "2K"

        # 仅当 imageConfig 不为空时添加到 generationConfig
        if image_config:
            generation_config["imageConfig"] = image_config

        request_body: Dict[str, Any] = {
            "contents": [content],
            "generationConfig": generation_config,
        }

        return request_body

    # --- HTTP 发送逻辑 ----------------------------------------------------
    def _get_session(self, bypass_proxy: bool = False) -> requests.Session:
        attr_name = "session_no_proxy" if bypass_proxy else "session"
        session = getattr(self._thread_local, attr_name, None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=0)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            if bypass_proxy:
                session.trust_env = False
                session.proxies = {}
            setattr(self._thread_local, attr_name, session)
        return session

    def _ensure_not_interrupted(self) -> None:
        if self.interrupt_checker is not None:
            self.interrupt_checker()

    def _interruptible_post(
        self,
        session: requests.Session,
        url: str,
        payload: bytes,
        headers: Dict[str, str],
        timeout: Tuple[float, Optional[float]],
        verify: bool,
        bypass_proxy: bool,
    ) -> requests.Response:
        """
        让网络请求在长耗时阶段也能响应 ComfyUI 的中断。
        使用后台线程发起请求，主线程轮询中断标志，必要时关闭 session 终止阻塞。
        """
        if self.interrupt_checker is None:
            return session.post(
                url,
                data=payload,
                headers=headers,
                timeout=timeout,
                verify=verify,
            )

        done_event = threading.Event()
        resp_holder: Dict[str, Any] = {}
        exc_holder: Dict[str, BaseException] = {}

        def _do_request() -> None:
            try:
                resp_holder["resp"] = session.post(
                    url,
                    data=payload,
                    headers=headers,
                    timeout=timeout,
                    verify=verify,
                )
            except BaseException as exc:  # pragma: no cover - 直接回传给主线程
                exc_holder["exc"] = exc
            finally:
                done_event.set()

        thread = threading.Thread(target=_do_request, daemon=True)
        thread.start()

        poll_interval = 0.25
        attr_name = "session_no_proxy" if bypass_proxy else "session"
        try:
            while not done_event.wait(timeout=poll_interval):
                self._ensure_not_interrupted()
            self._ensure_not_interrupted()
        except BaseException:
            # 强制关闭当前 session，尽快打断正在阻塞的 request
            try:
                session.close()
            finally:
                setattr(self._thread_local, attr_name, None)
            raise

        if "exc" in exc_holder:
            raise exc_holder["exc"]

        resp = resp_holder.get("resp")
        if resp is None:
            # 极端情况下 session.post 未返回但线程结束，视为连接失败
            raise RuntimeError("请求被中断或未获得响应")
        return resp

    @classmethod
    def _suppress_insecure_warning(cls, verify_ssl: bool) -> None:
        if verify_ssl or cls._INSECURE_WARNING_SUPPRESSED:
            return
        # urllib3 的 InsecureRequestWarning 会在关闭 SSL 验证时提示真实域名。
        # 当用户显式关闭验证时,统一在客户端级别关闭该告警,避免源站泄露。
        warnings.filterwarnings("ignore", category=InsecureRequestWarning)
        urllib3.disable_warnings(InsecureRequestWarning)
        if requests_urllib3 is not None:
            try:
                requests_urllib3.disable_warnings(InsecureRequestWarning)
            except Exception:
                pass
        cls._INSECURE_WARNING_SUPPRESSED = True

    def _build_headers(self, api_key: str) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-API-Key": api_key,
            "X-Banana-Client": "sillydream-banana",
        }

    def _resolve_timeout(self, timeout: Optional[Any]) -> Tuple[float, Optional[float]]:
        """
        解析超时参数。
        返回 (connect_timeout, read_timeout)，其中 read_timeout 为 None 表示无限制。
        """
        if isinstance(timeout, (tuple, list)) and len(timeout) == 2:
            connect = float(timeout[0]) if timeout[0] else self._DEFAULT_CONNECT_TIMEOUT
            # read_timeout 为 None 表示无限制
            if timeout[1] is None:
                read = None
            else:
                read = float(timeout[1]) if timeout[1] else self._DEFAULT_READ_TIMEOUT
        elif isinstance(timeout, (int, float)) and timeout > 0:
            connect = read = float(timeout)
        else:
            connect = self._DEFAULT_CONNECT_TIMEOUT
            read = self._DEFAULT_READ_TIMEOUT
        connect = max(1.0, connect)
        if read is not None:
            read = max(5.0, read)
        return (connect, read)

    def _summarize_error_response(self, response: Optional[requests.Response]) -> str:
        """
        提取对用户安全的错误摘要，避免暴露源站费用或请求 ID 等细节。
        """
        if response is None:
            return "无响应内容"

        try:
            payload = response.json()
            if isinstance(payload, dict):
                error_obj = payload.get("error")
                if isinstance(error_obj, dict):
                    message = (error_obj.get("message") or "").strip()
                    normalized = message.lower()
                    if "token quota" in normalized and "not enough" in normalized:
                        return "余额不足：账户额度不足以完成本次请求，请充值后重试"
                    if message:
                        return message[:300]
                message = payload.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()[:300]
        except Exception:
            # JSON 解析失败时回退到纯文本
            pass

        body = response.text or "无响应内容"
        return body[:300]

    def _build_generate_content_url(self, base_url: str, model_type: str) -> str:
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("未配置有效的 API Base URL")
        model = (model_type or "").strip()
        if not model:
            raise ValueError("未指定模型类型")

        if model.startswith("models/"):
            model = model.split("/", 1)[1]
        if model.startswith("v1beta/"):
            model = model.split("/", 1)[1]

        if base.endswith(":generateContent"):
            return base
        if ":generate" in base:
            return base
        if base.endswith(f"/{model}:generateContent"):
            return base
        if base.endswith(f"/{model}"):
            return f"{base}:generateContent"
        if "/models/" in base:
            return f"{base.rstrip('/')}:generateContent"
        return f"{base}/v1beta/models/{model}:generateContent"

    def send_request(
        self,
        api_key: str,
        request_data: Dict[str, Any],
        model_type: str,
        api_base_url: str,
        timeout: Optional[Any] = None,
        bypass_proxy: bool = False,
        verify_ssl: bool = True,
        max_retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        sanitized_key = self.config_manager.sanitize_api_key(api_key)
        if not sanitized_key:
            raise ValueError("请填写有效的 API Key")

        url = self._build_generate_content_url(api_base_url, model_type)
        session = self._get_session(bypass_proxy)
        self._suppress_insecure_warning(verify_ssl)
        connect_timeout, read_timeout_global = self._resolve_timeout(timeout)
        headers = self._build_headers(sanitized_key)

        # 注意：requests 在 data 为 str 时会将其直接传递给 http.client，
        # 后者默认使用 latin-1 编码字符串，这会在请求体包含中文等非 latin-1 字符时
        # 触发 "Body (...) is not valid Latin-1" 错误。
        # 这里显式将 JSON 序列化结果编码为 UTF-8 bytes，避免依赖 http.client 的默认编码。
        payload = json.dumps(request_data, ensure_ascii=False).encode("utf-8")
        last_error: Optional[BaseException] = None
        last_error_phase: Optional[str] = None  # connect/read
        last_error_hint: Optional[str] = None

        effective_max_retries = (
            max_retries
            if isinstance(max_retries, int) and max_retries >= 1
            else self._MAX_RETRIES
        )

        # 采用“全局读取超时 + 每次连接 15s”语义：
        # - connect_timeout：单次连接阶段的超时时间（例如 15s），每次尝试独立计算
        # - read_timeout_global：从第一次尝试开始计时的全局读取超时（例如 90s 或 70s）
        #   后续重试只使用剩余的读取时间，确保总耗时不会超过全局读取超时
        global_start = time.time()
        attempt_delay = self._BASE_BACKOFF  # 初始重试间隔（秒）

        for attempt in range(1, effective_max_retries + 1):
            self._ensure_not_interrupted()
            # 计算本次尝试可用的剩余读取时间
            elapsed = time.time() - global_start
            # read_timeout_global 为 None 表示无限制
            if read_timeout_global is None:
                remaining_read = None
            else:
                remaining_read = read_timeout_global - elapsed
                if remaining_read <= 0:
                    # 全局读取超时已耗尽，不再发起新的请求
                    raise RuntimeError(
                        f"模型 {model_type} 响应超时：总耗时 {elapsed:.1f}s 已超过读取上限 {read_timeout_global:.1f}s"
                    )

            start = time.time()
            try:
                response = self._interruptible_post(
                    session,
                    url,
                    payload,
                    headers,
                    (connect_timeout, remaining_read),
                    verify_ssl,
                    bypass_proxy,
                )
                if (
                    response.status_code in self._RETRYABLE_STATUS
                    and attempt < effective_max_retries
                ):
                    raise requests.HTTPError(
                        f"HTTP {response.status_code}", response=response
                    )
                response.raise_for_status()
                return response.json()
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                duration = time.time() - start
                exc_text = str(exc).lower()
                # 识别“已连接但被远端关闭/重置”的场景，避免误判为 DNS/代理问题
                remote_closed = isinstance(exc, requests.ConnectionError) and any(
                    keyword in exc_text
                    for keyword in (
                        "remote end closed",
                        "connection reset",
                        "connection aborted",
                        "bad status line",
                        "broken pipe",
                    )
                )
                exceeded_connect_budget = duration > (connect_timeout + 1.0)
                is_read_timeout = (
                    isinstance(exc, requests.Timeout)
                    and not isinstance(exc, requests.ConnectTimeout)
                    and not isinstance(exc, requests.ConnectionError)
                )

                if is_read_timeout:
                    # 读取阶段超时：请求已送达且可能仍在处理，避免自动重试造成额外压力
                    last_error_phase = "read"
                    hint = (
                        f"服务器在 {duration:.1f}s 内未返回数据，可能仍在生成；为避免重复请求干扰，已停止自动重试"
                    )
                    last_error_hint = hint
                    raise RuntimeError(
                        f"模型 {model_type} 响应超时：{hint}"
                    )

                if remote_closed or exceeded_connect_budget:
                    # 已建立连接但在生成阶段被远端关闭，多见于上游/LB 空闲超时（4K 耗时更长时更容易触发）
                    last_error_phase = "read"
                    hint = (
                        f"服务器在 {duration:.1f}s 后中断连接，通常是生成阶段耗时超过上游或网关的空闲时间限制，"
                        "请稍后重试、或尝试绕过代理"
                    )
                    last_error_hint = hint
                    self.logger.warning(
                        f"生成阶段连接被远端关闭：{model_type}（耗时 {duration:.1f}s，尝试 {attempt}/{effective_max_retries}）"
                    )
                    raise RuntimeError(f"模型 {model_type} 响应中途断开：{hint}")

                # 连接阶段失败：可以安全重试，不会触发生成流程
                if isinstance(exc, requests.ConnectTimeout) or isinstance(
                    exc, requests.ConnectionError
                ):
                    last_error_phase = "connect"
                    hint = (
                        "连接阶段耗时过长或无法建立，请检查代理、DNS 或 Base URL 域名是否可达"
                    )
                    self.logger.warning(
                        f"连接阶段失败：{model_type}（耗时 {duration:.1f}s，尝试 {attempt}/{effective_max_retries}）"
                    )
                    last_error_hint = hint
                else:
                    # 兜底：视为读取阶段异常
                    last_error_phase = "read"
                    last_error_hint = "模型响应阶段异常，请检查网络链路或服务状态"
                    raise RuntimeError(
                        f"模型 {model_type} 响应异常：{last_error_hint}"
                    )
            except requests.HTTPError as exc:
                last_error = exc
                status = exc.response.status_code if exc.response else None
                # 避免泄露源站域名和费用细节：仅使用脱敏后的摘要
                truncated = self._summarize_error_response(exc.response)
                if status in self._RETRYABLE_STATUS and attempt < effective_max_retries:
                    self.logger.warning(
                        f"HTTP {status}，将重试：{truncated}"
                    )
                else:
                    raise RuntimeError(
                        f"远端返回异常（HTTP {status}）：{truncated}"
                    )
            except requests.RequestException as exc:
                last_error = exc
                error_type = type(exc).__name__
                status = None
                if getattr(exc, "response", None) is not None:
                    try:
                        status = exc.response.status_code
                    except Exception:
                        status = None
                status_text = f"（HTTP {status}）" if status else ""
                raise RuntimeError(
                    f"HTTP 请求失败{status_text}（{error_type}），请检查网络连接、代理或证书配置"
                )

            if attempt < effective_max_retries:
                time.sleep(attempt_delay)
                attempt_delay *= 1.5

        # 对最终用户仅暴露抽象错误类型，避免泄露真实源站地址或 URL 细节
        error_label = type(last_error).__name__ if last_error is not None else "未知错误"
        if last_error_phase == "connect":
            hint = last_error_hint or "请检查网络、代理或 API Base URL 配置"
            raise RuntimeError(
                f"连接 {model_type} 失败（{error_label}）：{hint}"
            )
        if last_error_phase == "read":
            hint = last_error_hint or "模型响应过慢，超过读取时间上限"
            raise RuntimeError(
                f"模型 {model_type} 响应超时（{error_label}）：{hint}"
            )
        raise RuntimeError(
            f"连续 {effective_max_retries} 次请求失败（错误类型：{error_label}），"
            f"请检查网络环境或服务状态"
        )

    # --- 响应解析 --------------------------------------------------------
    # 图床 URL 正则匹配模式
    _IMAGE_URL_PATTERN = re.compile(
        r'https?://[^\s<>"\']+\.(?:png|jpg|jpeg|gif|webp|bmp)(?:\?[^\s<>"\']*)?',
        re.IGNORECASE
    )
    # Markdown base64 图像格式匹配模式: ![...](data:image/...;base64,...)
    _MARKDOWN_BASE64_IMAGE_PATTERN = re.compile(
        r'!\[([^\]]*)\]\(data:image/(png|jpeg|jpg|gif|webp|bmp);base64,([A-Za-z0-9+/=]+)\)',
        re.IGNORECASE
    )
    # 常见图床域名特征
    _IMAGE_HOST_PATTERNS = [
        r'imgur\.com',
        r'i\.imgur\.com',
        r'imgbb\.com',
        r'i\.ibb\.co',
        r'postimg\.cc',
        r'i\.postimg\.cc',
        r'cloudinary\.com',
        r'res\.cloudinary\.com',
        r'imagekit\.io',
        r'ik\.imagekit\.io',
        r'storage\.googleapis\.com',
        r'blob\.core\.windows\.net',
        r's3\.amazonaws\.com',
        r'cdn\.',
        r'img\.',
        r'image\.',
        r'images\.',
        r'pic\.',
        r'pics\.',
        r'photo\.',
        r'photos\.',
        r'upload\.',
        r'uploads\.',
        r'static\.',
        r'assets\.',
        r'media\.',
    ]

    def _is_image_url(self, text: str) -> bool:
        """判断文本是否为图床 URL"""
        if not text or not isinstance(text, str):
            return False
        text = text.strip()
        # 检查是否以 http:// 或 https:// 开头
        if not text.startswith(('http://', 'https://')):
            return False
        # 检查是否匹配图片 URL 模式
        if self._IMAGE_URL_PATTERN.match(text):
            return True
        # 检查是否包含常见图床域名特征
        text_lower = text.lower()
        for pattern in self._IMAGE_HOST_PATTERNS:
            if re.search(pattern, text_lower):
                # 进一步检查 URL 是否可能是图片
                if any(ext in text_lower for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', 'image', 'img', 'pic', 'photo']):
                    return True
        return False

    def _extract_image_urls_from_text(self, text: str) -> List[str]:
        """从文本中提取所有图床 URL"""
        if not text:
            return []
        urls = self._IMAGE_URL_PATTERN.findall(text)
        return [url for url in urls if self._is_image_url(url)]

    def _download_image_to_base64(self, url: str, timeout: float = 30.0) -> Optional[str]:
        """
        下载图床图片并转换为 base64 格式。
        返回 base64 编码的图片数据，失败返回 None。
        """
        try:
            session = self._get_session(bypass_proxy=False)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "image/*,*/*;q=0.8",
            }
            response = session.get(url, headers=headers, timeout=timeout, verify=True)
            response.raise_for_status()
            
            # 检查 Content-Type 是否为图片
            content_type = response.headers.get("Content-Type", "").lower()
            if not content_type.startswith("image/"):
                # 尝试通过文件头判断
                content = response.content
                if len(content) < 8:
                    self.logger.warning(f"图床下载失败：内容过短，可能不是图片 - {url[:50]}...")
                    return None
                # PNG: 89 50 4E 47
                # JPEG: FF D8 FF
                # GIF: 47 49 46 38
                # WebP: 52 49 46 46 ... 57 45 42 50
                if not (content[:4] == b'\x89PNG' or 
                        content[:3] == b'\xff\xd8\xff' or 
                        content[:4] == b'GIF8' or
                        (content[:4] == b'RIFF' and content[8:12] == b'WEBP')):
                    self.logger.warning(f"图床下载失败：Content-Type 不是图片且文件头不匹配 - {url[:50]}...")
                    return None
            
            # 转换为 base64
            image_data = response.content
            base64_data = base64.b64encode(image_data).decode('utf-8')
            self.logger.info(f"图床图片下载成功：{len(image_data)} 字节 - {url[:50]}...")
            return base64_data
        except requests.Timeout:
            self.logger.warning(f"图床下载超时：{url[:50]}...")
        except requests.RequestException as exc:
            self.logger.warning(f"图床下载失败：{type(exc).__name__} - {url[:50]}...")
        except Exception as exc:
            self.logger.warning(f"图床下载异常：{type(exc).__name__}: {exc} - {url[:50]}...")
        return None

    def extract_content(self, response_data: Dict[str, Any]) -> Tuple[List[str], str]:
        """
        从 API 响应中提取图片和文本内容。
        支持两种图片格式：
        1. inlineData 格式：直接包含 base64 编码的图片数据
        2. 图床 URL 格式：自动下载并转换为 base64
        """
        if not isinstance(response_data, dict):
            raise ValueError("接口返回数据格式异常")

        images: List[str] = []
        texts: List[str] = []
        image_urls_to_download: List[str] = []
        candidates = response_data.get("candidates") or []

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") or {}
            parts = content.get("parts") or []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                
                # 1. 检查 inlineData 格式（标准 Gemini 响应）
                inline = part.get("inlineData")
                if inline and isinstance(inline, dict):
                    data = inline.get("data")
                    mime = inline.get("mimeType", "")
                    if data and isinstance(data, str) and mime.startswith("image/"):
                        images.append(data)
                        continue
                
                # 2. 检查 fileData 格式（可能包含图床 URL）
                file_data = part.get("fileData")
                if file_data and isinstance(file_data, dict):
                    file_uri = file_data.get("fileUri") or file_data.get("uri") or file_data.get("url")
                    if file_uri and isinstance(file_uri, str) and self._is_image_url(file_uri):
                        image_urls_to_download.append(file_uri)
                        continue
                
                # 3. 检查 text 字段
                text_value = part.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    text_stripped = text_value.strip()
                    
                    # 3.1 首先检查是否包含 Markdown base64 图像格式
                    markdown_matches = self._MARKDOWN_BASE64_IMAGE_PATTERN.findall(text_stripped)
                    if markdown_matches:
                        for match in markdown_matches:
                            # match 是 (alt_text, image_type, base64_data) 元组
                            base64_data = match[2]
                            if base64_data:
                                images.append(base64_data)
                                self.logger.info(f"从 Markdown 格式提取 base64 图像：{len(base64_data)} 字符")
                        # 移除已提取的 Markdown 图像，保留剩余文本
                        remaining_text = self._MARKDOWN_BASE64_IMAGE_PATTERN.sub("", text_stripped).strip()
                        if remaining_text:
                            texts.append(remaining_text)
                        continue
                    
                    # 3.2 检查文本是否为单独的图床 URL
                    if self._is_image_url(text_stripped):
                        image_urls_to_download.append(text_stripped)
                    else:
                        # 尝试从文本中提取图床 URL
                        extracted_urls = self._extract_image_urls_from_text(text_stripped)
                        if extracted_urls:
                            image_urls_to_download.extend(extracted_urls)
                            # 如果文本中除了 URL 还有其他内容，保留文本
                            remaining_text = text_stripped
                            for url in extracted_urls:
                                remaining_text = remaining_text.replace(url, "").strip()
                            if remaining_text:
                                texts.append(remaining_text)
                        else:
                            texts.append(text_stripped)
                
                # 4. 检查 image_url 字段（某些 API 可能使用此格式）
                image_url = part.get("image_url") or part.get("imageUrl") or part.get("url")
                if image_url and isinstance(image_url, str) and self._is_image_url(image_url):
                    image_urls_to_download.append(image_url)

        # 下载图床图片并转换为 base64
        if image_urls_to_download:
            self.logger.info(f"检测到 {len(image_urls_to_download)} 个图床 URL，开始下载...")
            for url in image_urls_to_download:
                self._ensure_not_interrupted()
                base64_data = self._download_image_to_base64(url)
                if base64_data:
                    images.append(base64_data)
                else:
                    self.logger.warning(f"图床图片下载失败，跳过：{url[:80]}...")

        combined_text = "\n".join(texts).strip()
        return images, combined_text


__all__ = ["GeminiApiClient"]
