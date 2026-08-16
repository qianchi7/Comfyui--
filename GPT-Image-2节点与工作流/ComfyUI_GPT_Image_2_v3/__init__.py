from .gpt_image_2_node import GPTImage2Generator

# 导出节点类，使其在 ComfyUI 中可用
NODE_CLASS_MAPPINGS = {
    "GPTImage2Generator": GPTImage2Generator
}

# 导出节点显示名称
NODE_DISPLAY_NAME_MAPPINGS = {
    "GPTImage2Generator": "GPT Image 2 Generator"
}

# 前端 JS 扩展目录：实现「分辨率/比例」互斥下拉（选 1:1 就选不出 4K 等）
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
