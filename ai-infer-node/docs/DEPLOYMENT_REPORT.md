# 部署结果：2026-10-08

**2026-10-09 当前部署：Windows 原生 Tailscale `http://100.81.62.69:8765` → 官方后台 TCP Serve → WSL `127.0.0.1:8765`。不依赖 WSL Tailnet/SSH，未使用普通 LAN/public 端口或 Funnel。最终部署与复查以 [FINAL_REVIEW_REPORT.md](FINAL_REVIEW_REPORT.md) 为准。下面保留 10 月 8 日的部署记录；其中 WSL Tailnet 地址和“没有 Serve”的描述已失效。**

10 月 8 日方案：根据用户“只需要 Tailscale 内网直连”的选择，业务直接访问 WSL Tailnet `100.81.168.99:8765`，服务仅监听此接口。SSH 隧道已禁用，实验性 Windows 局域网桥接已停止并移除。

WSL 节点与 Mac 集成已实际运行并完成推理、回退和恢复测试。Windows 尚未登录时的开机触发器因权限拒绝未配置成功，真实 Windows 重启验收未执行。不能把这些剩余项标为通过。

## 实际部署

| 项目 | 结果 |
| --- | --- |
| 节点 | `ssh efinity`，Ubuntu-24.04 / WSL2 / liuhuayaxi |
| 部署目录 | `/home/liuhuayaxi/ai-infer-node`，gateway/mineru/asr 三个独立 venv |
| API | WSL `100.81.168.99:8765`，限制 Tailnet 来源，Bearer Token 全接口鉴权 |
| Mac 入口 | WSL Tailscale `100.81.168.99:8765` 直接访问；SSH 转发已禁用 |
| GPU | NVIDIA GeForce RTX 4060 Laptop GPU，8188 MiB |
| MinerU | 4.0.10，Torch 小模型 + vLLM 0.28.0，PDF/图片 Advanced |
| MinerU 依赖 | Torch 2.13.0 cu130，Transformers 5.19.0，Docvortex 0.5.11 |
| MinerU 权重 | `MinerU-4_models_torch` + `MinerU2.5-Pro-2605-1.2B`，官方 4.0 模型下载器选择并 verify 通过；VLM 名称中的 2.5 不表示使用 MinerU 3.x 配置 |
| VLM 精度 | BF16，quantization=None，max_num_seqs=1，eager，8192 context，GPU memory fraction 0.55 |
| Qwen-ASR | `Qwen/Qwen3-ASR-1.7B-hf`，Transformers 5.19.0 原生实现 |
| ASR 精度/设备 | Torch 2.14.1 cu130，全模型参数 CUDA/BF16，batch=1，无 CPU offload、量化或 ForcedAligner |
| ASR 权重提交 | Hugging Face `bcd2b5b7f32b480ab5790554cfa8347f246a14f3` |
| ASR 权重校验 | `model.safetensors` 4,076,193,080 bytes，SHA256 `2db53c7d81bd9b8cbc6a074e89be2c968a0d373fb4ee68bb1b1e14f7042dfee1`，与官方 LFS metadata 相同 |
| systemd | `ai-infer-gateway.service` enabled/active，非 root，Restart=on-failure，KillMode=control-group |
| 持久化 | SQLite WAL + FULL sync，幂等 key/内容哈希，结果目录原子发布，历史 7 天 |
| 调度 | 一个共享 GPU 队列和跨进程 flock；任务完成立即退出 Worker 及进程组 |
| 任务超时 | 正式配置恢复为 14400 秒，最大输入 2 GiB，上传超时 1800 秒 |
| Mac 默认 | `mineru_provider=local`、`asr_provider=local`；原 MinerU/MLX 环境与资料库保留 |

模型下载阶段访问官方 Hugging Face / ModelScope；推理 Worker 使用本地权重与 offline 标志，MinerU remote server_url 为空。没有向第三方推理 API 上传测试 PDF/音频。未读取、复制、上传或修改 SSH 私钥；未安装 NVIDIA Linux 显示驱动。

## 已处理的问题

1. 网络：初始 Hugging Face TLS EOF，依赖共享缓存锁超时。用户调整网络后重新连接成功，缓存复用完成安装与官方权重下载。ModelScope 与 HF 的 ASR 权重 SHA256 相同；最终模型来源为 HF 固定提交。
2. 隔离解释器：不能 resolve venv Python 的符号链接，否则会落到系统 Python。已保留 venv 路径并真实验证。
3. CUDA 编译：vLLM 的 FlashInfer 需要 nvcc/ninja；pip 的 nvcc 13.4 与 Torch 的 13.0 headers 不匹配。已固定编译器、CRT、NVVM 13.0.88，并在本节点提供独立 toolkit 视图，链接原 WSL libcuda 与 pip libcudart。没有降低模型版本、档位或精度。
4. 混合语音：最初 25 秒上限把一段中文与一段英文放到同一段，模型偏向英文。已用 ≥0.8 秒静音作为较早分界，保留约 1 秒重叠，并修正重叠窗口必须推进的条件。最终双语短/长样本都保留两种语言。
5. GPU 释放：强杀后驱动会延迟报告已释放的显存。网关保持 UNLOADING 和 GPU 锁，等待基线附近再启动下一项；实测约 0.7 秒。
6. RAM 页缓存：退出模型进程后，权重文件仍可能留在 Linux 文件缓存。默认只对本节点权重文件使用 POSIX_FADV_DONTNEED；保留文件且不清空全局缓存。一次建议回收 9 个权重文件、7,279,164,874 bytes。Windows WSL Working Set 的现场采样从约 11.8 GB 降至约 1.8 GB；这与网关 RSS 是不同指标。
7. Mac 图片引用：生成的 `(AI).pdf` 额外嵌入完整 `mineru-result.zip`，包含 Markdown、materialized JSON、素材和 provider.json，防止临时目录删除后图片引用丢失。原 3 个附加文件、逐页隐藏文字层与原页保留。

## 测量概况

| 测试 | 加载 | 推理/解析 | 总任务 | 峰值 GPU | 完成后 GPU |
| --- | ---: | ---: | ---: | ---: | ---: |
| MinerU 12 页文本/公式/表格 | VLM 81.38 s（首次编译） | 143.40 s（包含加载） | 150.81 s | 4817 MiB | 43 MiB |
| MinerU 2 页扫描/图表，真实 API | VLM 30.07 s | 60.20 s（包含加载） | 67.77 s | 4817 MiB | 43 MiB |
| ASR 5 分钟双语，最终分段 | 1.95 s | 25.98 s，RTF 0.087 | 34.28 s | 4324 MiB | 43 MiB |
| ASR 30 分钟双语，最终分段 | 1.70 s | 150.23 s，RTF 0.083 | 160.91 s | 4324 MiB | 43 MiB |
| ASR 19 秒，启用文件页缓存回收后的冷启动 | 4.17 s | 3.56 s | 16.95 s | 4316 MiB | 43 MiB |

MinerU 的 `vlm_load_seconds` 为实际 VLM 引擎初始化时间，小模型初始化分散在官方 parse 流程中；因此解析时间明确包含加载，不能将两列相加。首次编译、磁盘缓存与文件缓存回收会影响加载时间。这里没有把减法估算当作独立测得的小模型加载时间。

最终空闲网关 RSS 约 49 MiB，worker_pid=null，GPU 43 MiB。Linux 最后一次 free：总 20417 MiB，used 约 991–1002 MiB，available 约 19425 MiB，swap 基本未使用。整个 WSL VM 包含既有服务、系统文件缓存与动态宿主机回收，Windows Working Set 应以 `evidence/windows.json` 的采样时间为准。

独立验收输入成功率：两种 PDF 输入 2/2（可选文本 12 页、扫描图表 2 页），另一个 PNG 输入 1/1，均 Advanced。技术初始化失败尝试与故意强杀/超时任务不混入这个样本成功率；真实语料总体成功率尚未统计。ASR 的加载列是 processor 与权重加载，不包括 FFmpeg、Python imports 和 CUDA 初始化；这些时间包含在总任务中。

补充实测：原生 DOCX 按官方规则 Flash 解析，GPU 始终 43 MiB；课程资料转换在本机 MinerU 不存在时可真实远程完成，并保存 inference 元数据。Mac 取消整段离线录音会向 Windows 取消同一 Job ID，约 3 秒内终态 canceled，原始输入不丢失。

## 开机状态与限制

Windows `AI Infer WSL Gateway` 已注册当前 WSL 所属用户 liuhuayaxi，Interactive / Limited / AtLogon，任务 Running；实际启动的 WSL `/usr/bin/sleep infinity` 为一个进程，持续存在。systemd 自动重启已经通过真实 SIGKILL 验证。

`-BootBeforeLogin` 的 S4U + AtStartup 注册返回 `0x80070005` 拒绝访问。安装脚本位于 `%LOCALAPPDATA%\AIInferNode\install_autostart.ps1`，需该用户的管理员 PowerShell 执行后另行测试。没有绕过 UAC、使用 SYSTEM、保存用户密码、重启 Windows 或执行 wsl --shutdown。

真正的 Windows 重启、重新登录触发、用户未登录启动、Mac 关机后的持续运行以及 Windows 网络适配器中断均未实测。已验证的是现有任务运行、keepalive 进程、Linux 服务恢复与 SSH 隧道断开/自动重连。没有将配置或手动启动当作开机验收。

支持 30–120 分钟的分段处理设计；实测最长为 30 分钟公开样本重复构成的双语音频。没有把它称为真实课程录音的完整准确率评估，也没有测试 120 分钟或逐词对齐。

当前即时卸载，未实现 60/120 秒模型复用；CPU 原生文档也串行执行。当前业务通过 Tailscale 内网 HTTP 端口直接访问，没有 SSH 隧道、Serve 或 Funnel。

## 交付与回滚

代码、脚本、配置、冻结依赖、报告和 JSON 证据均在本目录。Mac 主项目中已接入 provider、设置界面、完整录音单任务路径和 PDF adapter。没有创建或推送 Git 提交。

已在真实 WSL 节点第二次执行 `scripts/install_wsl.sh`，退出码 0。三套依赖复用冻结版本；MinerU 两个模型集再次 verify=ok，ASR 复用同一 HF 提交并重新校验文件。原配置与令牌保留，服务继续工作。smoke_test.sh 在 SSH 环境下也能识别 WSL 内核并直接检查本机网关，不要求手动修改客户端地址。

切回 local 即恢复原推理路径。源码/MinerU 配置备份：`~/.qwenasr/backups/ai-infer-20261008-203532`。服务器卸载脚本保留全部模型和任务；Windows 任务、Mac launchd 转发可按 README 单独移除。
