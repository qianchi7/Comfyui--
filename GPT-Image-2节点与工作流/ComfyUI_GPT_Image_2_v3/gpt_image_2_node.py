import json
import base64
import io
import requests
import torch
import numpy as np
import re
import threading
import time
from PIL import Image
import comfy.utils
import comfy.model_management

# === v2 改进: 节点内部不重试 + 502 全量诊断 ===
# 用户偏好: 重试 = 重复扣费 (尤其 new-api channel 内部还会再 4 次).
# 失败立即抛带诊断的 RuntimeError, 让用户自己决定下一步.
from . import endpoint_diagnostics as _diag
try:
    import config_manager  # base_url / model 自动持久化
except ImportError:
    config_manager = None

class GPTImage2Generator:
    _CONNECT_TIMEOUT = 30.0
    _REQUEST_POLL_INTERVAL = 0.25
    _RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
    # 远端断开/连接被网关截断这类异常可以安全地重试
    _REMOTE_CLOSED_HINTS = (
        "remote end closed",
        "connection reset",
        "connection aborted",
        "bad status line",
        "broken pipe",
        "chunkedencodingerror",
        "incompleteread",
        "eof occurred",
        "server disconnected",
    )

    # === v2.2: 兼容"图床 Markdown / 裸 URL"返回格式 ===
    # 部分中转/relay 不走标准 Images API 结构(data[].b64_json / data[].url)，
    # 而是把生图结果伪装成"聊天回复"文本塞进任意字段(如 content/text/message)，
    # 里面夹带 Markdown 图片语法 ![alt](https://img-bed.example.com/xxx.png)
    # 或者一个裸的图片 URL。原逻辑只认 b64_json/url/image_url/result 这几个
    # 已知字段，且要求值本身就是纯 URL/base64——遇到这种夹带格式会直接漏解析。
    # 下面两个正则用于从任意字符串字段里兜底抠出图片链接。
    _MARKDOWN_IMAGE_RE = re.compile(r'!\[[^\]]*\]\(\s*(https?://[^\s\)]+)')
    _BARE_IMAGE_URL_RE = re.compile(
        r'https?://[^\s\'"<>\)\]]+?\.(?:png|jpe?g|webp|gif|bmp)(?:\?[^\s\'"<>\)\]]*)?',
        re.IGNORECASE,
    )

    GPT_IMAGE_2_MIN_PIXELS = 655_360
    GPT_IMAGE_2_MAX_PIXELS = 8_294_400
    GPT_IMAGE_2_MAX_EDGE = 3840
    GPT_IMAGE_2_MAX_RATIO = 3
    RESOLUTION_OPTIONS = ["auto", "1K", "2K", "4K"]
    # === 已知稳定的比例列表 ===
    # 只保留 relay/上游实测在 pixel budget 内、不会 502 的合法比例。
    ASPECT_RATIO_OPTIONS = [
        "auto",
        "1:1",
        "3:2",
        "2:3",
        "16:9",
        "9:16",
        "4:3",
        "3:4",
        "21:9",
        "1:3",
        "3:1",
    ]

    # === 权威尺寸表 (GPT_IMAGE_SIZE_TABLE) ===
    # 结构: 宽高比 -> { 分辨率档(小写): "宽x高" }。
    # 缺失档位 = 该比例在该分辨率下【无合法尺寸】——这就是互斥规则本身。
    #   例: "1:1" 没有 "4k"  → 选 1:1 就不能选 4K
    #       "16:9" 没有 "1k" → 选 16:9 就不能选 1K
    #       "3:1" 只有 "4k"  → 只能 4K
    # 这些尺寸是实测「不会 502」的固定尺寸；不再自由拼接任意尺寸。
    GPT_IMAGE_SIZE_TABLE = {
        "1:1":  {"1k": "1024x1024", "2k": "2048x2048"},
        "3:2":  {"1k": "1536x1024", "2k": "2048x1360", "4k": "3520x2352"},
        "2:3":  {"1k": "1024x1536", "2k": "1360x2048", "4k": "2352x3520"},
        "16:9": {"2k": "2048x1152", "4k": "3840x2160"},
        "9:16": {"2k": "1152x2048", "4k": "2160x3840"},
        "4:3":  {"2k": "2048x1536", "4k": "3312x2480"},
        "3:4":  {"2k": "1536x2048", "4k": "2480x3312"},
        "21:9": {"2k": "2688x1152", "4k": "3840x1648"},
        "1:3":  {"2k": "1024x3072", "4k": "1280x3840"},
        "3:1":  {"4k": "3840x1280"},
    }
    _RES_ORDER = ["1k", "2k", "4k"]

    @classmethod
    def _resolutions_for(cls, aspect_ratio):
        """给定比例，返回其有合法尺寸的分辨率档（小写）。用于互斥判断。"""
        row = cls.GPT_IMAGE_SIZE_TABLE.get(aspect_ratio, {})
        return [r for r in cls._RES_ORDER if r in row]

    ENDPOINT_OPTIONS = [
        "auto",
        "/v1/images/generations",
        "/v1/images/edits",
        "/v1/responses"
    ]

    def __init__(self):
        self._thread_local = threading.local()
    
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "api_key": ("STRING", {"multiline": False, "default": "", "tooltip": "必填，输入您的 OpenAI 格式 API Key"}),
                "base_url": ("STRING", {"multiline": False, "default": "http://38.145.218.40:12001", "tooltip": "只填写服务地址即可，例如 http://38.145.218.40:12001；节点会根据 api_endpoint 自动拼接 /v1/images/generations、/v1/images/edits 或 /v1/responses；若你填完整接口也会兼容。"}),
                "model": ("STRING", {"multiline": False, "default": "「CS」gpt-image-2", "tooltip": "允许自由填写，如 gpt-image-2, openai/gpt-5.4-image-2, 「AZ」gpt-image-2 等"}),
                "prompt": ("STRING", {"multiline": True, "default": "A beautiful cat, high resolution, 4k", "tooltip": "正向提示词，必填"}),
                "negative_prompt": ("STRING", {"multiline": True, "default": "", "tooltip": "负面提示词，可选"}),
                "resolution": (s.RESOLUTION_OPTIONS, {"default": "2K", "tooltip": "基准分辨率档。注意与比例【互斥】：1:1 只有 1K/2K（无 4K）；16:9/9:16/4:3/3:4/21:9 只有 2K/4K（无 1K）；3:1 只有 4K。auto = 让上游自选（需 aspect_ratio 也为 auto）。所有尺寸均为实测稳定档位，超出的组合会被拦截以避免 502。"}),
                "aspect_ratio": (s.ASPECT_RATIO_OPTIONS, {"default": "1:1", "tooltip": "图像比例：1:1 3:2 2:3 16:9 9:16 4:3 3:4 21:9 1:3 3:1。auto = 按 image_1 输入图比例自动推断（需连接参考图）。"}),
                "quality": (["auto", "low", "medium", "high"], {"default": "high", "tooltip": "Quality. auto omits this parameter for maximum compatibility."}),
                "n": ("INT", {"default": 1, "min": 1, "max": 10, "step": 1, "tooltip": "生成数量，范围 1-10"}),
                "seed": ("INT", {"default": -1, "min": -1, "max": 0xffffffffffffffff, "tooltip": "随机种子，-1 表示随机"}),
                "style_preset": (["none", "photographic", "digital-art", "anime", "3d-render", "oil-painting", "watercolor", "sketch"], {"default": "none", "tooltip": "风格预设"}),
                "enhance_prompt": ("BOOLEAN", {"default": True, "tooltip": "增强提示词，开启后 OpenAI 自动优化提示词"}),
                "safety_check": ("BOOLEAN", {"default": True, "tooltip": "安全检查，拦截不合规内容"}),
                "response_format": (["auto", "b64_json", "url"], {"default": "auto", "tooltip": "auto = 未选择时优先显式请求 b64_json（避免依赖下载图床图片，减少失败面）；也可强制选 url 或 b64_json。"}),
                "edit_mode": (["generate", "reference", "outpaint"], {"default": "generate", "tooltip": "编辑模式：纯生成 / 参考图生图 / 扩图"}),
                "reference_strength": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 1.0, "step": 0.05, "tooltip": "参考强度，范围 0.0-1.0，仅在 reference 等模式生效"}),
                "timeout": ("INT", {"default": 900, "min": 30, "max": 7200, "step": 1, "tooltip": "总超时时间（秒）。GPT-Image-2 在 4K/High 质量下生成常需 5-15 分钟，建议保持 600s 以上；如经常生成 4K 大图请调到 1800s 或更高。"}),
                "infinite_timeout": ("BOOLEAN", {"default": True, "tooltip": "无限总超时（推荐开启）。开启后底层 read_timeout 不再限制，专门用于规避反向代理 300s 默认超时导致的 502/连接断开；ComfyUI 中断按钮仍然可以随时停止任务。"}),
                "api_endpoint": (s.ENDPOINT_OPTIONS, {"default": "auto", "tooltip": "auto: text uses /v1/images/generations; input images/reference uses /v1/images/edits; can force /v1/responses."})
            },
            "optional": {
                "image_1": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_2": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_3": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_4": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_5": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_6": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_7": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_8": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_9": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_10": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_11": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_12": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_13": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_14": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_15": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"}),
                "image_16": ("IMAGE", {"tooltip": "最多支持16张参考图；auto 下会走 /v1/images/edits multipart，强制 generations 时才放入 JSON image 数组"})
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("IMAGE", "INFO_JSON")
    FUNCTION = "generate_image"
    CATEGORY = "GPT Image 2"

    # ==========================================
    # 最佳实践说明（Best Practices）：
    # 1. 多图融合推荐使用 3-8 张参考图，效果最稳定。最多支持16张。
    # 2. High 质量 4K 图成本较高，建议先用 Medium 质量调试。
    # 3. 开启 Enhance Prompt 可显著提升生成质量。
    # 4. 参考强度 0.6-0.8 时风格融合最自然。
    # 5. 生成数量最多 10 张，超过会被 API 拒绝。
    # ==========================================

    def generate_image(self, api_key, base_url, model, prompt, negative_prompt, resolution, aspect_ratio,
                       quality, n, seed, style_preset, enhance_prompt, safety_check,
                       response_format, edit_mode, reference_strength, timeout, infinite_timeout, api_endpoint,
                       image_1=None, image_2=None, image_3=None, image_4=None,
                       image_5=None, image_6=None, image_7=None, image_8=None,
                       image_9=None, image_10=None, image_11=None, image_12=None,
                       image_13=None, image_14=None, image_15=None, image_16=None):
        
        if not api_key:
            raise ValueError("API Key is required. 请填写 API 密钥。")

        # 初始化进度条
        pbar = comfy.utils.ProgressBar(3)
        pbar.update(1) # 准备请求阶段

        # 根据分辨率 + 比例计算实际尺寸 (aspect_ratio=auto 时从 image_1 推断)
        size_str = self.calculate_size(resolution, aspect_ratio, image_1=image_1)

        # 构建请求头
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Connection": "keep-alive",
            "User-Agent": "ComfyUI-GPT-Image-2/1.1",
            # 长耗时请求禁用 Expect: 100-continue，避免某些代理握手 1s 等待后断流
            "Expect": "",
        }

        # === 构建请求 payload（对齐已验证稳定的生产用法）===
        # 只发这些字段: model / prompt / size / n / quality (+ seed 等)。
        # 绝不发 enhance_prompt / safety_check / negative_prompt / style_preset ——
        # 它们不是 GPT-Image-2 的合法参数，发出去轻则被 relay 判 400、重则拖慢上游导致 502。
        payload = {
            "model": model,
            "prompt": prompt,
            "n": n,
        }
        # size='auto' 时不发 size 字段（有才发），让上游自选
        if size_str and size_str != "auto":
            payload["size"] = size_str
        # quality='auto' 时不发
        if quality and quality != "auto":
            payload["quality"] = quality
        # === response_format: auto 时不再"不发交给上游默认" ===
        # 原假设"不发 = 上游默认给 b64_json"并不可靠：不少中转/relay 在缺省该
        # 字段时其实默认返回 url（下载图床图片这条路更容易因反代超时/跨域/
        # 图床失效而失败）。用户没有显式选择 url/b64_json 时，主动显式请求
        # b64_json，只有用户明确选 url 才走"下载"分支。
        effective_response_format = "b64_json" if response_format == "auto" else response_format
        payload["response_format"] = effective_response_format
        if response_format == "auto":
            print("[GPT Image 2] response_format=auto → 已显式请求 b64_json（未选择时优先要 base64，避免依赖图床下载）。")

        if seed != -1:
            payload["seed"] = seed

        # 以下参数 GPT-Image-2 不支持：只做提示，不发送（避免 400/502）。
        if negative_prompt and negative_prompt.strip() != "":
            print("[GPT Image 2] 提示: GPT-Image-2 不支持 negative_prompt，已忽略（可把要避免的内容写进正向 prompt）。")
        if style_preset and style_preset != "none":
            print(f"[GPT Image 2] 提示: GPT-Image-2 不支持 style_preset={style_preset!r}，已忽略。")
        if enhance_prompt is False or safety_check is False:
            print("[GPT Image 2] 提示: enhance_prompt/safety_check 非 GPT-Image-2 参数，已忽略。")
        # edit_mode 仅用于本地选择接口，不作为官方 Images 参数发送。

        # 收集所有输入的参考图
        all_images = []
        for img in [image_1, image_2, image_3, image_4, image_5, image_6, image_7, image_8,
                    image_9, image_10, image_11, image_12, image_13, image_14, image_15, image_16]:
            if img is not None:
                all_images.append(img)

        # 处理输入图片：转为 data:image/png;base64，后续按所选接口组装请求。
        b64_list = []
        if len(all_images) > 0:
            b64_images = []
            for img_tensor in all_images:
                b64_images.extend(self.tensor_to_base64_list(img_tensor))

            # 限制最多同时输入16张图片
            limit = min(len(b64_images), 16)
            b64_list = b64_images[:limit]
            payload["image"] = b64_list
            print(f"[GPT Image 2] 成功读取 {limit} 张输入图片")

        # 根据用户选择构建最终请求；不再混用 generations / edits / responses 的字段。
        # 注：response_format 不再在 auto 时剔除——已在上面显式设为 b64_json。
        if quality == 'auto':
            payload.pop('quality', None)

        if api_endpoint == 'auto':
            resolved_endpoint = '/v1/images/edits' if len(b64_list) > 0 else '/v1/images/generations'
        else:
            resolved_endpoint = api_endpoint

        if edit_mode in ['reference', 'outpaint'] and len(b64_list) == 0:
            print('Warning: 选择了 reference/outpaint 模式但未连接输入图片，auto 将按文生图 generations 处理。')

        if resolved_endpoint == '/v1/images/edits' and len(b64_list) == 0:
            raise ValueError('/v1/images/edits 需要至少连接一张输入图片；请连接 image_1~image_16，或把 api_endpoint 改为 auto/generations/responses。')

        api_url = self.build_api_url(base_url, resolved_endpoint)
        request_kwargs = {}

        # 用本地 helper 构造 request_kwargs，方便去掉某个未知参数后重新打包 JSON 字节
        def _build_json_kwargs(p):
            # 显式 UTF-8 编码，避免 requests 内部 latin-1 编码中文 prompt 时报错
            return {
                "data": json.dumps(p, ensure_ascii=False).encode("utf-8"),
            }

        if resolved_endpoint == '/v1/responses':
            content = [{'type': 'input_text', 'text': prompt}]
            for b64 in b64_list:
                content.append({'type': 'input_image', 'image_url': b64})

            image_tool = {'type': 'image_generation'}
            if size_str != 'auto':
                image_tool['size'] = size_str
            if quality != 'auto':
                image_tool['quality'] = quality
            if n > 1:
                content[0]['text'] = f'{prompt}\n\nPlease generate {n} separate image result(s).'

            payload = {
                'model': model,
                'input': [{'role': 'user', 'content': content}],
                'tools': [image_tool]
            }
            headers['Content-Type'] = 'application/json'
            request_kwargs = _build_json_kwargs(payload)

        elif resolved_endpoint == '/v1/images/edits':
            payload.pop('image', None)
            payload.pop('edit_mode', None)
            payload.pop('reference_strength', None)
            headers.pop('Content-Type', None)
            files = []
            for idx, b64 in enumerate(b64_list, start=1):
                pure_b64 = b64.split(',', 1)[1] if ',' in b64 else b64
                image_bytes = base64.b64decode(pure_b64)
                files.append(('image[]', (f'image_{idx}.png', image_bytes, 'image/png')))
            request_kwargs = {'data': payload, 'files': files}

        else:
            headers['Content-Type'] = 'application/json'
            if len(b64_list) > 0:
                payload['image'] = b64_list
                print('[GPT Image 2] api_endpoint 强制为 generations，输入图将按旧代理 JSON image 数组发送。')
            request_kwargs = _build_json_kwargs(payload)

        print(f'[GPT Image 2] Sending request to {api_url} ({resolved_endpoint}) with model: {model}, size: {size_str}')
        
        # 发送网络请求：用后台线程请求，主线程轮询 ComfyUI 中断；
        # v2 改进：**节点内部不重试** (用户偏好, 重试 = 重复扣费).
        # 网络异常和 5xx 都立即抛带诊断的 RuntimeError.
        data = None
        is_multipart = resolved_endpoint == '/v1/images/edits'
        max_attempts = 12  # 总尝试次数上限（含未知参数剔除）
        max_network_retries = 0  # ✅ v2: 关闭网络/5xx 重试
        network_retry_count = 0
        retry_delay = 2.0

        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            self._ensure_not_interrupted()
            response = None
            net_exc = None
            net_exc_label = None  # connect / read / remote_closed
            try:
                response = self._interruptible_request(
                    "POST",
                    api_url,
                    headers=headers,
                    timeout=timeout,
                    infinite_timeout=infinite_timeout,
                    **request_kwargs
                )
            except comfy.model_management.InterruptProcessingException:
                raise
            except TimeoutError as e:
                # 主线程总超时：是用户配置的硬上限，不再继续重试，避免无限等待。
                raise RuntimeError(f"[GPT Image 2] 请求总超时: {str(e)}。建议把 timeout 设大或开启 infinite_timeout。")
            except requests.ConnectionError as e:
                net_exc = e
                exc_text = str(e).lower()
                if any(hint in exc_text for hint in self._REMOTE_CLOSED_HINTS):
                    net_exc_label = "remote_closed"
                else:
                    net_exc_label = "connect"
            except requests.Timeout as e:
                # 通常是底层 read_timeout 触发（仅 infinite_timeout=False 时可能出现）
                net_exc = e
                net_exc_label = "read"
            except requests.RequestException as e:
                net_exc = e
                net_exc_label = "request"
            except Exception as e:
                # 兜底：UnknownNetworkError / SSLError / ProtocolError 等
                exc_text = str(e).lower()
                if any(hint in exc_text for hint in self._REMOTE_CLOSED_HINTS):
                    net_exc = e
                    net_exc_label = "remote_closed"
                else:
                    raise RuntimeError(f"[GPT Image 2] 网络请求异常: {type(e).__name__}: {e}")

            # === 网络层异常处理：分阶段重试 ===
            if response is None:
                # connect 阶段：请求未发出，重试完全安全
                # remote_closed / read：连接已发但被网关/上游中途关闭，常见于反代 proxy_read_timeout
                #   GPT-Image-2 端可能仍在算图，重试可能造成重复扣费；但绝大多数代理会去重，
                #   且不重试就一定失败 —— 给出明确日志后小心重试。
                if network_retry_count >= max_network_retries:
                    raise RuntimeError(
                        f"[GPT Image 2] 网络异常已重试 {network_retry_count} 次仍失败 "
                        f"({net_exc_label}): {type(net_exc).__name__}: {net_exc}。"
                        f"建议：1) 增大 timeout 至 1800s 以上；2) 开启 infinite_timeout；"
                        f"3) 检查中间反向代理的 proxy_read_timeout / keepalive_timeout 配置。"
                    )
                network_retry_count += 1
                wait_s = min(retry_delay * (1.6 ** (network_retry_count - 1)), 15.0)
                print(
                    f"[GPT Image 2] 网络异常({net_exc_label}) "
                    f"{type(net_exc).__name__}: {str(net_exc)[:160]}；"
                    f"{wait_s:.1f}s 后重试 ({network_retry_count}/{max_network_retries})..."
                )
                # 远端断开/读超时，强制重建 session，避免复用半坏连接
                if net_exc_label in ("remote_closed", "read"):
                    self._reset_session()
                # 可中断地 sleep
                slept = 0.0
                while slept < wait_s:
                    self._ensure_not_interrupted()
                    time.sleep(min(0.25, wait_s - slept))
                    slept += 0.25
                continue

            # === 成功收到 HTTP 响应：按状态码分支 ===
            if response.status_code == 400:
                try:
                    err_data = response.json() if response.text else {}
                except Exception:
                    err_data = {}
                err_msg = err_data.get("error", {}).get("message", str(err_data) or response.text)
                match = re.search(r'Unknown parameter[^A-Za-z0-9_\.]+([A-Za-z_][A-Za-z0-9_\.]*)', err_msg)
                if not match:
                    match = re.search(r'Unrecognized request argument[^A-Za-z0-9_\.]+([A-Za-z_][A-Za-z0-9_\.]*)', err_msg)
                if match:
                    unknown_param = match.group(1)
                    if unknown_param in payload:
                        print(f"[GPT Image 2] API Proxy rejects '{unknown_param}', removing and retrying...")
                        del payload[unknown_param]
                        # 按当前请求类型重新打包
                        if is_multipart:
                            request_kwargs["data"] = payload
                        else:
                            request_kwargs = _build_json_kwargs(payload)
                        continue

            if response.status_code in self._RETRYABLE_STATUS:
                # v2: 不重试 5xx, 立即抛带诊断的 RuntimeError (用户偏好)
                _d = _diag.diagnose(response)
                raise RuntimeError(_diag.build_error_message(
                    _d, model=model, endpoint=resolved_endpoint
                ))

            if response.status_code != 200:
                # v2: 4xx 也走全量诊断, 让用户看到 server / cf-ray / request-id
                _d = _diag.diagnose(response)
                raise RuntimeError(_diag.build_error_message(
                    _d, model=model, endpoint=resolved_endpoint
                ))

            try:
                data = response.json()
            except ValueError as e:
                raise RuntimeError(f"[GPT Image 2] 接口返回不是有效 JSON: {e}; body={response.text[:300]}")
            break

        if data is None:
            raise RuntimeError("[GPT Image 2] 请求失败，未能获取到有效数据。")
        pbar.update(1) # 请求完成，开始处理图像

        # 解析返回的图像数据：兼容 Images API(data[].b64_json/url) 与 Responses API(output[].result)。
        result_images = []
        original_sizes = []
        final_sizes = []
        image_values = self.extract_image_values(data)
        for image_value in image_values:
            image_value = str(image_value)
            if image_value.startswith(('http://', 'https://')):
                print(f'[GPT Image 2] Downloading image from URL: {image_value}')
                # === 关键修复：原版这里硬编码 timeout=60 / infinite_timeout=False ===
                # 官网反代类渠道(如 RS/MuleRouter)只返回图片 URL，4K 大图从中转站
                # 下载常 >60s → 被节点自己掐断 → 图其实已生成【已扣费】、本地却没拿到。
                # 改为复用用户配置的 timeout / infinite_timeout，并至少给 300s 兜底；
                # 主线程轮询仍可随时中断，不会卡死。
                try:
                    img_resp = self._interruptible_request(
                        "GET",
                        image_value,
                        timeout=max(300.0, float(timeout)),
                        infinite_timeout=infinite_timeout,
                    )
                    img_resp.raise_for_status()
                    img = Image.open(io.BytesIO(img_resp.content)).convert('RGB')
                except comfy.model_management.InterruptProcessingException:
                    raise
                except Exception as e:
                    raise RuntimeError(
                        f"[GPT Image 2] 图片下载失败: {type(e).__name__}: {str(e)[:160]}。\n"
                        f"URL: {image_value[:150]}\n"
                        f"⚠️ 此类【只返回 URL】的渠道(官网反代)图片已在中转站服务器生成，很可能【已扣费】。\n"
                        f"可执行的下一步: 1) 直接用上面的 URL 到渠道后台/浏览器手动下载(通常保存数十天);\n"
                        f"                  2) 增大 timeout 或保持 infinite_timeout=True 后重试下载;\n"
                        f"                  3) 若渠道支持，改用 b64_json 返回(response_format=b64_json)可免二次下载。"
                    )
            else:
                img = self.decode_image_to_pil(image_value)

            original_sizes.append(f'{img.width}x{img.height}')
            # 不再因为返回分辨率与请求 size 不一致而自动缩放；保留接口真实输出尺寸。
            final_sizes.append(f'{img.width}x{img.height}')
            result_images.append(self.pil_to_tensor(img))

        if not result_images:
            raise ValueError("[GPT Image 2] 接口未返回任何图像 (No images returned). 数据: " + str(data))

        # 将所有的图像合并成一个 Batch
        out_tensor = torch.cat(result_images, dim=0)

        # 估算消耗 Token 和费用（仅为估算展示，具体以官方账单为准）
        cost_estimate = "Unknown"
        tokens = data.get("usage", {}).get("total_tokens", 0)
        if tokens > 0:
            # 假定大约每 1000 token 消耗 0.02 美元，这里按需替换为实际费率
            cost = (tokens / 1000.0) * 0.02
            cost_estimate = f"${cost:.4f}"

        # 构造生成信息 JSON
        info = {
            "parameters": {
                "model": model,
                "size": size_str,
                "quality": quality,
                "n": n,
                "seed": data.get("seed", seed),
                "edit_mode": edit_mode,
                "api_endpoint": resolved_endpoint
            },
            "original_output_sizes": original_sizes,
            "final_output_sizes": final_sizes,
            "tokens_used": tokens,
            "estimated_cost": cost_estimate
        }
        
        pbar.update(1) # 全部完成

        return (out_tensor, json.dumps(info, indent=2, ensure_ascii=False))

    @staticmethod
    def _ensure_not_interrupted():
        '''复用 ComfyUI 原生取消机制，让用户点取消/停止时尽快退出。'''
        comfy.model_management.throw_exception_if_processing_interrupted()

    def _get_session(self):
        session = getattr(self._thread_local, 'session', None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
            session.mount('http://', adapter)
            session.mount('https://', adapter)
            setattr(self._thread_local, 'session', session)
        return session

    def _reset_session(self):
        session = getattr(self._thread_local, 'session', None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        setattr(self._thread_local, 'session', None)

    def _resolve_request_timeout(self, timeout, infinite_timeout=False):
        '''返回 requests 使用的 (connect_timeout, read_timeout)。
        - infinite_timeout=True 时 read_timeout=None，让底层 socket 永远等待返回，
          避免反向代理/网关默认 300s 读超时把生成中的连接砍掉（502/连接断开的根源）。
        - 业务总超时由主线程每 0.25s 轮询保护，不依赖底层 read_timeout，因此不会卡死。
        '''
        connect_timeout = self._CONNECT_TIMEOUT
        if infinite_timeout:
            read_timeout = None
        else:
            read_timeout = max(60.0, float(timeout or 600))
        return (connect_timeout, read_timeout)

    def _interruptible_request(self, method, url, *, timeout, infinite_timeout=False, **kwargs):
        '''后台线程执行 requests，主线程每 0.25s 检查 ComfyUI 中断和总超时。'''
        session = self._get_session()
        done_event = threading.Event()
        resp_holder = {}
        exc_holder = {}
        request_timeout = self._resolve_request_timeout(timeout, infinite_timeout)

        def _do_request():
            try:
                resp_holder['resp'] = session.request(method, url, timeout=request_timeout, **kwargs)
            except BaseException as exc:
                exc_holder['exc'] = exc
            finally:
                done_event.set()

        thread = threading.Thread(target=_do_request, daemon=True)
        thread.start()

        start_time = time.time()
        timed_out_or_interrupted = False
        try:
            while not done_event.wait(self._REQUEST_POLL_INTERVAL):
                self._ensure_not_interrupted()
                if not infinite_timeout and timeout and time.time() - start_time > float(timeout):
                    timed_out_or_interrupted = True
                    self._reset_session()
                    raise TimeoutError(f'请求总超时：超过 {timeout} 秒仍未返回，已终止等待')
            self._ensure_not_interrupted()
        except BaseException:
            timed_out_or_interrupted = True
            # 仅在主动中断/超时时强制关闭 session 以打断阻塞，正常完成时保留连接复用
            if not done_event.is_set():
                self._reset_session()
            raise

        if 'exc' in exc_holder:
            # 网络层异常：保留 session 不重置，由上层根据异常类型决定是否重试
            raise exc_holder['exc']
        response = resp_holder.get('resp')
        if response is None:
            raise RuntimeError('请求线程结束但没有返回响应')
        return response

    def build_api_url(self, base_url, endpoint='/v1/images/generations'):
        '''根据用户填写的根地址和选择的接口，拼接 OpenAI-style API URL。'''
        url = str(base_url or '').strip().rstrip('/')
        if not url:
            raise ValueError('base_url 不能为空，请填写例如 http://38.145.218.40:12001')

        endpoint = str(endpoint or '/v1/images/generations').strip()
        if not endpoint.startswith('/'):
            endpoint = '/' + endpoint

        known_endpoints = [
            '/v1/images/generations',
            '/v1/images/edits',
            '/v1/responses'
        ]

        # 如果用户 base_url 已经填了完整接口，先还原为服务根地址，再按当前选择接口拼接。
        for known in known_endpoints:
            if re.search(re.escape(known) + r'/?$', url):
                root = re.sub(re.escape(known) + r'/?$', '', url).rstrip('/')
                return root + endpoint

        if re.search(r'/v1/images/?$', url):
            if endpoint.startswith('/v1/images/'):
                return url.rstrip('/') + endpoint[len('/v1/images'):]
            root = re.sub(r'/v1/images/?$', '', url).rstrip('/')
            return root + endpoint

        if re.search(r'/v1/?$', url):
            return url.rstrip('/') + endpoint[len('/v1'):] if endpoint.startswith('/v1/') else url.rstrip('/') + endpoint

        return url + endpoint

    @staticmethod
    def _gcd(a, b):
        a, b = abs(int(a)), abs(int(b))
        while b:
            a, b = b, a % b
        return max(a, 1)

    def _infer_ratio_from_image(self, image_tensor):
        """从 ComfyUI IMAGE tensor (B, H, W, C) 推断最接近的标准比例.

        返回 (ratio_str, w, h). 若无法识别返回 1:1 + 原始尺寸.
        """
        if image_tensor is None or image_tensor.dim() < 3:
            return "1:1", 0, 0
        # ComfyUI 规范: (B, H, W, C). 取第一张
        t = image_tensor[0] if image_tensor.dim() == 4 else image_tensor
        h, w = int(t.shape[0]), int(t.shape[1])
        g = self._gcd(w, h)
        rw, rh = w // g, h // g
        # 已知比例表 (含 (1, 1) (3, 2) (2, 3) (4, 3) (3, 4) (5, 4) (4, 5) (16, 9) (9, 16) (2, 1) (1, 2) (21, 9) (9, 21))
        known = {
            (1, 1): "1:1",
            (3, 2): "3:2", (2, 3): "2:3",
            (4, 3): "4:3", (3, 4): "3:4",
            (16, 9): "16:9", (9, 16): "9:16",
            (21, 9): "21:9",
            (1, 3): "1:3", (3, 1): "3:1",
        }
        if (rw, rh) in known:
            return known[(rw, rh)], w, h
        # 找不到精确匹配, 取最接近的 (按 ratio 差值)
        actual_ratio = w / h
        best = "1:1"
        best_diff = 1.0
        for (kw, kh), label in known.items():
            diff = abs(actual_ratio - kw / kh)
            if diff < best_diff:
                best_diff = diff
                best = label
        return best, w, h

    def calculate_size(self, resolution, aspect_ratio, image_1=None):
        """
        按权威尺寸表 (GPT_IMAGE_SIZE_TABLE) 解析尺寸。
        只返回 relay 线上实测【不会 502】的固定尺寸；非法的「比例 × 分辨率」组合
        直接在发请求前报错（这就是互斥），避免浪费一次可能 5-15 分钟又 502 的请求。

        auto 规则:
          - (auto, auto)             → "auto"（由上游自选）
          - (auto 分辨率, 具体比例)  → 报错（必须两者都 auto）
          - (具体分辨率, auto 比例)  → 需要 image_1，按其比例查表；
                                       若该比例没有此分辨率档，自动回退到最近的合法档（不打断出图）
          - (具体, 具体)             → 查表；非法组合直接报错（互斥）
        """
        res_norm = (resolution or "2K").strip().lower()
        ratio_norm = (aspect_ratio or "1:1").strip().lower()

        # auto 分辨率 必须配 auto 比例
        if res_norm == "auto" and ratio_norm != "auto":
            raise ValueError(
                "resolution=auto 必须配 aspect_ratio=auto。"
                f"当前 resolution={resolution!r} aspect_ratio={aspect_ratio!r}。"
                "要么两个都 auto, 要么都用具体值。"
            )

        # 两者都 auto → 交给上游
        if ratio_norm == "auto" and res_norm == "auto":
            return "auto"

        # auto 比例：需要参考图来推断
        auto_ratio = False
        if ratio_norm == "auto":
            if image_1 is None:
                raise ValueError(
                    "aspect_ratio=auto 但没连接 image_1。"
                    "auto 比例需要至少 1 张参考图来推断比例, 或把 aspect_ratio 改成具体值 (如 1:1)。"
                )
            ratio_norm, w, h = self._infer_ratio_from_image(image_1)
            auto_ratio = True
            print(f"[GPT Image 2] aspect_ratio=auto 推断自 image_1 ({w}x{h}) → {ratio_norm}")

        # 查表
        row = self.GPT_IMAGE_SIZE_TABLE.get(ratio_norm)
        if not row:
            valid = ", ".join(self.GPT_IMAGE_SIZE_TABLE.keys())
            raise ValueError(
                f"不支持的比例 {ratio_norm!r}。GPT-Image-2 仅支持: {valid}。"
            )

        if res_norm in row:
            return row[res_norm]

        # === 组合非法：该比例没有这个分辨率档（互斥触发）===
        avail = self._resolutions_for(ratio_norm)
        avail_disp = "/".join(r.upper() for r in avail)

        if auto_ratio:
            # 自动比例场景：不打断出图，优雅回退到该比例的最高可用档
            chosen = avail[-1]
            print(
                f"[GPT Image 2] 比例 {ratio_norm} 无 {res_norm.upper()} 档, "
                f"自动回退到 {chosen.upper()} ({row[chosen]})。该比例可用档: {avail_disp}"
            )
            return row[chosen]

        # 显式选择的非法组合 → 直接报错（互斥）
        raise ValueError(
            f"【互斥】比例 {ratio_norm} 不支持 {res_norm.upper()} 分辨率。"
            f"该比例仅支持: {avail_disp}。"
            f"（如 1:1 只有 1K/2K 没有 4K；16:9/9:16/4:3/3:4/21:9 只有 2K/4K 没有 1K；3:1 只有 4K。）"
            f" 请改选合法档位后重试。"
        )

    def normalize_size(self, size):
        """
        校验并规范化 gpt-image-2 的 size 参数。

        gpt-image-2 支持 auto 或任意满足以下条件的 宽x高：
        - 最大边长 <= 3840px
        - 两个边长都必须是 16px 的倍数
        - 长边与短边之比 <= 3:1
        - 总像素数在 655,360 到 8,294,400 之间
        """
        value = str(size or "auto").strip().lower().replace("×", "x")
        if value.startswith("auto"):
            return "auto"

        match = re.search(r"(\d+)\s*x\s*(\d+)", value)
        if not match:
            raise ValueError("size 格式错误：请输入 auto 或 宽x高，例如 1536x864。")

        width = int(match.group(1))
        height = int(match.group(2))
        if width <= 0 or height <= 0:
            raise ValueError("size 格式错误：宽高必须为正整数。")

        long_edge = max(width, height)
        short_edge = min(width, height)
        total_pixels = width * height

        if long_edge > self.GPT_IMAGE_2_MAX_EDGE:
            raise ValueError("gpt-image-2 size 无效：最大边长必须小于或等于 3840px。")
        if width % 16 != 0 or height % 16 != 0:
            raise ValueError("gpt-image-2 size 无效：两个边长都必须是 16px 的倍数。")
        if long_edge / short_edge > self.GPT_IMAGE_2_MAX_RATIO:
            raise ValueError("gpt-image-2 size 无效：长边与短边之比不得超过 3:1。")
        if total_pixels < self.GPT_IMAGE_2_MIN_PIXELS or total_pixels > self.GPT_IMAGE_2_MAX_PIXELS:
            raise ValueError("gpt-image-2 size 无效：总像素数必须至少为 655,360 且不超过 8,294,400。")

        return f"{width}x{height}"

    def extract_image_values(self, data):
        '''从不同 OpenAI-style 返回结构中提取图片 URL 或 Base64。

        兼容三种情况：
          1) 标准 Images API: data[].b64_json / data[].url
          2) Responses API: output[].result (b64) 或 image_url
          3) 部分中转/relay 把图片"伪装"成聊天文本返回，字段名不固定(content/
             text/message/...)，内容里夹带 Markdown 图片语法
             ![alt](https://img-bed.example.com/xxx.png) 或一个裸图片 URL——
             这类字段不在已知 key 列表里，也不是"纯 URL/base64"，原逻辑会漏解析。
             第 3 类走兜底：扫描**所有**字符串字段，用正则抠出 Markdown 图床
             链接 / 裸图片 URL，交给后续统一的"下载或 base64 解码"流程处理。
        '''
        images = []

        def add(item):
            if isinstance(item, str) and item:
                images.append(item)

        def walk(value):
            if isinstance(value, dict):
                for key in ['b64_json', 'url', 'image_url', 'result']:
                    item = value.get(key)
                    if isinstance(item, str) and (
                        item.startswith('data:image') or
                        item.startswith('http://') or
                        item.startswith('https://') or
                        len(item) > 200
                    ):
                        add(item)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            elif isinstance(value, str):
                # 兜底：任意字符串字段里夹带的 Markdown 图床链接 / 裸图片 URL。
                for m in self._MARKDOWN_IMAGE_RE.finditer(value):
                    add(m.group(1))
                for m in self._BARE_IMAGE_URL_RE.finditer(value):
                    add(m.group(0))

        walk(data)

        # 去重且保持顺序，避免同一 Responses 结果被递归重复采集。
        unique = []
        seen = set()
        for item in images:
            if item not in seen:
                unique.append(item)
                seen.add(item)
        return unique

    def decode_image_to_pil(self, image_data):
        """兼容 b64_json 纯 Base64、data:image/...;base64 和缺少 padding 的返回值。"""
        if image_data is None:
            raise ValueError("空的图片 Base64 数据。")

        b64_data = str(image_data).strip()
        if b64_data.startswith("data:") and "," in b64_data:
            b64_data = b64_data.split(",", 1)[1]

        b64_data = re.sub(r"\s+", "", b64_data)
        padding = (-len(b64_data)) % 4
        if padding:
            b64_data += "=" * padding

        try:
            img_data = base64.b64decode(b64_data, validate=False)
            img = Image.open(io.BytesIO(img_data)).convert("RGB")
        except Exception as e:
            raise ValueError(f"返回图片 Base64 解码失败：{e}")

        return img

    def decode_image_to_tensor(self, image_data):
        """兼容旧调用：将返回图片数据解码为 ComfyUI Tensor。"""
        return self.pil_to_tensor(self.decode_image_to_pil(image_data))

    def ensure_output_size(self, img, size_str):
        '''保留旧方法名用于兼容；现在不再自动缩放 API 返回图片。'''
        return img

    def tensor_to_base64_list(self, tensor):
        """将 ComfyUI 的 Image Tensor Batch 转换为 Base64 字符串列表"""
        b64_list = []
        for i in range(tensor.shape[0]):
            image_np = (tensor[i].numpy() * 255).clip(0, 255).astype(np.uint8)
            img = Image.fromarray(image_np)
            buffered = io.BytesIO()
            img.save(buffered, format="PNG")
            b64_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
            b64_list.append(f"data:image/png;base64,{b64_str}")
        return b64_list

    def pil_to_tensor(self, img):
        """将 PIL 图像转换为 ComfyUI 标准 Tensor"""
        img_np = np.array(img).astype(np.float32) / 255.0
        tensor = torch.from_numpy(img_np).unsqueeze(0)
        return tensor