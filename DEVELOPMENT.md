# 亦欣 AI 生图工作台开发维护说明

本文面向开发和维护人员，说明项目结构、数据边界、启动流程、测试、打包以及数据维护方法。普通用户安装和功能说明请优先阅读 `README.md`。

## 1. 项目概览

- 目标平台：Windows 10/11 x64
- 开发语言：Python 3.13
- 桌面界面：PySide6
- 本机数据库：SQLite
- 内网协作数据库：MySQL
- 图片处理：Pillow
- HTTP 请求：httpx
- Excel 导出：openpyxl
- 打包工具：PyInstaller onedir

核心目录：

```text
main.py                         程序入口
yixin/app.py                    启动、登录、数据库和工作线程初始化
yixin/ui/                       桌面界面与交互
yixin/collaboration/            内网协作、权限、任务和报表
yixin/database.py               本机 SQLite 数据库
yixin/storage.py                图片校验、保存和缩略图
yixin/worker.py                 本机任务队列工作线程
yixin/provider_router.py        AI 平台路由与故障切换
tests/                          自动化测试
tools/                          初始化、清理和构建辅助脚本
assets/                         图标与静态资源
亦欣AI生图工作台.spec           PyInstaller 打包配置
```

## 2. 启动链路

程序入口为 `main.py`，实际启动逻辑位于 `yixin/app.py`：

```text
main.py
  -> yixin.app.run()
  -> 初始化 Qt、中文界面和本机 data 目录
  -> 初始化本机 SQLite
  -> 检查并升级 MySQL 表结构
  -> 无管理员时要求创建首个管理员
  -> 登录并加载账号权限
  -> 初始化共享盘、API 平台路由和任务工作线程
  -> 创建 MainWindow 并进入页面
```

启动后会：

- 每 24 小时清理过期的本机临时文件和未选中的协作候选图。
- 恢复因上次异常退出而中断的本机任务状态。
- 恢复超过时限的内网生成任务状态。
- 应用关闭时停止本机任务工作线程。

## 3. 主要模块

### 界面

- `yixin/ui/main_window.py`：顶部导航、按权限加载页面、页面缓存和跨页面刷新。
- `task_create_page.py`：运营创建任务、扫描共享盘和提交项目。
- `submission_list_page.py`：查看提交记录和任务详情。
- `task_assignment_page.py`：AI 人员领取待分配主任务。
- `ai_generation_page.py`：处理子任务、参考图、提示词和生成队列。
- `batch_dress_page.py`：批量换装、素材配对和批量入队。
- `create_page.py`：普通创作与参考图编辑。
- `unified_history_page.py`：统一历史记录。
- `library_page.py`：本机作品库。
- `reports_page.py`：管理员经营报告和 Excel 导出。
- `admin_page.py`：账号、权限、API 平台、价格、共享设置和任务异常管理。

页面采用按需创建方式。首次进入页面时创建，后续切换复用已有页面；跨页面数据变化通过 `MainWindow` 的刷新和脏页标记机制同步。

### 权限与账号

- 角色分为运营、AI 制作和管理员。
- 菜单权限和运营项目权限保存在 MySQL。
- 管理员可新增、编辑、停用、软删除账号以及重置密码。
- 首次登录或管理员重置密码后，用户必须修改初始密码。
- 权限限制同时存在于界面和数据仓库层，不能只依赖隐藏按钮。

### 任务与生图

- 本机普通任务由 SQLite 保存，`TaskWorker` 从队列取任务并调用生图客户端。
- 内网协作任务由 MySQL 保存，包含主任务、原始子任务、拆后子任务、素材、生成轮次和结果。
- 运营提交项目后生成主任务；AI 领取任务并处理子任务，运营验收或退回。
- `provider_router.py` 负责 API 平台选择、模型路由、成本记录和故障切换。
- 请求是否允许自动重试必须保持现有业务规则，不应由界面层擅自改变。

### 图片与文件

- `FileStore` 负责图片格式校验、哈希、原子写入、缩略图和临时文件清理。
- 参考图和结果图会复制到本机受控目录，避免外部文件被修改后影响历史记录。
- 共享盘路径通过 `SharedPathResolver` 解析，必须防止目录穿越和非法路径。

### 经营报告

- `yixin/collaboration/reports.py` 负责统计聚合和 Excel 导出。
- `yixin/ui/reports_page.py` 负责筛选、指标卡、趋势图、排行和明细展示。
- 报表支持按任务创建时间或任务完成时间统计。
- 修改报表字段时，需要同步检查数据仓库查询、聚合对象、界面表格和 Excel 导出。

## 4. 数据边界

### 本机数据

默认位于 EXE 同级的 `data` 目录；源码运行时位于项目根目录的 `data`。可通过环境变量 `YIXIN_DATA_DIR` 覆盖。

```text
data/app.db           本机 SQLite 历史、队列、作品和设置
data/config           本机加密配置
data/temp             临时图片和生成结果
data/library          作品库文件
data/logs/app.log     脱敏运行日志
```

### MySQL 数据

包含：

- 账号、菜单权限和运营项目权限。
- 项目、主任务、子任务、素材和审查记录。
- 生成轮次、供应商调用、生成结果和成本。
- API 平台、模型路由、提示词模板和价格。
- 共享设置、任务编号序列和数据库迁移版本。

数据库连接和共享盘默认值由 `yixin/private_config.py` 提供。不要在日志、文档、截图或提交记录中暴露数据库密码、API Key 或其他敏感值。

## 5. 本地开发

在项目根目录执行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

启动前应确保：

- 本机能够访问内网 MySQL。
- MySQL 账号具备业务表读写权限。
- 共享盘 UNC 路径可访问，或管理员已配置有效共享根目录。
- 当前账号有至少一个可见菜单。

## 6. 测试

运行完整自动化测试：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

未配置 MySQL 测试环境时，真实数据库集成测试会自动跳过，这是预期行为。

MySQL 集成测试使用以下环境变量：

```text
YIXIN_TEST_MYSQL_HOST
YIXIN_TEST_MYSQL_PORT
YIXIN_TEST_MYSQL_DATABASE
YIXIN_TEST_MYSQL_USER
YIXIN_TEST_MYSQL_PASSWORD
```

测试库名称必须以 `_test` 结尾，禁止连接正式业务库执行测试。

新增或修改功能时至少检查：

- 对应业务模块的单元测试。
- 页面创建和关键按钮操作。
- 权限边界和角色切换。
- 本机 SQLite 与 MySQL 数据是否会出现不一致。
- 打包后是否仍包含新增模块和资源文件。

## 7. 打包发布

先安装 PyInstaller，再使用项目现有 `.spec`：

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean "亦欣AI生图工作台.spec"
```

默认输出：

```text
dist/亦欣AI生图工作台/亦欣AI生图工作台.exe
dist/亦欣AI生图工作台/_internal/
```

发布时需注意：

- 必须分发完整 onedir 目录，不能只复制 EXE。
- `_internal` 必须与 EXE 保持同级。
- 升级时不要覆盖目标电脑已有的 `data`。
- 新包不应包含开发机 `app.db`、API Key、日志或测试图片。
- 建议从临时副本启动新 EXE 做冒烟测试，避免测试数据写入正式分发目录。
- 如果构建目标目录正在运行或被日志文件占用，应改用新的 `--distpath`，不要强行关闭用户程序。

## 8. 数据维护

### 仅清空业务数据

保留账号、模板、价格、共享设置和表结构：

```powershell
.\.venv\Scripts\python.exe tools\clear_business_data.py --confirm "清空业务数据"
```

该脚本会删除项目、任务、素材、生成记录、审核记录和工作流事件，并重置相关自增编号。

### 全量重置

```powershell
.\.venv\Scripts\python.exe tools\reset_all_data.py
```

该脚本会先备份 MySQL 和本机 `data`，再清空全部业务数据、账号和本机数据。默认备份位置为桌面 `yixin-data-backups/<时间戳>`。

仅备份、不清理：

```powershell
.\.venv\Scripts\python.exe tools\reset_all_data.py --backup-only
```

清空操作不可恢复。执行前必须关闭应用、确认数据库目标并保留备份。

## 9. 常见故障

### Qt 地区枚举报错

如果出现 `QLocale has no attribute Territory`，使用当前 PySide6 支持的写法：

```python
QLocale(QLocale.Language.Chinese, QLocale.Country.China)
```

### PyInstaller 提示文件被占用

常见原因是旧程序仍在运行或杀毒软件正在扫描 DLL、日志。关闭对应程序，或使用新的 `--distpath` 生成到独立目录。

### 无法连接内网数据库

检查网络、MySQL 服务、账号权限和 `yixin/private_config.py` 配置。错误日志不应包含密码；界面只应显示脱敏错误。

### 共享盘路径错误

检查管理员配置的共享根目录、文件夹层级和当前账号项目权限。不要通过拼接未经校验的外部路径绕过 `SharedPathResolver`。

### 首次启动没有管理员

首次使用空数据库时，程序会要求创建首个管理员。如果数据库仍有用户但无法登录，应使用管理员账号重置密码，不要直接删除表。

### 界面入口存在但点击时报缺少属性

优先检查页面使用的对话框类是否同时具备：

- 控件状态读取方法。
- 保存或确认后的数据提取方法。
- 数据仓库方法的必填参数和关键字参数。

修改对话框时，应增加覆盖“打开对话框 -> 填写数据 -> 确认 -> 调用保存”的完整流程测试，而不只测试控件是否存在。

## 10. 修改与交付检查

提交代码前建议执行：

```powershell
git diff --check
git status --short
git diff --stat
```

交付前确认：

- 只修改与当前任务直接相关的文件。
- 没有遗留调试输出、真实密钥或测试数据。
- 自动化测试通过。
- 需要打包时已完成新版 EXE 启动验证。
- 发布 ZIP 与源码版本一致。
