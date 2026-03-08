# wolf-kill

一个可在本地运行的强回合制狼人杀 MVP。

当前只聚焦 `6` 人局：`2 狼人 + 预言家 + 女巫 + 2 平民`。
支持 `human_cli`、`codex_cli`、`kimi_cli`、`claude_cli` 混合参局。

## 当前特性

- 只使用白名单赛前知识库：`learn/strategy_guide.md`
- 开局会并行初始化全部 AI session，后续主要走增量事件同步
- 隐藏夜间流程对普通视角只显示通用等待文案，避免额外泄漏
- 白天平票进入 `PK 台`：平票玩家各自发言，其他存活玩家复投；`PK` 再平或全部弃票则无人出局
- 人类玩家在狼人密谈时可直接输入数字结束讨论：`2 = 无更多讨论`
- 每局会自动写一份 AI 调试日志，记录事件、请求、prompt、原始输出、解析结果与 fallback 信息

## 快速开始

固定自己座位运行一局：

```bash
python3 -m wolfkill run --preset classic-6 --human-seat p1 --human-name 你
```

随机你的座位：

```bash
python3 -m wolfkill run --preset classic-6 --human-name 你
```

去掉播报停顿，方便压测和排查：

```bash
python3 -m wolfkill run --preset classic-6 --human-name 你 --narration-delay-seconds 0
```

## 常用配置

内置 `codex + kimi` 示例：

```bash
python3 -m wolfkill run --config examples/classic6-codex-kimi.json
```

你自己 + 5 个全 `codex` agent：

```bash
python3 -m wolfkill run --config examples/classic6-you-plus-5codex.json --human-name 你
```

你自己 + `2 codex(medium) + 2 kimi + 1 claude`：

```bash
python3 -m wolfkill run --config examples/classic6-you-plus-2codex-2kimi-1claude.json --human-name 你
```

你自己 + `3 kimi + 2 claude`：

```bash
python3 -m wolfkill run --config examples/classic6-you-plus-3kimi-2claude.json --human-name 你
```

你自己 + `3 kimi + 1 codex(medium) + 1 claude`：

```bash
python3 -m wolfkill run --config examples/classic6-you-plus-3kimi-1codex-medium-1claude.json --human-name 你
```

## 调试与排查

每局开始时会打印类似：

```text
【诊断】AI 调试日志写入：logs/20260308_214225-game1-seed36218-agent-debug.jsonl
```

这份 `jsonl` 会记录：

- `event`：游戏事件
- `agent_call`：给 AI 的请求、完整 prompt、原始输出、解析结果、最终采用结果、异常与 fallback

适合排查：

- 是否发生状态泄漏
- 某个 AI 到底看到了什么
- 为什么某次发言/决策异常或超慢
- fallback 是如何触发的

## 说明

- `strategy_briefing` 只在 session 的首次 full-context 请求里发送一次
- 后续请求主要依赖 resume/session + `new_public_events` / `new_visible_events`
- `Claude` 使用 `claude-sonnet-4-6` + `effort=medium`
- 示例里的 `codex` 可通过 `config_overrides` 调整 `model_reasoning_effort`
- `Kimi` 支持 `--thinking / --no-thinking`；你也可以在 `~/.kimi/config.toml` 里配置默认值

## 测试

运行全量测试：

```bash
python3 -m unittest discover -s tests
```
