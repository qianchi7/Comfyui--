# Banana节点与工作流 — Google Gemini(NanoBanana) 文生图/图生图

通过任意 OpenAI/Gemini 兼容的云端网关调用 Google NanoBanana(Gemini) 图像生成能力，支持文生图、图生图（最多 14 张参考图）、批量并发生成、端点延迟测速等。

本目录下分两个文件夹：

- [`comfyui-Banana-API-3.5/`](./comfyui-Banana-API-3.5) — **custom nodes 代码**，自包含、可独立安装的 ComfyUI 节点包（文件夹名即建议的 custom_nodes 目录名）
- [`工作流/`](./工作流) — 现成的工作流 json 示例，可直接拖进 ComfyUI 网页界面使用

## 安装节点

**方式一（推荐，只装这一套节点）：**

```bash
cp -r Banana节点与工作流/comfyui-Banana-API-3.5 ComfyUI/custom_nodes/comfyui-Banana-API-3.5
```

**方式二（把整个 SillyDream-ComfyUI-Cloud 仓库当作一个 custom node 包装进去）：**

```bash
git clone https://github.com/qianchi7/SillyDream-ComfyUI-Cloud.git ComfyUI/custom_nodes/SillyDream-ComfyUI-Cloud
```

仓库根目录的 `__init__.py` 会自动汇总各子文件夹里的节点。方式二的好处是通过 ComfyUI-Manager 的「Update」按钮（`git pull`）就能自动拉取本仓库后续新增的所有节点/更新。

安装依赖：

```bash
pip install requests
```

首次启动会在 `comfyui-Banana-API-3.5/` 目录下自动生成一份 `config.ini` 示例（如果不存在），请把里面的 `api_key` 换成你自己的，并把 `api_base_url` 换成你自己的 OpenAI/Gemini 兼容网关地址。也可以不改 config.ini，直接在节点的 `api_key` / `api_base_url` 输入框里临时填写，节点输入优先于 config.ini。

重启 ComfyUI 后即可在节点搜索里找到 `image/ai_generation → Banana Gemini Image Generator` 以及 `Banana/tools → Banana Endpoint Tester`。

## 使用工作流示例

两种方式任选其一，加载后按提示填入你自己的 API Key / Base URL 即可使用：

- **菜单加载（推荐）**：重启 ComfyUI 后，顶部菜单 **工作流 → 浏览模板 (Browse Templates)** 里选 `NanoBanana-API-V3.5`
  （节点包内的 `example_workflows/` 会被 ComfyUI 自动登记为模板，随 `git pull` 一起更新）
- **手动拖入**：把 `工作流/▶NanoBanana-API-V3.5工作流-0816 .json` 直接拖进 ComfyUI 网页界面

## 节点参数（Banana Gemini Image Generator）

| 参数 | 说明 |
|------|------|
| `prompt` | 生成图像的文本提示词，支持多行 |
| `api_key` | 你自己网关对应的 API Key，留空则回退使用 `config.ini` 中的配置 |
| `api_base_url` | 你自己的 API 服务地址，留空则回退使用 `config.ini` 中的配置 |
| `model_type` | 模型名称，需与你所用网关中配置的模型名一致 |
| `batch_size` | 单次请求生成的图片数量，1~8 |
| `aspect_ratio` | 输出图像宽高比，`Auto` 由服务端自动决定，也可从内置的常见比例中选择 |
| `seed` | 随机种子，`-1` 为自动随机 |
| `top_p` | 采样参数 Top-P |
| `imageSize` | 分辨率档位：无 / 1K / 2K / 4K |
| `image_1`~`image_14` | 最多 14 张参考图输入，用于图生图或多图融合 |
| `超时秒数` / `无限超时` | 请求读取超时设置；开启「无限超时」时忽略「超时秒数」 |
| `绕过代理` | 本机代理不稳定时可开启，绕过系统代理直连 |
| `测试延迟` / `ping次数` | 开启后先对当前 Base URL 做 ping 测速，输出延迟和丢包率，便于挑选更稳定的节点线路 |

`Banana Endpoint Tester` 是一个独立的辅助节点，只做 Base URL 的 ping 测速，不发起真实生图请求，方便你在正式跑图前先确认线路质量。

## 故障排除

**❌ 无法连接到服务器 / Connection error**
- 确认 `api_base_url` 能从本机浏览器/curl 正常访问
- 尝试开启「绕过代理」或使用 `Banana Endpoint Tester` 先测速

**❌ HTTP 4xx / 5xx**
- 401/403：检查 `api_key` 是否正确、该 Key 是否有权限访问所填模型
- 404：检查 `model_type` 是否与网关中配置的名称完全一致
- 429：请求过于频繁，稍后重试
- 502/503：上游模型服务或网关本身异常，可尝试更换 `api_base_url`

**❌ 生成结果里图片是空的 / 只有文字**
- 部分模型在遇到内容策略限制时只会返回文字说明，节点会把该说明附带输出在 `text` 里，可据此调整提示词
