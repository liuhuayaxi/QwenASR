# 实测验收：2026-10-08

**当前验收以 [FINAL_REVIEW_REPORT.md](FINAL_REVIEW_REPORT.md) 为准。2026-10-09 已改为 Windows 原生 Tailscale Serve，重新完成真实 12 页 PDF、扫描 PDF、5/30 分钟双语音频、共享队列、取消和完整 Mac 回归；本报告保存此前的实测与失败记录。**

10 月 8 日补充：已完成 Tailscale 直接端口调用；按用户选择停用了普通 Wi-Fi 发布和实验桥接，SSH 转发禁用；统一设置控件、任务侧栏及手机导航。详细证据见 `DIRECT_ACCESS_AND_UI_REPORT.md`。当时新增网络来源过滤后网关测试为 8 项。

本报告区分真实模型/部署测试、隔离测试和未执行项目。机器实测 JSON 在 `evidence/`，WSL 原始 Worker 日志与结果在 `~/ai-infer-node/benchmarks`，任务数据库在 `data/jobs.sqlite3`。

## A：空闲与资源

- PASS：systemd 运行 API，health 不预加载模型；独立 gateway venv 和网关测试确认没有 torch/transformers/vllm/mineru 导入。
- PASS：最终 IDLE、worker_pid=null，无模型进程，GPU 基线/完成后 43 MiB。
- PASS：网关 RSS 约 49 MiB，WSL Linux used 约 1 GiB，记录了整个 Windows WSL VM Working Set；模型文件页缓存回收后观测到约 1.8 GB 的宿主机采样。
- PASS：每项任务采用独立进程退出卸载；强杀和取消也等待显存稳定后再释放锁。
- 未执行：真正 Windows/WSL 冷开机后首次 API 访问。

## B：MinerU

- PASS：12 页可选文本的中英 PDF，Advanced、Torch + vLLM，输出 Middle JSON 12 页，未截断前 10 页。
- PASS：2 页无文字层扫描 PDF，包含中文、英文、3 个公式、三列表格、波形图。
- PASS：LaTeX 包含 E=mc²、hν、λ、定积分；表格列关系正确；两个 Markdown 图片路径都存在。扫描件结果 10 个素材文件，原 12 页样本 48 个素材。
- PASS：官方 ParseResult.save 单次解析输出 Markdown、Middle JSON、Structured Content、素材；ZIP 下载与 materialized JSON 适配。
- PASS：记录版本 4.0.10、Advanced、实际 vLLM 0.28.0 BF16/无量化、耗时和峰值 4817 MiB。
- PASS：另有 1 页 PNG 图像 Advanced 实测，页数 1，实际 vLLM，峰值 4817 MiB、完成后 43 MiB，完整记录见 `evidence/image-advanced.json`。
- PASS：原生 DOCX 通过 API 使用官方 Flash 档位，中文和英文正文正确，VLM 未加载，GPU 始终 43 MiB；没有强制套用 Advanced。见 `evidence/native-office.json`。
- 质量边界：样本为自行构造、视觉检查过的课件。未对真实论文/课程语料做统计准确率比较；没有 OOM，因而没有改用低精度或 CPU 引擎。

## C：Qwen3-ASR

- PASS：1.7B HF 权重 BF16，全部参数在 RTX 4060，无 CPU offload/量化，官方权重 SHA256 匹配。
- PASS：官方中文样本识别；英文长片段与 19 秒混合样本回归，超过 8 秒无注意力窗口错误。
- PASS：最终双语样本保留开头中文和后续英文，真实分段为 0–4.6491 秒和 3.6491–19.2552 秒。
- PASS：5 分钟音频完整处理，中文和英文锚点存在，最后段到 300 秒；RTF 约 0.087。
- PASS：30 分钟音频完整处理，一次模型加载，188 个分段，最后到 1800 秒；94 次中文和 93 次英文完整句锚点，RTF 约 0.083，无 OOM，峰值 4324 MiB。
- PASS：保留原始段文本、边界去重贡献、真实分段时间，未加载 ForcedAligner、未伪造逐词时间。
- 未执行：120 分钟和真实课程长录音准确率/丢词率量化。首版未按长静音切段时偏英文、遗漏中文，该版结果仅保留为失败/性能证据，不计入混合语言质量通过。

## D：并发与异常

- PASS：真实同时提交 ASR 和 PDF，后者 queued；两种 Worker 共享同一 GPU 锁。
- PASS：vLLM 引擎启动后 SIGKILL MinerU 主 Worker，failed=WORKER_FAILED；GPU 还原至 43 MiB后才启动排队 ASR，下一项 completed。
- PASS：取消运行中的真实 ASR，终态 canceled，显存 43 MiB，没有遗留 Worker。
- PASS：临时将任务超时设为 12 秒，真实 5 分钟任务 failed=JOB_TIMEOUT；清理完成，随后正式配置恢复 14400 秒。
- PASS：真实 SIGKILL 网关，systemd 自动恢复；进行中任务 failed=GATEWAY_RESTARTED，queued ASR 自动完成，历史完成/失败记录保留。
- PASS：API 未授权 401；无效文件路径、格式和下载路径拒绝；同幂等 key 同输入返回同 Job ID，不同输入 409；最大上传限制与取消清理测试。
- 历史阶段 PASS：原 SSH 转发曾验证自动重连；当前方案已停用该隧道，改为 Tailscale 内网直接端口访问。
- 未执行：Windows 网络适配器/Tailscale 服务真正中断。没有把 SSH 转发故障当作 Windows 全机网络故障。

## E：Mac Provider 与原输出

- PASS：真实 local MLX ASR，中文转写正确，现有 `(text, language, elapsed)` 接口保持。
- PASS：真实 remote_only ASR，故意使用不存在的本地模型目录，仍由 Windows 成功完成，本机 Worker 未启动。
- PASS：真实 remote_preferred 成功由 Windows 处理，本机 Worker 未启动。
- PASS：remote_preferred 连接到关闭的本机端口，实际加载 MLX 并识别成功，记录 local/fallback=true。
- PASS：remote_only 同样断线，返回 ConnectError，本机 Worker 未启动。
- PASS：真实 PDF remote_only 与远程断线后 local 回退，中文文件名、原始文件 SHA256 不变、原两页 + 封面，原三个附加文件及完整结果 ZIP 均正确，ZIP 中图片引用完整。
- PASS：在隔离 Mac 资料库执行原 `batch_transcription_stream`，一个远程 Job ID、2 个双语分段、AAC 和转写归档完成，asr_seconds=2.83，本机模型目录不存在且 Worker 未启动。
- PASS：课程资料转换在本机 MinerU 不可用时执行 remote_only，生成 3 页 AI 友好 PDF，保存实际远程 Provider 和完整 ZIP。见 `evidence/course-remote.json`。
- PASS：取消隔离资料库中的真实离线录音任务，取消传递到 Windows，约 3 秒后终态 canceled；源音频保留，本机 Worker 未加载。见 `evidence/macos-batch-cancel.json`。
- PASS：真实工作台设置界面显示两项独立选择器，保存默认 local/local；最终 API 再确认两项均 local，当前工作台 idle。

## F：自启动

- PASS：Linux systemd enabled/active，非 root，真实异常重启与队列恢复。
- 已配置/手动启动：Windows 当前用户 AtLogon 任务，Running，keepalive sleep infinity 进程存在。
- 未通过配置：用户未登录 AtStartup/S4U，普通权限注册被 0x80070005 拒绝，管理员脚本已生成并复制。
- 未执行：Windows 真正重启、重新登录触发、用户未登录可访问 API。严格不勾选开机验收通过。

## 自动测试与记录修订

Mac：59 项相关回归/Provider/存储/调度/PDF 测试通过；WSL：7 项网关和分段测试通过。网关单元测试使用明确的 fake Worker，不证明模型能力；上述独立 Worker/API/Provider 测试才是真实推理证据。

PASS：真实节点重复执行完整 install_wsl.sh，exit=0，模型重新验证通过，配置/凭据复用。真实 smoke_test.sh 返回 healthy、IDLE、worker_pid=null、GPU 43 MiB。

端到端采样过程中另外一个授权 Mac 客户端正在执行任务，第一次 final_idle 断言因此不满足。这不是模型常驻；交付证据中的 final_idle 明确改为全部授权客户端任务结束后的真实采样，并记录注释。首次强杀测试发现驱动显存释放延迟，修复后重做了真实强杀与调度验证。

未隐藏安装/初始化失败：解释器链接错误、nvcc 路径、CUDA 13.4/13.0 不兼容、lib64 链接布局和初版静音重叠推进问题均已修正，WSL 的旧 benchmark/log 仍保留。没有把失败尝试计入成功率。
