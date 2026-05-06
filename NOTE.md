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

## 其它

- 顶部新增 `from ..interaction import Interaction` 导入（merge 流程用到 `Interaction.confirm`）。
- 没有改动 `pyproject.toml` 等元数据；纯 `.py` 改动，重启进程即可生效。
