# 给 Astra 的独立审查提示词

请独立审查并验收这个项目的整个功能与代码质量。不要相信前一个代理的“已完成”结论；报告只能帮助找到证据，不能替代你自己的源码检查、进程检查和真实请求。

项目根目录：`/Users/liuhuayaxi/AIcreatedapplication/QwenASR`。新增节点源码：`ai-infer-node/`；真实 WSL 部署：`/home/liuhuayaxi/ai-infer-node`。原始需求及后续约束：`ai-infer-node/docs/ORIGINAL_REQUIREMENTS.md`。最终说明以 `ai-infer-node/docs/FINAL_REVIEW_REPORT.md` 和当前 README 为准；旧 evidence 有历史失败和废弃方案，不能混为当前通过项。

目标是 Windows RTX 4060 Laptop 8 GB / WSL2 的按需高质量推理节点，支持 MinerU 4.0.10 Advanced 和官方 Qwen3-ASR-1.7B-hf 全 GPU BF16。空闲只有轻网关，无模型常驻；每个任务独立进程退出卸载，PDF/ASR 共用一个 GPU 独占队列。Mac 原有 MinerU/MLX 能力保留，每项能力支持 local、remote_preferred、remote_only，默认 local。长录音一个离线任务只加载一次模型，保留真实分段时间，不能伪造逐词时间。

当前业务入口应该是 `http://100.81.62.69:8765`，即与 Windows Syncthing 相同的原生 Tailscale 节点。Windows 官方 `tailscale serve --bg --tcp=8765 tcp://127.0.0.1:8765` 转发到 WSL 后端 `127.0.0.1:8765`。不应依赖 `100.81.168.99` 的 WSL Tailscale 路径，不使用 SSH 隧道、Node 桥接、LAN/public 端口或 Funnel。`ssh efinity` 仅是管理入口，可能因 WSL 自身网络不稳定间歇失效；这时先独立测试正式 API，不能将管理失败等同于推理失败。

首先检查 `git status`、现有 AGENTS.md、进程和任务状态。工作区可能包含用户其他任务的修改；保留它们，不能 reset/clean 或覆盖。发现正在识别、录音或同步的用户任务时，保留运行状态，未经安排不得重启工作台、Windows、WSL 或停止 Tailscale。禁止读取、复制、上传、修改 SSH 私钥。鉴权 Token 只可从受保护文件用于该节点请求，不可输出、存入普通源码/报告或上传第三方。测试输入用公开或合成文件；测试不能发送实际课程/录音到第三方云服务，不改变 Syncthing/Clash 全局配置，不绕过 UAC。

请逐项检查：

1. **实际版本和模型质量。** 独立确认最新稳定 MinerU 发行版、两台机器的实际 Python 环境、4.0 API、Advanced 档位、官方权重模型集。区分软件版本与模型仓库名字；Mac 原有 llama.cpp 的 GGUF Q8_0 与 Windows BF16 vLLM 是不同执行方案，不能把它们都称为 BF16。检查所有 PDF 页面、公式、表格、图片素材及引用，尤其超过 10 页和扫描件；Office 原生文档应按官方 Flash 规则处理。检查实际 GPU、精度和 CPU offload，不能以导入 torch 成功当作真实推理通过。
2. **任务生命周期和资源。** 网关启动是否避免 torch/transformers/vllm/mineru 导入？任务从 queued/loading/running/saving/unloading 到终态是否正确？跨能力/进程 GPU 锁是否有效？Worker 成功、崩溃、超时、取消、网关崩溃后是否清掉所有子进程组，显存是否真实回归基线？文件页缓存回收是否仅针对自己的权重？`nvidia-smi` 不可用时有没有错误宣称验证通过？如果清理失败，是否仍可能提前调度下一套模型？
3. **持久化、幂等和输出。** SQLite 崩溃恢复、历史任务、取消竞态、排队容量、原子保存和过期清理是否安全？响应丢失、提交重试、轮询中断是否保留同一个 Idempotency-Key/Job ID、文件内容和参数，避免重复推理？断线恢复窗口与模型冷启动超时是否区分？ZIP、Markdown、materialized Middle JSON、Structured Content、图片和资源指标是否来自同一次推理、可离线读取？保存/压缩中取消的竞态是否处理？
4. **输入与 API 安全。** 全接口鉴权、仅 Tailnet 公布、后端 loopback、实际 TCP 来源检查、转发头信任是否正确？检查路径穿越、绝对路径、ZIP symlink/爆炸、扩展名、请求体长度、上传超时、最大文件和请求断线。错误/日志是否可能输出 Token、专业提示词、正文或录音内容？是否存在任意路径下载或端口过度暴露？
5. **连接稳定性。** Windows Serve 是否真实持久化、保留其他 Serve 配置、不启用 Funnel？重启网关、重新执行 Serve 安装器、临时 WSL Tailscale 路径失败时，正式 Windows API 是否继续工作？恢复测试必须先安排可靠恢复手段，不能把机器留在失联状态。检查 Windows keepalive、任务所属用户与 WSL systemd；未真正测试 Windows 重启/用户尚未登录启动时，必须标未验证。
6. **Mac 兼容和回退。** 检查 ModelManager、整段上传/重转、实时片段、PDFJobs、课程资料转换和任务中心。local 行为是否保持；remote_only 是否绝不静默回退；Preferred 在连接/加载/任务失败后是否保留原输入并真实回退；取消是否传到远程任务；元数据是否记录实际 Provider/fallback。新/排队/重试任务是否保存配置快照，切换 Provider 是否导致错误缓存复用？中文文件名、图片相对路径、编码、ASR 分段覆盖和去重是否正确？隐藏文字层与嵌入完整 ZIP 能否被下游读取？
7. **长音频。** FFmpeg、16 kHz 单声道、静音分段、重叠推进、最大 tokens、超过 8 秒、混合语言切换和 30–120 分钟内存是否正确？审查静音边界可能无限重复、去重误删正常重复、最终短段漏掉、生成达到上限却仍标完成等问题。用真实内容核对质量，不能只看音频秒数。30 分钟重复公开样本不等于真实课程准确率或 120 分钟验收。
8. **UI。** 实际打开工作台、任务中心、课程资料库和设置，在深浅主题及窄屏检查按钮、输入框、展开行、说明文字、状态信息、侧栏、导航和溢出。确认任务信息能说明配置/实际 Provider、MinerU 版本/档位。测试连接不应加载模型或改写设置；不能用静态截图替代状态检查。

至少运行以下适当检查，并解释哪些是真实运行、哪些是隔离/mock 测试：

```bash
cd /Users/liuhuayaxi/AIcreatedapplication/QwenASR
.venv/bin/python scripts/check_tests.py
node tests/test_ai_settings.cjs
node tests/test_session_controls.cjs
node --check static/js/inference-settings.js
node --check static/js/tasks.js
git diff --check
# 在可用的 WSL 管理连接中：
cd /home/liuhuayaxi/ai-infer-node
venvs/gateway/bin/python -m unittest discover -s tests -q
scripts/smoke_test.sh --config config.toml
```

真实 API 验收脚本为 `ai-infer-node/scripts/final_acceptance.py`；准备公开 fixtures 后通过 Windows 正式入口运行。不要直接复制历史通过结论。网络和硬件问题要复现并提供响应/进程/日志证据。测试需保护原文件和现有数据，禁止虚构“开机通过”、准确率或资源数据。

输出要求：先给出“需求达到/部分达到/未达到”的明确判断，再按 P0–P3 列出真正的问题，每项给出绝对文件路径、准确行号、触发条件、后果和最小修复建议。没有问题时说明检查覆盖及剩余证据边界。提供原始需求逐项验收表（通过、失败、未测、不适用），分别列源码、隔离测试、本机/远程实测、真实重启的证据。先完成独立审查；如发现问题，保存可复现的修复方案和验证结果，避免在审查过程中擅自重构整个业务或中断用户任务。
