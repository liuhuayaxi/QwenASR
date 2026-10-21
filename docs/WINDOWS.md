# Windows 本地运行

QwenASR 原来的本地 ASR 使用 MLX，只适用于 Apple Silicon。本分支增加 Windows 原生 Transformers 后端：Windows 使用官方 qwen-asr Python 包和 PyTorch，优先 NVIDIA CUDA，无 CUDA 时回退 CPU；macOS/Linux 继续使用 MLX。

## 安装

1. Windows 10/11、Python 3.12、uv。
2. PowerShell 运行 start_qwen_asr.ps1。
3. 启动脚本会安装 Windows 依赖、下载官方 Qwen/Qwen3-ASR-1.7B 权重并启动服务。
4. 浏览器访问 http://127.0.0.1:8765。

Windows 与 Apple Silicon 使用不同模型权重：Windows 使用 Qwen/Qwen3-ASR-1.7B Transformers 权重，不复用 mlx-community/Qwen3-ASR-1.7B-bf16。

## CUDA

PyTorch 的 CUDA wheel 应按 NVIDIA 驱动选择。默认依赖文件使用 PyPI PyTorch，因此首次运行也可以 CPU 验证。对 1.7B 模型，NVIDIA GPU 是推荐配置。

## 当前范围

Windows 本地离线 ASR 已接入现有 ModelManager、任务队列和服务生命周期。模型级 streaming 暂不承诺；Qwen 官方文档目前将 streaming 能力放在 vLLM 后端，因此浏览器实时模式继续使用已有分段请求机制。

MLX 专用采样参数不能一一映射到 Transformers API；Windows 当前保证语言、上下文和 max_tokens，其他参数保持校验兼容但不改变 Transformers 的生成策略。

MinerU、课程抓取等外部组件的 Windows 原生可用性不属于本阶段承诺；已有远程推理节点继续可用。
