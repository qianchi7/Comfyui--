from __future__ import annotations

import json
import math
import torch
from typing import List, Dict, Optional, Tuple, Any
import locale
import platform
import re
import random
import shutil
import subprocess
import time
import threading
import os
import sys
from datetime import datetime
from urllib.parse import urlparse

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

# 在非 ComfyUI 运行环境中,server 可能无法正常导入
# 这里做一个兼容处理:导入失败时提供一个占位 PromptServer,
# 仅用于避免测试脚本导入本模块时报错
try:
    from server import PromptServer
except ImportError:
    class _DummyPromptServer:
        instance = None
    PromptServer = _DummyPromptServer()

import comfy.utils
import comfy.model_management

from logger import logger
from config_manager import ConfigManager
from endpoint_diagnostics import EndpointDiagnostics
from image_codec import ImageCodec, ErrorCanvas
from api_client import GeminiApiClient
from task_runner import BatchGenerationRunner


CONFIG_MANAGER = ConfigManager(MODULE_DIR)
API_CLIENT = GeminiApiClient(
    CONFIG_MANAGER,
    logger,
    interrupt_checker=comfy.model_management.throw_exception_if_processing_interrupted,
)

class BananaImageNode:
    """
    ComfyUI节点: NanoBanana图像生成，适配Gemini兼容端点
    支持从config.ini读取API Key
    """

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "text")
    FUNCTION = "generate_images"
    OUTPUT_NODE = True
    CATEGORY = "image/ai_generation"
    _PING_COUNT_MIN = EndpointDiagnostics.PING_COUNT_MIN
    _PING_COUNT_MAX = EndpointDiagnostics.PING_COUNT_MAX
    _MAX_REFERENCE_IMAGES = 14
    _MIN_READ_TIMEOUT_SECONDS = 60
    _SUPPORTED_ASPECT_RATIOS = (
        "1:1", "1:4", "1:8", "4:1", "8:1", "9:16", "16:9", "21:9",
        "2:3", "3:2", "3:4", "4:3", "4:5", "5:4",
    )

    def __init__(self):
        self.config_manager = CONFIG_MANAGER
        self.endpoint_diagnostics = EndpointDiagnostics(logger, self._ensure_not_interrupted)
        self.image_codec = ImageCodec(logger, self._ensure_not_interrupted)
        self.error_canvas = ErrorCanvas(logger)
        self.task_runner = BatchGenerationRunner(
            logger,
            self._ensure_not_interrupted,
            lambda total: comfy.utils.ProgressBar(total),
        )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {
                    "multiline": True,
                    "default": "Peace and love",
                    "tooltip": "生成图像的文本提示词，可多行描述内容、风格等"
                }),
                "api_key": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "调用服务的 API Key；留空则优先使用 config.ini 中的配置"
                }),
                "api_base_url": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "API 服务地址；留空则使用 config.ini 中的配置"
                }),
                "model_type": ("STRING", {
                    "default": "「Rim」gemini-3-pro-image-preview",
                    "multiline": False,
                    "tooltip": "模型名称）"
                }),
                "batch_size": ("INT", {
                    "default": 1,
                    "min": 1,
                    "max": 8,
                    "tooltip": "一次请求中要生成的图片数量，范围 1~8"
                }),
                "aspect_ratio": (["Auto", "1:1", "1:4", "1:8", "4:1", "8:1", "9:16", "16:9", "21:9", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4"], {
                    "default": "Auto",
                    "tooltip": "生成图像的宽高比例，Auto 为由服务端自动决定"
                }),
            },
            "optional": {
                "seed": ("INT", {
                    "default": -1,
                    "min": -1,
                    "max": 102400,
                    "control_after_generate": True,
                    "tooltip": "随机种子，-1 为自动随机；固定种子可复现同一输出"
                }),
                "top_p": ("FLOAT", {
                    "default": 0.95,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.01,
                    "tooltip": "采样参数 Top-P，数值越低越保守，越高多样性越强"
                }),
                "imageSize": (["无", "1K", "2K", "4K"], {
                    "default": "2K",
                    "tooltip": "图像分辨率选项：1K/2K/4K，适用于所有支持图像生成的模型"
                }),
                "image_1": ("IMAGE", {
                    "tooltip": "参考图像 1，可为空；用于图生图或多图融合"
                }),
                "image_2": ("IMAGE", {
                    "tooltip": "参考图像 2，可为空；用于图生图或多图融合"
                }),
                "image_3": ("IMAGE", {
                    "tooltip": "参考图像 3，可为空；用于图生图或多图融合"
                }),
                "image_4": ("IMAGE", {
                    "tooltip": "参考图像 4，可为空；用于图生图或多图融合"
                }),
                "image_5": ("IMAGE", {
                    "tooltip": "参考图像 5，可为空；用于图生图或多图融合"
                }),
                "image_6": ("IMAGE", {
                    "tooltip": "参考图像 6，可为空；用于图生图或多图融合"
                }),
                "image_7": ("IMAGE", {
                    "tooltip": "参考图像 7，可为空；用于图生图或多图融合"
                }),
                "image_8": ("IMAGE", {
                    "tooltip": "参考图像 8，可为空；用于图生图或多图融合"
                }),
                "image_9": ("IMAGE", {
                    "tooltip": "参考图像 9，可为空；用于图生图或多图融合"
                }),
                "image_10": ("IMAGE", {
                    "tooltip": "参考图像 10，可为空；用于图生图或多图融合"
                }),
                "image_11": ("IMAGE", {
                    "tooltip": "参考图像 11，可为空；用于图生图或多图融合"
                }),
                "image_12": ("IMAGE", {
                    "tooltip": "参考图像 12，可为空；用于图生图或多图融合"
                }),
                "image_13": ("IMAGE", {
                    "tooltip": "参考图像 13，可为空；用于图生图或多图融合"
                }),
                "image_14": ("IMAGE", {
                    "tooltip": "参考图像 14，可为空；用于图生图或多图融合"
                }),
                "超时秒数": ("INT", {
                    "default": cls._MIN_READ_TIMEOUT_SECONDS,
                    "min": cls._MIN_READ_TIMEOUT_SECONDS,
                    "max": 1800,
                    "step": 10,
                    "tooltip": f"API 请求的读取超时时间（秒），最低 {cls._MIN_READ_TIMEOUT_SECONDS} 秒；开启“无限超时”时此值会被忽略"
                }),
                "无限超时": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "开启后读取超时无限制；关闭后使用上面的“超时秒数”"
                }),
                "绕过代理": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "梯子速度不佳、不可靠时开启"
                }),
                "测试延迟": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "对当前 API Base URL 执行 ping 测试，输出延迟和丢包率，便于挑选更稳的端点"
                }),
                "ping次数": ("INT", {
                    "default": 4,
                    "min": cls._PING_COUNT_MIN,
                    "max": cls._PING_COUNT_MAX,
                    "step": 1,
                    "tooltip": "ping 探测次数，次数越多结果越稳定，但会增加等待时间"
                }),
            }
        }
    
    @staticmethod
    def _ensure_not_interrupted():
        """统一的中断检查，复用 ComfyUI 原生取消机制"""
        comfy.model_management.throw_exception_if_processing_interrupted()

    def _build_failure_result(self, index: int, seed: int, error_msg: str) -> Dict[str, Any]:
        """构造统一的失败返回结构，便于上层聚合处理"""
        return {
            "index": index,
            "success": False,
            "error": error_msg,
            "seed": seed,
            "tensor": None,
            "image_count": 0,
        }

    @classmethod
    def _ratio_string_to_float(cls, ratio_text: str) -> Optional[float]:
        try:
            width_text, height_text = ratio_text.split(":", 1)
            width = float(width_text)
            height = float(height_text)
            if width <= 0 or height <= 0:
                return None
            return width / height
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_first_reference_dimensions(tensor: Any) -> Optional[Tuple[int, int]]:
        if tensor is None or not hasattr(tensor, "shape"):
            return None

        shape = tuple(int(dim) for dim in tensor.shape)
        if len(shape) == 4:
            _, height, width, _ = shape
            return (width, height)
        if len(shape) == 3:
            height, width, _ = shape
            return (width, height)
        return None

    def _resolve_aspect_ratio_from_reference_images(
        self,
        requested_aspect_ratio: str,
        input_tensors: List[Any],
    ) -> str:
        if requested_aspect_ratio != "Auto" or not input_tensors:
            return requested_aspect_ratio

        dimensions = self._extract_first_reference_dimensions(input_tensors[0])
        if dimensions is None:
            return requested_aspect_ratio

        width, height = dimensions
        if width <= 0 or height <= 0:
            return requested_aspect_ratio

        actual_ratio = width / height
        closest_ratio = requested_aspect_ratio
        smallest_distance = float("inf")

        for candidate in self._SUPPORTED_ASPECT_RATIOS:
            candidate_value = self._ratio_string_to_float(candidate)
            if candidate_value is None:
                continue
            distance = abs(math.log(actual_ratio) - math.log(candidate_value))
            if distance < smallest_distance:
                smallest_distance = distance
                closest_ratio = candidate

        return closest_ratio

    def generate_single_image(self, args):
        """生成单张图片（用于并发）"""
        (
            i,
            current_seed,
            api_key,
            prompt,
            model_type,
            aspect_ratio,
            image_size,
            top_p,
            input_images_b64,
            timeout,
            stagger_delay,
            decode_workers,
            bypass_proxy,
            peak_mode,
            request_start_event,
            request_start_time_holder,
            request_start_lock,
            effective_base_url,
            verify_ssl,
        ) = args

        self._ensure_not_interrupted()
        if stagger_delay > 0:
            delay = i * stagger_delay
            if delay > 0:
                time.sleep(delay)

        thread_id = threading.current_thread().name
        logger.info(f"批次 {i+1} 开始请求...")

        try:
            self._ensure_not_interrupted()
            request_data = API_CLIENT.create_request_data(
                prompt=prompt,
                seed=current_seed,
                aspect_ratio=aspect_ratio,
                top_p=top_p,
                input_images_b64=input_images_b64,
                model_type=model_type,
                image_size=image_size,
            )
            self._ensure_not_interrupted()
            if not request_start_event.is_set():
                with request_start_lock:
                    if not request_start_event.is_set():
                        request_start_time_holder[0] = time.time()
                        request_start_event.set()
            response_data = API_CLIENT.send_request(
                api_key,
                request_data,
                model_type,
                effective_base_url,
                timeout,
                bypass_proxy=bypass_proxy,
                verify_ssl=verify_ssl,
                max_retries=1 if peak_mode else None,
            )
            self._ensure_not_interrupted()
            base64_images, text_content = API_CLIENT.extract_content(response_data)
            decoded_tensor = None
            decoded_count = 0
            if base64_images:
                self._ensure_not_interrupted()
                decoded_tensor = self.image_codec.base64_to_tensor_parallel(
                    base64_images,
                    log_prefix=f"[{thread_id}] 批次 {i+1}",
                    max_workers=decode_workers
                )
                decoded_count = decoded_tensor.shape[0]

            # 更明显地区分“有图返回”和“未返回任何图片”的情况
            if decoded_count > 0:
                logger.success(f"批次 {i+1} 完成 - 生成 {decoded_count} 张图片")
            else:
                # 简化日志输出,尽可能给出用户能理解的原因说明
                reason = ""
                # 1. 检查 finishReason 信息
                try:
                    if isinstance(response_data, dict):
                        candidates = response_data.get("candidates") or []
                        if candidates and isinstance(candidates[0], dict):
                            finish_reason = candidates[0].get("finishReason") or ""
                            if finish_reason:
                                if finish_reason == "NO_IMAGE":
                                    reason = "模型未生成任何图片（finishReason=NO_IMAGE，一般表示当前提示或参考图不触发图像输出，可能是内容被过滤或未通过安全审查）"
                                else:
                                    reason = f"模型未生成图片（finishReason={finish_reason}）"
                except Exception:
                    # 如果解析 finishReason 失败,忽略即可
                    pass

                # 2. 如果有文本内容,补充展示一小段
                brief_text = (text_content or "").strip().replace("\n", " ")
                if brief_text:
                    if reason:
                        reason = f"{reason}；模型返回文本: {brief_text[:100]}"
                    else:
                        reason = f"模型仅返回文本: {brief_text[:100]}"

                # 3. 都没有就给一个通用说明
                if not reason:
                    reason = "模型未给出图片或说明文本，可能是服务端策略或参数设置导致本次未产出图片"

                logger.warning(f"批次 {i+1} 完成，但未返回任何图片。{reason}")

            return {
                'index': i,
                'success': True,
                'images': base64_images,
                'tensor': decoded_tensor,
                'image_count': decoded_count,
                'text': text_content,
                'seed': current_seed
            }
        except comfy.model_management.InterruptProcessingException:
            logger.warning(f"批次 {i+1} 已取消")
            raise
        except Exception as e:
            error_msg = str(e)[:200]
            logger.error(f"批次 {i+1} 失败")
            logger.error(f"错误: {error_msg}")
            return self._build_failure_result(i, current_seed, error_msg)

    def generate_images(self, prompt, api_key="", api_base_url="", model_type="gemini-2.0-flash-exp",
                       batch_size=1, aspect_ratio="Auto", imageSize="2K", seed=-1, top_p=0.95, max_workers=None,
                       image_1=None, image_2=None, image_3=None,
                       image_4=None, image_5=None, image_6=None, image_7=None,
                       image_8=None, image_9=None, image_10=None, image_11=None, image_12=None,
                       image_13=None, image_14=None, 超时秒数=60, 无限超时=True, 绕过代理=None, 测试延迟=False,
                       ping次数=4, 高峰模式=False, 禁用SSL验证=False):

        # 解析 API Key：优先使用节点输入，留空时回退 config
        raw_input_key = (api_key or "").strip()

        # 解析 API Base URL：优先使用节点输入，留空时回退 config
        raw_input_url = (api_base_url or "").strip()
        if raw_input_url:
            effective_base_url = raw_input_url
        else:
            effective_base_url = self.config_manager.get_effective_api_base_url()

        sanitized_input_key = self.config_manager.sanitize_api_key(api_key)
        resolved_api_key: Optional[str] = sanitized_input_key or self.config_manager.sanitize_api_key(
            self.config_manager.load_api_key()
        )

        endpoint_diagnostics = ""
        if 测试延迟:
            endpoint_diagnostics = self.endpoint_diagnostics.run_base_url_ping_diagnostics(
                effective_base_url,
                ping次数,
            )
            logger.info(endpoint_diagnostics)

        # 验证API key
        if not resolved_api_key:
            error_msg = "请在 config.ini 中配置 API Key 或在节点中填写"
            logger.error(error_msg)
            final_error_text = error_msg
            if endpoint_diagnostics:
                final_error_text = f"{final_error_text}\n{endpoint_diagnostics}"
            error_tensor = self.error_canvas.build_error_tensor_from_text(
                "配置缺失",
                f"{final_error_text}\n请在 config.ini 或节点输入中填写有效 API Key"
            )
            return (error_tensor, final_error_text)

        # 输出实际使用的配置信息（用于调试）
        masked_key = resolved_api_key[:8] + "..." + resolved_api_key[-4:] if len(resolved_api_key) > 12 else "***"
        logger.info(f"使用 API Base URL: {effective_base_url}")
        logger.info(f"使用 API Key: {masked_key}")
        logger.info(f"使用模型: {model_type}")

        # 绕过代理完全由节点开关控制，不再读取 config.ini
        bypass_proxy_flag = bool(绕过代理)
        disable_ssl_flag = bool(禁用SSL验证)
        verify_ssl_flag = not disable_ssl_flag
        if disable_ssl_flag:
            logger.warning("已禁用 SSL 证书验证，请确保你信任当前网络环境，以免密钥被中间人窃取")

        start_time = time.time()
        raw_input_images = [
            image_1, image_2, image_3, image_4, image_5, image_6, image_7,
            image_8, image_9, image_10, image_11, image_12, image_13, image_14
        ]
        input_tensors = [img for img in raw_input_images if img is not None]
        requested_aspect_ratio = aspect_ratio
        effective_aspect_ratio = self._resolve_aspect_ratio_from_reference_images(
            requested_aspect_ratio,
            input_tensors,
        )
        encoded_input_images = self.image_codec.prepare_input_images(input_tensors)

        # 固定配置
        concurrent_mode = True   # 总是开启并发
        # 为网络请求增加轻微交错延迟,减少瞬时请求尖峰
        stagger_delay = 0.2      # 每个批次相对前一个延迟 0.2 秒
        # 超时设置：连接超时固定 15s，读取超时由用户通过"超时秒数"参数控制
        connect_timeout = 15
        # “无限超时”优先；关闭后使用超时秒数，并对旧工作流里可能残留的 <=0 值做兼容
        requested_read_timeout = int(超时秒数) if 超时秒数 is not None else self._MIN_READ_TIMEOUT_SECONDS
        if 无限超时 or requested_read_timeout <= 0:
            read_timeout = None
        else:
            read_timeout = max(self._MIN_READ_TIMEOUT_SECONDS, requested_read_timeout)
        request_timeout = (connect_timeout, read_timeout)
        read_timeout_label = "无限制" if read_timeout is None else f"{read_timeout}s"
        logger.info(f"请求超时设置: 连接 {connect_timeout}s, 读取 {read_timeout_label}")
        peak_mode = bool(高峰模式)
        continue_on_error = True  # 总是容错
        configured_workers = self.config_manager.load_max_workers()
        decode_workers = max(1, configured_workers)
        request_start_event = threading.Event()
        request_start_time_holder: List[Optional[float]] = [None]
        request_start_lock = threading.Lock()

        if seed == -1:
            base_seed = random.randint(0, 102400)
        else:
            base_seed = seed

        decoded_tensors: List[torch.Tensor] = []
        total_generated_images = 0
        all_texts: List[str] = []
        results: List[Dict[str, Any]] = []
        tasks: List[Tuple[Any, ...]] = []

        for i in range(batch_size):
            current_seed = base_seed + i if seed != -1 else -1
            tasks.append((
                i,
                current_seed,
                resolved_api_key,
                prompt,
                model_type,
                effective_aspect_ratio,
                imageSize,
                top_p,
                encoded_input_images,
                request_timeout,
                stagger_delay,
                decode_workers,
                bypass_proxy_flag,
                peak_mode,
                request_start_event,
                request_start_time_holder,
                request_start_lock,
                effective_base_url,
                verify_ssl_flag,
            ))

        # 显示任务开始信息
        logger.header("🎨 Gemini 图像生成任务")
        logger.info(f"批次数量: {batch_size} 张")
        if requested_aspect_ratio == "Auto" and effective_aspect_ratio != "Auto":
            logger.info(f"图片比例: Auto -> 跟随首张参考图，已解析为 {effective_aspect_ratio}")
        else:
            logger.info(f"图片比例: {effective_aspect_ratio}")
        if seed != -1:
            logger.info(f"随机种子: {seed}")
        if top_p != 0.95:
            logger.info(f"Top-P 参数: {top_p}")
        logger.separator()

        configured_network_cap = self.config_manager.load_network_workers_cap()
        network_workers_cap = min(configured_workers, configured_network_cap)
        actual_workers = min(network_workers_cap, batch_size) if concurrent_mode and batch_size > 1 else 1
        actual_workers = max(1, actual_workers)

        def progress_callback(result: Dict[str, Any], completed_count: int, total_count: int, progress_bar: object):
            if result.get('success'):
                logger.success(
                    f"[{completed_count}/{total_count}] 批次 {result['index']+1} 完成"
                )
            else:
                batch_label = result.get('index', -1)
                batch_text = "?" if batch_label < 0 else batch_label + 1
                logger.error(
                    f"[{completed_count}/{total_count}] 批次 {batch_text} 失败"
                )

            preview_tensor = result.get('tensor')
            if result.get('success') and preview_tensor is not None:
                preview_tuple = self.image_codec.build_preview_tuple(
                    preview_tensor, result['index']
                )
                if preview_tuple is not None:
                    progress_bar.update_absolute(completed_count, total_count, preview_tuple)
                else:
                    progress_bar.update(1)
            else:
                progress_bar.update(1)

        results = self.task_runner.run(
            tasks,
            self.generate_single_image,
            batch_size,
            actual_workers,
            continue_on_error,
            progress_callback,
        )
        request_start_time = request_start_time_holder[0] or start_time

        if not results:
            elapsed = time.time() - request_start_time
            error_text = f"未生成任何图像\n总耗时: {elapsed:.2f}s"
            if endpoint_diagnostics:
                error_text = f"{endpoint_diagnostics}\n{error_text}"
            logger.error(error_text)
            error_tensor = self.error_canvas.build_error_tensor_from_text("生成失败", error_text)
            return (error_tensor, error_text)

        results.sort(key=lambda x: x['index'])

        for result in results:
            if result.get('success'):
                tensor = result.get('tensor')
                if tensor is not None:
                    decoded_tensors.append(tensor)
                    total_generated_images += result.get('image_count', tensor.shape[0])
                if result.get('text'):
                    all_texts.append(f"[批次 {result['index']+1}] {result['text']}")
            else:
                error_msg = f"[批次 {result['index']+1}] ❌ {result.get('error', '未知错误')}"
                all_texts.append(error_msg)
                if not continue_on_error:
                    break

        total_time = time.time() - request_start_time

        if not decoded_tensors or total_generated_images == 0:
            error_text = f"未生成任何图像\n总耗时: {total_time:.2f}s\n\n" + "\n".join(all_texts)
            if endpoint_diagnostics:
                error_text = f"{endpoint_diagnostics}\n{error_text}"
            logger.error(error_text)
            error_tensor = self.error_canvas.build_error_tensor_from_text("生成失败", error_text)
            return (error_tensor, error_text)

        if len(decoded_tensors) == 1:
            image_tensor = decoded_tensors[0]
        else:
            image_tensor = torch.cat(decoded_tensors, dim=0)

        actual_count = total_generated_images
        if requested_aspect_ratio == "Auto" and effective_aspect_ratio != "Auto":
            ratio_text = f"自动(跟随参考图: {effective_aspect_ratio})"
        elif requested_aspect_ratio == "Auto":
            ratio_text = "自动"
        else:
            ratio_text = effective_aspect_ratio
        success_info = f"✅ 成功生成 {actual_count} 张图像（比例: {ratio_text}）"
        avg_time = total_time / actual_count if actual_count > 0 else 0
        time_info = f"总耗时: {total_time:.2f}s，平均 {avg_time:.2f}s/张"
        if actual_count != batch_size:
            time_info += f" ⚠️ 请求{batch_size}张，实际生成{actual_count}张"
            # 若实际生成数量少于请求数量，在日志中额外给出明显提示
            logger.warning(f"部分批次未返回图片：请求 {batch_size} 张，实际上只生成 {actual_count} 张，请查看上方各批次日志中的“未返回任何图片”提示")

        combined_text = f"{success_info}\n{time_info}"
        if endpoint_diagnostics:
            combined_text = f"{endpoint_diagnostics}\n{combined_text}"
        if all_texts:
            combined_text += "\n\n" + "\n".join(all_texts)

        # 显示完成统计
        logger.summary("任务完成", {
            "总批次": f"{batch_size} 个",
            "成功生成": f"{actual_count} 张",
            "总耗时": f"{total_time:.2f}s",
            "平均速度": f"{avg_time:.2f}s/张"
        })

        return (image_tensor, combined_text)

# 注册节点
NODE_CLASS_MAPPINGS = {"HeiHe001_BananaImageNode": BananaImageNode}
NODE_DISPLAY_NAME_MAPPINGS = {"HeiHe001_BananaImageNode": "Banana-API-3"}
