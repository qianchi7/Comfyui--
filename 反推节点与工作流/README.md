# 反推节点与工作流 — 图像反推 / 文本扩写

通过任意 OpenAI 兼容的 HTTP API（New-API / One-API / ModelScope 等网关）把 ComfyUI 中的图像反推为详细提示词，或对已有文本进行扩写。

本目录下分两个文件夹：

- [`Comfyui-reverse_promp/`](./Comfyui-reverse_promp) — **custom nodes 代码**，自包含、可独立安装的 ComfyUI 节点包（文件夹名即建议的 custom_nodes 目录名）
- [`工作流/`](./工作流) — 现成的工作流 json 示例，可直接拖进 ComfyUI 网页界面使用

## 安装节点

**方式一（推荐，只装这一套节点）：**
把 `Comfyui-reverse_promp/` 这一个文件夹复制或软链到 `ComfyUI/custom_nodes/` 下：

```bash
# 直接从本仓库单独 clone 这个子目录，或复制文件夹都可以
cp -r 反推节点与工作流/Comfyui-reverse_promp ComfyUI/custom_nodes/Comfyui-reverse_promp
```

**方式二（把整个 SillyDream-ComfyUI-Cloud 仓库当作一个 custom node 包装进去）：**

```bash
git clone https://github.com/qianchi7/SillyDream-ComfyUI-Cloud.git ComfyUI/custom_nodes/SillyDream-ComfyUI-Cloud
```

仓库根目录的 `__init__.py` 会自动汇总各子文件夹里的节点，两种方式都能正常加载出 `图像反推` / `文本扩写` 两个节点。方式二的好处是通过 ComfyUI-Manager 的「Update」按钮（`git pull`）就能自动拉取本仓库后续新增的所有节点/更新。

安装依赖：

```bash
pip install requests Pillow numpy
```

重启 ComfyUI 后即可在节点搜索里找到 `Image/Prompt → 图像反推` / `文本扩写`。

## 使用工作流示例

把 `工作流/Grok提示词反推API调用流(无限制).json` 直接拖进 ComfyUI 网页界面即可加载。

## 节点参数

| 参数 | 说明 |
|------|------|
| `api_url` | 你自己的 OpenAI 兼容网关地址，只需填 `http://IP:端口`，节点会自动补全 `/v1`（默认预填的是作者本人的演示网关，建议替换成你自己的） |
| `model_name` | 目标模型名称，须与你所用网关中配置的模型名一致（图像反推需选支持读图的视觉模型） |
| `api_key` | 你自己网关对应的 API Key，留空表示不发送 Authorization 头 |
| `language` | 仅图像反推节点：输出中文还是英文提示词 |
| `system_prompt` / `user_prompt` | 仅图像反推节点：系统提示词与用户指令，需自行填写 |
| `top_p` / `temperature` / `seed` / `max_tokens` | 生成参数，含义与 OpenAI Chat Completions 接口一致 |

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
