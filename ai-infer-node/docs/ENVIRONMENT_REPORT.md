# 环境检查（2026-10-08，部署前）

SSH 指定命令成功，账户 liuhuayaxi。没有读取、复制或修改 SSH 私钥。

| 项目 | 现场检查结果 |
| --- | --- |
| WSL | Ubuntu-24.04，WSL2，Ubuntu 24.04.4 LTS |
| 内核 | 6.18.33.2-microsoft-standard-WSL2 |
| Python | 3.12.3 |
| GPU | NVIDIA GeForce RTX 4060 Laptop，8188 MiB |
| 驱动 | Windows KMD 610.88 / CUDA UMD 13.3；WSL NVIDIA-SMI 610.57.01 |
| 空闲 GPU 基线 | 43 MiB，无计算进程 |
| WSL RAM | 20417 MiB，总使用 859 MiB，可用 19558 MiB |
| swap | 6144 MiB，当前使用 0 |
| 磁盘 | WSL Linux 根目录可用 939 GiB |
| systemd | PID 1 systemd，running；/etc/wsl.conf 已有 systemd=true |
| user systemd | running，Linger=no |
| 提权 | sudo -n 不可用；不修改密码或绕过 UAC |
| 初始依赖 | PATH 中未找到 uv、ffmpeg；系统 pip 未报告 torch/mineru/transformers/qwen-asr |
| 已有环境 | ~/.venvs/dft-qe、fpga-tools，保留 |
| 网络 | SSH_CONNECTION 两端为 100.x Tailnet 地址 |
| Windows | 当前身份 laptop-8072847s\liuhuayaxi，Tailscale 1.102.3，任务计划程序查询成功 |
| WSL 配置 | Windows .wslconfig 已有 memory=21923627008，保留原文件 |
| Mac 网关 | 现有工作台监听 127.0.0.1:8765；远程 SSH 映射使用 18765 避免冲突 |
| Mac MinerU | ~/.qwenasr/runtimes/mineru4，4.0.10，当前 Advanced，llama-cpp，窗口 8 |
| Mac ASR | models/Qwen3-ASR-1.7B-MLX-bf16，ModelManager 独立 MLX 子进程，空闲回收 |

Mac 接口：ModelManager.transcribe 返回 `(text, language, elapsed)`；PDF `convert_pdf_to_ai` 用 MinerU 4 SDK 单次推理保存 Markdown、Middle JSON、V1 content_list，生成同目录 `(AI).pdf`。新增远程 Adapter 必须保留这些结构。

新增服务、模型和 Python 环境使用 ~/ai-infer-node 和其隔离 venv，不替换已有运行时。部署开始前保存 Mac 相关源码及 MinerU 配置到项目外受保护备份目录。后续 PyTorch 实测、模型版本核查和权限执行结果记录在 DEPLOYMENT_REPORT.md。
