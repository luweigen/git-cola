# 在 DAG 里显示 agent session 的头、尾和整条线索

配套方案：`~/work/sources/doc/agent-session-refs.md`（2026-09-05 实现）。
那边把 session 与 commit 的对应关系从「一个 session 一个分支」改成了两个 ref：

```
refs/agent/session/{session_id}/base    session 起点，一次写定
refs/agent/session/{session_id}/tip     每次 commit 后立刻推进
```

再加上 commit trailer `Agent-Session-Id: {session_id}`。

这份文档设计 git-cola 的 DAG 怎么把它画出来：**头**（base）、**尾**（tip）、
以及**整条线索**（base..tip 这一段，含中间被别人插进来的 commit）。

---

## 0. 先说三个实测出来的坑

这三条决定了下面的架构，不是细节。

### 0.1 `git log --decorate` 默认不装饰 `refs/agent/*`

```
$ git log --decorate=full --oneline
cac30c9 (HEAD -> refs/heads/main) three          ← tip ref 没出现
32ad565 one                                       ← base ref 没出现
```

只有 `refs/heads` `refs/remotes` `refs/tags` `refs/stash` `HEAD` 等已知命名空间
会被装饰。要让 agent ref 出现必须 `--decorate-refs=refs/agent/*`。

### 0.2 但 `--decorate-refs` 是**白名单**，会顶掉默认值

```
$ git log --decorate=full --decorate-refs='refs/*' --oneline
cac30c9 (refs/heads/main, refs/agent/session/abc123/tip) three
        ↑ "HEAD -> " 没了
```

要保住原来的显示，得把 `refs/heads/*` `refs/tags/*` `refs/remotes/*` `HEAD`
连同 `refs/agent/*` 一起显式列出来。而且 `--decorate-refs` 是 git 2.13+，
git-cola 的 README 声明支持 git 2.2.0。

**结论：不动 `--decorate`。** agent ref 我们自己用 `for-each-ref` 读，按 oid 贴到
`Commit` 上。这样零兼容风险，也不会破坏 `HEAD -> main` 的现有显示。

### 0.3 `for-each-ref 'refs/agent/session/*'` 返回空

```
$ git for-each-ref 'refs/agent/session/*'
                                     ← 空
$ git for-each-ref 'refs/agent/session/'
32ad565 commit refs/agent/session/abc123/base
cac30c9 commit refs/agent/session/abc123/tip
```

`for-each-ref` 的 `*` 用的是 `WM_PATHNAME` 语义，不跨 `/`。要么用**前缀**
（末尾带 `/`），要么写全 `refs/agent/session/*/tip`。用前缀，一次拿全 base+tip。

---

## 1. 数据层

### 1.1 新模块 `cola/models/agentsession.py`

```python
@dataclass(frozen=True)
class AgentSession:
    session_id: str
    base_oid: str | None      # base ref 缺失时为 None（只装了 Stop hook 的仓库）
    tip_oid: str | None
    updated: str              # tip 的 creatordate:iso-strict

    @property
    def short(self) -> str:
        return self.session_id[:8]
```

```python
def load_agent_sessions(context, prefix='refs/agent/session/'):
    """一次 for-each-ref 读全部 session。"""
    _, out, _ = context.git.for_each_ref(
        prefix,
        format='%(objectname)%01%(refname)%01%(creatordate:iso-strict)',
        sort='-creatordate',
        _readonly=True,
    )
    # refs/agent/session/{id}/{base|tip} → 归并成 AgentSession
```

解析函数写成带 doctest 的纯函数，和现有 `_agent_branch_part` 同一风格
（`pytest.ini` 开了 `--doctest-modules`，会自动跑）：

```python
def parse_session_ref(refname, prefix='refs/agent/session/'):
    """
    >>> parse_session_ref('refs/agent/session/abc123/tip')
    ('abc123', 'tip')
    >>> parse_session_ref('refs/agent/session/abc/def/base')
    ('abc/def', 'base')
    >>> parse_session_ref('refs/heads/main') is None
    True
    """
```

### 1.2 commit 的 session 归属（trailer）

`cola/models/dag.py` 的 `LOGFMT` 加一个字段，**放在 summary 之前**
（summary 必须最后，它可以含任意字符）：

```python
LOGFMT_TRAILERS = (
    r'format:%H%x01%P%x01%d%x01%an%x01%ad%x01%ae%x01'
    r'%(trailers:key=Agent-Session-Id,valueonly,separator=%x02)%x01%s'
)
```

`%(trailers:key=…)` 需要 git 2.22+。探测一次 git 版本：够新用上面这个（`split(sep, 6)`），
不够新退回现有 `LOGFMT`（`split(sep, 5)`），此时只有 ref 归属、没有 trailer 归属。
版本探测结果缓存在 `RepoReader` 上，不是每行都判。

`Commit.__slots__` 增加 `session_ids: list[str]`。

> **旧格式**：`Co-authored-by: claude code/{model}/{uuid} <…>`。
> `base.git_session_of_commit()` 那套剥 UUID 的正则只在 Sessions 面板的
> 「Rebuild refs」动作里用，不进 DAG 的热路径——热路径每行多跑一个正则不值。

### 1.3 三态归属：这是整个设计的重点

PLAN 里最在意的不变量是「上一个 commit 是不是我预期的那个 SHA」。
DAG 应该把**这个不变量被破坏的样子**直接画出来。所以每个 commit 相对某个 session
分三种状态：

| 状态 | 判定 | 含义 | 画法 |
|---|---|---|---|
| `OWN` | 在 `base..tip` 里 **且** trailer == session_id | 这个 session 自己提的 | 实线、实心 |
| `FOREIGN` | 在 `base..tip` 里 **但** trailer 不是它（或没 trailer） | **有人在中间插了 commit** | 虚线、淡色 |
| `STRAY` | trailer == session_id **但** 不在 `base..tip` 里 | rewind / reset / cherry-pick 走散了 | 点线、警告色 |

`FOREIGN` 和 `STRAY` 正是 `userpromptsubmit_hook.py` 里那条
「`HEAD != tip` 就用 systemMessage 提醒」的图形版——文字提醒只说「有问题」，
DAG 能直接指出**是哪几个 commit**。

### 1.4 范围计算：在内存里做，不额外起进程

`base..tip` 不要跑 `git rev-list`。commit 全集已经读进内存、`Commit.parents`
就是现成的图：从 `tip` 沿 parents 做 BFS，遇到 `base` 或已访问过就停。

```python
def range_members(commits_by_oid, base_oid, tip_oid):
    """tip 可达、base 不可达的 commit 集合（base..tip）。"""
```

只有 base 或 tip 不在已读集合里（session 在另一条没被 walk 到的历史上）才落回
`git rev-list base..tip`，并且这时候通常也该把它 pin 进 rev walk（下一节）。

### 1.5 让 session 的 commit 进入 rev walk

`refs/agent/*` 不在 `--all` 里。默认的 `HEAD` 参数下，一个跑在别的分支上、
或者被 `reset --hard` 甩掉的 session，它的 commit 根本不会出现在 log 输出里。

`RepoReader.get()` 组命令时，把**被点亮的 session** 的 base/tip ref 名追加到
rev 参数末尾。wildcard `git log` 不展开，得自己从 for-each-ref 的结果里展开成
具体 ref 名。

限流（几百个 session 的老仓库会炸）：

- 默认只自动 pin **最近 `cola.dag.agentsessionlimit`（默认 10）个**、
  且 tip 的 creatordate 在 `cola.dag.agentsessiondays`（默认 30）天内的 session
- Sessions 面板里手动勾选可以覆盖这个默认

### 1.6 刷新触发

`GitDAG.display()` 现在靠

```python
refs = set(model.local_branches + model.remote_branches + model.tags)
```

变化来决定要不要重画。hook **每次 commit 后立刻推进 tip**，但 tip 不在这三个集合里，
所以 agent 提交完 DAG 不会动。要把 session ref 的 `(refname, oid)` 并进这个集合：

```python
refs = set(model.local_branches + model.remote_branches + model.tags)
if self.params.agent_sessions_enabled:
    refs |= {(s.session_id, s.base_oid, s.tip_oid) for s in sessions}
```

---

## 2. 左边 DAG（log_dock：CommitTreeWidget + GraphDelegate 内联）

### 2.1 头尾：label

复用现有 `GraphDelegate._draw_labels()`。给 tag 列表注入两个合成标签：

```
⚑ 23ecce3c base
▶ 23ecce3c tip
```

`_draw_labels()` 里加一个前缀分支（和现有 `heads/` `tags/` `remotes/` 并列），
用 agent 专属配色（建议青/紫系，避开 head 的绿、remote 的黄）。
tip 恰好 == HEAD 时再叠一圈金色描边（复用 `current_head_color`），
一眼能看出「这个 session 就是当前状态」。

合成标签存在 `Commit.session_labels` 里，不污染 `Commit.tags`——
`tags` 被 `GitDAG.add_commits()` 拿去做 `self.commits[tag] = commit` 的索引，
往里塞东西会污染按名字查 commit 的路径。

### 2.2 整条线索：session 边槽（gutter）

在 lane 图**左侧**再分配一条窄列，每个点亮的 session 占 10px。
最多并排 4 条，超出的折叠成一条「多 session」灰带（点击展开面板）。

```
 ┌  a1b2c3  feat: 加载 session refs        ← tip 帽：实心圆角上盖
 │  d4e5f6  fix: 边槽宽度                   ← OWN，实线
 ┊  9f8e7d  chore: 同事顺手提的             ← FOREIGN，虚线 + 淡色
 │  1a2b3c  test: 三态归属                  ← OWN
 └  7f8e9d  （base 那个 commit）            ← base 帽：实心圆角下盖
 ╎  0011aa  更早的历史                      ← 不在带内，不画
```

实现落点：

- `GraphDelegate.paint()`：画 lane 之前先画 gutter
- `GraphDelegate._graph_width()` / `sizeHint()`：加上 `gutter_count * GUTTER_WIDTH`
- 新 role `SESSION_BAND_ROLE = Qt.UserRole + 4`，值是
  `list[tuple[gutter_index, SessionMark]]`
- 在 `CommitTreeWidget.apply_graph_result()` 那一趟里一次算完存进 item；
  `paint()` 里只查表，不做计算

为什么选边槽而不是「给 session 单独占一条 lane」：lane 是给**父子关系**用的，
session 是一个**区间**，语义不同；塞进 lane 会让 `build_graph()` 的着色和列分配
全部要跟着改，而边槽跟现有布局完全正交。

### 2.3 三态的视觉

`FOREIGN` 用虚线，直接回答「我的 session 是不是被打断了」。
`STRAY` 单独用点线画在带外，并在那一行右侧加一个小警告角标。

---

## 3. 右边 DAG（graphview_dock：GraphView / QGraphicsScene）

### 3.1 Session 缎带（ribbon）

新增 `SessionRibbon(QtWidgets.QGraphicsItem)`：

- **Z 序**：`setZValue(-3)`。现有是 `Edge` = -2、`Label` / summary text = -1、
  `Commit` = 0，所以 -3 稳稳压在所有东西下面
- **路径**：把该 session 的节点按 `row` 排序，中心连成一条 `QPainterPath` spline
  （和 `Edge.recompute_path()` 的 `cubicTo` 同风格，保持观感一致）
- **笔**：宽 = `Commit.commit_radius * 1.6`，`RoundCap` + `RoundJoin`，alpha ≈ 60。
  这样它是一条从 base 流到 tip 的半透明宽带，节点浮在上面
- **分段**：`OWN` 实线、`FOREIGN` `Qt.DashLine`、`STRAY` 另起一条 item 用 `Qt.DotLine`
- **两端**：base 节点套一个双圈「锚」环，tip 节点套一个「箭头」环，
  各挂一个 `QGraphicsSimpleTextItem`：`base ⟨23ecce3c⟩` / `tip ⟨23ecce3c⟩`
- **配色**：`session_id` 哈希到一个稳定色相（HSV，S/V 固定），
  保证同一个 session 每次打开颜色一致，且和 `EdgeColor` 的调色板分开取值

重建时机：跟 `_update_summary_labels()` 一样，挂在 `layout_commits()` 末尾——
节点位置变了 path 必须重算。

### 3.2 为什么是缎带不是别的

场景图里表达「一组节点属于同一件事」，加背景色块会跟 lane 打架，
加连线会和 parent 边混淆。半透明宽带压在最底层，既圈定了范围，
又天然沿着真实的父子路径走，不需要额外的几何。

---

## 4. Sessions 面板（新 dock）

`QTreeWidget`，放 `Qt.LeftDockWidgetArea` 和 `log_dock` tab 在一起，默认隐藏，
`View` 菜单给 toggle（和现有 `log_dock.toggleViewAction()` 那几条并列）。

| 列 | 内容 |
|---|---|
| ● | 颜色块 + 勾选框（点亮 / pin 进 rev walk） |
| Session | `23ecce3c…`，tooltip 全 id |
| Commits | `7 (+2 foreign)` |
| Updated | tip 的 creatordate，相对时间 |
| State | `at HEAD` / `behind` / `diverged` / `unreachable` |

交互：

- 单击 → 高亮该 session 的边槽 + 缎带，滚到 tip
- 双击 → `revtext` 设为
  `refs/agent/session/<id>/base..refs/agent/session/<id>/tip`，只看这一段
- 右键：
  - `Copy Session Id`
  - `Diff base..tip`（直接走已有的 `diff_commits` 信号）
  - `Show Reflog` → `git reflog show refs/agent/session/<id>/tip`，
    弹只读文本框。这就是 PLAN 里说的「`--create-reflog` 白得的一份带时间戳的
    session 进展日志」，本来就在那儿，只是没人看得到
  - `Create Branch at tip`（走已有的 `create_branch`）
  - `Prune`（`update-ref -d` 两个 ref，需确认；ref 是派生数据，删了不丢信息）
  - `Rebuild from trailers`（对老仓库：扫 `Agent-Session-Id` 和旧三段式
    `Co-authored-by`，重建 ref。对应 `agent-sessions.py rebuild`）
- 顶部：筛选框 + 「只看最近 30 天」勾选

---

## 5. revtext 记号与右键菜单

### 5.1 `agent:<id>` 记号

`GitDagLineEdit` 右键菜单加一条 `Agent Session…`（和现有
`_filter_to_current_author` / `_pickaxe_search` 那几条并列），插入 `agent:<id>`。

`RepoReader.get()` 组参数时把 `agent:<id>` 展开成
`refs/agent/session/<id>/base..refs/agent/session/<id>/tip`。
支持前缀匹配（写前几位就行，跟 `agent-sessions.py` 一致），不唯一时弹选择框。

### 5.2 commit 右键菜单

`ViewerMixin.update_menu_actions()`：选中的 commit 有 session 归属时，加一组
`Agent Session ▸ Show this session / Copy Session Id / Diff base..tip / Reflog`。

这跟现有的 `_agent_branch_part()`（分支方案，`agent/{id}.{files}` 那套）**并存**：
老仓库走分支、新仓库走 ref，菜单项来源不同但入口一致。
`base.agent_branch_mode()` 那边就是两种方案共存的，DAG 这边保持一致。

---

## 6. 配置

沿用现有 `cola.dag.*` 风格（`arcedges`、`legacylabelcolors`、`orphan_isolate`）：

| key | 默认 | 含义 |
|---|---|---|
| `cola.dag.agentsessions` | `true` | 总开关 |
| `cola.dag.agentsessionrefs` | `refs/agent/session/` | ref 前缀，换命名空间改这里 |
| `cola.dag.agentsessionlimit` | `10` | 自动点亮最近几个 session |
| `cola.dag.agentsessiondays` | `30` | 只自动点亮这么多天内的 |
| `cola.dag.agentsessiongutter` | `true` | 左图边槽 |
| `cola.dag.agentsessionribbon` | `true` | 右图缎带 |

命令行（`cola/dag.py` 的 `parse_args`）：
`git dag --agent-session <id>`（可重复）、`--no-agent-sessions`。

---

## 7. 性能

| 项 | 代价 |
|---|---|
| 读 session ref | 1 次 `for-each-ref`，前缀查询 |
| trailer 归属 | 同一条 log 命令多一个格式串，**0 个额外进程** |
| `base..tip` 范围 | 内存 BFS（parents 图现成的），**0 个额外进程** |
| 边槽 per-row 数据 | `apply_graph_result()` 一趟算完，`paint()` 只查表 |
| 缎带 path | 只在 `layout_commits()` 后重算一次 |

唯一可能变贵的是 1.5 的 pin：多 pin 一个 session 就多一条 walk 起点。
限流默认值（10 个 / 30 天）就是为这个设的。

---

## 8. 分阶段落地

| 阶段 | 内容 | 改动范围 |
|---|---|---|
| **M1** | 数据层 + 左图 base/tip 两个 label | `models/agentsession.py`(新)、`models/dag.py`、`GraphDelegate._draw_labels` |
| **M2** | 内存 BFS 算范围 + 左图 gutter 带（含 FOREIGN 虚线） | `GraphDelegate.paint/sizeHint`、`CommitTreeWidget.apply_graph_result` |
| **M3** | 右图 ribbon + base/tip 特殊环 | `SessionRibbon`(新)、`GraphView.layout_commits` |
| **M4** | Sessions 面板 + reflog 查看 + prune / rebuild | `widgets/dag.py` 新 dock |
| **M5** | `agent:<id>` 记号、commit 右键菜单、配置项、命令行开关、旧三段式兼容 | 各处 |

M1 就已经能回答「头在哪、尾在哪」；M2 补上「整条线索」；M3 之后两个视图对齐。

---

## 9. 测试

`test/dag_agent_session_test.py`（`test/helper.py` 起临时仓库，`update-ref` 造 base/tip）：

- 解析：`parse_session_ref` 的 doctest + 前缀查询返回值
- 三态：造一个中间插了别人 commit 的历史，断言 `FOREIGN`；
  造一个 `reset --hard` 后的历史，断言 `STRAY`
- 边槽数据：`SESSION_BAND_ROLE` 的值对不对
- 边界：
  - `base == tip`（session 一个 commit 都没提）
  - base ref 缺失（只装了 Stop hook）
  - tip 不可达（`reset --hard` 之后）
  - 几百个 session（限流生效、不 pin 全部）
  - 一个 session 跨多个分支
- git 版本降级：假装 git < 2.22，断言退回旧 `LOGFMT` 且 ref 归属仍然工作
