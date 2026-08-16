# 反推节点与工作流 — 图像反推 / 文本扩写

通过任意 OpenAI 兼容的 HTTP API（New-API / One-API / ModelScope 等网关）把 ComfyUI 中的图像反推为详细提示词，或对已有文本进行扩写。

包含两个节点：

1. **图像反推**（Image Prompt Reverse）
   - 输入一张图片，输出详细的中/英文提示词
   - 系统提示词与用户指令提示词分离，可自定义
   - 支持 `top_p` / `temperature` / `seed` / `max_tokens` 参数
2. **文本扩写**（Text Expand）
   - 对输入文本进行扩写，使描述更详细
   - 内置扩写提示词，也可自行调整

另附一个独立网页小工具 `prompt_expander.html`（不依赖 ComfyUI，可直接用浏览器打开，用于快速调试提示词扩写效果）。

`example_workflows/` 目录下附带一个现成工作流示例：`Grok提示词反推API调用流(无限制).json`。

## 节点参数

| 参数 | 说明 |
|------|------|
| `api_url` | 你自己的 OpenAI 兼容网关地址，只需填 `http://IP:端口`，节点会自动补全 `/v1`（默认预填的是作者本人的演示网关，建议替换成你自己的） |
| `model_name` | 目标模型名称，须与你所用网关中配置的模型名一致（图像反推需选支持读图的视觉模型） |
| `api_key` | 你自己网关对应的 API Key，留空表示不发送 Authorization 头 |
| `language` | 仅图像反推节点：输出中文还是英文提示词 |
| `system_prompt` / `user_prompt` | 仅图像反推节点：系统提示词与用户指令，需自行填写 |
| `top_p` / `temperature` / `seed` / `max_tokens` | 生成参数，含义与 OpenAI Chat Completions 接口一致 |

## 安装

1. 把整个仓库 clone 到 ComfyUI 的 `custom_nodes` 目录下（推荐用 ComfyUI-Manager 的「Install via Git URL」，这样后续可以直接点「Update」自动拉取最新版）：
   ```
   git clone https://github.com/qianchi7/Comfyui--.git ComfyUI/custom_nodes/Comfyui--
   ```
2. 安装依赖：
   ```
   pip install requests Pillow numpy
   ```
3. 重启 ComfyUI。

## 使用方法

1. 在 ComfyUI 节点搜索里找到：`Image/Prompt → 图像反推` 或 `文本扩写`
2. 图像反推节点需连接图像输入 (IMAGE)
3. 把 `api_url` / `model_name` / `api_key` 换成你自己网关的信息
4. 运行工作流获取结果

也可以直接把 `example_workflows/Grok提示词反推API调用流(无限制).json` 拖进 ComfyUI 网页界面，作为现成模板参考。

## 故障排除

**❌ 无法连接到服务器 / Connection error**
- 确认 `api_url` 能从本机浏览器/curl 正常访问
- 确认服务器端口未被防火墙拦截

**❌ HTTP 4xx / 5xx**
- 401/403：检查 `api_key` 是否正确、该 Key 是否有权限访问所填模型
- 404：检查 `model_name` 是否与网关中配置的名称完全一致
- 429：请求过于频繁，稍后重试
- 502/503：上游模型服务或网关本身异常

**❌ 未获取到有效结果 / 响应不是 JSON**
- 多为网关地址填错（把请求转到了网页而非 API 接口），错误信息里会附带实际请求的完整 URL，可用浏览器/curl 直接访问该地址核对
