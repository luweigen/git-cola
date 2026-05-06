# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Git Cola 是一个用 Python + Qt（通过 `qtpy`）编写的 Git GUI 客户端。源码可直接运行（无需安装），通过 `bin/git-cola`、`bin/git-dag`、`bin/git-cola-sequence-editor` 启动；也可通过 `python -m cola` 作为模块运行。Qt 后端通过环境变量 `QT_API` 在 `pyqt5` / `pyqt6` / `pyside2` / `pyside6` 之间切换（默认 `pyqt5`，回退到 `pyqt6` 再到 `pyside2`）。

## 构建与开发命令

项目使用 [garden](https://gitlab.com/garden-rs/garden) 作为主要任务运行器；`garden.yaml` 是真实数据源，`Makefile` 与 `tox.ini` 仅作补充。

```bash
# 一次性建立开发虚拟环境（创建 ./env3 并安装 dev/extras/build 依赖）
garden dev

# 直接从源码运行
garden run                  # 等价于 ./bin/git-cola
garden run/qt6              # 强制 QT_API=PyQt6
./bin/git-cola              # 不通过 garden 时的等价命令

# 单元测试
garden test                 # 运行 pytest（含 --doctest-modules）
garden test -- -k name      # 运行单个测试，例如：garden test -- -k cmds_test
garden test -- test/cmds_test.py::test_name  # 透传 pytest 参数

# 完整检查（提交前应跑通）
garden check                # = test + fmt --check + pyupgrade + mypy
garden fmt                  # 用 cercis + isort 自动格式化
garden check/mypy           # 仅类型检查（pyproject.toml 里配置）

# 多 Python 版本
garden tox                  # 通过 tox 运行 py39..py313

# 文档
garden doc                  # 同时构建 html 与 manpage
garden html                 # 仅 html

# 翻译
garden pot                  # 重新生成 cola/i18n/git-cola.pot
garden po                   # 用 pot 更新各 .po 文件（提交翻译前必须先跑）
```

`pytest.ini` 启用了 `--doctest-modules`：所有被收集的模块的 docstring 也会作为测试运行，编辑 docstring 中的示例时要确保它们仍然正确。

## 代码架构

代码按"模型 / 视图 / 命令"分层，由一个全局 `ApplicationContext` 把所有可变服务粘在一起。新代码不要从 `cola.app` 直接拉取全局，而是接受 `context` 参数。

### 入口

- `bin/git-cola` 与 `cola/__main__.py` 都委托给 `cola.main:main`。
- `cola/main.py` 用 `argparse` 子命令路由到子工具（`cola`、`am`、`archive`、`branch`、`browse`、`config`、`dag`、`diff`、`fetch`、`find`、`grep`、`merge`、`pull`、`push`、`rebase`、`remote`、`search`、`stash`、`tag`、`version`），每个子命令最终在 `cola/app.py` 里构建 `ApplicationContext` 并打开对应的 `ViewType`。
- `cola/app.py` 还负责：单实例锁（`QSharedMemory`）、HiDPI、信号处理、Qt 应用生命周期。

### `ApplicationContext`（`cola/app.py`）

聚合下列长寿命对象，多数视图、命令、模型只需持有 `context`：

- `git: cola.git.Git` — `git` CLI 的进程封装。
- `cfg: cola.gitcfg.GitConfig` — 缓存的 Git 配置。
- `model: cola.models.main.MainModel` — 仓库状态（暂存/工作区/未跟踪/合并/分支/diff），是 `QObject`，通过 Qt 信号广播变更。
- `selection: cola.models.selection.SelectionModel` — 状态视图当前选中的文件。
- `fsmonitor` — 文件系统监视（变更后触发后台刷新）。
- `notifier: Notifier` — 一次性消息总线（`command/critical/information/log/message`）。
- `command_bus: CommandBus` — 异步执行命令的信号桥（见下）。
- `runtask: qtutils.RunTask` — 后台任务调度。
- `settings: cola.settings.Settings` — 用户偏好（独立于 git config）。
- `view`、`browser_windows` — 当前主窗口与额外的 browse 窗口。

### 命令系统（`cola/cmd.py`、`cola/cmds.py`）

所有可撤销/可观察的操作都包装为 `Command`。常见基类：

- `Command` — 最小接口：`do()` / `undo()` / `is_undoable()`。
- `ContextCommand(Command)` — 持有 `context`、`model`、`cfg`、`git`、`selection`、`fsmonitor` 与时间戳；`do()` 通过比对 `context.timestamp` 丢弃过时的后台结果（避免旧的 diff/查找结果覆盖新的）。

`cola/cmds.py` 包含主要的领域命令（暂存、提交、合并、Apply、Diff、Stash 等）。GUI 触发命令时通常发到 `context.notifier.command` 或 `context.command_bus`，从而保证它们在事件循环中串行化、可被状态视图统一观察。新增功能的"动作"应当继承 `ContextCommand` 并把 UI 副作用通过 `Interaction` / `notifier` 的信号汇报。

### 模型（`cola/models/`）

- `main.MainModel` — 中心模型，已知偏大；其内部注释说明计划拆分为 `DiffModel` / `CommitMessageModel` / `StatusModel` / `DiffEditorState`。改动时优先保持现有信号契约（`about_to_update`、`diff_text_changed`、`diff_type_changed` 等）。
- `browse`、`dag`、`graph`、`prefs`、`selection`、`stash` — 各自对应窗口/面板的状态。

视图层在 `cola/widgets/` 与 `cola/dag.py`，每个文件对应一个对话框或面板（`commitmsg.py`、`status.py`、`diff.py`、`dag.py`、`finder.py` 等）。这些 widget 接受 `context`，监听模型信号并通过命令把用户操作回写。

### Git 抽象（`cola/git.py`、`cola/gitcmds.py`、`cola/gitcfg.py`）

- `git.Git` 通过 `subprocess` 调用 `git` 二进制；统一使用 `from .git import STDOUT` 索引返回值的 `(status, stdout, stderr)` 元组。
- `gitcmds.py` 是高级 Git 操作（log、diff、branch 列表等）的纯函数集合，输入 `context`、输出原始数据，由模型/命令再加工。
- `gitcfg.GitConfig` 缓存 `git config` 并监听变更。
- 不要在 widget/命令中直接 `subprocess`，要么走 `context.git`，要么走 `gitcmds`。

### Qt 兼容层

`cola/qtpy/` 与本仓库根目录的 `qtpy/` 是为打包冗余携带的副本，运行时永远 `from qtpy import ...`。新代码应保持 PyQt5/PyQt6/PySide2/PySide6 全部可用：避免使用某一绑定独有的枚举写法（统一用 `Qt.X` 而非 `QtCore.Qt.X` 形式时遵循现有模式）。

### 国际化

字符串用 `from .i18n import N_` 包裹（`N_("...")`）。新增翻译串后运行 `garden pot && garden po`，再编辑 `cola/i18n/*.po`。`garden po` 会规范化 PO 文件——提交翻译前必须运行。

### 测试（`test/`）

- 命名约定 `*_test.py`，按被测模块分组。
- `test/helper.py` 提供常用 fixture；`test/fixtures/` 含真实 git 仓库样例。
- 由于 `pytest.ini` 启用了 `--doctest-modules`，`cola/*.py` 与 `test/*.py` 的 docstring 示例也会被收集，遇到 doctest 失败请同时检查 docstring。

## 代码风格

- Python 3.9+；`mypy` 严格度遵循 `pyproject.toml`。命令行检查走 `garden check/mypy`。
- 使用 `cercis`（基于 black 的格式化器）+ `isort --force-single-line-imports --py=39 --no-lines-before=STDLIB`。提交前 `garden fmt`。
- 命名使用 `snake_case`；只有覆盖 Qt 方法时才允许 `camelCase`。
- 提交信息使用 `area: short imperative` 格式（例如 `dag: fix line overlap`）。

## 任务约定

当开发者描述左边dag图时，指的是运行python -m cola dag后，左 dock (log_dock)：CommitTreeWidget 列表 + GraphDelegate 内联。
描述右边dag图时，指的是运行python -m cola dag后，右 dock (graphview_dock)：GraphView (QGraphicsScene 节点+连线) 。