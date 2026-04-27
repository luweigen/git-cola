# 开发模式安装与代码变更生效说明

## 问题

用 `./venv/bin/pip install --editable '.[extras,pyqt6]'` 安装依赖后，修改代码，`./venv/bin/python -m cola dag` 运行就能看到变化吗？

## 结论

可以。`--editable`（即 `pip install -e`）会把项目以"开发模式"装进 venv：site-packages 里只放一个指向源码目录的链接（`.pth` 或 `__editable__` 钩子），`import cola` 实际上是直接从 `/Users/luwei/work/git-cola/cola/` 读文件。所以改完 `.py` 文件，**重新运行** `./venv/bin/python -m cola dag` 立刻就能看到变化，不需要再 `pip install`。

## 注意事项

1. **必须重启进程**。Python 不会热重载模块——改完代码要关掉当前的 git-dag 窗口再重启。
2. **`pyproject.toml` 里的元数据改动需要重装**。比如修改 `[project.scripts]`、`dependencies`、`optional-dependencies`、入口点等，要再跑一次 `pip install --editable '.[extras,pyqt6]'`，否则 `bin/git-cola` 那种 console_script 包装器不会更新。纯 `.py` 源码改动不受影响。
3. **非 Python 资源**（`cola/icons/*.svg`、`cola/i18n/*.po`、`cola/data/*.html` 等）也是直接从源码目录读的，改完同样立刻生效；但 `.po` 翻译需要先编译/重载（git-cola 启动时读 `.po`，重启即可）。
4. **两种等价启动方式**，二选一即可：
   - `./venv/bin/python -m cola dag`
   - `./venv/bin/git-cola dag`（或激活 venv 后 `git cola dag` / `git dag`）
5. **想强制 Qt6**：`QT_API=pyqt6 ./venv/bin/python -m cola dag`，或用项目自带的 `garden run/qt6`。

## 排查"改了代码却没生效"

多半是以下三种情况：

- **(a) 没重启进程** —— Python 模块已被加载到内存，必须退出当前进程重新启动。
- **(b) 改的是 `pyproject.toml` 而没重装** —— 元数据/入口点变更需要再跑一次 `pip install --editable`。
- **(c) 跑成了系统 `git-cola` 而不是 venv 里的** —— 用以下命令确认：

```bash
which git-cola
./venv/bin/python -c "import cola; print(cola.__file__)"
```

第二条命令应当输出 `/Users/luwei/work/git-cola/cola/__init__.py`，说明导入路径指向你的源码目录。

---

# DAG 视图定制改动

以下所有改动均集中在 `cola/widgets/dag.py`。

## 1. 缩短节点垂直间距

- `GraphView.y_off`：`-20` → `-15`。
- 推导：`commit_radius = 12.0` 实际上是节点的渲染**直径**（`QRectF(-r/2, -r/2, r, r)`），原中心距 20 = 直径 12 + 可见空隙 8；空隙缩小到 1/3 ≈ 3，即新中心距 12 + 3 = 15。
- 节点垂直距离视觉上缩窄了约 2/3。

## 2. 点击分支/标签复制名字

`Label` 类（每个 commit 旁的分支/标签框）改造：

- `__init__`：新增 `_label_hits = []`，光标设为 `Qt.PointingHandCursor`。
- `paint()`：除了原有绘制，每个标签的 `(QRectF, 显示文本, 是否 HEAD)` 被记录到 `_label_hits`，供命中测试使用。
- `mousePressEvent`：左键点击命中某个标签框时——
  - HEAD：直接 `qtutils.set_clipboard('HEAD')`。
  - 其它：弹出菜单（见下条）。
  - 处于 merge 模式时（见 §4）：把当前命中标签当作 merge 目标。

## 3. agent 分支特殊菜单

`Label._show_branch_menu` + 模块级 `_agent_branch_part(name)`：

- `_agent_branch_part` 识别 `agent_` / `agent/` 两种前缀，返回前缀后到下一个 `/` 之前的段；否则返回 `None`。例：
  - `agent_foo/bar` → `foo`
  - `agent/foo/bar` → `foo`
  - `agent_foo` → `foo`
  - `main` → `None`
- 菜单结构：
  - `Copy "完整名"`
  - `Copy "agent 段"`（仅在以 `agent_` / `agent/` 起首时出现）
  - 分隔线
  - `Merge to...`

普通分支（非 agent、非 HEAD）的菜单只有 `Copy "完整名"` + `Merge to...`。HEAD 不弹菜单。

## 4. Merge to 流程

源头：用户在分支菜单选 `Merge to...`。

`GraphView` 端：

- 类信号 `merge_source_changed = Signal(object)`。
- 状态 `self._merge_source`（None 表示未激活）。
- `enter_merge_mode(source_branch)`：记录 source、`setFocus`、`emit(source)`。
- `exit_merge_mode()`：清状态、`emit(None)`。
- `complete_merge(target_branch)`：调用 `Interaction.confirm` 弹一次确认框，OK 后顺序执行：
  ```
  cmds.do(cmds.CheckoutBranch, context, target_branch)
  cmds.do(cmds.MergeBranch, context, source)
  ```
- `keyPressEvent`：merge 模式下 ESC 取消。
- `mousePressEvent`：merge 模式下左键命中非 `Label`（commit 圆点 / 空白）即取消；右键也取消；命中 `Label` 时正常分发，由 `Label.mousePressEvent` 调 `complete_merge`。
- `Label.mousePressEvent`：merge 模式下命中 HEAD 忽略（HEAD 不能作为目标）。

## 5. 工具栏 merge 状态提示

不再用光标变化提示 merge 模式，而是在 graph 工具栏 `Zoom Out` 按钮**左侧**显示文字。

- `GitDAG.__init__`：新建隐藏 `QLabel self.merge_source_label`，作为 `graph_controls_layout` 的第一个元素。
- `graphview.merge_source_changed` 连到 `GitDAG._update_merge_source_label`：
  - source 非空：显示 `"<source> → ?"` 并 `show()`。
  - source 为 `None`：`clear()` + `hide()`。

`enter_merge_mode` / `exit_merge_mode` 不再修改 `viewport()` 的 cursor。

## 6. 无分支节点显示 commit 消息首行

为了在没有分支标签的提交点旁边给一些上下文：

- 模块常量 `_SUMMARY_MAX_CHARS = 50`。
- `Commit.__init__`：保留原有 `Label` 的创建（仅当 `commit.tags` 非空）；同时初始化 `summary_label = None`、`_summary_side = None`。
- `Commit.update_summary_label(side)`：`side ∈ {'right', 'left', None}`。和上次相同的 side 直接返回；否则移除已有 `summary_label` 并按需新建：
  - 取 `commit.summary` 第一行；超 50 字截到 49 + `…`。
  - 用 `QGraphicsSimpleTextItem`，`Cache.label_font()`，`QApplication.palette().text()` 作 brush（兼容暗色主题）。
  - `right`：`x = commit_radius/2 + 2`；`left`：`x = -commit_radius/2 - 2 - text_width`。
  - `zValue = -1`。

## 7. 同行多节点：只在两端显示消息

- `GraphView.layout_commits` 末尾调用新增方法 `_update_summary_labels()`。
- `_update_summary_labels`：
  - 按 `node.row` 分组。
  - 行内只 1 个 → 右侧显示。
  - ≥2 个 → 按 `node.column` 排序：列号最大 = 视觉最左（因 `x_off=-18`）→ 左侧显示；列号最小 = 视觉最右 → 右侧显示；中间节点 `update_summary_label(None)` 清除。

每次 `add_commits` 触发的 `layout_commits` 都会重算，所以增量加载也会动态调整。

## 8. 右键菜单新增 "Copy Commit Message"

在 `treewidget` / `graphview` 的右键菜单（`viewer_actions` 共享）中，`Copy Commit` 之下加 `Copy Commit Message`，复制完整 commit message。

- `ViewerMixin.copy_commit_message_to_clipboard` + `_copy_commit_message(oid)`：
  ```
  status, out, _ = context.git.log('-1', '--format=%B', oid, _readonly=True)
  qtutils.set_clipboard(out.rstrip('\n'))
  ```
- `viewer_actions` 字典新增 `'copy_message'` 条目（`icons.copy()` icon，`N_('Copy Commit Message')`）。
- `context_menu_event`：在 `copy_short` / `copy` 之后追加 `copy_message`。
- `update_menu_actions`：和其它 copy 项一致，按 `has_single_selection_or_clicked and has_oid` 启用。

## 其它

- 顶部新增 `from ..interaction import Interaction` 导入（merge 流程用到 `Interaction.confirm`）。
- 没有改动 `pyproject.toml` 等元数据；纯 `.py` 改动，重启进程即可生效。
