# comfyui-Banana-API-3.5

ComfyUI 自定义节点：调用 Google NanoBanana(Gemini) 文生图/图生图接口，内置批量并发调度与端点延迟测速。

节点会自动加载本文件夹内除若干公共模块外的所有 `.py` 文件，可作为独立 custom_nodes 子目录直接安装：

```bash
cp -r comfyui-Banana-API-3.5 ComfyUI/custom_nodes/comfyui-Banana-API-3.5
pip install requests
```

本目录下的 `example_workflows/` 会被 ComfyUI 启动时自动扫描并登记到「工作流 → 浏览模板 (Browse Templates)」菜单（模板名 `NanoBanana-API-V3.5`），无需手动拖 json；同样的工作流也放了一份在上级目录的 [`../工作流/`](../工作流) 里。

## 配置

首次运行会在本目录自动创建示例 `config.ini`：

```ini
[gemini]
api_key = your-api-key-here
api_base_url = http://your-gateway:port
max_workers = 4
network_workers_cap = 4
```

- `api_key` / `api_base_url` 也可以直接在节点面板里临时填写，节点输入优先于 config.ini
- `max_workers` / `network_workers_cap`：批量生成时的并发上限
- 可选 `bypass_proxy = true`：绕过本机系统代理直连

## 节点

- **Banana Gemini Image Generator**（`image/ai_generation`）：主生成节点，详细参数见上级目录 [`../README.md`](../README.md)
- **Banana Endpoint Tester**（`Banana/tools`）：独立的 Base URL 测速节点，不发起生图请求

## 依赖

```
requests>=2.20.0
```

## License

MIT

## 来源与致谢

本节点基于 [xinbao](https://github.com/98624017) 的 [comfyui-banana-li](https://github.com/98624017/comfyui-banana-li) 二次开发。
原项目以 MIT 协议发布，其版权声明已按协议要求保留在本目录 `LICENSE` 中。
本仓库在其基础上做了改动（新增端点延迟测速节点、批量并发调度、移除原代码中隐藏的后台地址切换逻辑等），修改部分版权归 qianchi7 所有。
