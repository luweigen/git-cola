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

### 1.2 commit 的 session 归属（trailer）✅ M2

**最初的方案是给 `LOGFMT` 加一个字段**，把
`%(trailers:key=Agent-Session-Id,valueonly,separator=%x02)` 塞在 summary 之前，
理由是「0 个额外进程」。实现时换掉了，换成**第二遍只取 trailer 的 `git log`**：

```python
TRAILER_FORMAT = 'format:%H%x01%(trailers:key=Agent-Session-Id,valueonly,separator=%x02)'
```

换的原因：

1. `%(trailers:key=…)` 要 git 2.22，git-cola 支持到 2.2。加进 `LOGFMT` 就意味着
   **字段数随 git 版本变**，`Commit.parse()` 得按版本切 `split(sep, 5)` / `split(sep, 6)`。
   而 `parse()` 是整个 DAG 最热的函数，让它依赖全局版本状态是自找的麻烦。
2. `test/dag_test.py` 的 `LOG_TEXT` 是写死 6 字段的 fixture，字段数一变它就得跟着
   本机 git 版本走——fixture 不该有这种依赖。
3. 实测第二遍根本不贵：本仓库 9352 个 commit，主 walk 70ms，只取 trailer 的第二遍
   也是 70ms，而 Python 侧几乎零成本（只有带 trailer 的 commit 会进 dict）。
   端到端 `RepoReader.get()` 从 0.124s 变成 0.223s（含 11 个 session 的三态计算）。

代价是多一个进程、git 侧时间翻倍；换来的是 `Commit.parse()` 一行没动、
现有 fixture 一个没改、版本降级只是一句 `return {}`。

`separator=` **不是可选的**：不写的话 trailers atom 会在值后面附一个换行，
直接串进下一条记录。

版本探测走项目已有的机制：`cola/version.py` 的特性表加一行
`'trailers-key': '2.22.0'`，用 `version.check_git(context, ...)` 判断，结果 memoize。

> **旧格式**：`Co-authored-by: claude code/{model}/{uuid} <…>`。
> `base.git_session_of_commit()` 那套剥 UUID 的正则只在 Sessions 面板的
> 「Rebuild refs」动作里用，不进 DAG 的热路径——热路径每行多跑一个正则不值。

### 1.3 三态归属：这是整个设计的重点 ✅ M2

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

两个退化情况给了明确答案，不是含糊过去：

| ref 状态 | 怎么判 | 为什么 |
|---|---|---|
| **没有 tip ref** | 带 trailer 的 commit 全部 `STRAY` | 该锚住它们的 ref 不存在，这本身就是异常 |
| **有 tip ref 但 tip 不在视野里** | 什么都不判，面板报 `not visible` | 见下 |
| **没有 base ref** | 退回只按 trailer 认成员，全是 `OWN` | 没有 base 就没得减，硬算范围会变成整部历史；也就无从判断 `FOREIGN` |
| **git < 2.22** | 范围内全是 `OWN` | 拿不到 trailer，没有证据说别人插过队，就不能瞎标 `FOREIGN` |

「tip 不在视野里」这条是补测「session 跨多个分支」时才发现的。原来的写法会把
可见的那半边全标成 `STRAY`——但 hook 是**在当前分支上 commit** 的，用户中途
`checkout -b` 之后 session 天然横跨两个分支，只看 `main` 时 tip 在另一头，
这时候报 `STRAY` 就是**谎报了一次 rewind**。面板上一个完全健康的 session
会显示 `1 stray`。改成不判，让面板说 `not visible`，双击就能看全。

### 1.4 范围计算：在内存里做，不额外起进程 ✅ M2

`base..tip` 不跑 `git rev-list`。commit 全集已经读进内存、`Commit.parents`
就是现成的图。

**但不能写成「从 tip 沿 parents 走，遇到 base 就停」**——这是最初的写法，它是错的：
merge 可以把比 base 还老、但不是 base 祖先的 commit 拉进范围，
「遇到 base 就停」会把它们漏掉。要老老实实做集合减法：

```python
def range_members(commits_by_oid, base_oid, tip_oid, cache=None):
    """tip 可达、base 不可达（base..tip）。"""
    members = ancestors(tip_oid)
    if base_oid:
        members -= ancestors(base_oid)
    return members
```

`test_range_excludes_merged_in_ancestors_of_base` 就是钉这个的：
造一个 merge 进来的 side 分支，断言 side 在范围里、base 的祖先不在。

**还有第二个坑，是 M5 的截图暴露出来的**：`ancestors()` 最初会把
「走到了但不在已读集合里」的 oid 也放进结果。正常视野下无所谓（两边都这么做，
减法抵消），但 `agent:<id>` 把视野收窄到 `base..tip` 之后，**base 正好在边界外一格**：
`ancestors(tip)` 报了 base，而 `ancestors(base)` 因为 base 不在集合里直接返回空集，
减不掉——base 就漏进范围，还因为没 trailer 被标成 `FOREIGN`。
面板上显示成 `2, +1 foreign`，图上却只有 2 个 commit。
改成只收录**确实在已读集合里**的 oid，
`test_range_does_not_leak_an_offscreen_base` 钉住。

`cache` 是跨 session 共享的 `{oid: 祖先集合}`，背靠背的 session 共用端点，命中率高。

**这个缓存就是为什么要限流。** 每个 session 要两个端点的祖先集合，
实测 9352 个 commit / 51 个 session：`build_threads` 88ms、缓存约 8MB，
两者都随 `session 数 × commit 数` 长。十万 commit 的仓库配上几百个 session
就是几秒钟和几百 MB——而且算的还是用户根本看不见的 session。

所以 `build_threads(..., limit=10)`：只算最新的 N 个（`load_agent_sessions()`
用 `-creatordate` 排好序，一个 session 的 tip 必然不早于它的 base，
所以按扁平 ref 列表排序等价于按 tip 排序）。加了上限之后同样 51 个 session
降到 25ms。**没被算 thread 的 session 照样有 base/tip 标签**——贴标签是免费的，
算 thread 不是。

> 这个上限原计划在 M4，提前到 M2 是因为它修的是上面这个实测出来的问题，
> 不是新功能。

### 1.5 视野外的 session：不自动 pin，给记号 ✅ M5

`refs/agent/*` 不在 `--all` 里。默认的 `HEAD` / `main --` 参数下，一个跑在别的分支上、
或者被 `reset --hard` 甩掉的 session，它的 commit 根本不会出现在 log 输出里。

原设计是**自动把被点亮 session 的 ref 追加到 rev 参数**。实现时否掉了：
用户在 revtext 里打的是 `main --`，那是他提的问题；
悄悄往里塞别的 ref 会改变答案，图上多出来的 commit 没有任何东西解释它们从哪来。

改成显式的两条路：

- **`agent:<id>` 记号**（`agentsession.expand_ref_args()`）——
  在 revtext 里直接写，展开成 `refs/agent/session/<id>/base..refs/agent/session/<id>/tip`。
  短 id 就行，跟 `agent-sessions.py` 一致；匹配到多个就都展开，
  当搜索用而不是报歧义错。匹配不到、或者 session 缺 base/tip 的直接丢掉——
  原样传给 git 会让**整条** rev 列表失败
- **命令行 `--agent-session <id>`**（可重复），以及 Sessions 面板的双击

面板的 `State` 列在 session 被分类了、但一个 commit 都没落进视野时显示
`not visible`，告诉用户「它在，只是不在你现在看的这段历史里」，双击即可看它。

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

## 2. 显示都做在右边 DAG（graphview_dock：GraphView / QGraphicsScene）

**左边 DAG（log_dock）不动。** 最初的方案是把头尾 label 画在左边、gutter 带也在左边，
实际做出来之后决定全部挪到右边：

- 左边是一个**列表**，SUMMARY 列有宽度上限（`viewport_w * 6 // 10`），
  多塞几个标签就把 `main` 挤成 `mai…`，而分支名才是眼睛第一时间要找的东西
- 右边是**场景图**，节点周围本来就是留给标签的空地，`alloc_cell()`
  会为带标签的 commit 预留横向空间，多一个标签不挤压任何别的信息
- session 的「线索」是一段**连着的路径**，场景图里能顺着真实的父子连线画出来；
  列表里只能靠边槽模拟，表达力反而更弱

所以左边保持原样（lane + 分支/tag 标签 + summary），session 的全部可视化都在右边。

### 2.1 头尾：base / tip 标签

挂在现有的 `Label`（`QGraphicsItem`，Z = -1）上，画在分支/tag 标签之后：

```
HEAD  main  ▶ b47c8939
             ▶ 23ecce3c  ⚑ b47c8939
             ⚑ 23ecce3c
```

- **不写 "tip" / "base" 这两个词**：`▶` / `⚑` 已经说清是哪一头，
  再加一个词等于把标签宽度翻倍换零信息量
- 配色：**跟这个 session 的缎带同一个色相**（`session_label_color()`）。
  最初用的是固定的青（tip）/ 紫（base）——那是 M1 的产物，当时缎带还不存在。
  等 M3 引入「一个 session 一个色相」之后就成了同一个概念两套配色：
  多个 session 同时在图上时，所有 tip 都是青的、所有 base 都是紫的，
  **看不出哪个标签属于哪条缎带**。而「哪一头」这件事图标已经说了，
  颜色这个通道花在那上面是浪费。现在一个 session 一个色相，贯穿标签、缎带、面板色块

  比缎带更浅、更不饱和，有两个原因：标签是不透明的而缎带是 110 alpha 的淡色；
  标签上有黑字，按缎带那个饱和度蓝色系会算出接近 `rgb(79, 79, 225)`，黑字读不了。
  base 又比 tip 再浅一档——一个 commit 同时是上个 session 的 tip 和下个的 base
  是常态，两头得能一眼分开
- 顺序在分支/tag **之后**：分支名是主信息，session 锚点是次要信息
- 数据存 `Commit.session_labels`，**不进 `Commit.tags`**——`tags` 被
  `GitDAG.add_commits()` 拿去做 `self.commits[tag] = commit` 的索引，
  往里塞合成标签会污染按名字查 commit 的路径

三处必须跟着改，漏一个就出 bug：

| 位置 | 原来 | 改成 |
|---|---|---|
| `Commit.__init__`（图元） | `if commit.tags:` 才建 `Label` | `if commit.tags or commit.session_labels:` |
| `Commit.update_summary_label()` | `if side is None or self.commit.tags: return` | 加上 `session_labels`，否则 summary 文字和标签叠在一起 |
| `GraphView.recompute_grid()` | `alloc_cell(node.column, node.tags)` | `node.tags or node.session_labels`，否则标签框会压到邻居节点上 |

### 2.2 点 session 标签弹的菜单 ✅ M1 / M4

跟分支标签一样弹菜单，但走单独的 `show_session_menu()`——session 锚点不是分支，
没有 checkout / merge / push 可做。`_label_hits` 的元组多带一个
`(kind, session_id)` 字段来区分（普通标签是 `None`）。

| 菜单项 | 干什么 | 阶段 |
|---|---|---|
| `Copy "<session id>"` | 完整 id，喂给 `agent-sessions.py` 或比对 trailer | M1 |
| `Copy "refs/agent/session/<id>/tip"` | 完整 ref 名，喂给 `git log` / `git reflog` | M1 |
| `Diff base..tip` | 走已有的 `diff_commits` 信号开 difftool | M4 |
| `Show Reflog` | `git reflog show <ref>`，见下 | M4 |
| `Create Branch "agent/{id}.{files}" at tip...` | rename 的等价物，见 2.3 | M4 |
| `Prune Session Refs` | 删两个 ref，带确认 | M4 |

base 和 tip 标签用**同一个**菜单：它们指的是同一个 session，点哪一头都该给同样的东西。

`show_session_menu()` 是**模块级函数**不是 `Label` 的方法——Sessions 面板的右键菜单
复用同一个，两处不能各写一套。

**`Show Reflog` 是白捡的。** hook 建 ref 时加了 `--create-reflog`
（`refs/agent/` 不在默认开 reflog 的命名空间里，所以那个 flag 是刻意的），
git 于是一直在维护一份带时间戳、按序的 session 进展记录。
`SessionReflogDialog` 只是把它显示出来，不用自己写日志文件。

**所有会触发刷新的动作都要 `QTimer.singleShot(0, ...)` 延后执行**，
并且闭包里不能捕获 `self`。刷新会重建场景（`scene().clear()`），
而这时 `Label` 的 `mousePressEvent` 还在 C++ 的事件派发栈上——item 被删掉就是
use-after-free。这条是照抄 `_show_branch_menu()` 里已有的做法。

### 2.3 rename 在 ref 方案里对应什么 ✅ M4

分支菜单的 `Rename to "agent/{id}.{files}"` 是把 session 分支用它改过的文件名标注一下。
**这个操作不能照搬到 session ref 上**：session id 就是身份本身，
commit 上的 `Agent-Session-Id` trailer、`agent-sessions.py` 的查询、
reflog 全都按它对应。改了 ref 名，这些对应关系当场断掉，而且没有任何东西会跟着改。

真正对应的意图是「给这个 session 一个人能读的名字」，实现方式是
**在 tip 上建一个分支**：

```
Create branch "agent/{session_id}.{files}" at tip
```

`{files}` 复用现有的 `_branch_tip_basenames()`。建出来之后它就是一个普通的本地分支，
现有的分支标签菜单（rename / push / merge to / 删远端）**全部**自动适用——
不需要在 session 菜单里重造一套。session ref 原封不动继续指着同一个 commit。

名字由 `agentsession.branch_name(session_id, basenames)` 拼（纯函数、带 doctest），
菜单项选中后打开现有的 `createbranch` 对话框、把 revision 填成 tip、
名字预填好，剩下的交给用户确认。

### 2.4 整条线索：Session 缎带（ribbon）✅ M3

`SessionRibbon(QtWidgets.QGraphicsItem)`：

- **Z 序**：`setZValue(-3)`。现有是 `Edge` = -2、`Label` / summary text = -1、
  `Commit` = 0，所以 -3 稳稳压在所有东西下面
- **路径**：**沿真实的 parent 边走**，不是把节点按 row 排序连直线。
  `agentsession.thread_segments()` 挑出「两端都属于这个 session」的 parent 边，
  merge 的两个父边都会被画上，跨 merge 时缎带仍然如实。
  base 虽然不是 `base..tip` 的成员，但要作为端点算进去——它是缎带要够到的那一头
- **配色**：`session_id` → `session_hue()`（sha256 前两字节），HSV 的 S/V
  固定成 165/225。哈希而不是按出现顺序分配，是为了让同一个 session 每次打开、
  以及别的 session 来来去去时颜色都不变。

  **色相跳过 40-140 这一段**：黄色在 DAG 里已经是 HEAD / tag / remote 的意思，
  绿色是非当前本地分支。而 **tip 就是 HEAD 是最常见的情况**——真实仓库上一跑，
  一个哈希到 66 度的 session，它的 tip 标签紧挨着黄色的 `HEAD` `dev`，糊成一片。
  剩下的 260 度（青、蓝、紫、品红、红、橙）区分 session 绰绰有余

### 2.4.1 三态怎么画：两个独立的轴

一开始按原设计用 `Qt.SolidLine` / `DashLine` / `DotLine`，**在这个尺度上完全不work**，
踩了三个坑：

1. **alpha 70 等于隐形**。实测采样：淡到 `#f2f7fd`，跟白底差 3%。抬到 110。
2. **Qt 的 dash 长度按笔宽缩放**。`Qt.DashLine` 在 14px 笔下每段 dash 是 56px，
   比 12px 的行距还长——每一段都渲染成实线，三态看起来一模一样。
   必须用 `setDashPattern()` 自己给（单位仍是笔宽，但可以给小数）。
3. **`RoundCap` 会把 dash 焊回实线**。圆头给每个 dash 两端各加半个笔宽（7px），
   比空隙还大。改用 `FlatCap`——共线段在节点中心相接，照样无缝。

改完虚线能看见了，但 `FOREIGN` 和 `STRAY` 还是分不出来。最终换成**两个独立的轴**：

| 轴 | 回答的问题 | 编码 |
|---|---|---|
| **粗细** | 这个 commit 是不是这个 session 提的 | 14px = 是；7px = 不是（带子在这里「掐细」） |
| **实/虚** | 有没有被 tip ref 锚住 | 实线 = 是；虚线 = 不是 |

于是：

| 状态 | 画法 | 读作 |
|---|---|---|
| `OWN` | 粗实 | 我提的，锚住了 |
| `FOREIGN` | 细实 | 别人在中间插的 |
| `STRAY` | 粗虚 | 我提的，但 tip ref 够不到了 |

`FOREIGN` 的 7px 也是量出来的：4px 时整条被上面那根 2px 的红色 `Edge` 线盖住，
读起来是「没有带子」而不是「细带子」，跟「不属于这个 session」混了。

**重建时机**：挂在 `layout_commits()` 末尾，跟 `_update_summary_labels()` 一起——
节点位置变了 path 必须重算。`GraphView.clear()` 里要把
`self.session_ribbons` 一起清掉：`scene().clear()` 已经把 item 删了，
留着引用就是悬空指针。

### 2.5 为什么是缎带不是别的

场景图里表达「一组节点属于同一件事」，加背景色块会跟 lane 打架，
加连线会和 parent 边混淆。半透明宽带压在最底层，既圈定了范围，
又天然沿着真实的父子路径走，不需要额外的几何。

---

## 3. Sessions 面板（新 dock）✅ M4

`SessionsWidget`，放 `Qt.LeftDockWidgetArea` 跟 `log_dock` tab 在一起，
`View` 菜单给 toggle。它回答的是图回答不了的那个问题：
**一共有哪些 session**——某个 session 的 commit 不在当前视野里时，图上什么都没有。

（加了新 dock，所以 `GitDAG.widget_version` 从 2 提到 3，
否则会把旧版本存的布局还原到新布局上。）

| 列 | 内容 | 说明 |
|---|---|---|
| ● | 颜色块 | 就是 `session_ribbon_color()`，跟缎带一个颜色，一眼对上 |
| Session | `23ecce3c` | tooltip 是全 id |
| Commits | `2, +2 foreign, 1 stray` | 只在非零时才列后两项 |
| Updated | tip 的 creatordate（日期部分） | tooltip 是完整时间戳 |
| State | `at HEAD` / `no commits` / `no base ref` / `not classified` | 正常时留空 |

`not classified` 是限流的诚实表述：超出 `agentsessionlimit` 的 session
仍然有标签、仍然在列表里，只是没算三态。

交互：

- 单击 → 在左右两个视图里选中该 session 的 tip commit
- 双击 → `revtext` 设成
  `refs/agent/session/<id>/base..refs/agent/session/<id>/tip`，只看这一段
- 右键 → **和标签一模一样的那个菜单**（`show_session_menu()`）
- 顶部：筛选框（按 session id 子串）+ 「Recent only」勾选
  （`agentsession.is_recent()`，天数取 `cola.dag.agentsessiondays`，默认 30）

`is_recent()` 有一条刻意的规则：**时间戳读不出来的 session 算「最近」**。
因为时间戳解析失败而把一个 session 藏起来，比多显示一行糟糕得多。

顶部还有一个 `Rebuild` 按钮（M5，见 4.3）：扫 `Agent-Session-Id`
和旧三段式 `Co-authored-by` 重建缺失的 ref，对应 `agent-sessions.py rebuild`。

---

## 4. 记号、右键菜单与老仓库 ✅ M5

### 4.1 `agent:<id>` 记号

在 revtext 里直接写 `agent:<id>`，`RepoReader.get()` 展开成
`refs/agent/session/<id>/base..refs/agent/session/<id>/tip`（见 1.5）。

原计划「不唯一时弹选择框」没做——展开发生在**reader 线程**里，
那里不能弹模态框。改成匹配到几个就展开几个，当搜索用；
展开完图上的标签自己会说回来的是哪几个 session。
完整 id 永远只匹配它自己，所以不会歧义。

### 4.2 commit 右键子菜单

`ViewerMixin._add_session_submenu()`：选中的 commit 属于某个 session 时，
在已有的 Actions 菜单末尾加一条 `Agent Session ▸ <短 id>`，
点进去就是 2.2 那个菜单（同一个 `show_session_menu()`）。

**只列「认领」这个 commit 的 session**：`OWN` 和 `STRAY` 算，`FOREIGN` 不算——
一个 commit 只是碰巧落在别人的 `base..tip` 区间里，那不是它的 session，
列出来会误导。

这跟现有的 `_agent_branch_part()`（分支方案，`agent/{id}.{files}` 那套）**并存**：
老仓库走分支、新仓库走 ref，菜单项来源不同但入口一致。
`base.agent_branch_mode()` 那边就是两种方案共存的，DAG 这边保持一致。

### 4.3 老仓库：Rebuild from trailers

Sessions 面板上的 `Rebuild` 按钮 → `rebuild_session_refs()`：

```
git log --all --pretty=format:%H|%P|<Agent-Session-Id>|<Co-authored-by>
```

一趟同时读**新旧两种** trailer：先取 `Agent-Session-Id`，取不到再用
`session_id_from_coauthor()` 从旧的三段式
`Co-authored-by: claude code/{model}/{uuid} <…>` 里剥 UUID。
然后 `rebuild_plan()` 按「tip = 该 session 最新的 commit，
base = 最老那个的第一父提交」定出 ref，跟 `agent-sessions.py rebuild` 同一套规则。
根提交没有父，那个 session 就只建 tip 不建 base，不编一个假的。

**已经有 ref 的 session 一个都不碰。** 活的 ref 是 hook 写的，
知道 trailer 不知道的事——比如一个没有任何 commit 记录的 base，
或者一个刻意落后于最新 commit 的 tip。

写 ref 用逐个 `git update-ref --create-reflog`，不用 `--stdin`：
`Git.execute()` 的 `_stdin` 收的是文件句柄不是字符串，
而 rebuild 是低频的显式操作，每次写都是独立且幂等的。

---

## 5. 配置

沿用现有 `cola.dag.*` 风格（`arcedges`、`legacylabelcolors`、`orphan_isolate`）：

（标 ✅ 的已实现，其余随对应 M 阶段落地。）

| key | 默认 | 含义 | 状态 |
|---|---|---|---|
| `cola.dag.agentsessions` | `true` | 总开关 | ✅ M1 |
| `cola.dag.agentsessionlimit` | `10` | 算三态/点亮的最新 session 数 | ✅ M2 |
| `cola.dag.agentsessiondays` | `30` | 面板「Recent only」的时间窗 | ✅ M4 |
| `cola.dag.agentsessionlabels` | `true` | 右图 base/tip 标签开关 | ✅ M5 |
| `cola.dag.agentsessionribbon` | `true` | 右图缎带开关 | ✅ M4 |

**`cola.dag.agentsessionrefs`（ref 前缀）没做，是有意的。** 它要一路穿过 reader、
标签、菜单和面板，而换命名空间本来就得同时改 hook（`sessionstart_hook.py` /
`stop_hook.py` 里也是写死的）。真要换的话，`agentsession.SESSION_PREFIX` 改一行就行。

命令行（`cola/dag.py` 的 `parse_args`）✅ M5：

```bash
git dag --agent-session 23ecce3c     # 只看这个 session（可重复，短 id 即可）
git dag --no-agent-sessions          # 完全不读 refs/agent/session/
```

`--agent-session` 会**替换**掉 revision 参数而不是追加——这个选项的意思就是
「只看这个 session」。

---

## 6. 性能

| 项 | 代价 |
|---|---|
| 读 session ref | 1 次 `for-each-ref`，前缀查询 |
| trailer 归属 | 第二遍 `git log`，只取 `%H` + trailer。9352 commit 实测 70ms |
| `base..tip` 范围 | 内存集合减法（parents 图现成的），**0 个额外进程**；51 session 88ms → 限流后 25ms |
| base/tip 标签 | 只多几个 `Label` 的绘制项，`alloc_cell()` 照旧预留空间 |
| 缎带 path | 只在 `layout_commits()` 后重算一次，每个 session 一个 item |

端到端实测（本仓库 9352 commit，51 个 session）：`RepoReader.get()`
关掉功能 0.138s，打开 0.243s。多出来的 0.1s 里绝大部分是第二遍 git log。

唯一可能变贵的是 1.5 的 pin：多 pin 一个 session 就多一条 walk 起点。
`agentsessionlimit`（默认 10）同时管住 thread 计算和 pin。

---

## 7. 分阶段落地

| 阶段 | 内容 | 改动范围 |
|---|---|---|
| **M1** ✅ | 数据层 + 右图 base/tip 标签 + 标签上的两条 `Copy` 菜单 | `models/agentsession.py`(新)、`models/dag.py`、`Label` / `Commit` / `recompute_grid`、`test/dag_agent_session_test.py`(新) |
| **M2** ✅ | `base..tip` 集合减法 + 三态归属 + session 限流 + 结果送到视图层 | `agentsession.range_members/build_threads`、`RepoReader._read_trailers`、`version.py` 特性表、`ReaderThread.sessions` 信号 |
| **M3** ✅ | 右图缎带（三态用粗细 + 实虚两个轴编码） | `SessionRibbon`(新)、`GraphView._update_session_ribbons`、`agentsession.thread_segments/session_hue` |
| **M4** ✅ | session 菜单补齐 + Sessions 面板 + 缎带/时间窗配置项 | `show_session_menu()`(抽成模块级)、`SessionsWidget`(新)、`SessionReflogDialog`(新)、`agentsession.branch_name/is_recent` |
| **M5** ✅ | `agent:<id>` 记号、commit 右键子菜单、`Rebuild from trailers`（认新旧两种 trailer）、命令行开关、标签开关 | `agentsession.expand_ref_args/rebuild_plan/rebuild_refs/session_id_from_coauthor`、`ViewerMixin._add_session_submenu`、`rebuild_session_refs()` |

M1 回答「头在哪、尾在哪」；M2 把「整条线索」算出来；M3 把它画出来；
M4 加菜单和面板；M5 补上导航、老仓库兼容和开关。左边 DAG 全程不动。

截图（同一个 demo 仓库、同一套布局，可以直接对比）：

| 文件 | 内容 |
|---|---|
| `test/log/dag-agent-session-m1.png` | 只有 base/tip 标签（`cola.dag.agentsessionribbon=false` 就是这个样子） |
| `test/log/dag-agent-session-m3.png` | 加上缎带，三态可见 |
| `test/log/dag-agent-session-m4.png` | Sessions 面板 |
| `test/log/dag-agent-session-m5.png` | `agent:<id>` 收窄视野，另一个 session 报 `not visible` |
| `test/log/dag-agent-session-branches.png` | session 跨分支：缎带横跨两条 lane，三态齐全（`mkdemo-branch.sh`） |

demo 仓库由 `mkdemo.sh` 生成：两个 session，其中一个带 2 个 FOREIGN
和 1 个 STRAY，正好覆盖三态。

---

## 8. 测试

`test/dag_agent_session_test.py`（`test/helper.py` 起临时仓库，`update-ref` 造 base/tip）。

**M1 已有的（11 个测试 + 7 个 doctest）：**

- 解析：`parse_session_ref` / `parse_for_each_ref` / `labels_by_oid` / `label_text`
  的 doctest；`load_agent_sessions()` 前缀查询同时拿到 base 和 tip
- 不误判：分支、tag、以及 `refs/agent/session/<id>`（少一层，不带 `/base` `/tip`）
  都不算 session
- 标签数据：`Commit.session_labels` 的内容和顺序（tip 在 base 前），
  背靠背 session 共享同一个 commit 时两个标签都在
- 开关：`set_agent_sessions(False)` 后不读 ref、不贴标签
- 边界：`base == tip`（session 一个 commit 都没提）、base ref 缺失
  （只装了 Stop hook）、tip 被 `reset --hard` 甩掉后仍能标出 base
- 刷新：`refresh_key()` 在 tip 推进后改变
- 菜单要复制的东西：`label_text()` 不含 tip/base 字样；`session_ref()` 造出来的
  ref 名能被 `parse_session_ref()` 原样解回同一个 `(session_id, kind)`
- 纯函数的 doctest：`parse_trailers` / `ancestors` / `range_members` /
  `counts` / `build_threads` 的限流

**M2 加的（10 个）：**

- 范围：`base..tip` 不含 base 本身；**merge 进来的、比 base 老但不是 base 祖先的
  commit 要算在范围里**（这条钉住 1.4 那个「遇到 base 就停」的错法）
- 三态：中间插了别人 commit → `FOREIGN`；tip ref 没跟上 → `STRAY`；
  `counts()` 三个数对得上
- 退化：没有 tip ref、没有 base ref、拿不到 trailer（git < 2.22）各一个
- 限流：`set_agent_session_limit(1)` 时只算最新的那个，但两个 session 的标签都还在
- 没有 session ref 时第二遍 log 根本不跑

**M3 加的（3 个）：**

- 缎带够到 base（base 不是 `base..tip` 的成员，但必须是端点）
- 段的样式取**子**（较新的那个）的 mark：插进来的那个 commit 的入边才是掐细的那条
- 跨 merge：merge 的两个父边都在缎带里

这三条测的是 `agentsession.thread_segments()`——把「哪些边组成缎带」这段纯逻辑
从 `GraphView` 抽出来，就是为了让它能进测试套件（`GraphView` 那半只剩把 oid
换成坐标）。

**M4 加的（3 个）：**

- `branch_name()` 拼出来的名字跟老的分支布局一致，且 `_branch_tip_basenames()`
  会跳过下划线开头的路径
- prune 之后 ref 没了、但 commit 和它的 trailer 还在（refs 是派生数据）
- `is_recent()` 按 tip 的日期过滤，`days=0` 表示不限

**M5 加的（8 个）：**

- `agent:<id>` 真的收窄到 base..tip；匹配不到的 id 被丢掉而不是让 git 整条失败
- `--agent-session` 替换 revision 参数、`--no-agent-sessions` 关掉整个功能
- rebuild：新 trailer、**旧三段式 `Co-authored-by`**、以及「已有 ref 不覆盖」
- **base 在视野外时不能漏进 range**（1.4 里那个 M5 截图暴露的 bug）

**跨分支（4 个）：**

hook 是在**当前分支**上 commit 的，所以用户中途 `checkout -b` 之后，
一个 session 天然横跨两个分支。这组测的就是那些形状：

| 场景 | 断言 |
|---|---|
| session 中途换到新分支 | 两个 commit 都是 `OWN`；缎带的边跨过分支点 |
| session 换到一条**分叉**的分支 | 三态一次齐活：新分支上自己的 commit `OWN`、那条分支原有的 commit `FOREIGN`、换分支前提的 commit `STRAY` |
| 只看 `main`、tip 在另一分支 | `marks` 为空、`stray == 0`（不谎报），`agent:<id>` 一问就全回来 |
| session 以一个 merge 收尾 | merge 的两个父边都在缎带里；merge commit 自己没 trailer，所以是 `FOREIGN` |

第三条就是上面说的那个假 `STRAY`——测试写出来才发现的。

Qt 层（`Label` / `SessionRibbon` 的绘制、`alloc_cell` 预留、菜单和面板本身）
测试套件里没有覆盖——
`test/` 目前完全不起 `QApplication`。这部分靠离屏渲染人工核对，
靠离屏脚本人工核对：一个反复 `display()` 重建场景、确认
`scene().clear()` 之后没有悬空的 ribbon；一个不用鼠标就把菜单建出来、
打印每一项文字；一个驱动面板的筛选、选中和双击。
