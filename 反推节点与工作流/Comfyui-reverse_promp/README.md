# Comfyui-reverse_promp — ComfyUI 图像提示词反推 / 文本扩写节点

这是一个自包含的 ComfyUI custom nodes 包，把此文件夹整体放进 `ComfyUI/custom_nodes/` 即可使用。

包含两个节点：

1. **图像反推 (Image Prompt Reverse)** — 通过任意 HTTP API 将 ComfyUI 中的图像反推为详细提示词
2. **文本扩写 (Text Expand)** — 对输入的文本进行扩写，使其更加详细

工作流示例：本目录下的 `example_workflows/` 会被 ComfyUI 自动登记到「工作流 → 浏览模板」菜单（模板名 `Image-Prompt-Reverse`）；同样的 json 也放了一份在上一级目录的 `../工作流/` 里可手动拖入。
`prompt_playground.html` 是配套的独立网页调试小工具（不依赖 ComfyUI，浏览器直接打开即可）。

## 安装

1. 把此文件夹复制到 ComfyUI 的 `custom_nodes` 目录，文件夹名保持 `Comfyui-reverse_promp` 不变
2. 安装依赖：`pip install requests Pillow numpy`
3. 重启 ComfyUI

## 使用方法

1. 在 ComfyUI 中找到节点：`Image/Prompt → 图像反推` / `文本扩写`
2. 图像反推节点需连接图像输入 (IMAGE)
3. 把 `api_url` / `model_name` / `api_key` 换成你自己网关的信息
4. 运行工作流获取结果

## 节点参数

| 参数 | 说明 |
|------|------|
| `api_url` | 你自己的 OpenAI 兼容网关地址（默认预填作者演示网关，建议替换） |
| `model_name` | 目标模型名称，须与所用网关中配置的一致 |
| `api_key` | 网关对应的 API Key，留空则不发送 Authorization 头 |
| `top_p` / `temperature` / `seed` / `max_tokens` | 生成参数 |

## 故障排除

**❌ Connection error**
- 确认 `api_url` 可从本机浏览器/curl 访问

**❌ HTTP 4xx / 5xx**
- 检查 API 返回的完整错误信息
- 确认 `api_key` 是否正确

**❌ 未获取到有效结果**
- 可能是响应格式不兼容，错误信息会附带原始响应内容

## 实现说明

本节点为完全独立实现，代码由本人从功能需求重新编写，不含任何第三方代码。
