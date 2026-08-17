# CourseOrganizer 设计与发布验收

更新日期：2026-08-14

## 当前结论

**候选源码与 NAS 部署已通过，发布验收尚未闭环。**

普通用户主路径固定为：

`选择目录 → 安全预览 → 处理异常 → 开启自动整理`

选定视觉真值：

- 文件：`/Users/x/.codex/visualizations/2026/08/13/019ffc03-c6ba-7fb2-98fb-b452703a7e5b/courseorganizer-ux-audit/06-selected-redesign.png`
- SHA-256：`808fa266bdcdd606030d714dc47a096271933b5b78706c0e389bf47f36c957f2`

## 验收矩阵

| 边界 | 状态 | 当前证据 | 仍需满足 |
|---|---|---|---|
| 本地候选 | PASS | 部署前现场为 `172 passed`；新增确定性 one-shot 生命周期回归后，本轮为 `173 passed`。`py_compile`、`git diff --check` 通过，四个生产源码哈希未漂移。 | 无本地代码阻塞；本地测试不替代真实 MoviePilot 宿主验收。 |
| NAS 双副本与健康 | PASS（部署阶段现场证据） | 候选已部署到源副本与运行副本；备份已创建；仅重启一次后容器为 healthy，配置保持 `enabled=false / naming_mode=preview / run_once=false`。 | 后续不得用旧健康记录代替 one-shot、真实 schema 或在线 UI 验收。 |
| 真实 one-shot preview | BLOCKED | 源码与测试证明 preview 在 `move_started` 前返回；本轮新增生命周期测试证明本地 `manual + preview + moved=0`、配置复位与目录指纹不变。 | 必须由正在运行的插件实例产生恰好一条 `scan_started trigger=manual mode=preview`、一条 `scan_completed ... moved=0`、零条 `move_started/move_completed`；最终配置复位且媒体目录指纹不变。当前缺安全认证句柄或已登录会话。 |
| 真实 `get_form/get_page` | BLOCKED | 本地 schema 测试覆盖四步顺序、Advanced 默认折叠与四列中文预览。 | 必须通过 MoviePilot 已加载实例的官方接口构造；不能另起 Python 进程重新实例化插件。 |
| 在线 UI | BLOCKED | 当前内置浏览器停在 MoviePilot `v2.15.6` 登录页；无可复用登录态或已连接 Chrome。 | 登录后分别在桌面与移动视口打开真实插件弹窗，核对四步交互、默认折叠、四列表格、空态、滚动/底栏、键盘焦点和控制台错误。旧截图不得复用为当前通过证据。 |
| Git 与编排 | BLOCKED | `HEAD` 与 `origin/main` 均为 `7a5734ca7cd7`，但核心源码、测试、文档及运行产物仍有修改或未跟踪。r2 Store 根运行仍为 `active`，`repair-commit-rollback` 为 `blocked`，另两项为 `proposed`；另一个 Store 又记录为 `cancelled`。 | 形成可复现提交或明确冻结清单，并调和唯一权威 Store 到终态。 |

## 本地候选证据

本轮验证：

- 全量测试：`173 passed in 0.39s`
- 新增生命周期测试：`1 passed`（无真实等待或后台线程）
- 编译：四个生产源码和新增测试均通过 `py_compile`
- 差异检查：`git diff --check` 通过

生产源码 SHA-256：

- `__init__.py`：`958364014b6ad3a74e73801d0e265040faa383a82a99a0dc295e53f115ffb5d4`
- `naming.py`：`816ccf8e24d3ad8b8ffe831a775d5b1aaec63f79f93bbd8d31d126790b30c772`
- `providers.py`：`a810eae1474f769411d6c175a8f92768cc55771c19a16a6bd4d2f68d56775eab`
- `resolver.py`：`0d614d1d59b2ad8929ab1c91f971b761906e6ec60c8c556c3de908e92601dcdc`

这四个哈希与部署阶段记录的 NAS 源副本、运行副本一致。新增内容只涉及测试和本文档，没有改变已部署生产源码。

## NAS 部署证据

部署位置：

- 源副本：`/config/plugins.v2/courseorganizer`
- 运行副本：`/app/app/plugins/courseorganizer`

部署前备份：

- 数据库：`/config/user.db.courseorganizer-predeploy-20260814T1940.bak`
- 双副本：`/config/courseorganizer-backup-option2-predeploy-20260814T1940/{source,runtime}`

部署阶段现场记录显示：四个真实源码文件经原子替换后双副本逐字节一致；仅重启容器一次；MoviePilot 主进程与 nginx 正常；容器内 `3000=200`、`3001=404`；未发现 CourseOrganizer 导入、初始化或 FormRender 异常。TMDB/GitHub 外网错误单独归为网络问题，不等于媒体移动。

这些证据只证明部署与重启后健康，不证明真实 preview、运行时 schema 或浏览器 UI 已通过。

## 解除阻塞后的唯一执行顺序

1. 用户在已打开的 MoviePilot 登录页完成登录；不得读取、复制或输出密码、Cookie、认证头或页面令牌。
2. 先通过官方只读接口从运行进程取得 CourseOrganizer 的现有实例，验证 `get_form()` 与 `get_page()`；不得直接 import 并新建插件实例。
3. 保留全部现有配置，仅覆盖 `enabled=false`、`naming_mode=preview`、`run_once=true`、`naming_clear_cache_once=false`，提交前后都只报告脱敏摘要和哈希。
4. 触发且只触发一次 one-shot preview；核对日志计数、`moved=0`、零 move 事件、最终配置复位和媒体目录指纹不变。失败或超时不得重试。
5. 在真实插件弹窗完成桌面与移动端浏览器验收，再处理 Git 与编排收口。

## 安全边界

- 禁止 `apply` 或任何真实媒体搬移。
- 禁止第二次容器重启。
- 禁止提取或暴露密码、Cookie、认证头、API token 或绿联页面令牌。
- 禁止另起 Python 进程伪装为 MoviePilot 已加载插件实例。
- preview 允许写预览缓存和插件状态，但不得改变媒体目录。
- UGREEN NAS.app 的内嵌 xterm 当前可显示，但自动键盘事件无法可靠进入终端；不得绕过该边界改用页面令牌直连。

## 最终结果

`final result: blocked`

阻塞原因不是本地测试或部署失败，而是缺少安全的已认证实时通道，因而真实 one-shot、真实宿主 schema、在线 UI、Git 与编排仍未验收完成。
