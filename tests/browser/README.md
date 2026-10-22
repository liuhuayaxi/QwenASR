浏览器验收使用真正的 Chrome 与 Playwright，HTTP API、WebSocket、麦克风输入全部为合成数据。脚本自己创建本地静态服务器，使用随机端口与新浏览器上下文，结束后关闭。它不会导入应用、读取个人配置、访问学校、AI 或 IdeaShell 服务。

安装 Playwright 到独立开发工具目录，或使用已经安装的模块：

```sh
npm install --prefix /tmp/qwenasr-browser-tools playwright
PLAYWRIGHT_MODULE=/tmp/qwenasr-browser-tools/node_modules/playwright node tests/browser/workspace-smoke.cjs
```

需要本机已安装 Google Chrome。`PLAYWRIGHT_MODULE` 只指定测试工具位置，不是应用依赖或账号配置。也可以在模块可解析的环境直接执行 `node tests/browser/workspace-smoke.cjs`。

验收覆盖以下路径：来源证据和首页、新建来源入口、课堂记录打开笔记、任务过滤分组和恢复、课程资料上传、刷新恢复课程与活动会话、上传完成打开笔记、模拟麦克风开始结束打开笔记、课程回放入库链接、课程内问答选择与笔记快捷入口、外部复习资料折叠、课程内笔记与任务抽屉。额外记录页面未捕获异常并检查恢复请求使用服务端保存设置。

结果在 `output/playwright/smoke-results.json`；截图在同目录。失败时写入失败截图并返回非零退出码。运行产物不提交 Git。

这些检查验证交互、请求意图和刷新恢复，不证明真实音频识别、Windows 推理、学校下载、外部 AI 生成或 IdeaShell 同步已经成功。


稳定性修复增加四条场景：摘要列表的服务端分页／课程筛选与笔记入口、跨页课程统计、IdeaShell 结果未知时默认核对，以及服务端已确认关联时默认更新已有笔记。所有列表夹具均去掉完整笔记正文，详情夹具保留正文。后续回归还覆盖录音丢弃、全屏笔记保存与滚动恢复、识别和索引状态等路径；`workspace-smoke.cjs` 目前共 36 条场景。

CI 使用本目录的锁文件：`npm ci --ignore-scripts` 后执行 `npx playwright install --with-deps chromium`，再以 `QWEN_ASR_BROWSER_CHANNEL=chromium node workspace-smoke.cjs` 运行。本机可以继续复用 `PLAYWRIGHT_MODULE` 指定的独立工具，不必安装到正式环境。WebSocket 的服务端所有权和生产配置验证在 Python 的 `tests/test_review_capture.py` 中执行；这里的合成 WebSocket 只验证浏览器控制流程。

## 前端生命周期与空闲资源回归

`frontend-lifecycle.cjs` 在真实 Chromium 中加载完整生产页面，通过合成 HTTP、WebSocket、麦克风和音频上下文控制异步时序。13 条场景覆盖速记保存期间的新输入、跨课堂回包与增强、编辑保存与轮询、引用回听音源、重复启动及过期启动清理、隐藏后录音继续、空闲刷新、课程回放与资料转换的状态恢复、Drive 设置按需加载，以及关联资料主动刷新。不会连接真实账号、模型或收费 API。

在仓库根目录执行；无参数时使用 `tests/browser/node_modules/playwright` 和 Playwright 安装的默认 Chromium：

```sh
node tests/browser/frontend-lifecycle.cjs
```

复用独立 Playwright / Chromium，或将产物写入其他目录：

```sh
PLAYWRIGHT_MODULE=/tmp/qwenasr-browser-tools/node_modules/playwright \
QWEN_ASR_BROWSER_EXECUTABLE=/path/to/chromium \
node tests/browser/frontend-lifecycle.cjs . /tmp/qwenasr-frontend-lifecycle
```

第三个可选位置参数是场景名称的正则表达式，例如 `'^04-'` 仅执行录音重复启动场景。默认结果、请求记录、截图和 Playwright trace 位于 `output/playwright/frontend-lifecycle/`，失败会返回非零退出码。CI 已包含整组运行。

空闲测试记录合成场景中的请求与 DOM 变化，并通过可控的可见性事件验证隐藏及恢复流程；它不等同于真实操作系统的后台调度，也不测量 Mac 的 CPU 或内存。录音持续性另用 PCM 发送和媒体资源释放断言验证。
