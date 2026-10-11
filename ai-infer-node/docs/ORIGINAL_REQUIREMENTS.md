# Codex 执行任务：构建 Windows WSL2 按需加载 AI 推理服务，并集成 Mac 本地回退

## 一、任务背景

我有两台电脑：

**Mac：** 日常工作设备，目前已有本地文档解析和语音识别工作流，需要保留原有功能。

**Windows：** 作为备用远程 AI 推理服务器，配置如下：

- CPU：AMD Ryzen AI 9 HX 370
- iGPU：AMD Radeon 890M
- GPU：NVIDIA RTX 4060 Laptop，8GB 独立显存
- RAM：32GB
- 系统：Windows + WSL2 Linux

Mac 已经配置 SSH 别名：

`ssh efinity`

请直接使用此命令访问远程 WSL2。SSH 私钥保存在 Mac 本地，禁止读取、复制、上传或修改私钥文件。

本任务的目标是完成一个能够实际工作的部署，而不是只生成操作教程。

## 二、总体目标

实现一套远程 AI 推理系统，具备以下功能：

1. 在 WSL2 内部署最新稳定版 MinerU，用于高质量文档解析。
2. 在 RTX 4060 上部署 Qwen3-ASR 1.7B，用于高质量语音识别。
3. 在 WSL2 中运行长期驻留的轻量级 API 网关。
4. 系统空闲时不加载任何大型推理模型，不占用额外 GPU 模型显存。
5. 收到任务后按需加载模型，处理结束后卸载模型并释放资源。
6. Windows 启动后自动拉起 WSL2 和 API 网关。
7. Mac 能通过 Tailscale 网络或现有 SSH 连接访问服务。
8. Mac 保留原有本地推理方案，可以在配置中选择使用本地或远程推理。
9. 远程不可用时，可以自动回退到现有 Mac 本地模型。
10. 所有安装、配置、服务管理、故障恢复和测试过程可重复执行。

**重要约束：优先保证正确性和解析质量，其次保证低空闲内存，最后才考虑推理速度。**

不得通过降低模型版本、解析质量档位、默认量化模型或使用云端 API 来绕过性能问题。

## 三、第一阶段：环境检查与现有项目保护

首先在 Mac 执行：

`ssh -o BatchMode=yes -o ConnectTimeout=10 efinity 'uname -a; whoami'`

然后检查：

- WSL 发行版、版本、Linux 内核和 Python 版本
- `nvidia-smi`、CUDA 驱动、显存容量
- PyTorch 是否可检测到 RTX 4060
- 系统 RAM、磁盘可用空间
- systemd 是否可用
- WSL 内是否已有 MinerU 或 Qwen-ASR
- Windows 是否安装 Tailscale、任务计划程序是否可用
- 当前 SSH 是否通过 Tailscale 网络连接
- Mac 目前的 MinerU 和 ASR 调用代码、配置文件及输出格式

检查当前 Mac 项目使用的本地解析后端，记录接口和配置结构。

禁止直接覆盖已有模型环境、删除旧文件或破坏当前工作流。所有新增依赖使用隔离环境。

WSL2 不要安装 NVIDIA Linux 显示驱动，使用 Windows 驱动提供的 CUDA 支持。

先生成环境检查报告，再开始部署。

## 四、第二阶段：部署 MinerU

### 4.1 版本要求

目标版本：`mineru==4.0.10`。

这是 2026-10-08 已核实的最新正式版本。部署前再次检查官方稳定发行版，如果有更新，先确认兼容性，不要使用未经验证的开发分支。

参考：

- https://github.com/opendatalab/MinerU
- https://pypi.org/project/mineru/4.0.10/
- https://mineru.atomgit.com/

### 4.2 解析质量

PDF、图片解析默认选择：

`--tier advanced`

这是 MinerU 4.0 的最高质量档位。

不要继续使用 3.x 的 `pipeline`、`vlm` 或 `hybrid` 旧配置。

对于官方限定使用 Flash 解析的 Office 等原生文档格式，应按照 4.0 的实际格式支持规则处理，不要错误地强制指定 Advanced。

### 4.3 推理引擎

首先尝试：

- MinerU 4.0.10
- PyTorch 小模型
- vLLM VLM 引擎
- RTX 4060
- Advanced 质量档

在专用环境安装 `mineru[full]==4.0.10`，并验证实际使用的模型和运行时。

参考官方模型准备命令：

`mineru-kit models download --tier standard --small-backend torch --vlm-engine vllm --source modelscope`

`mineru-kit models verify --tier standard --small-backend torch --vlm-engine vllm`

注意：Advanced 和 Standard 共用模型集，因此模型下载使用 Standard，实际解析请求使用 Advanced。

优先使用已验证的最新版官方模型权重，不要混用 MinerU 3.x 模型。

如果 Advanced + vLLM 在 8GB 显存下 OOM：

1. 检查显存占用与 VLM 运行参数。
2. 尝试降低并发、按页或小批量分页处理。
3. 评估 PyTorch + llama.cpp 等替代引擎。
4. 必要时测试 ONNX + llama.cpp 的 CPU Advanced 方案。
5. 记录不同引擎的速度、精度与显存差异。

**不允许为了使部署通过而悄悄将 Advanced 降为 Standard、Basic 或 Flash。**

当更换引擎可能影响模型精度时，必须记录，并进行解析质量对照。

### 4.4 输出要求

优先使用 MinerU 4.0 官方无状态接口：

`mineru-kit parse`

建议通过单独的工作进程完成解析。可以先验证：

`mineru-kit parse input.pdf -o result.zip --tier advanced --format zip`

最终需要返回：

- 完整 Markdown
- Middle JSON 或 Structured Content
- 公式、表格、图片等素材
- 完整 ZIP 结果包
- 解析版本、档位、耗时和实际推理引擎

不要只返回纯文本。必须保持公式、表格及图片之间的引用关系。

优先基于官方 `ParseResult.save()` 或 ZIP 输出构建服务，避免重复推理生成不同格式。

## 五、第三阶段：部署 Qwen3-ASR

### 5.1 模型选择

使用官方最高参数规模的 Qwen3-ASR 模型：

`Qwen/Qwen3-ASR-1.7B-hf`

官方模型：

https://huggingface.co/Qwen/Qwen3-ASR-1.7B-hf

优先使用 Transformers 原生实现（至少支持该模型的 5.13.0 版本），而非自行重写模型架构。

### 5.2 GPU 要求

- 使用 RTX 4060。
- 首选 BF16 精度。
- 单路推理，初始 batch size 为 1。
- 使用推理模式，不加载训练组件。
- 禁止默认 INT4/INT8 量化。
- 不允许静默将部分模型权重卸载到 CPU。
- 验证全部必要模型计算确实可以在 GPU 上完成。
- 检查实际峰值显存，确保推理过程中无 OOM。

模型启动仅发生在收到真实推理任务之后。

先验证短音频，再测试长音频。

### 5.3 长录音支持

需要支持日常课程录音，例如 30–120 分钟的音频。

处理流程：

1. 接收原始音频。
2. 使用 FFmpeg 标准化格式。
3. 转换为模型适用的采样率和声道。
4. 使用 VAD 或静音检测分段。
5. 以约 20–30 秒作为初始分段目标，后续根据显存和准确率调整。
6. 为分段保留时间偏移和适当重叠。
7. 在一个任务中加载一次 Qwen3-ASR，连续处理所有分段。
8. 合并文本，处理边界重复和遗漏。
9. 输出完整文字与分段元数据。

需要支持中文、英文及中英混合语音，并保留可选的语言提示与专业术语提示。

默认不加载 ForcedAligner。如果需要逐词时间戳，将其作为独立的可选推理阶段，不能直接同时占满 8GB 显存。

没有经过对齐模型计算时，只能标注真实的分段时间信息，不能伪造精确的逐词时间戳。

需要特别回归测试超过 8 秒的音频，避免旧版 Transformers 后端的注意力窗口兼容性问题。

## 六、第四阶段：实现按需加载与显存调度

这是本任务最重要的部分。

### 6.1 常驻网关

实现一个轻量级 Python FastAPI 服务，监听：

`127.0.0.1:8765`

该服务负责：

- 接收任务
- 保存输入文件
- 维护任务队列
- 调用推理工作进程
- 查询状态
- 返回结果
- 清理文件和工作进程
- 记录日志与资源指标

**网关启动时不得导入 torch、transformers、vllm、mineru 等大型推理组件。**

### 6.2 模型生命周期

预期状态：

`IDLE → QUEUED → LOADING → RUNNING → SAVING → UNLOADING → IDLE`

启动时：

- 只运行 FastAPI 网关。
- 不存在 MinerU GPU 工作进程。
- 不存在 Qwen-ASR GPU 工作进程。
- 不预加载任何模型权重。
- 模型已经提前下载到本地磁盘，避免第一次调用时临时联网下载。

收到任务：

- 选择对应 Worker。
- 获取 GPU 独占锁。
- 启动独立子进程。
- 加载对应模型。
- 完成推理。
- 保存结果。
- 关闭子进程及其子进程组。
- 释放 GPU 独占锁。

必须通过进程退出真正释放显存，而不能仅依赖 `torch.cuda.empty_cache()`。

### 6.3 GPU 独占调度

RTX 4060 只有 8GB 显存。

默认设置：

`max_concurrent_gpu_jobs = 1`

MinerU 和 Qwen-ASR 共享同一 GPU 任务队列。

如果正在执行 ASR，又收到 MinerU 任务，后者应进入等待队列，而不是立即加载第二套模型。

同理，MinerU 工作期间的 ASR 任务也应排队。

对于 CPU 解析任务，可以独立执行，但应限制系统内存与 CPU 并发。

Worker 异常、超时、取消时，必须清理子进程组并释放锁，避免 GPU 永久被占用。

### 6.4 空闲内存策略

默认：

`model_idle_timeout_seconds = 0`

即每个任务完成后立即卸载模型。

可以提供可配置的延迟卸载选项，例如 60 秒或 120 秒，但默认关闭。

WSL2 可根据宿主机内存情况考虑：

- 20GB 左右的 WSL RAM 上限，实际由检测结果决定
- 自动内存回收
- 合理的 swap
- 将模型缓存存放在 WSL Linux 文件系统内，而不是反复跨 `/mnt/c` 读取

不要用永久驻留的 `vllm serve` 或 MinerU 高质量推理服务来代替按需 Worker。

### 6.5 任务持久化

采用 SQLite 或同等级别的轻量持久化方案。

需要支持：

- Job ID
- queued / loading / running / completed / failed / canceled
- 崩溃恢复
- 任务幂等
- 错误信息
- 输入输出路径
- 原子化结果保存
- 自动清理历史临时文件

长任务不得依赖单个同步 HTTP 请求一直等待。

建议采用“提交任务 → 返回 Job ID → 轮询状态 → 下载结果”的模式。

## 七、第五阶段：API 设计

提供以下接口：

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/v1/health` | 网关健康状态，不加载模型 |
| GET | `/v1/status` | GPU、内存、队列及 Worker 状态 |
| POST | `/v1/jobs` | 上传文件并创建 PDF 或 ASR 任务 |
| GET | `/v1/jobs/{id}` | 获取任务状态和进度 |
| GET | `/v1/jobs/{id}/result` | 下载解析结果 |
| POST | `/v1/jobs/{id}/cancel` | 取消任务 |

要求：

- 使用统一 JSON 响应。
- 完成任务后可以下载 ZIP、Markdown、JSON 或 TXT。
- 不因客户端断线而丢失已经提交的任务。
- 提供请求超时、任务超时和最大上传大小设置。
- 文件上传使用流式写盘，避免整个文件先读入 RAM。
- 使用 Bearer Token 鉴权，密钥放在环境变量或受保护的配置文件中。
- 不公开暴露推理端口。
- 禁止任意路径读取、目录穿越和未授权文件下载。
- API 日志不输出完整录音、PDF 正文或鉴权密钥。

## 八、第六阶段：后台运行与 Windows 开机自启动

### 8.1 systemd

检查并按需启用 WSL2 systemd：

`/etc/wsl.conf` 中应包含：

`[boot] systemd=true`

创建：

`ai-infer-gateway.service`

要求：

- 自动启动。
- 异常自动重启。
- 使用非 root 用户运行。
- 统一管理日志。
- 网关进程退出后自动恢复。
- 停止服务时安全处理任务。
- 不在服务启动时加载模型。

需要提供：

`systemctl start ai-infer-gateway`

`systemctl stop ai-infer-gateway`

`systemctl restart ai-infer-gateway`

`systemctl status ai-infer-gateway`

### 8.2 Windows 启动

注意：systemd 本身不能保证 WSL2 虚拟机一直存活。

请创建 Windows 任务计划程序启动方案：

1. Windows 启动时触发。
2. 使用拥有目标 WSL 发行版的 Windows 用户身份运行。
3. 启动正确的 WSL 发行版。
4. 必要时保持一个极低资源消耗的 WSL 前台 keepalive 进程，避免实例因为空闲退出。
5. 配置失败重试。
6. 不依赖 Mac 保持开机。
7. 不使用 SYSTEM 账户错误地启动另一个 Windows 用户的 WSL 环境。

可以研究使用 `wsl.exe -d <发行版名称> --exec /usr/bin/sleep infinity` 保持 WSL 实例运行，但必须实测。

应分别测试：

- Windows 登录后的自动启动。
- Windows 重启后的自动启动。
- 无人操作时服务是否持续运行。
- 如果需要在用户尚未登录时启动，验证任务计划程序的实际执行权限。

如果 Windows 权限不足，先生成 PowerShell 安装脚本和手动执行说明，不要尝试绕过 UAC。

不要在当前 SSH 会话内未经安排直接重启 Windows 或执行可能使连接中断的 `wsl --shutdown`。

## 九、第七阶段：Mac 连接与网络访问

优先沿用当前连接：

`ssh efinity`

MVP 阶段推荐 SSH 本地端口转发：

`ssh -N -L 18765:127.0.0.1:8765 efinity`

Mac 可以通过以下地址访问：

`http://127.0.0.1:18765`

先验证这一方案。它不需要公开 WSL 的推理端口，也无需重新分发 SSH 私钥。

之后如果确认 Tailscale 已在 Windows 正常运行，可进一步测试 Tailscale Serve，将 Windows 本机可访问的 WSL 服务安全映射为 Tailnet 内部 HTTPS 端点。

不使用 Tailscale Funnel，不开放公网端口。

如果需要在 Mac 后台维持 SSH 隧道，可以配置 launchd 自动重连，但必须确保不会重复创建冲突隧道。

## 十、第八阶段：Mac 本地与远程推理切换

**这是备用方案，必须保持原来的 Mac 本地解析能力不受影响。**

首先检查 Mac 上现有的代码、项目结构和配置文件，找出本地 MinerU 和 ASR 调用方式。

不要假设 Mac 与远程服务器使用相同模型版本。

增加统一 Provider 接口：

- `LocalMinerUProvider`
- `RemoteMinerUProvider`
- `LocalASRProvider`
- `RemoteASRProvider`

两种 Provider 应向上层返回兼容的结果结构。

### 10.1 配置模式

每种能力独立支持三个模式：

| 模式 | 行为 |
|---|---|
| `local` | 始终使用 Mac 原有本地模型 |
| `remote_preferred` | 优先 Windows，失败则回退 Mac |
| `remote_only` | 只使用 Windows，供调试和性能测试 |

默认：

`mineru.provider = local`

`asr.provider = local`

远程属于可选备用配置。

在 `remote_preferred` 模式下：

1. 检测远程服务是否可连接。
2. 如果可用，发送任务。
3. 如果连接失败、模型加载失败、远程任务失败或发生超时，调用 Mac 本地模型。
4. 远程冷启动耗时较长不应立即被当作故障。
5. 文件不应因回退而丢失。
6. 不应因为网络重试重复生成多个远程任务。
7. 返回结果必须标注实际使用的 Provider，以及是否发生回退。

如果现有 Mac 程序有设置界面，就在设置中加入选择项；如果目前是 CLI 或 Python 工作流，则提供 TOML/YAML 配置和命令行切换方式。

不要为了添加 Provider 而重构无关业务逻辑。

### 10.2 数据一致性

确保本地与远程模式的输出能够被现有下游代码读取。

特别注意：

- Markdown 图片引用
- 公式和表格
- 中文文件名
- ASR 文本
- 分段时间戳
- 文件编码
- 相对路径
- JSON 字段兼容性

如果 MinerU 4.0 新的 Middle JSON 与 Mac 现有版本不兼容，需要添加 Adapter 转换层，而不是直接修改旧数据。

## 十一、第九阶段：测试与验收

必须完成以下测试。

### A. 冷启动与空闲内存

- [ ] Windows/WSL 启动后 API 网关可以访问。
- [ ] 没有 MinerU GPU Worker。
- [ ] 没有 Qwen-ASR GPU Worker。
- [ ] GPU 显存占用不因本服务常驻而明显增加。
- [ ] 记录网关 RSS 和 WSL2 空闲内存。
- [ ] 任务完成后模型进程退出，显存恢复到任务前基线附近。

### B. MinerU

- [ ] 能解析可选中文本 PDF。
- [ ] 能解析扫描版 PDF。
- [ ] 能解析包含公式和表格的论文或课件。
- [ ] 采用 Advanced 档位。
- [ ] 全部页数被处理，不截断到前 10 页。
- [ ] Markdown、JSON 和图片素材完整。
- [ ] 图片路径和公式引用正确。
- [ ] 记录总耗时与峰值显存。

### C. Qwen3-ASR

- [ ] BF16 1.7B 模型在 RTX 4060 上成功加载。
- [ ] 中文语音识别正常。
- [ ] 英文及中英混合语音识别正常。
- [ ] 5 分钟测试音频完整处理。
- [ ] 长音频分段合并不明显丢词。
- [ ] 不出现显存 OOM。
- [ ] 记录实时因子 RTF、加载时间和峰值显存。

### D. 并发与异常

- [ ] 同时提交 PDF 和 ASR 任务时，GPU 任务正确排队。
- [ ] 杀死 Worker 后，队列锁能恢复。
- [ ] 超时或取消任务后没有遗留 GPU 进程。
- [ ] Windows 网络中断后可以恢复。
- [ ] 服务重启后历史任务状态不会丢失。
- [ ] 恶意路径和未授权 API 请求会被拒绝。

### E. Mac 回退

- [ ] Local 模式原有行为完全正常。
- [ ] Remote Preferred 模式能真正远程处理。
- [ ] Remote Only 模式不会悄悄调用本地模型。
- [ ] 远程关闭或不可连接时，Remote Preferred 自动回退本地。
- [ ] 回退后输出格式兼容，文件无损坏。
- [ ] 可以随时切回原有配置。

### F. 开机自启

- [ ] systemd 服务正常启用。
- [ ] Windows 任务计划程序正确配置。
- [ ] 重启 Windows 后，无需在 Mac 手动 SSH 启动服务。
- [ ] 如果无法测试真正的开机自启，不得将这一项标记为通过。

## 十二、交付文件

至少交付：

- `README.md`：完整部署、使用说明。
- `config.example.toml`：服务器和 Mac 客户端配置示例。
- `server/`：FastAPI 轻量网关。
- `workers/mineru_worker.py`：按需 MinerU 工作进程。
- `workers/asr_worker.py`：按需 Qwen-ASR 工作进程。
- `client/`：Mac 远程 Provider 或集成 Adapter。
- `scripts/install_wsl.sh`：WSL 安装与初始化。
- `scripts/install_autostart.ps1`：Windows 自启动配置。
- `scripts/smoke_test.sh`：基础功能测试。
- `scripts/benchmark.sh`：性能与内存测试。
- `scripts/uninstall.sh`：安全停止和移除服务。
- `docs/DEPLOYMENT_REPORT.md`：部署结果。
- `docs/TEST_REPORT.md`：真实测试记录。

推荐使用独立目录管理项目和虚拟环境，避免修改已有 WSL 全局 Python 环境。

## 十三、执行顺序与最终报告

请按以下顺序执行：

1. 检查 Mac 和 WSL 环境，备份相关配置。
2. 安装隔离环境和模型依赖。
3. 提前下载并验证模型权重。
4. 独立测试 MinerU Advanced。
5. 独立测试 Qwen3-ASR 1.7B BF16。
6. 实现轻量网关和 GPU 独占队列。
7. 验证零模型常驻和自动显存释放。
8. 配置 systemd。
9. 配置 Windows 自动启动。
10. 建立 Mac 到 WSL 的安全连接。
11. 集成 Mac Provider 设置和本地回退。
12. 进行完整的端到端测试。
13. 输出测试结果和使用说明。

**执行原则：**

- 可以实际操作就实际操作，不要只写方案。
- 不要未经验证就宣称部署成功。
- 如果某一步受权限、网络或硬件条件限制，保留已有成果并明确记录阻碍。
- 不要使用伪造的测试结果。
- 不要删除 Mac 原有本地推理程序。
- 不要自动更改 SSH 凭据。
- 不要为了提高稳定性而静默降低质量档位。
- 不要启动需要长期占用 GPU 显存的常驻模型服务。
- 不要将用户的 PDF、音频上传到第三方云推理服务。

最终报告必须明确给出：

1. MinerU 实际版本、引擎、档位和成功率。
2. Qwen-ASR 实际版本、精度和所用 GPU。
3. 两个模型各自的加载耗时、推理耗时和峰值显存。
4. API 网关空闲内存占用。
5. GPU 推理完成后的显存释放情况。
6. Windows 开机自启验证结果。
7. Mac 本地、远程及自动回退的验证结果。
8. 剩余问题、解决建议和可回滚步骤。

最终目标不是让两个模型永久驻留，而是让这台 Windows 电脑成为一台**低空闲资源占用、高解析质量、可远程唤醒模型、具备可靠本地回退能力的 AI 推理节点**。


## 后续用户约束

仅使用 Tailscale 内网直连，不使用 SSH 隧道或普通 Wi-Fi 发布。连接稳定性修复后重新验收；整理最终代码，并提供给 Astra 的独立功能与代码审查提示词。
