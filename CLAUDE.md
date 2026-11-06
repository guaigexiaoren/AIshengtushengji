# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Windows 桌面工具「亦欣 AI 生图工作台」：PySide6 界面 + 内网协作业务（运营提交项目、AI 人员领取并生图、管理员配置）。使用 Python 3.13，PyInstaller onedir 打包。

用户文档见 `README.md`，维护说明见 `DEVELOPMENT.md`。

## 常用命令

在仓库根目录执行（所有相对路径都基于根目录）：

```bash
# 安装依赖
py -3.13 -m pip install -r requirements.txt

# 运行程序（需要内网 MySQL 可访问）
py -3.13 main.py

# 全量测试（GUI 测试必须离屏运行）
QT_QPA_PLATFORM=offscreen py -3.13 -m unittest discover -s tests -t . -v

# 单个测试模块 / 单个用例
QT_QPA_PLATFORM=offscreen py -3.13 -m unittest tests.test_collaboration_pages
QT_QPA_PLATFORM=offscreen py -3.13 -m unittest tests.test_collaboration_pages -k product_header

# 打包（首次需先安装 PyInstaller）
py -3.13 -m pip install pyinstaller
py -3.13 -m PyInstaller --noconfirm --clean "亦欣AI生图工作台.spec"
# 产物：dist/亦欣AI生图工作台/（EXE 与 _internal 必须同目录分发）
```

MySQL 集成测试默认跳过，只有设置以下环境变量且库名以 `_test` 结尾时才运行：
`YIXIN_TEST_MYSQL_HOST`、`YIXIN_TEST_MYSQL_PORT`、`YIXIN_TEST_MYSQL_DATABASE`、`YIXIN_TEST_MYSQL_USER`、`YIXIN_TEST_MYSQL_PASSWORD`。

## 两套数据存储（最容易搞混的地方）

| 存储 | 模块 | 内容 |
|---|---|---|
| 本机 SQLite `data/app.db` | `yixin/database.py` | 普通创作任务、本机队列、作品库、设置、历史 |
| 内网 MySQL | `yixin/collaboration/repository.py` | 账号与权限、项目（主任务）、子任务、生成轮次、结果、报表 |

- 两套 SQL 占位符不同：SQLite 用 `?`，MySQL 用 `%s`，不要混用。
- 两套队列也是独立的：`yixin/worker.py`（本机 TaskWorker，处理普通创作）与 `yixin/collaboration/worker.py`（协作生成轮次），互不替代。
- MySQL 连接由 `_ConnectionPool` 复用（最多 16 条，取用前 `ping`）；新增查询一律走 `repository.connection()` 上下文管理器。
- 数据目录默认在 EXE 同级 `data/`，可用 `YIXIN_DATA_DIR` 覆盖（`yixin/paths.py`）。

## 启动链路

`main.py` → `yixin/app.py:run()`：

Qt 初始化 → `AppPaths.ensure()` + 单实例锁 → 旧数据备份/迁移 → SQLite 初始化与中断恢复 → MySQL 表结构升级 → 首个管理员/登录 → 共享盘解析器 → ProviderRouter → 本机 TaskWorker → `MainWindow`。

## 界面架构

- `yixin/ui/main_window.py`：顶部一级菜单，**按需创建页面**（首次进入才实例化），页面以中文菜单名作键（如 `"AI生图"`、`"历史记录"`）。
- 跨页面数据变化通过 `_mark_pages_dirty(...)` 标记，切页时刷新；不要把刷新逻辑直接写进别的页面。
- 大多数页面同时支持同步与异步两种模式：异步模式用 `yixin/ui/async_queries.py` 的 `QueryRunner` 提交后台任务，并用递增的 request id 丢弃过期回调（参考 `task_assignment_page.py`、`ai_generation_page.py`）。新增异步查询请沿用这个 request id 模式。
- 共享盘根目录每次 `refresh()` 重新读取并重建 `SharedPathResolver`，异步分支也要处理（否则管理员改路径后不生效）。

## 协作业务模型（命名对应关系）

- **项目 project = 主任务**：一次「货号」的提交，`ProjectRecord`。
- **子任务 task**：每个模特图一个原始子任务；AI 生图按配对拆分出拆后子任务，`ProjectTask`。
- **轮次 attempt**：一次生成请求，`GenerationAttempt`，状态 `queued → running → succeeded/failed/cancelled`。
- 状态常量集中在 `yixin/collaboration/types.py`，不要硬编码字符串。

链式关系：`projects → project_tasks → task_garments / project_assets → generation_attempts → generation_results`。

「提交记录」页只读；「历史记录」页用于查看与重试，两者不是同一功能。

## 共享盘目录规则

```text
<share_root>\内部项目\<项目名>\<月.日>\<货号>\衣服图
<share_root>\内部项目\<项目名>\<月.日>\<货号>\模特图
<share_root>\外部项目\<项目名>\<月.日>\<货号>\衣服图
<share_root>\外部项目\<项目名>\<月.日>\<货号>\模特图
<货号目录>\AI成品\_r001_01_<哈希>.png
```

- **只有「内部项目」校验货号格式**（`yixin/collaboration/product_code.py`），外部项目保持自由命名。
- 货号形如 `DV263q25445p80723`：品牌 2 位 + 年份 2 位 + 季节 1 位（1春2夏3秋4冬）+ 分隔字母 + 流水号 + 分隔字母 + 品类 2 位 + 颜色 2 位 + 可选尺码 1 位。
- 尺码位可有可无，系统统一截断到「品类+颜色」（`base_code`），作为系统内保存的货号。
- 不符合规则的货号不识别、只在提交页提示，不弹错误框。

## 生图链路与不可随意改动的规则

- `yixin/provider_router.py`：多平台按优先级调用；网络错误/限流/5xx 自动切换，**请求超时不会自动切换**（服务端可能已生成并计费），这是业务要求。
- 账号并发名额已满时抛 `GenerationSlotBusyError`，轮次保持排队、延迟重试，不能当成失败处理。
- AI 生图每个配对固定生成 1 张；普通创作与批量换装支持 1–4 张。
- 生成成功后子任务自动完成，全部完成或标记「无需制作」后主任务自动完成。
- 轮次插入逻辑统一走 `repository._insert_attempt` 等 5 个内部助手（`_next_attempt_no`、`_assign_task_to_actor`、`_update_task_generation_params`、`_attempt_record`），不要在新入口里再复制一份 SQL。

## 配置与敏感信息

- `yixin/private_config.py` 内置 MySQL 账号、共享盘根目录、API Key 主密钥，可通过环境变量覆盖：`YIXIN_DB_HOST/PORT/NAME/USER/PASSWORD`、`YIXIN_SHARED_ROOT`、`YIXIN_API_KEY_SECRET`。
- 日志统一经 `yixin/logging_utils.sanitize_text` / `redact_data` 脱敏；新增日志不要绕过。

## 测试约定

- 测试全部基于 `unittest`，GUI 测试需要 `QT_QPA_PLATFORM=offscreen`。
- 页面测试大量使用 `tests/test_collaboration_pages.py` 里的 `FakeCentralRepository`，不需要真实 MySQL；涉及 SQL 的改动请在 `tests/test_collaboration_mysql_integration.py` 补充（按现有跳过机制）。
- 修改共享盘扫描、货号、任务状态时，至少覆盖：内部/外部项目差异、重复货号、异常目录。
