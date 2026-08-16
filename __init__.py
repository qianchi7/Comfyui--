"""
Comfyui-- 汇总入口（仓库名：SillyDream-ComfyUI-Cloud）
==============================================
本仓库收录多套云端模型节点/工作流，每套放在各自的子文件夹中。
每个「custom nodes」子文件夹本身都是自包含、可独立安装的 ComfyUI 节点包
（文件夹名即为建议的 custom_nodes 目录名，可以直接把该子文件夹复制/软链到
ComfyUI/custom_nodes/ 下单独使用，不依赖本文件）。

本文件的作用：如果你选择把整个仓库 clone 进 ComfyUI/custom_nodes/，
这里会用 importlib 动态加载各子文件夹里的节点包（因为子文件夹名可能包含
中文或短横线，不能直接写成 Python 的 import 语句），把它们的
NODE_CLASS_MAPPINGS / NODE_DISPLAY_NAME_MAPPINGS 汇总起来，使所有节点
都能被自动加载。
"""

import importlib.util
import os
import sys

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# 在这里登记每一套「custom nodes」子文件夹的相对路径。
# 只登记真正的节点代码目录，纯工作流/资源目录不用登记。
_NODE_PACKAGE_DIRS = [
    os.path.join(_THIS_DIR, "反推节点与工作流", "Comfyui-reverse_promp"),
    os.path.join(_THIS_DIR, "Banana节点与工作流", "comfyui-Banana-API-3.5"),
]


def _load_package_mappings(package_dir: str):
    init_path = os.path.join(package_dir, "__init__.py")
    if not os.path.isfile(init_path):
        return {}, {}

    module_name = "sillydream_comfyui_cloud_" + os.path.basename(package_dir)
    spec = importlib.util.spec_from_file_location(
        module_name, init_path, submodule_search_locations=[package_dir]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return (
        getattr(module, "NODE_CLASS_MAPPINGS", {}),
        getattr(module, "NODE_DISPLAY_NAME_MAPPINGS", {}),
    )


for _pkg_dir in _NODE_PACKAGE_DIRS:
    try:
        _classes, _display = _load_package_mappings(_pkg_dir)
        NODE_CLASS_MAPPINGS.update(_classes)
        NODE_DISPLAY_NAME_MAPPINGS.update(_display)
    except Exception as exc:  # noqa: BLE001
        print(f"[SillyDream-ComfyUI-Cloud] 加载节点包失败: {_pkg_dir} -> {exc}")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
