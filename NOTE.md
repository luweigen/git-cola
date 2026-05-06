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

- `GraphView.y_off`：`-20` → `-12`。
- 推导：`commit_radius = 12.0` 实际上是节点的渲染**直径**（`QRectF(-r/2, -r/2, r, r)`），原中心距 20 = 直径 12 + 可见空隙 8；空隙缩小到 1/3 ≈ 3，即新中心距 12 + 3 = 15。
- 节点垂直距离视觉上缩窄了约 2/3。
- 手工调整到 -12 视觉效果更好。

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

## 9. orphan 分支列隔离（`--orphan-isolate`）

### 动机

仓库里如果有 orphan 分支（`git checkout --orphan ...` 建出来的、没有共同祖先的链），它和其它链在 GraphView（右侧 dock）里经常会被画到**同一列**——视觉上看起来像一条共享的纵线，紫线还可能撞色，让人误以为两条链是连接在一起的。例：

```
长链 fork → 07e7305 → ... → 6e45127 → a5eb66d → cfd4542   全在 col 1
                                       (cfd4542 处 leave_column(1)，col 1 释放)
orphan tip → 696ea85 → 745d4cc → ... → b33df90              紧接着也拿到 col 1
```

`recompute_grid` 按 `generation` 升序遍历，但 `CommitFactory` 给无父 commit 的 generation 设成当前 `root_generation`（已建过 commit 的最大 gen），所以 orphan 自己的迭代步反而排到很后——前面长链的 fork 早就把它"想要"的列拿走又放回去了，等 orphan 来时刚好捡到同一列。

### 新参数（CLI / git config / DAG 模型）

| 层 | 名称 | 默认 | 说明 |
|---|---|---|---|
| CLI | `--orphan-isolate`（`cola/main.py:171`） | 关 | `python -m cola dag --orphan-isolate --all` |
| git config | `cola.dag.orphanisolate`（`cola/widgets/dag.py:55`，`git_dag()` 启动时读） | `false` | 持久化默认值；CLI 优先 |
| `DAG` 模型 | `DAG.orphan_isolate: bool` + `set_orphan_isolate(value)`（`cola/models/dag.py:48-90`） | `False` | 接受 bool / "true"/"yes"/"on"/"1" 字串 |
| `GraphView` 字段 | `GraphView.orphan_isolate`（`cola/widgets/dag.py:2628`） | `False` | 在 `GitDAG.set_params` 中从 `params.orphan_isolate` 同步 |

`CommitTreeWidget`（左侧 inline graph）**不受**这个参数影响——左侧用的是 `cola/models/graph.py::build_graph`，本来就没有这个视觉问题，2025-XX 的回退把 `build_graph` 恢复到 a4d121b 之前的版本。

### 算法改动（全部在 `cola/widgets/dag.py::GraphView`）

#### 9.1 `recompute_grid` 前置 orphan 列预分配（`dag.py:3289-3304`）

主循环之前先扫一遍 `self.commits`，给所有无父 commit 用 `alloc_column()` 各占一个列号，并立刻：
- 加入 `self._orphan_columns`：标记「这是 orphan 链占的列」。
- 加入 `self._reserved_columns`：让后续 `alloc_column` 永远跳过它。

这样 orphan 还没轮到主循环时它的列就已经被锁住——长链 fork 的第二个子分支再 `alloc_column` 时会绕开这一列，cfd4542 一类的链不会再撞进 orphan 列。

```python
if self.orphan_isolate:
    for node in list(self.commits):
        if not node.parents and node.column is None:
            node.column = self.alloc_column()
            self._orphan_columns.add(node.column)
            self._reserved_columns.add(node.column)
```

#### 9.2 `alloc_column` 跳过 reserved 列（`dag.py:3198-3242`）

新增 `is_free(c) := c not in self.columns and c not in self._reserved_columns`，Phase 1（`desired → 0`）和 Phase 2（从中心扩散）都用它替代原来的 `c not in columns`。reserved 列就是 9.1 预分配的 orphan 列——对所有非 orphan 的 fork/分配都不可见。

#### 9.3 `alloc_column` Phase 2 同侧优先（`dag.py:3219-3239`）

原 Phase 2 的展开顺序是 `0, 1, -1, 2, -2, …` 围绕中心对称。当 desired 是负数（fork 的次级支线想留在父亲所在的负半边）时，原顺序会先去到 `+offset` 再到 `-offset`，结果次级支线被弹到正半边，必须横穿 col 0/1 才能连回父亲。

改成根据 `desired` 的正负决定哪一侧先尝试：

```python
sign = -1 if column < 0 else 1
for offset in itertools.count(0):
    same_side = sign * offset
    if is_free(same_side): col = same_side; break
    other_side = -sign * offset
    if is_free(other_side): col = other_side; break
```

`desired ≥ 0` 时序列还是 `0, 0, 1, -1, 2, -2, …`（与原版一致）；`desired < 0` 时变成 `0, 0, -1, 1, -2, 2, …`，次级支线优先落在负侧。

直接受益：在 isolate=True 下，2d9d1d3 fork 的两个子之一（次级 7d8040f）以前被推到 col 2（跨过 orphan col 1），现在留在 col -2，与父链同侧；`cfd4542 → d06c8b7` 这条边整段都在负半边，**不再穿越 col 1 的 orphan 链**。

#### 9.4 `leave_column` 永久 reserve 兜底（`dag.py:3274-3287`）

在 9.1 已经把 orphan 列写进 `_orphan_columns` + `_reserved_columns`。orphan 链跑到 leaf（`b33df90` 之类）触发 `leave_column(col)` 时，`self.columns[col]` 计数归零、被 `del`，但只要 isolate 还开着且这个列在 `_orphan_columns`，就 `_reserved_columns.add(col)`（实际已经在里面，等价 no-op）。这是历史上唯一一次 reserve 路径，9.1 加上之后它退化成「冗余但安全」——保留是为了：
1. 让代码意图自洽：「leave 一个 orphan 列就是不能再被复用」是个独立的不变量。
2. 万一以后改 9.1 的预分配策略，这一行仍能兜底。

```python
def leave_column(self, column):
    count = self.columns[column]
    if count == 1:
        del self.columns[column]
        if column in self._orphan_columns:
            self._orphan_columns.discard(column)
            if self.orphan_isolate:
                self._reserved_columns.add(column)
    else:
        self.columns[column] = count - 1
```

#### 9.5 `GitDAG.set_params` 同步给 GraphView（`dag.py:1599-1601`）

```python
self.graphview.orphan_isolate = bool(getattr(params, 'orphan_isolate', False))
```

每次 `set_params` 都会把 `params.orphan_isolate` 传给 graphview；后续 `recompute_grid()` 直接读这个字段。

### 实测对比（Simulation repo，1310 个 commit，`--all`）

| commit | 默认 col | `--orphan-isolate` col |
|---|---|---|
| f4b5f1c（老 orphan-root） | 0 | 0 |
| 6e45127 / a5eb66d / ee6ec7d / 4222070 / cfd4542 | 1 | **−1** |
| 2d9d1d3 / d06c8b7 | 0 / 0 | **−2** / **−2** |
| 696ea85 / 745d4cc / b33df90（orphan 链） | 1（撞 cfd4542） | **1**（独占）|
| 7d8040f / e7c4c94 | −1 | 2 |
| max / min column | 1 / −1 | 2 / −2 |

`cfd4542 → d06c8b7` 这条边在默认下从 col 1 →（cfd4542 离开）→ col 0，再加颜色 cycle 撞到 696ea85 链同色，视觉上像 696ea85 的延续；isolate 后整段在 col -1 / -2 上，与 col 1 的 orphan 链彻底分开。

### 已知未做（如果以后还想推进）

- `7d8040f → 2d9d1d3` 这条 task-notification 链的边在 isolate 下从 col 2 跨到 col -2，会斜穿过 col 1。但它的 row 范围（≈1216–1217）低于 orphan 链（≈1220–1226），y 上不重叠，视觉上和 orphan 链分得开，没继续修。
- 如果撞色仍然让人混淆，下一步可以加方案 B（orphan 链的边走专属色 + `Qt.DashLine`），实现位置在 `Edge.__init__`（`cola/widgets/dag.py:2001-2028`）+ `recompute_grid` 末尾给 commit 打个 `is_orphan_chain` 标志位。
- `cola/models/graph.py::build_graph` 已回退到 a4d121b 之前，不再接受任何 isolate 类参数；以后若要让左侧 inline graph 也支持，需另起一套 lane-reserved 标志（先前实现已被回退掉）。

## 10. 边走直线还是弧线（`cola.dag.arcedges`）

### 动机

GraphView 原来对所有 `source.x() != dest.x()` 的边一律走"垂直短桩 + 两个 90° 圆角 + 中间水平段"的弧形路径（`Edge.recompute_path` 老版）。在没有 fork 的简单图上没问题，但只要源和目标列差距大，弧线就会**先朝上"逃离"源点几个像素再向右走水平段**——结果它会从一个不相关分支的 commit 圆点正上方/正下方贴着穿过，看起来像是弧线"经过"了那个 commit。例：

```
6e45127(col 0) ─┐                a5eb66d(col -1) ●
                │ ↑ 先垂直 5px       │
                └→─→─→─→─→─→─→─→─→─→●  2d9d1d3(col -2)
```

`6e45127 → 2d9d1d3` 弧线先向上、再水平、向上跨过 a5eb66d，再下到 2d9d1d3，肉眼以为 a5eb66d 也在这条边上。

### 新参数

| 层 | 名称 | 默认 | 说明 |
|---|---|---|---|
| git config | `cola.dag.arcedges` | `false` | `git_dag()` 启动时读，`bool` / `"true"`/`"yes"`/`"on"`/`"1"` 都识别（`_config_truthy`） |
| `GraphView` 字段 | `GraphView.arc_edges` | `False` | 启动时由 `git_dag()` 直接 set 到 graphview，没经过 `DAG` 模型（纯渲染偏好） |
| `Edge` 构造参数 | `Edge.__init__(..., arc_edges=False)` | `False` | `GraphView.link()` 创建 Edge 时传 `self.arc_edges` |

**没有 CLI 选项**——这是渲染样式偏好，不是数据层选项。要切换风格用：

```bash
git config --global cola.dag.arcedges true   # 保留旧的弧形风格
git config --global --unset cola.dag.arcedges  # 回到默认直线
```

注意：和 `--orphan-isolate` 一样，配置只在 `git_dag()` 启动时读一次；改完 git config 要重启 git-dag 才生效。

### 算法改动

#### 10.1 `Edge.recompute_path` 直线分支（`cola/widgets/dag.py:2059-2107`）

把原来的 `if source.x == dest.x: 直线 else: 弧形` 改成 `if not arc_edges or source.x == dest.x: 直线 else: 弧形`：

```python
if not self.arc_edges or self.source.x() == self.dest.x():
    path.moveTo(self.source.x(), self.source.y())
    path.lineTo(self.dest.x(), self.dest.y())
else:
    # ... 原弧形路径不动 ...
```

直线就是 `moveTo(source.center) + lineTo(dest.center)`。`Edge.setZValue(-2)` 让边低于 commit 圆点（z=0），所以线的两端被两个圆点遮住，中间是干净的点对点斜线段——不会贴着源点起步往上"逃"，自然也不会从其它分支头顶蹭过去。

#### 10.2 `GraphView.link()` 透传到 Edge（`cola/widgets/dag.py:3016`）

```python
edge = Edge(parent_item, commit_item, arc_edges=self.arc_edges)
```

#### 10.3 `git_dag()` 在 `set_params` 之后写入 graphview（`cola/widgets/dag.py:60-77`）

紧接 `view = GitDAG(...)` / `view.set_params(...)`，在 `view.show()` / `view.display()` 之前：

```python
view.graphview.arc_edges = _config_truthy(
    context.cfg.get('cola.dag.arcedges', default=False)
)
```

放这里是因为：
1. `view.display()` 之后 ReaderThread 才启动 → `add_commits` → `link()` 创建 Edge，所以 `arc_edges` 必须早于 `display()` 设好。
2. 不走 `DAG` 模型 / `set_params` 链路：`arc_edges` 是渲染偏好，不像 `orphan_isolate` 那样会改 row/col 数据，没必要污染数据层。

新增模块级辅助函数 `_config_truthy(value)`：把 git config 的字符串值（`"true"` / `"yes"` / `"on"` / `"1"`）和 Python bool 都规范成 bool。

### 实测对比（Simulation repo）

`cfg unset` 时：`view.graphview.arc_edges == False` → 默认直线。
`cfg cola.dag.arcedges true` 后：`view.graphview.arc_edges == True` → 老弧形回来。

视觉上：默认直线模式下 `6e45127 → 2d9d1d3` 不再贴着 a5eb66d 走 ┐ 形，而是从源圆点中心直接拉一条斜线到目标圆点中心，中间不再"绕过"任何无关分支。

### 已知未做

- 直线模式下，commit 圆点和边线的接触点是数学上的圆心而非圆周——`zValue=-2` 让圆点把这段遮住，但放大很多倍时仍可能露出极短一段。如果以后想做得更精致，可以在 `recompute_path` 把端点截到圆周（`commit_radius/2` 半径外的交点），不过当前缩放下没必要。
- `arc_edges=True` 时仍是老弧形路径，问题（绕过其它分支头顶）依旧存在。这条路径只服务于"想要旧视觉"的用户，没改它的几何。

## 11. "Rename to ..." 菜单（按 commit 文件命名）

### 动机

agent 自动化经常把当前正在处理的文档/脚本作为分支的语义标识。手工把 `agent/<id>` 改名成 `agent/<id>/foo.md,bar.py` 这种格式可以一眼看到这条分支当前在搞什么——但每次都要手动敲文件名。新加的 `Rename to ...` 直接读分支 tip commit **本次修改了哪些文件**，把不以 `_` 起首的相对路径的 basename 拼好，作为重命名建议预填到弹窗里。

### 触发位置

点 commit 旁边的 branch 标签（`Label`），弹出菜单，紧跟 `Rename "<full>"...` 之后（仅本地分支）。如果该 commit 没有任何符合条件的修改文件，**这条菜单不出现**——避免出现 `branch/`（空建议）这种没用的形态。

### 实现

#### 11.1 helper：`_branch_tip_basenames(context, oid)`（`cola/widgets/dag.py:_branch_tip_basenames`）

```python
def _branch_tip_basenames(context, oid):
    if not oid:
        return []
    try:
        paths = gitcmds.changed_files(context, oid)
    except Exception:
        return []
    seen = set()
    out = []
    for path in paths:
        if not path or path.startswith('_'):
            continue
        name = path.rsplit('/', 1)[-1]
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out
```

- 走已有的 `gitcmds.changed_files(context, oid)`：底层 `git diff-tree --no-commit-id --name-only -r -z <oid>~ <oid>`；root commit 时自动回退到对 `empty_tree_oid` 的 diff（即把 initial commit 当作"全部新增"）。merge commit 默认返回空。
- 过滤 `path.startswith('_')`：只看**相对路径首字符**不是下划线的（`_traj/x.json` 整条丢；`tmp/_foo.py` 保留——因为 path 首字符是 `t`）。
- `path.rsplit('/', 1)[-1]` 取 basename。
- 用 `seen` 去重并保留首次出现顺序（同名文件出现在多个目录时，结果里只留一份）。
- 任何 git 错误返回 `[]`，调用方据此隐藏菜单项。

#### 11.2 `_show_branch_menu` 加菜单条目（`cola/widgets/dag.py:_show_branch_menu`）

在 `is_local_branch` 分支里现有的 `rename = menu.addAction(...)` 之后：

```python
graph_view = self._graph_view()
if graph_view is not None:
    basenames = _branch_tip_basenames(
        graph_view.context, getattr(self.commit, 'oid', None)
    )
    if basenames:
        rename_to_target = '%s/%s' % (full_name, ','.join(basenames))
        rename_to = menu.addAction(
            N_('Rename to "%s"...') % rename_to_target
        )
```

`Label.commit` 是这个标签所属的模型 commit（直接构造时存的，见 `Label.__init__:2368`），oid 即分支 tip。

#### 11.3 抽出 `_rename_branch(full_name, suggestion)`（`cola/widgets/dag.py`）

原来的 `Rename` 处理路径直接内联在 `_show_branch_menu` 里。现在 `Rename` 和 `Rename to` 共用同一段「弹 `_prompt_wide` → strip → 跑 `cmds.RenameBranch` → 触发 `merge_finished` 让图刷新」的流程，差别只是 **预填的 `text=` 不同**：

```python
def _rename_branch(self, full_name, suggestion):
    graph_view = self._graph_view()
    if graph_view is None:
        return
    new_name, ok = _prompt_wide(
        N_('Enter new branch name'),
        title=N_('Rename "%s"') % full_name,
        text=suggestion,
        width_factor=4,
    )
    if not ok:
        return
    new_name = new_name.strip()
    if not new_name or new_name == full_name:
        return
    result = cmds.do(cmds.RenameBranch, graph_view.context, full_name, new_name)
    if result and result[0] == 0:
        graph_view.merge_finished.emit()
```

调用：

- `Rename "..."` → `self._rename_branch(full_name, suggestion=full_name)`（弹窗预填原名，等价旧行为）。
- `Rename to "..."` → `self._rename_branch(full_name, suggestion=rename_to_target)`（弹窗预填 `<full>/<basenames>`）。

弹窗给用户最后一次确认/编辑的机会——分支命名规则会拒绝某些字符（`~`、`^`、`:`、空格等），如果文件名里含这些，提交时 `git branch -m` 会报错，此时用户可以在弹窗里改掉。

### 实测

本仓库 `agent/98f9b71f-2af1-4088-a254-12b95ee60c2a` 分支的最新 commit `4f86d1678c46` 改动了：

```
NOTE.md
_traj/98f9b71f-2af1-4088-a254-12b95ee60c2a.json
_traj/98f9b71f-2af1-4088-a254-12b95ee60c2a.jsonl
cola/widgets/dag.py
```

`_branch_tip_basenames(context, '4f86d1678c46...')` 返回 `['NOTE.md', 'dag.py']`——`_traj/...` 两条因为相对路径首字符是 `_` 被过滤，剩下两条取 basename 后即得。建议名 `agent/98f9b71f-2af1-4088-a254-12b95ee60c2a/NOTE.md,dag.py`。

orphan root（如 Simulation repo 的 `696ea85`，"Initial commit"，空 tree、对空树 diff 也是空）→ 返回 `[]`，菜单不出现。

220 个测试全过。

## 12. 分支标签按 lane 上色（`cola.dag.legacylabelcolors`）

### 动机

GraphView 里 `Label` 原本用三档固定色：
- HEAD / `tags/*` / 当前 local branch → `remote_color = yellow`
- 其它 `heads/*`（非当前）→ `head_color = green`
- `remotes/*` 等 → `other_color = white`

每条 lane 的 commit 圆圈连线已经按调色板循环着色（红/青/紫/绿/橙），但分支标签自己还是上面三档不变，结果 dot 是紫线、label 却是白底/绿底，肉眼对不上"哪条 lane 是哪个 branch"。

### 行为

- **HEAD 和当前 local branch 永远黄色**（不变，这是用户当前位置的强信号）。
- `tags/*` 仍黄色（标签不是分支，保留原配色）。
- `heads/<非当前>` 和 `remotes/*`：**默认改成"该 commit 出去那条边的颜色"**，让标签和 lane 视觉对齐。
- 想要回到旧三档配色：`git config --global cola.dag.legacylabelcolors true`。

### 新参数

| 层 | 名称 | 默认 | 说明 |
|---|---|---|---|
| git config | `cola.dag.legacylabelcolors` | `false` | `_config_truthy` 解析（`true/yes/on/1`） |
| `GraphView` 字段 | `GraphView.legacy_label_colors` | `False` | `git_dag()` 启动时 set 一次；`Label.paint()` 里读 |

没有 CLI 选项。和 `arc_edges` 一样，改 git config 后要重启 git-dag 才生效。

### 实现

#### 12.1 `Label._edge_color()`（`cola/widgets/dag.py:Label._edge_color`）

```python
def _edge_color(self):
    commit_item = self.parentItem()
    if commit_item is None:
        return None
    commit = getattr(commit_item, 'commit', None)
    parents = getattr(commit, 'parents', None) if commit else None
    edges = getattr(commit_item, 'edges', None)
    if not parents or not edges:
        return None
    edge = edges.get(parents[0].oid)
    if edge is None or edge.pen is None:
        return None
    color = QtGui.QColor(edge.pen.color())
    color.setAlpha(255)
    return color
```

- `Label.parentItem()` 是该 commit 的 `Commit` graphics item（在 `Commit.__init__` 里 `label.setParentItem(self)` 设的）。
- `Commit` 的 `edges` dict 在 `GraphView.link()` 中填充，键是父 oid，值是 `Edge`。取**第一父**那条边的 `pen.color()`。
- root commit（`parents == []`）→ 没边 → 返回 `None`，调用方退回老配色。
- `Edge.pen` 的颜色 alpha 是 128（半透明，画线时柔和），但作为标签底色用 alpha=128 会让场景背景透出来，所以拷贝一份 `QColor` 后 `setAlpha(255)` 改成不透明。

#### 12.2 `Label.paint()` 三处分支挂上 `edge_color`（`cola/widgets/dag.py:Label.paint`）

每次 paint 起点：

```python
graph_view = self._graph_view()
use_edge_colors = bool(
    graph_view and not getattr(graph_view, 'legacy_label_colors', False)
)
edge_color = self._edge_color() if use_edge_colors else None
```

- `remotes/*`：`edge_color is not None → 用 edge_color；否则 → other_color`（白）。
- `tags/*`：保持 `remote_color`（黄），不动。
- `heads/<X>`：`X == current_branch → remote_color`（黄）；否则 `edge_color is not None → 用 edge_color`；最后兜底 `head_color`（绿）。
- HEAD 和其它默认分支：保持原色。

每个 commit 的所有非当前分支/远端标签共享同一个 `edge_color`（一个 commit 出去只有一条 first-parent 边），所以一行多个 label 会"同色一致"。

#### 12.3 `git_dag()` 读 config 后挂到 graphview（`cola/widgets/dag.py:git_dag`）

```python
view.graphview.legacy_label_colors = _config_truthy(
    context.cfg.get('cola.dag.legacylabelcolors', default=False)
)
```

复用了 §10 的 `_config_truthy`。

### 实测

`cfg unset` → `legacy_label_colors == False`，新行为生效。
`git config cola.dag.legacylabelcolors true` → `legacy_label_colors == True`，三档配色回来。

视觉上：在 Simulation repo `--orphan-isolate --all` 下，`agent/cc998470-...` 这个分支位于 col -1（cfd4542 lane，调色板里的紫色 lane），它的 label 默认就显示紫色底；`agent/db620589-...` 在另一条 lane（红色），label 显示红色底；只有 HEAD 和 `current_branch` 仍是黄色。

220 个测试全过。

### 边角

- 文本笔仍是黑色（`text_pen`）。调色板的 5 种基础色（红/青/紫/绿/橙）配黑字都可读；如果以后加了暗色或低对比度色，可能要按背景亮度切换 text_pen 的颜色。
- root commit 的分支标签（无父 → 无 edge）会落到老配色（绿/白）。
- 多条标签同 commit 时，所有非 current 标签都用同一个 lane 色。这是按"这个 commit 在哪条 lane"决定的，不是按各自分支的 lane（实际上多个分支头都指着同一个 commit，它们物理上就是同一条 lane）。
- 法外狂徒：`Edge.recompute_path()` 用直线（§10 默认）时，pen color 仍按 `EdgeColor.cycle/current` 选取，与本节读取方式无关。

## 其它

- 顶部新增 `from ..interaction import Interaction` 导入（merge 流程用到 `Interaction.confirm`）。
- 没有改动 `pyproject.toml` 等元数据；纯 `.py` 改动，重启进程即可生效。
