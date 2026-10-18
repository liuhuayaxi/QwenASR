# Google Drive 资料备份

入口：录音工作台 → 全部设置 → 资料库与配置 → Google Drive 备份。
也可打开 `http://127.0.0.1:8765/?settings=library`。

## 第一次连接

1. 在自己的 Google Cloud 项目中启用 **Google Drive API**。
2. 配置 Google Auth Platform 的应用信息和受众。个人账号使用 External；Testing 阶段把自己的账号加入测试用户。
3. 创建 **Desktop app / 桌面应用** 类型的 OAuth 客户端，下载 JSON。
4. 在 QwenASR 的“首次连接”中导入 JSON，点击“保存备份选项”。客户端文件只保存到本机私有目录。
5. 点击“连接 Google 账号”，在浏览器中完成授权。只请求 `drive.file` 权限。
6. 返回工作台，先预览清单，再点击“小批量测试”。最多选择 3 个较小文件，存在 PDF 和文本时会兼顾两类。
7. 核对云盘中的文件后，点击“立即增量备份”。需要定期备份时勾选自动备份并保存。

外部应用处于 Testing 时，包含 Drive 权限的刷新令牌会在 7 天后过期。长期使用需要正确配置发布状态；实际验证要求以 Google 控制台为准。授权过期后界面会提示重新连接。

官方说明：
- [OAuth 桌面应用与 PKCE](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Drive API Python 配置流程](https://developers.google.com/workspace/drive/api/quickstart/python)
- [Drive 文件权限](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [断点续传](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
- [刷新令牌有效期](https://developers.google.com/identity/protocols/oauth2)

## 保存内容与行为

- 默认备份资料库中非隐藏目录下的 PDF，以及 `transcript.txt`、`原始转写.txt`、`AI修正后文本.txt`、`transcript.json`。
- 可以额外勾选课堂笔记、AI 总结和 `AI生成笔记` 目录中的 Markdown。笔记导出的 PDF 属于 PDF 选项。
- 兼容资料库登记的外部录音目录及外部笔记。跳过符号链接、隐藏目录、已知缓存和备份目录。
- 不扫描私有配置目录，不上传应用配置、凭证、音频、模型或 ZIP 备份。
- 云端目录是 `QwenASR备份/资料库/…`，保留本地课程层级。原文件格式不转换为 Google Docs。
- 首次上传，之后按文件标识更新。文件内容校验相同则跳过。每次成功都核对云端文件大小和 MD5。
- 本地删除不删除云端副本；本地重命名会生成新路径副本，旧路径副本保留。更新使用同一个 Drive 文件 ID，历史版本的保留遵循 Google Drive 策略。
- 自动备份只在本机 QwenASR 服务运行时工作，默认关闭，支持 5 分钟到 1 天的间隔。首次服务启动约 1 分钟后可补查，随后按间隔运行。
- “暂停并关闭自动备份”保留已上传内容与续传进度。恢复时重新扫描并继续。关闭服务同样保留进度。
- “断开本机连接”删除本机访问/刷新令牌并关闭自动备份，保留云端文件和增量记录；如需撤销 Google 侧授权，可在 Google 账号的第三方访问设置中移除应用。
- 默认沿用系统 HTTPS 代理或代理环境变量。需要本机能够访问 Google OAuth 和 Drive API。

## 实现与验证边界

`qwen_asr_app/drive_backup.py` 使用现有 httpx，不新增运行依赖。授权采用随机 loopback 端口、一次性 state 和 PKCE；回调不进入 Web 服务访问日志。令牌、OAuth 客户端和上传会话存入 `PRIVATE_ROOT/google-drive-backup/state.json`，目录 0700、文件 0600，状态接口不返回这些内容。

上传采用 4 MiB 分块。每个文件先取稳定快照，避免转写或笔记保存过程中混合两个版本。上传 ID 在创建远程对象前持久化；收到响应前断网时查询会话进度后续传。文件变化后重新传输新的快照。云端核对失败的文件不记为成功。

日常 ZIP 备份与云盘任务互斥，避免 ZIP 打包期间授权/续传状态持续变化。云盘任务本身不要求停止转写；变化中的文件可能暂时跳过，在下一轮重试。

本地验证命令：

```sh
.venv/bin/python -m unittest tests.test_drive_backup tests.test_library_backup tests.test_storage_read_only -v
node --check static/js/drive-backup.js
```

这些合成测试验证上传协议、重试、权限边界、增量状态、校验和 OAuth 处理。首次真实 OAuth 授权及 Google 服务端上传仍需使用用户自己的账号完成验证。
