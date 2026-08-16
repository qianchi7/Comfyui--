# ComfyUI_GPT_Image_2_v3

ComfyUI 自定义节点：GPT-Image-2 文生图/图生图，详细特性、参数说明和故障排除见上级目录 [`../README.md`](../README.md)。

可作为独立 custom_nodes 子目录直接安装：

```bash
cp -r ComfyUI_GPT_Image_2_v3 ComfyUI/custom_nodes/ComfyUI_GPT_Image_2_v3
pip install requests Pillow numpy
```

## 文件说明

- `gpt_image_2_node.py` — 主生成节点 `GPTImage2Generator`
- `endpoint_diagnostics.py` — 502/4xx 全量诊断（server / cf-ray / x-request-id / body），永不自动重试
- `config_manager.py` — 非敏感配置持久化（`base_url`、默认模型/分辨率等），**`api_key` 永不落盘**
- `web/gpt_image_2_size_lock.js` — 前端分辨率/比例互斥下拉框
- `example_workflows/` — 内置示例工作流（`GPT-Image-2.json` 完整流程、`GPT-Image-2-Text2Img.json` 最简文生图、`GPT-Image-2-Reference.json` 最简参考图）。ComfyUI 启动时会自动扫描本目录并登记到「工作流 → 浏览模板」菜单，无需手动拖 json

## 配置文件位置（自动写入，不含 API Key）

- Windows: `%APPDATA%\gpt_image_2\config.json`
- Linux/macOS: `~/.config/gpt_image_2/config.json`
- 回退: `<节点目录>/.gpt_image_2_config.json`

保存字段：`base_url`、`default_model`、`default_size`、`default_quality`、`default_endpoint`、`default_output_format`、`disable_proxy`。**不保存** `api_key`，每次都要在节点里重新填写。

## 主要改进历史

- 节点内部**不再自动重试**（重试等于重复扣费），失败立即给出全量诊断信息
- `base_url`、默认模型等非敏感参数自动持久化，`api_key` 永远不落盘
- 分辨率/比例改为查表 + 互斥校验，避免自由拼接出容易 502 的非法尺寸
- 图片返回值解析兼容标准 Images API 字段之外，还能从任意字符串字段兜底抠出 Markdown 图床链接 / 裸图片 URL
- `response_format=auto` 时主动显式请求 `b64_json`，减少依赖下载图床图片这条更容易失败的路径

## 依赖

```
requests
Pillow
numpy
torch (由 ComfyUI 提供)
```

## License

MIT
