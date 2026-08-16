from __future__ import annotations

import os
import sys
import time

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

try:
    import comfy.model_management
except ImportError:
    class _DummyModelManagement:
        @staticmethod
        def throw_exception_if_processing_interrupted():
            return None

    class _DummyComfy:
        model_management = _DummyModelManagement()

    comfy = _DummyComfy()

from config_manager import ConfigManager
from endpoint_diagnostics import EndpointDiagnostics
from logger import logger


CONFIG_MANAGER = ConfigManager(MODULE_DIR)


class BananaEndpointTesterNode:
    """独立的 Base URL 端点测速节点，只执行 ping 诊断。"""

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "test_endpoint"
    OUTPUT_NODE = False
    CATEGORY = "Banana/tools"

    def __init__(self):
        self.config_manager = CONFIG_MANAGER
        self.endpoint_diagnostics = EndpointDiagnostics(logger, self._ensure_not_interrupted)

    @staticmethod
    def _ensure_not_interrupted():
        comfy.model_management.throw_exception_if_processing_interrupted()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_base_url": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "要测试的 API Base URL；留空则使用 config.ini 中的默认配置"
                }),
                "ping次数": ("INT", {
                    "default": 4,
                    "min": EndpointDiagnostics.PING_COUNT_MIN,
                    "max": EndpointDiagnostics.PING_COUNT_MAX,
                    "step": 1,
                    "tooltip": "ping 探测次数，次数越多结果越稳定，但会增加等待时间"
                }),
            }
        }

    def test_endpoint(self, api_base_url="", ping次数=4):
        raw_input_url = (api_base_url or "").strip()
        if raw_input_url:
            effective_base_url = raw_input_url
            base_url_source = "节点输入"
        else:
            effective_base_url = self.config_manager.get_effective_api_base_url()
            base_url_source = "config.ini"

        logger.header("🌐 Banana Endpoint Tester")
        logger.info(f"测速目标: {effective_base_url}")
        logger.info(f"地址来源: {base_url_source}")

        start_time = time.time()
        diagnostics_text = self.endpoint_diagnostics.run_base_url_ping_diagnostics(
            effective_base_url,
            ping次数,
        )
        elapsed = time.time() - start_time

        logger.info(diagnostics_text)
        logger.info(f"测速耗时: {elapsed:.2f}s")

        result_text = (
            f"Base URL: {effective_base_url}\n"
            f"地址来源: {base_url_source}\n"
            f"{diagnostics_text}\n"
            f"测速耗时: {elapsed:.2f}s"
        )
        return (result_text,)


NODE_CLASS_MAPPINGS = {"HeiHe001_BananaEndpointTesterNode": BananaEndpointTesterNode}
NODE_DISPLAY_NAME_MAPPINGS = {"HeiHe001_BananaEndpointTesterNode": "Banana Endpoint Tester"}
