# Architecture

## MVP Scope

- 预设：`classic-6`
- 角色：`2 狼人 + 预言家 + 女巫 + 2 平民`
- participant：`human` / `mock` / `codex_cli` / `kimi_cli`
- 强回合制：夜晚狼人密谈 → 狼人刀口 → 预言家查验 → 女巫用药 → 白天发言 → 并行投票

## Components

- `wolfkill/models.py`: 状态模型、动作模型、事件模型
- `wolfkill/presets.py`: 6 人局预设与随机发牌
- `wolfkill/visibility.py`: public/private 可见性裁剪与 recent-window 上下文
- `wolfkill/participants.py`: 人类、mock、codex、kimi 适配器与提示词
- `wolfkill/gateway.py`: 请求分发、并行投票、上帝视角、等待诊断
- `wolfkill/engine.py`: 游戏规则与回合推进
- `wolfkill/cli.py`: 运行入口、回放、复盘、落盘
