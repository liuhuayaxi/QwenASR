浏览器验收使用真正的 Chrome 与 Playwright，HTTP API、WebSocket、麦克风输入全部为合成数据。脚本自己创建本地静态服务器，使用随机端口与新浏览器上下文，结束后关闭。它不会导入应用、读取个人配置、访问学校、AI 或 IdeaShell 服务。

安装 Playwright 到独立开发工具目录，或使用已经安装的模块：

```sh
npm install --prefix /tmp/qwenasr-browser-tools playwright
PLAYWRIGHT_MODULE=/tmp/qwenasr-browser-tools/node_modules/playwright node tests/browser/workspace-smoke.cjs
```

需要本机已安装 Google Chrome。`PLAYWRIGHT_MODULE` 只指定测试工具位置，不是应用依赖或账号配置。也可以在模块可解析的环境直接执行 `node tests/browser/workspace-smoke.cjs`。

验收包含十二条路径：来源证据和首页、新建来源入口、课堂记录打开笔记、任务过滤分组和恢复、课程资料上传、刷新恢复课程与活动会话、上传完成打开笔记、模拟麦克风开始结束打开笔记、课程回放入库链接、课程内问答选择与笔记快捷入口、外部复习资料折叠、课程内笔记与任务抽屉。额外记录页面未捕获异常并检查恢复请求使用服务端保存设置。

结果在 `output/playwright/smoke-results.json`；截图在同目录。失败时写入失败截图并返回非零退出码。运行产物不提交 Git。

这些检查验证交互、请求意图和刷新恢复，不证明真实音频识别、Windows 推理、学校下载、外部 AI 生成或 IdeaShell 同步已经成功。
