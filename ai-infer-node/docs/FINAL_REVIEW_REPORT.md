# 最终交付与复查（2026-10-09）

连接失败和本轮发现的集成/UI 问题已修复，正式入口已经部署，并通过真实推理复验。当前按需推理、本地/远程切换、回退和卸载功能可用。原始需求中真正 Windows 重启、用户尚未登录时自启动和 120 分钟真实课程录音尚未验收，不能把整个原始清单称为全部通过。

## 当前入口与运行状态

正式业务入口为 **`http://100.81.62.69:8765`**，Windows 原生 Tailscale 节点。Windows 官方后台 TCP Serve 转发到 Windows localhost，再通过现有 WSL localhost forwarding 到 WSL `127.0.0.1:8765`。配置为：

```powershell
tailscale.exe serve --bg --yes --tcp=8765 tcp://127.0.0.1:8765
tailscale.exe serve status --json
```

实际 Serve 配置 `TCP.8765.TCPForward=127.0.0.1:8765`。没有 SSH 隧道、Node 桥接、普通 LAN/public 监听或 Funnel。网关仅接受 loopback 来源和 Bearer 鉴权。安装器保留其他 Serve 端口，先备份配置并拒绝同端口冲突。

其他设备加入同一 Tailnet，获得允许访问此节点的 ACL 权限，并在受保护文件中配置 Bearer Token 后，可以使用同一个 API 地址，无需 SSH 密钥。直接在浏览器裸访问 `/v1/health` 会收到 401；工作台的“测试远程连接”在后端读取受保护 Token。

Mac 文档/语音两项配置仍为 `local`，远程地址已改为 Windows 原生入口。选择“优先 Windows，失败回退本机”即可启用备用节点。“仅 Windows”失败时明确报错，绝不静默回退。新 PDF 和课程转换任务保存 Provider 与 MinerU 配置快照；改变设置不会改写已排队任务。缓存复用校验 Provider/地址，避免切换后沿用错误来源的结果。

00:47 的真实状态：Mac 工作台 idle、活动任务 0；Windows 网关 IDLE、worker_pid=null，GPU 43 MiB，网关 RSS 51.45 MiB，WSL Linux used 约 904 MiB。整个 Windows WSL VM 的 Working Set 与 Linux used/网关 RSS 是不同指标，不能互相替代。最终状态原始记录在 `evidence/final-live-state-20261009.json`。

## 为什么同步正常而原连接失败

此前业务访问的是 **WSL 自己的 Tailscale 节点 `100.81.168.99`**，而 Syncthing 在 Windows 原生进程内运行。诊断时同步连接曾使用 Windows 原生 Tailscale `100.81.62.69`；最后复查自动选择了 LAN `192.168.1.204:22000`，connected=true、Syncthing system errors=null。同步一直正常，没有停止或改写 Syncthing 配置。

WSL 的 TCP 22/8765 和 ping 出现间歇超时，随后在网关 PID 不变、未重启推理服务时恢复；WSL tailscaled 日志出现 `UDP is blocked, trying HTTPS`、DERP `sin: EOF` 和 resolv.conf 被覆盖。这些观测支持 WSL Tailnet 路径不稳定，而不支持“Windows 或 Syncthing 停止工作”的判断；没有充分证据把全部根因归到某个代理或路由器配置。

修复后进行了独立性测试：先设置 systemd 定时恢复，再临时停止 WSL tailscaled。WSL SSH 在测试期间无法访问，Windows 正式入口连续健康检查均 200，真实 19 秒双语 ASR、状态轮询和结果下载全部完成；约 25 秒后安排恢复原管理服务。证据为 `evidence/native-entry-with-wsl-tailnet-stopped.json`。最后一次 WSL SSH 管理探测仍超时，但 Windows API 同时健康；SSH 管理路径的间歇故障没有被修复为稳定管理通道，已从业务依赖中移除。

客户端同时修复了短时连接故障处理：健康检查/提交有限重试；每次重新打开文件并保留同一个 Idempotency-Key；已接收任务保留 Job ID，只继续轮询。默认连续断线恢复窗口 60 秒，正常冷启动和排队使用独立的 14400 秒任务超时。鉴权错误不重试，取消可唤醒等待。下载/最终结果错误仍会明确报错并按配置选择回退，不承诺无限网络重试。

## 用户当时的识别任务与 4.0 版本

首次查询时确实正在识别 `微型计算机原理与接口技术-第3章.pdf`（203 页），任务 ID `course_pdf:92a804d64b53428e8aa8ac45b30dd47e`。实际进程使用 Mac 隔离环境 `~/.qwenasr/runtimes/mineru4/bin/python` 和 `scripts/run_mineru4.py`，软件 **MinerU 4.0.10，Advanced**。当时使用 Mac 本机，未调用 Windows。后续现场任务已变为 cancelled；本次代理未取消该任务，最终任务中心活动数量为 0。

现场核对 [官方 PyPI](https://pypi.org/project/mineru/) 后最新稳定版本仍是 4.0.10。软件 4.0 与模型仓库命名不同：官方 4.0 下载器使用 `MinerU-4_models_torch` 与 `MinerU2.5-Pro-2605-1.2B`；名字中的 2.5 不代表旧 3.x API。Mac 保留原 llama.cpp **GGUF Q8_0** 权重，Windows 使用 **BF16 vLLM**。不能把两台机器的执行精度混为一谈。

Windows 实测 MinerU 4.0.10 + Torch 小模型 + vLLM，PDF/图片固定 Advanced；Office 按官方规则 Flash。ASR 使用官方 `Qwen/Qwen3-ASR-1.7B-hf`，Transformers 5.19.0、Torch 2.14.1 cu130、全部必要权重 CUDA/BF16，无 CPU offload、INT4/INT8 或 ForcedAligner。

## 最终真实推理验收

统一通过 Windows 原生入口重新运行 `scripts/final_acceptance.py`。原始数据与检查结果在 `evidence/final-native-acceptance-20261009.json`，所有完成项下载的 ZIP 包内 metadata 与网关最终资源指标一致。

| 输入 | 实際结果 | 推理/解析 | 模型任务耗时 | 峰值 GPU | 卸载后 |
| --- | --- | ---: | ---: | ---: | ---: |
| 12 页中英文字/公式/表格 PDF | 全 12 页、Advanced、公式表格和 48 个素材 | 90.69 s（含模型加载） | 97.60 s | 4817 MiB | 43 MiB |
| 2 页扫描/图表 PDF | 两页完整，Advanced，图片引用有效 | 54.17 s（含模型加载） | 60.59 s | 4817 MiB | 43 MiB |
| 5 分钟双语录音 | 32 段、覆盖 300 s、中英文锚点存在 | 24.82 s，RTF 0.083 | 37.49 s | 4324 MiB | 43 MiB |
| 30 分钟双语录音 | 一次加载，188 段、覆盖 1800 s、中英文锚点存在 | 137.83 s，RTF 0.077 | 149.98 s | 4324 MiB | 43 MiB |

表中模型任务时间不含前一任务排队等待；PDF 解析时间包含初始化，不能再与加载列相加。音频为公开中英样本重复组成，证明分段推进、长度覆盖和资源行为，不代表真实课程语料准确率。只提供实际分段时间戳，未伪造逐词时间。

本轮还实际通过：同时提交 ASR/PDF 的共享 GPU 排队；同 key 同输入返回同 Job ID；未授权请求 401；路径穿越 400；运行中 ASR 取消及清理；完整结果下载；GPU 回到任务前基线。资源指标在 Worker 退出后写回 JSON 和 ZIP，使用同一次推理结果，不重复识别。

本轮重新实测 Mac local MLX、remote_only、连接失败后 Preferred 的真正本机回退，以及 remote_only 错误不加载本机 Worker，见 `evidence/mac-providers-final-20261009.json`。早期真实 PDF 回退、课程转换、整段上传单任务和取消、PNG/DOCX、Worker 强杀、任务超时、网关 SIGKILL/systemd 恢复记录在 `TEST_REPORT.md` 对应 evidence；本轮没有为重写报告重复停止生产网关。

## 代码回归和 UI

- Mac 完整 Python 回归 **193 项通过**，原用户资料 **278 个文件保持不变**。
- WSL 网关/客户端 **11 项通过**。新增响应丢失后同 key/同文件重试、轮询中断恢复、鉴权错误不重试、断线窗口和取消检查属于隔离测试，不冒充真实网络/模型运行。
- AI 设置、会话控制 JavaScript 检查通过；Python 编译、相关 JS 语法与 `git diff --check` 通过。
- 真浏览器检查工作台、设置、任务中心和课程资料库；深浅主题、390 px 窄屏和桌面样式已核对。控件统一按钮/输入框、字号、圆角、间距与展开行；任务侧栏和手机导航使用一致布局，保持黑灰底和绿色操作强调。
- 最后在运行中的页面再次点击“测试远程连接”，得到“远程网关连接正常 · 检查未加载模型”；任务中心正确显示“配置为 Mac 本机 · Advanced 档位”和已取消状态，1280 px 页面无横向溢出。

最新截图为 `evidence/ui-native-stable-final.jpg`、`evidence/ui-tasks-provenance-final.jpg`；深浅和窄屏证据见历史 `ui-direct-*.jpg`。测试汇总为 `evidence/full-regression-20261009.json`。

## 原始需求验收边界

| 需求组 | 当前判断 | 证据或剩余内容 |
| --- | --- | --- |
| 隔离部署、官方权重、MinerU 最新稳定 4.0 Advanced、ASR 1.7B BF16 | 通过 | 官方权重校验、真实 PDF/扫描/图像/Office/双语推理；Mac 原有环境保留 |
| 轻网关、按需进程、GPU 独占、任务完成立即卸载 | 通过 | 初始/最终 IDLE、共享队列、成功/取消/强杀/超时显存恢复 |
| 持久任务、幂等、崩溃恢复、完整素材输出 | 通过 | SQLite 和 systemd 恢复、同 Job ID、完整 ZIP/JSON/md/图片 |
| Bearer、路径限制、流式上传、Tailnet 内部访问 | 通过（已测试范围） | 鉴权/路径/容量测试；实际 loopback 后端与原生 Serve；未做全面安全审计 |
| Mac 原功能、三个独立模式、失败回退与兼容输出 | 通过（回归和样本范围） | 完整自动回归 + 真实 Provider/PDF/课程/离线上传测试 |
| 无 SSH 的稳定业务连接 | 通过（现场范围） | 原生 Windows 入口，WSL tailscaled 停止期间仍推理；管理 SSH 仍间歇超时 |
| Windows 网络完全中断后恢复 | 部分验证 | 客户端隔离断线重试 + WSL Tailnet 停止独立性；未断开整个 Windows 网络适配器 |
| systemd 自动恢复、Windows 登录任务、keepalive | 已配置并手动验证 | 当前用户 AtLogon、Running；真实 Linux 异常恢复；未把手动启动算登录触发验收 |
| Windows 真正重启、未登录时自动启动 | 未测 / 未登录触发配置未通过 | S4U/AtStartup 普通权限被 0x80070005 拒绝，管理员安装脚本保留；未重启 Windows/WSL |
| 30–120 分钟真实课程长录音 | 部分验证 | 最长实测 30 分钟重复公开样本；120 分钟和真实语料准确率/丢词率未测 |
| 60/120 秒延迟复用、独立 CPU 并行池 | 非默认扩展未实现 | 当前即时卸载，CPU 原生文档同队列串行；符合当前低空闲资源优先策略 |

## 交付、重复部署与回滚

源码、冻结依赖、配置样例、部署/服务/Serve 脚本、真实验收脚本、报告与 evidence 均在 `ai-infer-node/`。Mac 主项目中已集成 Provider、UI、配置快照、任务来源、长音频和 PDF adapter。最终源码 ZIP 在 `/Users/liuhuayaxi/Documents/Codex/2026-10-09/qwenasr-final/`，附逐文件 SHA256 清单。包不含模型、venv、原始录音/课件、数据库、真实配置或凭据；不能用它覆盖现有私人配置和数据目录。

当前工作区包含用户其他 AI 任务的修改，均保留，源码快照反映打包时整个项目状态；未执行 Git reset/clean、提交或推送。原始需求在 `ORIGINAL_REQUIREMENTS.md`，独立审查提示词在 `ASTRA_REVIEW_PROMPT.md`。

源码/配置备份：`~/.qwenasr/backups/ai-infer-20261008-203532`、`~/.qwenasr/backups/direct-lan-ui-20261008-224837`、`~/.qwenasr/backups/native-tailnet-stability-20261009-002231`。Linux 配置和 Serve 修改前另有受保护备份。安全回滚先将两项 Provider 切回 local，再按 README 停服务/移除本端口配置；保留模型、任务历史、凭据和其他 Serve 端口。
