# Tailscale 直连与 UI 整理（2026-10-08）

**2026-10-09 更新：本文的 WSL 直连入口已被替换。当前业务使用 Windows 原生 Tailscale `http://100.81.62.69:8765`，官方后台 TCP Serve 转发到 WSL loopback。已实测不依赖 WSL tailscaled；当前结论与最终复查见 [FINAL_REVIEW_REPORT.md](FINAL_REVIEW_REPORT.md)。下面保留 10 月 8 日的过程和 UI 检查记录，旧地址不再用于业务。**

当时采用用户确认的“只需要 Tailscale 内网直连”：业务直接访问 WSL Tailscale 接口，SSH 隧道和 Windows 局域网桥接均已停用。原模型、GPU 独占队列、Bearer 鉴权及本地默认模式保留。

## 历史连接（已替换）

- 地址：`http://100.81.168.99:8765`。
- 服务仅监听 `100.81.168.99:8765`，不监听 0.0.0.0 或普通局域网接口。
- 其他设备加入同一 Tailnet 后，使用此地址和相同 Bearer Token，不需要 SSH 密钥。
- Token 位于 WSL `~/ai-infer-node/private/token`、Mac `~/.qwenasr/remote-infer/token`，权限 600；没有写入源码、截图或聊天输出。
- 不带 Token 的请求返回 401；模拟公网客户端即使有 Token 也返回 403。
- Mac 默认地址已迁移，文档/语音模式仍为 local。

Mac launchd `com.liuhuayaxi.ai-infer-tunnel` 已 bootout，plist 的 RunAtLoad/KeepAlive 均为 false，备份保留。WSL tailscaled enabled/active；网关 systemd 在 tailscaled 之后启动，异常自动重启。不依赖额外 Windows Node 代理。

实验过程中曾用普通用户 Windows TCP 桥接完成同 Wi-Fi 与 Windows Tailnet 的真实访问，但其自动恢复测试未通过。用户确认只需要 Tailscale 后，该实验任务与进程已停止并移除，相关发布脚本不再交付。实验结果 JSON 保留为过程记录，不能作为当前访问地址或自启动验收。

真正 Windows 重启与用户尚未登录时启动仍未实测，沿用上一报告的证据边界；没有执行 Windows 重启、wsl --shutdown 或修改 Syncthing。

## UI 检查与修正

- 推理设置使用了不存在的 button secondary 样式，保存按钮退回浏览器默认外观；改用统一 ui-button/primary。
- 推理设置网格与相邻卡片不同，说明、状态与操作区贴边；统一 settings-group-grid、16 px 内边距和 settings-actions。
- MinerU 高级参数裸 summary 在原网格中排到右侧；改为与标题对齐的展开行。
- 复选框受通用输入宽度影响；单独设置 18 px，并使用标准开关行。
- 任务中心侧栏原为通栏分割线，资料库为浮动卡片；统一卡片边框、12 px 圆角、13 px 导航字号和页面留白。
- 手机导航原先只显示部分当前入口；480 px 以下按三列换行，所有顶层入口完整可见。

保留黑灰色深色背景，绿色用于主要操作与状态；浅色模式使用同一组字号、圆角和间距。各页共享样式版本已刷新，避免旧缓存。

新增“测试远程连接”只检查健康接口，不加载模型，也不保存或改写推理模式。

## 验证

真实浏览器检查工作台、推理/MinerU 设置、任务中心及资料库。桌面 1100 px 设置两列、390 px 窄屏单列，均无横向溢出；控件字体 13 px、圆角 8 px、最小高度 36 px。窄屏工作台和任务中心均正常，导航完整换行。深色/浅色截图及最终界面保存在 evidence/。

Mac 59 项相关 Python 回归测试通过；网关 8 项测试通过（新增公网来源拒绝）；AI 设置和 session control JS 检查通过。Tailscale 直连已实际完成双语 ASR，任务结束 GPU 恢复 43 MiB。最终直接监听与恢复记录见 evidence/direct-tailnet-final.json、evidence/tailnet-recovery.json。

源码和配置备份：`~/.qwenasr/backups/direct-lan-ui-20261008-224837`。原有资料没有删除，原始推理默认模式保留。
