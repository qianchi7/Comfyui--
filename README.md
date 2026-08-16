# SillyDream-Comfyui-yuan

一些 Comfyui 云端模型的节点与工作流，在去除原有的 Base URL 和模型之后，可以使用自己的模型和 URL。

如果感兴趣也可以尝试一下注册我的官网：https://wish.sillydream.top

## 仓库结构

每一套节点/工作流放在各自的子文件夹中，子文件夹里有各自的 README 说明具体用法：

- [`反推节点与工作流/`](./反推节点与工作流) — 图像反推 / 文本扩写节点，通过任意 OpenAI 兼容网关调用视觉/文本模型
  - [`Comfyui-reverse_promp/`](./反推节点与工作流/Comfyui-reverse_promp) — custom nodes 代码（自包含，可单独复制进 `custom_nodes` 使用）
  - [`工作流/`](./反推节点与工作流/工作流) — 配套的工作流 json 示例

## 安装

**方式一：只装某一套节点** —— 把对应的 `Comfyui-reverse_promp` 之类的 custom nodes 子文件夹复制/软链到 `ComfyUI/custom_nodes/` 下即可，具体见各子项目自己的 README。

**方式二：把整个仓库当成一个 custom node 包装进去**（好处是以后仓库里新增的所有节点都会一起被加载、一起更新）：

```bash
git clone https://github.com/qianchi7/SillyDream-ComfyUI-Cloud.git ComfyUI/custom_nodes/SillyDream-ComfyUI-Cloud
```

重启 ComfyUI 即可自动加载仓库内所有节点（根目录的 `__init__.py` 会自动汇总各子文件夹里的节点包）。

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
