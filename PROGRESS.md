# wolf-kill Progress

_Updated: 2026-03-08_

## Current Scope

- MVP preset: `classic-6`
- Roles: `2 狼人 + 预言家 + 女巫 + 2 平民`
- Supported participants:
  - `human_cli`
  - `mock`
  - `codex_cli`
  - `kimi_cli`

## Current Run Commands

### Preferred: 5 Codex + You

```bash
cd /home/ywxk/src/requirements/myswat/wolf-kill
python3 -m wolfkill run --config examples/classic6-you-plus-5codex.json --human-name 你
```

### Mixed Agents: 3 Codex + 2 Kimi + You

```bash
cd /home/ywxk/src/requirements/myswat/wolf-kill
python3 -m wolfkill run --config examples/classic6-you-plus-5mixed-agents.json --human-name 你
```

## What Has Been Fixed

### Gameplay / Rules

- `classic-6` preset aligned to MVP roles.
- Wolf win condition fixed:
  - wolves win by 屠边 / 屠城 only
  - villagers win when all wolves are out
- Witch rule remains:
  - cannot self-save
  - can choose save / poison / no-op when applicable
- White-day speech prompt no longer forces "brief" speech.
- Random human seat assignment works when `--human-seat` is omitted.

### Visibility / Information Safety

- Public state no longer leaks dead players' roles.
- Public state no longer leaks death causes.
- Night private actions are hidden from non-owners.
- Hidden night progress no longer leaks seat/name or implied wolf count.
- After death, observer switches to god view correctly.
- Observer visibility logic extracted into `wolfkill/observer_visibility.py`.

### AI Context / Sessions

- `codex_cli` reuses session via `resume`.
- `kimi_cli` reuses one session per game.
- Incremental context sync works after first full context request.
- Strategy briefing is only included on first full-context request.
- Advanced werewolf strategy knowledge base added in:
  - `learn/000_狼人杀进阶策略知识库.md`

### Timing / UX

- Waiting spinner added.
- Real-time noisy diagnostics removed from play view.
- End-of-game timing summary preserved.
- Per-step timing and true AI/IO wait stats are written to saved game records.
- Default narration pacing currently set to `1.0s` per displayed message.

### Git / Repo

- Repository initialized and pushed to:
  - `https://github.com/cfzjywxk/wolf-kill`
- Git author/committer corrected to:
  - `cfzjywxk <cfzjywxk@gmail.com>`
- `learn/` is git-ignored.

## Current Known Issue

### Kimi Authentication Is Still Unstable In Real Runs

Observed runtime error:

- `401 Invalid Authentication`
- previously also saw `LLM not set`

What has already been done:

- `kimi` model is now explicitly set in examples.
- default model is loaded from `~/.kimi/config.toml` if missing.
- `kimi` preflight check added.
- `kimi` switched to `stream-json` output parsing.
- removed `share_dir` from examples to avoid auth isolation.

Current practical recommendation:

- use the `5 codex + you` config for stable gameplay right now
- only use mixed config after confirming local `kimi` login/auth is healthy

## Test Status

Run:

```bash
python3 -m unittest discover -s tests
```

Current status:

- `54` tests passing

## Visibility Coverage

Focused visibility coverage target achieved for extracted policy module:

- `wolfkill/observer_visibility.py`: `97%`
- `tests/test_observer_visibility.py`: `97%`

## Files Worth Reading First

- `wolfkill/engine.py`
- `wolfkill/gateway.py`
- `wolfkill/observer_visibility.py`
- `wolfkill/visibility.py`
- `wolfkill/participants.py`
- `tests/test_visibility.py`
- `tests/test_gateway.py`
- `tests/test_participants.py`
- `tests/test_engine.py`

## Suggested Next Steps

1. Add a `wolfkill doctor` / preflight command.
2. Make Kimi auth health-check more explicit and actionable.
3. Continue reducing opening narration noise.
4. Add transcript/log audit tests to detect public info leaks automatically.
5. Optionally extract display/announcement policy into its own module and raise its coverage too.
