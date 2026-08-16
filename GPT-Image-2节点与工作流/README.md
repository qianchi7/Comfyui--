# GPT-Image-2节点与工作流

ComfyUI 自定义节点：通过任意 OpenAI 兼容的云端网关（New-API / One-API 等中转）调用 GPT-Image-2 完成文生图 / 图生图（最多支持 16 张参考图），内置分辨率/比例互斥校验、502 全量诊断、base_url 等非敏感配置自动持久化。

本目录下分两个文件夹：

- [`ComfyUI_GPT_Image_2_v3/`](./ComfyUI_GPT_Image_2_v3) — **custom nodes 代码**，自包含、可独立安装的 ComfyUI 节点包（文件夹名即建议的 custom_nodes 目录名）
- [`工作流/`](./工作流) — 现成的工作流 json 示例，可直接拖进 ComfyUI 网页界面使用

## 特性

- **文生图 + 图生图**：最多 16 张参考图，支持 `/v1/images/generations`、`/v1/images/edits`、`/v1/responses` 三种接口，也可设为 `auto` 自动选择
- **分辨率/比例互斥**：内置权威尺寸表（见下），非法的「比例 × 分辨率」组合会在发请求前直接拦截，避免浪费一次可能 5-15 分钟又 502 的请求；配套 `web/gpt_image_2_size_lock.js` 让前端下拉框直接只列出合法档位
- **不做内部重试**：网络异常/5xx 不自动重试（重试等于重复扣费），失败会抛出带完整诊断信息（server / cf-ray / x-request-id / body）的报错，方便判断故障出在 Cloudflare / nginx / 中转网关 / OpenAI 官方哪一层
- **兼容非标准返回格式**：除了标准 `data[].b64_json` / `data[].url`，也能从部分中转网关"伪装成聊天回复"的文本中兜底抠出 Markdown 图床链接或裸图片 URL
- **非敏感配置持久化**：`base_url` / 默认模型 / 默认分辨率等会自动记住，`api_key` **永远不落盘**，每次都要在节点里重新填写

## 安装节点

**方式一（推荐，只装这一套节点）：**

```bash
cp -r GPT-Image-2节点与工作流/ComfyUI_GPT_Image_2_v3 ComfyUI/custom_nodes/ComfyUI_GPT_Image_2_v3
```

**方式二（把整个 SillyDream-ComfyUI-Cloud 仓库当作一个 custom node 包装进去）：**

```bash
git clone https://github.com/qianchi7/SillyDream-ComfyUI-Cloud.git ComfyUI/custom_nodes/SillyDream-ComfyUI-Cloud
```

仓库根目录的 `__init__.py` 会自动汇总各子文件夹里的节点。方式二的好处是通过 ComfyUI-Manager 的「Update」按钮（`git pull`）就能自动拉取本仓库后续新增的所有节点/更新。

安装依赖：

```bash
pip install requests Pillow numpy
```

重启 ComfyUI 后即可在节点搜索里找到 `GPT Image 2 → GPT Image 2 Generator`。

> ⚠️ 分辨率/比例互斥的前端下拉框（`web/gpt_image_2_size_lock.js`）依赖 ComfyUI 直接扫描该子目录的 `WEB_DIRECTORY`，只有**方式一（独立安装该子文件夹）**能保证生效；方式二（整仓库聚合安装）目前只汇总了节点逻辑，前端互斥 UI 可能不生效（后端的尺寸校验不受影响，非法组合仍会被拦截报错，只是不会在下拉框里提前隐藏）。

## 使用工作流示例

两种方式任选其一，加载后按提示填入你自己的 API Key / base_url 即可使用：

- **菜单加载（推荐）**：重启 ComfyUI 后，顶部菜单 **工作流 → 浏览模板 (Browse Templates)**，可选：
  - `GPT-Image-2` — 完整工作流
  - `GPT-Image-2-Text2Img` — 最简纯文生图
  - `GPT-Image-2-Reference` — 最简带参考图
  （节点包内的 `example_workflows/` 会被 ComfyUI 自动登记为模板，随 `git pull` 一起更新）
- **手动拖入**：把 `工作流/GPT_Image_2 工作流-0816.json` 直接拖进 ComfyUI 网页界面

## 权威尺寸表（分辨率 / 比例互斥规则）

| 比例 | 1K | 2K | 4K |
|---|---|---|---|
| 1:1 | 1024x1024 | 2048x2048 | — |
| 3:2 | 1536x1024 | 2048x1360 | 3520x2352 |
| 2:3 | 1024x1536 | 1360x2048 | 2352x3520 |
| 16:9 | — | 2048x1152 | 3840x2160 |
| 9:16 | — | 1152x2048 | 2160x3840 |
| 4:3 | — | 2048x1536 | 3312x2480 |
| 3:4 | — | 1536x2048 | 2480x3312 |
| 21:9 | — | 2688x1152 | 3840x1648 |
| 1:3 | — | 1024x3072 | 1280x3840 |
| 3:1 | — | — | 3840x1280 |

「—」表示该比例在该分辨率档下没有合法尺寸（互斥）：例如 `1:1` 选不出 `4K`，`16:9` 选不出 `1K`，`3:1` 只能选 `4K`。这些尺寸经过实测，是不易触发 502 的固定档位；节点只发 `model/prompt/size/n/quality(/seed)` 等 GPT-Image-2 认可的字段，不发 `enhance_prompt/negative_prompt/style_preset` 等不支持的参数。

## 节点参数

| 参数 | 说明 |
|------|------|
| `api_key` | 你自己网关对应的 API Key |
| `base_url` | 你自己的 OpenAI 兼容网关地址，只需填 `http://IP:端口`（默认预填的是作者本人的演示网关，建议替换成你自己的） |
| `model` | 模型名称，需与你所用网关中配置的模型名一致 |
| `resolution` / `aspect_ratio` | 分辨率档位与宽高比，二者互斥（见上表）；均为 `auto` 时交给上游自选，`aspect_ratio=auto` 时会从 `image_1` 推断比例 |
| `quality` | `auto` / `low` / `medium` / `high` |
| `n` | 生成数量，1~10 |
| `seed` | 随机种子，-1 为自动随机 |
| `response_format` | `auto` 时会显式请求 `b64_json`（避免依赖下载图床图片），也可强制选 `url` |
| `edit_mode` | `generate`（纯生成）/ `reference`（参考图生图）/ `outpaint`（扩图），仅用于本地选择接口 |
| `timeout` / `infinite_timeout` | 请求总超时；4K/High 常需 5-15 分钟，建议保持较长超时或开启无限超时 |
| `api_endpoint` | `auto` 会根据是否有参考图自动选择 `/v1/images/generations` 或 `/v1/images/edits`，也可强制指定，包括 `/v1/responses` |
| `image_1`~`image_16` | 最多 16 张参考图输入 |

## 故障排除

节点失败时会抛出带完整上下文的报错（HTTP 状态码、`server`/`cf-ray`/`x-request-id`/响应体等），可据此判断故障出在哪一层：

- **Cloudflare（`cf-ray` 头）**：多为中转反代问题
- **nginx（`server: nginx/x.y.z`）**：自建反代问题，检查 `proxy_read_timeout` 是否够长（建议 ≥1800s，因为 4K/High 生成常需 5-15 分钟）
- **new-api / one-api（`server: Go`）**：上游中继问题，检查对应 channel 是否存活、token 是否有效
- **OpenAI 官方（`server: OpenAI`）**：真实上游返回的错误

**❌ 无法连接到服务器 / Connection error**
- 确认 `base_url` 能从本机浏览器/curl 正常访问

**❌ HTTP 502/503**
- 把 `resolution` 从 4K 降到 2K，`quality` 从 high 降到 medium 先验证链路
- 如果是自建反代/中转网关，检查其读超时配置

**❌ HTTP 400**
- 提示 `Unknown parameter` 时节点会自动去掉该字段重试一次；若持续失败，检查 `model` 名称是否与网关配置一致

**❌ 接口未返回任何图像**
- 节点已兼容"图片链接被伪装成聊天文本"的返回格式，若仍失败，把报错里附带的完整响应体拿去核对网关实际返回结构
