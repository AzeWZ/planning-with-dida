# HISTORY

## 2026-11-12

- 从 `https://github.com/AzeWZ/planning-with-dida`（fork of `OthmanAdi/planning-with-files`）下载 CodeBuddy 发行版，上游版本 3.28.2，落点 `orca/skills/planning-with-dida`。
- 改造目标：任务不再只存在文本里，滴答清单（Dida365）成为阶段与状态的唯一真相；`task_plan.md` 降级为本地投影/缓存。
- 新增 `scripts/dida-sync.py`：`init / sync / pull / phase / next / complete / status / restore`，父任务 `计划：<name>` + 子任务 `P<n> <phase>`，放 `Project` 清单。
- 新增 `references/dida.md`：映射、命令、生命周期、失败模式、边界。
- `skill-hook.sh`：UserPromptSubmit 注入前执行 `pull --quiet --max-age 15`；PostToolUse 提示改为 `phase <n> complete`；`PWF_DIDA=0` 可关闭。
- `init-session.sh`：创建文件后打印 `dida-sync.py init` 交接命令（此时模板还没填，不自动建任务）。
- `inject-plan.py`：nudge 文案同步改为 `dida-sync.py phase`，不再提示直接改 status。
- 测试：`python3 -m unittest discover -s tests -v`，10 项全绿（假 dida CLI：幂等 init、pull、phase、next、complete、离线降级、无 sidecar no-op、`#` 转义、restore）。
- 真实 Dida API 冒烟（2026-11-12）：在 `Project` 清单建 1 父 + 3 子，`phase`、`pull`、`next`、`complete` 全部通过；另跑 `init` 两次，父/子 ID 不变（幂等）。测试任务已删除（Dida 软删除进回收站，active 列表与搜索均不含）。
- 修了两个真实链路问题：`task complete` 返回纯文本「任务已完成」，`run_dida` 不再强制按 JSON 解析；`status --json` 支持放在子命令后。
- 已知限制：阶段数量/顺序仍由本地 plan 拥有；手机上新增的子任务不会被采纳为阶段；清空标签用 `--tags ","`（CLI 不发送空字符串）。
