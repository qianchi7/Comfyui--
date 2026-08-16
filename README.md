# SillyDream-Comfyui-yuan

一些 Comfyui 云端模型的节点与工作流，在去除原有的 Base URL 和模型之后，可以使用自己的模型和 URL。

如果感兴趣也可以尝试一下注册我的官网：https://wish.sillydream.top

## 仓库结构

每一套节点/工作流放在各自的子文件夹中，子文件夹里有各自的 README 说明具体用法：

- [`反推节点与工作流/`](./反推节点与工作流) — 图像反推 / 文本扩写节点，通过任意 OpenAI 兼容网关调用视觉/文本模型
  - [`Comfyui-reverse_promp/`](./反推节点与工作流/Comfyui-reverse_promp) — custom nodes 代码（自包含，可单独复制进 `custom_nodes` 使用）
  - [`工作流/`](./反推节点与工作流/工作流) — 配套的工作流 json 示例
- [`Banana节点与工作流/`](./Banana节点与工作流) — Google Gemini(NanoBanana) 文生图/图生图节点 + 端点测速节点
  - [`comfyui-Banana-API-3.5/`](./Banana节点与工作流/comfyui-Banana-API-3.5) — custom nodes 代码（自包含，可单独复制进 `custom_nodes` 使用）
  - [`工作流/`](./Banana节点与工作流/工作流) — 配套的工作流 json 示例
- [`GPT-Image-2节点与工作流/`](./GPT-Image-2节点与工作流) — GPT-Image-2 文生图/图生图节点（最多16张参考图、分辨率互斥校验、502全量诊断）
  - [`ComfyUI_GPT_Image_2_v3/`](./GPT-Image-2节点与工作流/ComfyUI_GPT_Image_2_v3) — custom nodes 代码（自包含，可单独复制进 `custom_nodes` 使用）
  - [`工作流/`](./GPT-Image-2节点与工作流/工作流) — 配套的工作流 json 示例

## 安装

**方式一：只装某一套节点** —— 把对应的 custom nodes 子文件夹（`Comfyui-reverse_promp` / `comfyui-Banana-API-3.5` / `ComfyUI_GPT_Image_2_v3`）复制/软链到 `ComfyUI/custom_nodes/` 下即可，具体见各子项目自己的 README。

**方式二：把整个仓库当成一个 custom node 包装进去**（好处是以后仓库里新增的所有节点都会一起被加载、一起更新）：

```bash
git clone https://github.com/qianchi7/SillyDream-ComfyUI-Cloud.git ComfyUI/custom_nodes/SillyDream-ComfyUI-Cloud
```

重启 ComfyUI 即可自动加载仓库内所有节点（根目录的 `__init__.py` 会自动汇总各子文件夹里的节点包）。

## 装好了但搜不到节点？

1. **必须重启 ComfyUI**（不是刷新网页），custom_nodes 只在启动时扫描一次。
2. 启动时看**控制台/终端日志**，重点找：
   - 红色 `Traceback` / `ImportError` / `ModuleNotFoundError`（多半是缺依赖，见下面「安装依赖」）
   - 方式二整仓库安装时，根目录 `__init__.py` 若某个子节点包加载失败，会打印 `[SillyDream-ComfyUI-Cloud] 加载节点包失败: ... -> ...`，可以据此定位是哪一套节点出的问题
3. **安装依赖**（用 ComfyUI 自带的 Python 环境执行，便携版要用 `python_embeded\python.exe -m pip install ...`）：
   ```bash
   pip install requests Pillow numpy
   ```
4. 确认放置路径正确：custom_nodes 下应该直接是节点文件夹（例如 `ComfyUI/custom_nodes/ComfyUI_GPT_Image_2_v3/__init__.py`），不要多嵌套一层。
5. 在节点搜索框（双击画布空白处，或右键 → Add Node）里按**显示名称**或**分类**搜索，而不是按仓库名/文件夹名：

   | 项目 | 搜索关键词 / 分类 |
   |---|---|
   | 反推节点与工作流 | `图像反推`、`文本扩写`（分类 `Image/Prompt`） |
   | Banana节点与工作流 | `Banana Gemini Image Generator`、`Banana Endpoint Tester`（分类 `image/ai_generation`、`Banana/tools`） |
   | GPT-Image-2节点与工作流 | `GPT Image 2 Generator`（分类 `GPT Image 2`） |

6. 如果通过 **ComfyUI-Manager** 装的，装完在 Manager 里点一下 **Restart**，不要只点 Reload。
7. 还是搜不到：把启动日志里从 `Import times for custom nodes` 那一段往上的报错内容发出来，可以精确定位。

## 自动更新

推荐通过 **ComfyUI-Manager** 安装（Manager 界面里选择「Install via Git URL」，填入本仓库地址）。
因为本仓库是标准 git 仓库，ComfyUI-Manager 会自动识别出它是通过 git 安装的节点，之后每次作者更新代码，
只需要在 Manager 里点击对应节点的 **Update** 按钮（本质是 `git pull`），或者手动在该目录下执行：

```bash
cd ComfyUI/custom_nodes/SillyDream-ComfyUI-Cloud
git pull
```

即可拉取到最新版本，无需重新下载整个节点。

## 协议

本仓库使用 [MIT License](./LICENSE)，可自由使用、修改、商用，保留版权声明即可。
