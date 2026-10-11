# Windows WSL2 按需 AI 推理节点

服务源码在此目录；实际部署在 WSL 的 `/home/liuhuayaxi/ai-infer-node`。独立 gateway、mineru、asr 虚拟环境保留 Mac 和 WSL 原有环境。PDF/图像固定 Advanced，ASR 固定官方 1.7B HF 权重和全 GPU BF16。默认模型空闲时间为 0，每项任务通过进程退出卸载模型。

2026-10-10 的 Windows PDF 配置使用 `processing_window_size=4`、`vlm_concurrency=4`、`max_num_seqs=4` 和 `enforce_eager=false`，启用 vLLM 编译与 CUDA Graph。并发发生在同一份文档的解析过程中，GPU 任务队列仍为 1，避免 8 GB 显卡同时加载 ASR 和多个视觉模型。显存预算保持 `gpu_memory_utilization=0.55`，模型、Advanced 档位和 BF16 保持原配置。首次运行需要建立编译缓存；之后仍按任务加载与卸载模型。结果 metadata 的 `performance_config` 记录实际传入的参数，CUDA Graph 是否成功捕获需核对 worker 日志。

最终状态、复查结果和当前入口见 `docs/FINAL_REVIEW_REPORT.md`。`docs/DEPLOYMENT_REPORT.md` 与 `docs/TEST_REPORT.md` 保存早期部署、失败尝试和资源数据；测试 Worker 的测试不能证明真实模型解析质量。

报告中引用的 `docs/evidence/` 为本地验收截图与运行记录，可能包含个人资料库和运行环境信息，保留在本机并从 Git 提交中排除。GitHub 源码副本不包含这些原始验收附件。

## 连接与设置

Mac 工作台设置入口：`http://127.0.0.1:8765/?settings=inference`。文档和语音分别支持 `local`、`remote_preferred`、`remote_only`，默认均为 `local`。本机 MinerU 档位、引擎和 MLX ASR 原有参数继续使用原配置。

**最终稳定入口：`http://100.81.62.69:8765`（Windows 原生 Tailscale）。** 其他设备加入同一 Tailnet 后使用此地址和 Bearer Token，不需要 SSH。原 WSL Tailscale 地址 `100.81.168.99` 因实际 UDP/DERP 网络故障间歇失联，已退出业务连接路径；它只供现有 SSH 管理使用。

Windows 官方 Tailscale Serve 在后台持久化 TCP 转发：

```powershell
tailscale.exe serve --bg --yes --tcp=8765 tcp://127.0.0.1:8765
tailscale.exe serve status --json
```

WSL 网关只监听 `127.0.0.1:8765`，通过 Windows/WSL 已有 localhost forwarding 可访问。Windows Tailscale 守护进程对 Tailnet TCP 8765 进行转发，Bearer 请求头保留；没有 SSH 隧道、Node 桥接、普通 LAN/public 监听或 Funnel。已实测在 WSL tailscaled 停止期间，Windows 入口仍可完成健康检查、转写、轮询和下载。

重复安装脚本为 `scripts/install_tailnet_serve.ps1`，已复制到 Windows `%LOCALAPPDATA%\AIInferNode`。它先验证后端、备份 Serve 配置、检查同端口冲突，只配置该端口而不 reset 整个 Serve：

```powershell
powershell.exe -NoProfile -File "$env:LOCALAPPDATA\AIInferNode\install_tailnet_serve.ps1"
```

令牌位于 WSL `~/ai-infer-node/private/token` 和 Mac `~/.qwenasr/remote-infer/token`，权限 600；其他设备安全复制到自己的受保护文件即可。裸浏览器请求无 Bearer 返回 401，这是正常鉴权。正式地址与 SSH 管理地址是两个不同的 Tailscale 节点，Syncthing 也使用 Windows 原生节点。

Mac 默认模式仍为 local。客户端对短暂连接/网关失败做有限重试，提交时复用同一个 key，轮询时复用 Job ID。连续断线超过 `network_recovery_seconds=60` 后失败，Preferred 可回退；冷启动和正常排队则继续等待正式 job_timeout，不混为网络故障。

命令行切换（在 QwenASR 项目目录使用原 `.venv`）：

```bash
.venv/bin/python ai-infer-node/client/provider_config.py --mineru remote_preferred --asr remote_preferred
.venv/bin/python ai-infer-node/client/provider_config.py --mineru remote_only --asr remote_only
.venv/bin/python ai-infer-node/client/provider_config.py --mineru local --asr local
```

设置保存在 `~/.qwenasr/inference-providers.json`。浏览器不接触远程令牌。`remote_only` 的任何错误都向上层返回；`remote_preferred` 失败后使用原始本机输入回退，结果记录实际 provider、fallback 和错误类型。

## 重复部署

将目录同步到 WSL，保留远程配置、模型、任务和凭据。推荐 `rsync`，不要传输 Mac `.venv`：

```bash
rsync -az --exclude config.toml --exclude private --exclude data --exclude venvs --exclude models --exclude __pycache__ ai-infer-node/ efinity:ai-infer-node/
ssh efinity 'cd ~/ai-infer-node && scripts/install_wsl.sh'
```

安装器创建隔离 venv、下载并验证模型、冻结依赖版本。`requirements-*.lock` 为实际部署版本。MinerU 的 CUDA 编译器、CRT、NVVM 固定到 13.0.88，与其 Torch cu130 头文件一致；模型和 MinerU 版本未因此降低。`configure_cuda_toolkit.py` 提供 pip CUDA 的标准目录布局，链接现有 WSL Windows-driver `libcuda`，不安装 Linux NVIDIA 显示驱动。

重装优先使用交付的 lock 文件；ASR 已保存 HF revision 时重用该提交。主动更新可显式设置 `AI_INFER_MODEL_REVISION`，更新后须重新做质量和资源验收。

系统 FFmpeg 需要管理员安装：`apt-get install ffmpeg`。此机器可通过已授权的 Windows 用户运行 `wsl.exe -d Ubuntu-24.04 -u root --exec ...` 管理 Linux 服务；这不改变 Windows UAC 权限。

安装 systemd（在 WSL root shell 执行）：

```bash
/home/liuhuayaxi/ai-infer-node/scripts/install_systemd.sh
systemctl start ai-infer-gateway
systemctl stop ai-infer-gateway
systemctl restart ai-infer-gateway
systemctl status ai-infer-gateway
journalctl -u ai-infer-gateway --since today
```

服务以 `liuhuayaxi` 运行，异常自动重启，停止时先安全终止任务，systemd `KillMode=control-group` 兜底清理全部子进程。服务仅使用一个 uvicorn worker；SQLite 和文件锁阻止同数据目录重复启动。

## Windows 登录和开机

Windows 安装脚本已复制到 `%LOCALAPPDATA%\AIInferNode\install_autostart.ps1`。普通用户运行默认安装登录触发器，并用同一用户的 `wsl.exe ... sleep infinity` 保持 WSL。任务为 `AI Infer WSL Gateway`，重复实例策略 IgnoreNew；不使用 SYSTEM、不保存用户密码。

要添加“尚未登录时的开机触发器”，在拥有 WSL 发行版的 Windows 用户的**管理员 PowerShell**执行：

```powershell
powershell.exe -NoProfile -File "$env:LOCALAPPDATA\AIInferNode\install_autostart.ps1" -BootBeforeLogin
Get-ScheduledTask -TaskName 'AI Infer WSL Gateway'
Get-ScheduledTaskInfo -TaskName 'AI Infer WSL Gateway'
```

该选项使用 S4U 与实际 WSL 所属用户。必须实测它是否能在用户未登录时访问发行版。权限拒绝时保留已有登录任务，不绕过 UAC。脚本本身不重启 Windows 或关闭 WSL。

真实重启验收需要预先安排：Windows 重启后，不从 Mac 手动启动 WSL，检查任务状态、服务健康和空闲显存。配置存在、手动 Start-ScheduledTask 成功和真实开机成功是不同证据。

当前 `.wslconfig` 已有约 20 GB RAM 上限，未覆盖原配置。若宿主机仍因 Linux 文件缓存占用较高，可在安排 WSL 重启时审查 autoMemoryReclaim 与 swap；模型进程退出与宿主机页缓存回收是不同指标。

网关默认在模型进程退出后，对本节点模型权重文件调用 Linux `POSIX_FADV_DONTNEED`，向内核发出不再需要这些文件页的建议。`bytes_advised` 是建议范围，不能表示实际回收字节，也不能证明宿主机 Working Set 下降。Linux 上的 mincore 前后驻留页观测只适用于这些文件。不会删除权重、清空全局系统缓存或改变模型精度。可以设 `reclaim_model_file_cache=false` 保留文件缓存以加快冷启动；默认优先低空闲内存。Windows 的整个 WSL 虚拟机 Working Set 还包含其他服务和系统文件缓存，不能用网关 RSS 代替它。

节点私有 ASR/MinerU 虚拟环境中的大型 `.so` 依赖也参与上述建议回收；仅处理有 `pyvenv.cfg` 的环境，拒绝符号链接和环境外文件，不处理系统库。`reclaim_runtime_file_cache=false` 可单独关闭这部分。`model_file_cache_reclaim.by_scope` 分开记录模型权重与运行库的 mincore 前后驻留量，宿主回收结论仍须外部 Windows 同时间轴采样。该策略会增加下次任务的文件读取，保持模型、BF16 和 Advanced 档不变。

运行指纹包括网关/Worker/客户端 Python 源码摘要、环境包元数据与模型文件清单。科学图像分析在实际 Markdown 和结构化内容内标记 `MODEL_IMAGE_ANALYSIS_UNVERIFIED`；`raw-mineru.zip` 保存标注前的完整导出。该标注说明推断来源，不代表公式、符号或图表数值已经准确。

## API

所有接口需要 `Authorization: Bearer ...`；JSON 成功响应为 `{"ok":true,"data":...}`，错误为 `{"ok":false,"error":...}`。文件结果直接返回对应文件。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/v1/health` | 健康检查，不加载模型 |
| GET | `/v1/status` | 状态、队列、GPU、RAM、网关 RSS |
| POST | `/v1/jobs?kind=mineru&filename=课件.pdf&options={}` | 流式提交原始文件字节 |
| GET | `/v1/jobs/{id}` | 查询持久任务 |
| GET | `/v1/jobs/{id}/result?format=zip` | 下载 ZIP、md、json、txt、metadata |
| POST | `/v1/jobs/{id}/cancel` | 取消任务并清理进程组 |

上传为 `application/octet-stream`，请求体直接流式写盘，避免 multipart 框架先完整缓冲。必须提供 `Idempotency-Key`（8–128 位字母数字及 `._:-`）。同 key、文件哈希和参数返回同一任务；同 key 不同输入返回 409。客户端断开不取消已接收任务，轮询和重新连接始终使用同一 Job ID。提交响应丢失时，客户端只以同一个 key 重试。

默认最大文件 2 GiB，上传超时 1800 秒，任务超时 14400 秒，最多排队 32 个任务，历史保留 7 天。未完成上传被删除；崩溃后的 running/loading 任务明确标为 failed（已取消的为 canceled），queued 任务继续处理。已完成结果通过同文件系统目录重命名原子发布。结果下载只接受固定格式名，没有任意文件路径接口。

PDF/图像强制 Advanced；Office 等原生文档按官方支持规则使用 Flash。CPU 原生文档也经过当前串行队列，避免小内存设备并发峰值；当前实现没有延迟模型复用或独立 CPU 并行池。

## 输出和长音频

MinerU 用官方无状态 SDK `parse(..., page_range="all")` 和 `ParseResult.save()`，一次推理保存 Markdown、materialized Middle JSON、Structured Content、图片和 ZIP。额外 V1 content_list adapter 接入原 Mac PDF 打包器。远程生成的 `(AI).pdf` 保持原来的隐藏文字层、逐页正文和附加文件结构。

`(AI).pdf` 同时附加完整 `mineru-result.zip`；取出该 ZIP 后 Markdown 图片路径可直接解析。图片和原生 Office 输入使用 whole-document 范围，不传 PDF 专用 page_range 参数。

ASR 通过 FFmpeg 转为 16 kHz 单声道 PCM，检测静音，最大分段目标 25 秒、相邻段重叠 1 秒。较长静音在语言/话题切换处形成边界；分段始终推进到音频结束。一个完整录音任务只加载一次原生 Qwen3-ASR，连续处理所有段。原始分段文本与去重贡献同时保存在 JSON 中；TXT 合并文本。只有真实分段起止时间，没有 ForcedAligner 或逐词时间戳。

工作台上传/离线重转把剩余整段音频提交为一个远程任务，结果按真实分段接回原有归档、AI 精修和进度流程。实时麦克风和在线流尚未结束的录音只能按当前可用片段提交，每个远程片段任务独立加载/卸载。

远程 ASR 接收语言和专业词汇上下文，使用确定性原生生成；MLX 特有参数用于本机模式。远程冷启动和排队会继续轮询，不按一次同步 HTTP 推理超时处理。

## 检查、基准与回滚

WSL 示例：复制 `config.example.toml` 为 `config.toml`。smoke/benchmark 脚本识别 WSL 时会自动用服务本机地址和受保护令牌；Mac 则读取 `[client]` 配置。

```bash
scripts/smoke_test.sh --config config.toml
scripts/benchmark.sh --config config.toml --kind asr --input fixtures/mixed-5min.wav --output benchmark-result/asr
scripts/benchmark.sh --config config.toml --kind mineru --input fixtures/scanned-formula-table.pdf --output benchmark-result/pdf
venvs/gateway/bin/python -m unittest discover -s tests -v
```

独立 Worker 实测可用 `scripts/standalone.py`，它与服务共享 GPU 文件锁。详细指标写到输出目录 `benchmark.json`。真实推理测试和显存采样不能由 mock 测试替代。

回滚先将 Mac 两项模式切回 local，再停止远程服务。`scripts/uninstall.sh` 仅停止、禁用并移除 systemd 单元，保留模型、凭据、历史任务和源码。Windows 可 `Unregister-ScheduledTask -TaskName 'AI Infer WSL Gateway' -Confirm:$false`；需要撤销 Tailnet 入口时，仅关闭 Serve 的 TCP 8765 配置，保留其他端口。Mac 的历史 SSH 隧道已禁用。项目源码和旧 MinerU 配置的备份位于 `~/.qwenasr/backups/ai-infer-20261008-203532`；本轮入口和集成修改备份位于 `~/.qwenasr/backups/native-tailnet-stability-20261009-002231`。

模型输入只发送到你选择的 Windows 节点；模型下载来源不会收到 PDF 或录音。未添加云端推理、公开端口或全局 SSH 凭据修改。


资源状态 `gpu_release_state` 使用 verified / unknown / failed。监测缺失或清理失败会持久隔离队列，health 返回 degraded/503；重新核对本节点进程组和显存连续三次后才自动恢复。基线缺失不会自动核准。`/v1/status.resources` 分开返回 Linux MemAvailable/AnonPages/Cached/SReclaimable、节点 RSS/PSS 和固定只读 PowerShell VmmemWSL 探测；读取不到的值保持 unknown。

客户端先写 durable operation receipt，再提交网络请求。继续/重试保持输入摘要、参数、配置和同一 key/Job ID；不确定提交或 completed 下载失败优先恢复同一结果，不触发 Preferred 重算。`GET /v1/operations/{key}` 受原鉴权保护。新识别需创建新操作。完整下载先写 `.part`，校验 ZIP 后原子改名。

若首次监测完全缺失而没有基线，`POST /v1/resources/recheck` 是受鉴权的操作员校准动作：必须连续三次观察到无本节点进程且显存低于 idle_gpu_ceiling_mib（默认 128 MiB），再经监督器三次复核才能恢复。不会杀其他 GPU 服务；不满足条件保持隔离。该动作不能修复存储异常。
