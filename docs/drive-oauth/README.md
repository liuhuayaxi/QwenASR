# QwenASR 个人备份

QwenASR 的 Google Drive 备份功能运行在使用者自己的电脑上，将本地资料库中的 PDF、录音转写和使用者选中的笔记保存到使用者自己的 Google Drive。

使用者通过 Google 官方页面授权。备份模块请求 `https://www.googleapis.com/auth/drive.file`，用于创建和管理通过本应用使用的特定云盘文件。账号密码由 Google 处理，本工具不接收 Google 账号密码。

功能包括备份清单预览、手动备份、定期增量备份、断点续传、失败重试和上传后的内容校验。自动备份需要本机 QwenASR 服务运行。本地删除不会自动删除云端副本。

这是个人自用工具，由使用者管理自己的电脑、Google 账号和云盘空间。

请参阅同一页面的[隐私说明](https://gist.github.com/liuhuayaxi/f979839837fd3de783d620b5005b1a57#file-privacy-md)和[使用条款](https://gist.github.com/liuhuayaxi/f979839837fd3de783d620b5005b1a57#file-terms-md)。
