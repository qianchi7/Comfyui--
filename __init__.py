"""
Comfyui-- 汇总入口
==============================================
本仓库可能包含多套云端模型节点，每套放在各自的子文件夹中。
这里把各子文件夹的 NODE_CLASS_MAPPINGS / NODE_DISPLAY_NAME_MAPPINGS 汇总起来，
使得把整个仓库 clone 进 ComfyUI/custom_nodes/ 之后，所有节点都能被自动加载。
"""

from .反推节点与工作流.image_prompt_node import (
    NODE_CLASS_MAPPINGS as _reverse_prompt_class_mappings,
    NODE_DISPLAY_NAME_MAPPINGS as _reverse_prompt_display_mappings,
)

NODE_CLASS_MAPPINGS = {
    **_reverse_prompt_class_mappings,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    **_reverse_prompt_display_mappings,
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
