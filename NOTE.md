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
