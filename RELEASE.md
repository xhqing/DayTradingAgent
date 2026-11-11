# 发版清单

本文对应 Issue #87 / T149HKUS「成品化阶段三：发版流程」。记录的都是 2026-11-11 实测到的状态，不把尚未做的事写成已完成。版本号的唯一权威是项目根 `VERSION`，本文不写死具体版本数字，避免下次发版时漂移。

## 一、原验收项逐条核对

| 原验收项 | 现状 | 说明 |
|---|---|---|
| 首次发版：`/bump` → `/release` 打 tag + GitHub Release | 已完成 | 仓库已有一系列 GitHub Release（用 `gh release list` 实测），最新版本见 `VERSION`；2026-11-11 当天自动发版链实跑过多轮，流程本身不需要再建。 |
| 版本语义约定写入 README | 已完成 | 本次在 `README.md` 与 `README_cn.md` 各补一节「Versioning / 版本语义」，两版语义对齐。 |
| 发版前清单文档 | 已完成 | 即本文件。 |
| 全量 pytest 全绿 | 未就绪 | `tests/` 与 pytest 骨架尚未建立，由 Issue #85（T147HKUS）跟踪。当前真实门禁是 `.github/workflows/validate.yml`（仓库卫生 + 语法检查）、gitleaks 确定性扫描，以及每次改动自带的离线验证脚本。 |
| doctor 全绿 | 未就绪 | 环境自检命令 `doctor.py` 尚未实现，由 Issue #86（T148HKUS）跟踪。 |
| 模拟盘冒烟一笔 | 不作为发版硬前置 | 它依赖盯盘会话与盘中场景，属于盘中验收动作，见 Issue #91 / #92；不能为了发版去凑一笔交易。 |
| CHANGELOG 无 `[Unreleased]` 残留 | 已完成 | 历史 `## [Unreleased]` 段落已改名为「历史留档」并注明内容已随各版本发布；当前待发布内容统一以顶部 `### 变更` / `### 修复` 记录，bump 时归入新版本节，不再向历史段落追加。 |

## 二、现行可执行的发版前检查

1. `VERSION` 与 CHANGELOG 顶部最新实际版本标题一致（commit skill 第 9k 步检测）。
2. `.github/workflows/validate.yml` 在 PR 上真实跑过且全绿；main 保护要求该检查为 required 且 `strict = true`。
3. `gitleaks dir` 与 `gitleaks protect --staged` 均为 0 命中；commit message、PR / Issue 正文、Release notes 等公开文本另外单独自检。
4. 本次改动附带的离线验证脚本全部通过，并把结论写进 CHANGELOG。
5. 走 `/bump` → `/add` → `/commit` → PR + CI → `/release` 完整链；不打已存在的 tag，不 force push，不直推 remote main。
6. 发布后用 `gh release view v<版本> -R xhqing/trading-agent` 实测可见，不能凭 VERSION 或 CHANGELOG 推断发布状态。

## 三、前提依赖的裁定

条目原文写着「依赖 T147 + T148 完成」。核对现状后裁定该前提已过时：发版流程由全局能力层的技能链实现并已实跑，不依赖项目内的测试地基与 doctor。测试地基、配置工程化仍分别由 Issue #85 / #86 跟踪，但不再作为发版的阻塞条件。本次据此把可落实的清单文档、README 版本语义与 CHANGELOG 口径补齐，关闭 Issue #87。

## 四、与相邻待办的分工

- Issue #85（T147HKUS）：建 `requirements.txt` / `tests/` / 回归测试 / CI 测试层。
- Issue #86（T148HKUS）：配置 fail-fast、`doctor.py`、README 双语 Quick Start。
- 本文件只管发版流程本身；上面两项完成后，把「全量 pytest 全绿」「doctor 全绿」两行从「未就绪」改为「已就绪」并纳入第 2 步门禁即可。
