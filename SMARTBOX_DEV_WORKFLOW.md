# SmartBox × hermes-agent — 开发工作流

> 这个文件只存在于 `smartbox-main` 分支，不会出现在 `main`（main 是 NousResearch/hermes-agent 上游的镜像）。

## 这个 fork 是做什么的

这是 `NousResearch/hermes-agent` 的 fork，用于 [SmartBox](https://github.com/shevapato2008/smartbox-software) 项目。fork 的存在是为了维护 **SmartBox 特有的补丁**（比如 `qr_login` 的 `on_qr` / `on_state` callback），同时保持能从上游拉最新改动。

## 分支拓扑

```
NousResearch/hermes-agent                       ← upstream
      │
      │  git pull upstream main  (~每月)
      ↓
      main                                      ← 镜像上游，0 commits ahead
      │
      │  git merge main  (解冲突后)
      ↓
      smartbox-main                             ← 我们的交付线，带所有 patch
      │
      ├── feat/qr-login-callbacks               ← 独立 patch 分支（备份 + upstream PR 用）
      ├── feat/your-next-thing                  ← 新功能分支，PR 回 smartbox-main
      └── ...
```

**`smartbox-software` 项目引用 fork 的方式**：submodule，pin 到 `smartbox-main` 的某个具体 commit。

## 开发工作流（推荐 worktree）

用 worktree 的好处：同时开多个功能分支，每个都是独立目录，不用 stash / 切分支，想看哪个开哪个。

### 场景 1 — 给 hermes-agent 加新功能

```bash
cd ~/Repositories/hermes-agent

# 确保 smartbox-main 是最新的
git checkout smartbox-main
git pull origin smartbox-main

# 开 worktree（独立目录 + 独立分支，都基于 smartbox-main）
git worktree add ../hermes-agent-feat-xxx -b feat/xxx smartbox-main
cd ../hermes-agent-feat-xxx

# 正常开发 —— 这个目录是完整的 git 工作树
# 装 venv、跑测试都在这里做
python -m venv venv && ./venv/bin/pip install -e .
# ... edit files ...
git commit -am "feat(xxx): do the thing"
git push -u origin feat/xxx

# GitHub 上开 PR: feat/xxx → smartbox-main，合并

# 合并之后清理 worktree（主 clone 的磁盘会回来）
cd ~/Repositories/hermes-agent
git worktree remove ../hermes-agent-feat-xxx
git fetch origin
```

合进 smartbox-main 之后，**smartbox-software 那边不会自动拿到**。需要显式升级 submodule pin（见下面场景 3）。

### 场景 2 — 同步上游 NousResearch/hermes-agent

```bash
cd ~/Repositories/hermes-agent

# 一次性：配上游 remote（首次才需要）
git remote add upstream https://github.com/NousResearch/hermes-agent.git
git remote -v  # 确认 origin (fork) + upstream 都在

# 把上游最新拉到 fork 的 main
git checkout main
git fetch upstream
git merge upstream/main          # main 理论上 0 commits ahead，应该快进
git push origin main

# 把新增内容合进我们的 delivery 分支
git checkout smartbox-main
git merge main                   # 可能有冲突 —— 比如我们的 qr_login 改动和上游新改动撞了
# 解冲突 → add → commit
git push origin smartbox-main

# 测试（重要）：跑一下 hermes 核心功能确认没破
./venv/bin/pytest tests/  # 或其他 smoke 测试

# 回 smartbox-software 升级 pin
cd ~/Repositories/smartbox-software
make update-vendor
git add vendor/hermes-agent
git commit -m "chore(vendor): sync hermes-agent with upstream (merged up to <upstream-sha>)"
git push
```

### 场景 3 — 新 patch 开发完，让 SmartBox 这边用上

```bash
# 假设 feat/xxx 已经 merge 进 smartbox-main

cd ~/Repositories/smartbox-software
make update-vendor      # 等价于：git submodule update --remote vendor/hermes-agent
# 看 diff 确认要接受
git diff vendor/hermes-agent
# Submodule vendor/hermes-agent 3b3f39e6...abcdef01 (n commits):
#   > feat(xxx): do the thing
#   ...

# 确认好
git add vendor/hermes-agent
git commit -m "chore(vendor): bump hermes-agent to <new-short-sha>"
git push
```

### 场景 4 — patch 可以上游化（推荐！）

我们的目标是尽可能让 patch 被 NousResearch 接收，减少长期维护负担。

```bash
cd ~/Repositories/hermes-agent
git checkout feat/xxx              # 独立 patch 分支，不是 merge commit
# 直接开 PR: your-fork:feat/xxx → NousResearch/hermes-agent:main
gh pr create --repo NousResearch/hermes-agent --base main --head shevapato2008:feat/xxx \
  --title "..." --body "..."
```

如果 merge 了，下次跑场景 2 同步上游时，我们的 smartbox-main 上对应的 commit 会和上游重合（或者可以 rebase 掉）。长远看 `smartbox-main` 上"独有" 的 patch 越少越好。

## 常见情况 / FAQ

### Q: 我在 smartbox-main 上直接 commit 可以吗？

可以，但**不推荐**。直接 commit 会跳过 review，而且如果这个 commit 适合上游化，需要 cherry-pick 到独立分支才能 PR —— 麻烦。**所有改动走 feature branch → PR 回 smartbox-main** 的流程，和 SmartBox 主项目一致。

### Q: 我的 feat 分支已经合进 smartbox-main 了，还需要保留吗？

**短期保留**，方便做 upstream PR。如果已经 PR 给上游且被 merge，下次 sync 后就可以删了。如果 upstream 拒了 / 没动静，长期保留作为审计记录也行 —— fork 上不值钱。

### Q: 我怎么知道 smartbox-main 现在比上游多了哪些 patch？

```bash
git log main..smartbox-main --oneline
# 看到的 commits 就是我们"独有"的。理想情况下越少越好。
```

### Q: 想试验性改动，又不想影响 smartbox-main 上跑的 SBC 设备怎么办？

submodule 机制天然隔离 —— 你在这个 clone 里瞎搞，smartbox-software 那边的 submodule 仍然 pin 在旧 commit，不会自动跟。等你玩够了再决定要不要合 smartbox-main + bump pin。

### Q: CLAUDE.md 是啥？

Anthropic Claude Code 的项目级规则文件。每个 repo 根目录放一份。对 AI 助手有效，对 git / CI / 人类都没感知。和我们 smartbox-main 策略无关。

### Q: worktree 好处到底是什么？

同时开 5 个功能，5 个 checkout，每个目录独立。不用 `git stash`、不用切分支、不怕 venv 不匹配。VSCode/Cursor 里每个 worktree 开一个窗口，完全独立。删 worktree 就是 `git worktree remove <path>`，没副作用。

```bash
# 看当前所有 worktree
git worktree list
#   /Users/fan/Repositories/hermes-agent             3b3f39e6 [smartbox-main]
#   /Users/fan/Repositories/hermes-agent-feat-xxx    abc12345 [feat/xxx]
```

## 紧急修复 smartbox-software 的 submodule 引用

如果 submodule 指到一个已经不存在的 commit（比如 force-push 删了），在 smartbox-software 里：

```bash
cd ~/Repositories/smartbox-software
# 让 submodule 回到当前 pin
git submodule update --init vendor/hermes-agent
# 或者强制重新 bump 到 smartbox-main tip
make update-vendor
```
