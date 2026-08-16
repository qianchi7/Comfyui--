# SillyDream-Comfyui-yuan

一些 Comfyui 云端模型的节点与工作流，在去除原有的 Base URL 和模型之后，可以使用自己的模型和 URL。

如果感兴趣也可以尝试一下注册我的官网：https://wish.sillydream.top

## 仓库结构

每一套节点/工作流放在各自的子文件夹中，子文件夹里有各自的 README 说明具体用法：

- [`反推节点与工作流/`](./反推节点与工作流) — 图像反推 / 文本扩写节点，通过任意 OpenAI 兼容网关调用视觉/文本模型

## 安装

把整个仓库 clone 到 ComfyUI 的 `custom_nodes` 目录下：

```bash
git clone https://github.com/qianchi7/Comfyui--.git ComfyUI/custom_nodes/Comfyui--
```

重启 ComfyUI 即可自动加载仓库内所有节点。

## 自动更新

推荐通过 **ComfyUI-Manager** 安装（Manager 界面里选择「Install via Git URL」，填入本仓库地址）。
因为本仓库是标准 git 仓库，ComfyUI-Manager 会自动识别出它是通过 git 安装的节点，之后每次作者更新代码，
只需要在 Manager 里点击对应节点的 **Update** 按钮（本质是 `git pull`），或者手动在该目录下执行：

```bash
cd ComfyUI/custom_nodes/Comfyui--
git pull
```

即可拉取到最新版本，无需重新下载整个节点。

## 协议

本仓库使用 [MIT License](./LICENSE)，可自由使用、修改、商用，保留版权声明即可。
