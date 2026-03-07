# wolf-kill

一个可在本地运行的强回合制狼人杀 MVP。

当前只聚焦 `6` 人局：`2 狼人 + 预言家 + 女巫 + 2 平民`。
AI 参与者主打 `codex cli` 和 `kimi cli`，并在同一局内复用同一个 session / resume 上下文。

## 快速开始

```bash
python3 -m wolfkill run --preset classic-6 --human-seat p1 --human-name 你
```

默认不额外插入播报停顿；如果你想让法官播报放慢节奏，便于跟进流程：

```bash
python3 -m wolfkill run --preset classic-6 --human-seat p1 --human-name 你 --narration-delay-seconds 3
```

使用内置 `codex` / `kimi` adapter：

```bash
python3 -m wolfkill run --config examples/classic6-codex-kimi.json
```

你自己 + 5 个全 `codex` agent：

```bash
python3 -m wolfkill run --config examples/classic6-you-plus-5codex.json --human-seat p1 --human-name 你
```

你自己 + 5 个混合 provider agent：

```bash
python3 -m wolfkill run --config examples/classic6-you-plus-5mixed-agents.json --human-seat p1 --human-name 你
```

每次等待 AI 发言或行动后，CLI 会打印一条 `【诊断】`，拆分本次等待中的 `AI/外部处理` 与 `程序额外耗时`（包括播报停顿）的占比；一局结束后还会再给出总汇总。

对局结束后会：
- 打印完整回放与最终身份
- 调用一个 AI 生成关键步骤复盘和简单评分
- 将记录汇总写入 `learn/<datetime>-game.md`

运行测试：

```bash
python3 -m unittest discover -s tests
```
