# IdeaShell MCP 迁移与附件能力

核对日期：2026-10-07。使用本机已有 IdeaShell OAuth 授权，分别对新旧端点执行 `initialize` 和 `tools/list`，没有创建或修改远端笔记，也没有上传测试附件。

## 地址和旧数据兼容

国内 MCP 请求改为 `https://mcp.ideashell.cn/mcp`。全球端点仍为 `https://api.ideashell.com/ideashell/mcp`。

旧国内地址 `https://api.ideashell.cn/ideashell/mcp` 在前后端均作为新地址的别名。已有笔记关联、课程资料匹配和附件上传去重仍然有效；成功推送或拉取后，笔记的同步记录会写入新地址。凭证仍使用原有的 `~/.qwenasr/api.ideashell.cn-oauth.json`，避免复制刷新令牌后出现两个存储同时刷新。

服务端目前存在 OAuth 发现配置不一致：新域名的 `/.well-known/oauth-protected-resource/mcp` 和 `/.well-known/oauth-protected-resource` 仍声明旧资源 URI。Codex CLI 0.160.0 新授权因此报 resource mismatch。

QwenASR 对已绑定旧 issuer 且保存了匹配 discovery 的国内授权，保留原 issuer、resource 和刷新端点，MCP 数据请求使用新地址。资源和 issuer 校验没有关闭，也没有把旧客户端注册改写成新 issuer。未登录用户或已经使用新 issuer 的授权仍按新地址正常发现；服务端修正元数据前，全新授权可能受上述问题阻碍。

## 实时工具与附件参数

本次实测两个端点都返回 16 个工具，工具名、描述和参数结构完全一致。下面说明的是当前服务端暴露的能力，相比本地旧技能说明更完整；不能据此断言各项都在本次域名迁移中新增。

附件随 `note_create` 或 `note_update` 的 `images`、`audios`、`documents` 参数提交。各元素均为 `{"name": "带扩展名的文件名", "data": "Base64 文件内容"}`，文件名最多 120 字符。对已有笔记只传 `note_id` 和附件参数即可，省略 `content` 保留原正文。

| 类别 | 支持格式 | 单次数量 | 每个文件上限 | 自动处理 |
| --- | --- | --- | --- | --- |
| 图片 | PNG、JPEG、GIF、WebP | 9 | 5 MB | 识别真实文件类型，作为笔记级附件，不是 Markdown 内嵌图片 |
| 音频 | MP3、M4A、WAV、AAC | 5 | 25 MB | 保存并可播放，不自动转写；其他格式须先转换 |
| PDF / Office | PDF、DOC、DOCX、XLS、XLSX、PPT、PPTX | 5 | 50 MB | PDF、Word、Excel 自动提取正文；PPT/PPTX 不提取正文 |
| 文本文件 | TXT、MD、CSV、LOG、JSON、XML、YAML、YML | 5 | 5 MB | TXT、MD 自动提取正文，其余只保存和供下载 |

`documents` 合计每次最多 5 个，PDF/Office 和文本格式共用这个数量限制。服务端说明：提取完正文的文档随后可通过内容搜索；PPT/PPTX、CSV、LOG、JSON、XML、YAML、YML 不能靠附件内部内容搜到。未转写音频的语音内容也不会出现在搜索或 `note_detail` 中；用户需在 IdeaShell App 内发起转写，消耗其积分。

`note_update` 附件为追加语义；已有附件保留，相同字节的附件跳过。没有暴露删除、替换附件或新增网页链接卡片的工具。说明中的“可下载”指附件保存后的能力，当前 16 个 MCP 工具没有独立的附件下载工具。

另有旧技能说明未覆盖的字段：`note_create.tags` 最多 20 个，并优先于 `tag`；`note_create.ai_tags` / `note_update.ai_tags` 最多 3 个；`note_create.source` 可标记集成来源。

## QwenASR 当前范围

课程资料库继续按实时 `note_update` schema 生成文档附件参数，并保持预览后上传的流程。自动上传的本地上限为 20 MB，尚未放宽至服务端的 50 MB；文本格式仍受服务端 5 MB 限制。本次真实验证覆盖连接与工具发现，附件写入仅用模拟接口验证，不代表真实上传或后台文档解析已经验收。

来源：新端点 `https://mcp.ideashell.cn/mcp` 和旧端点的实时 `tools/list`；公开技能说明 https://github.com/RoundRedDot/ideashell-skill 。
