from __future__ import annotations

import argparse
import datetime
import json
import random
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from .colors import bold, cyan, dim, green, magenta, red, seat_color, yellow
from .debug_logging import AgentDebugLogger
from .engine import GameEngine, build_previous_game_summary
from .gateway import ParticipantGateway
from .localization import (
    format_event_line,
    label_death_cause,
    label_issue_kind,
    label_issue_mode,
    label_phase,
    label_preset,
    label_role,
    label_seat,
    label_status,
    label_team,
)
from .participants import (
    ClaudeCliParticipant,
    CodexCliParticipant,
    HumanCliParticipant,
    KimiCliParticipant,
    MockParticipant,
    verify_claude_cli_ready,
    verify_kimi_cli_ready,
    build_process_env,
    normalize_timeout_seconds,
    resolve_subprocess_cwd,
)
from .presets import create_state_from_preset, get_preset
from .visibility import VisibilityCompiler


def main(argv: list[str] | None = None) -> int:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(prog="wolfkill")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Run a werewolf game")
    run_parser.add_argument("--preset", default="classic-6")
    run_parser.add_argument("--seed", type=int, default=None)
    run_parser.add_argument("--human-seat")
    run_parser.add_argument("--human-name", default="你")
    run_parser.add_argument("--config")
    run_parser.add_argument("--max-days", type=int, default=12)
    run_parser.add_argument("--narration-delay-seconds", type=float, default=1.0)
    args = parser.parse_args(raw_args)
    if args.command == "run":
        explicit_flags = {arg for arg in raw_args if arg.startswith("--")}
        return run_command(args, explicit_flags=explicit_flags)
    return 0


def run_command(args, *, explicit_flags: set[str] | None = None) -> int:
    explicit_flags = explicit_flags or set()
    config = load_config(args.config) if args.config else {}
    preset_name = args.preset if "--preset" in explicit_flags else config.get("preset", args.preset)
    seed_raw = args.seed if "--seed" in explicit_flags else config.get("seed", args.seed)
    seed = seed_raw if seed_raw is not None else random.randint(1, 99999)
    max_days = args.max_days if "--max-days" in explicit_flags else config.get("max_days", args.max_days)
    narration_delay_seconds = (
        args.narration_delay_seconds
        if "--narration-delay-seconds" in explicit_flags
        else config.get("narration_delay_seconds", args.narration_delay_seconds)
    )
    preset = get_preset(preset_name)
    project_dir = Path(config.get("_base_dir", ".")).resolve()
    learn_dir = resolve_learn_dir(project_dir)
    logs_dir = resolve_logs_dir(project_dir)
    learn_dir.mkdir(exist_ok=True)
    learn_history = load_learn_history(learn_dir)
    participants: dict | None = None
    previous_games: list[str] = []
    raw_human_seat = args.human_seat
    current_human_seat: str | None = None
    try:
        game_number = 0
        while True:
            game_number += 1
            current_seed = seed + game_number - 1
            human_seat = raw_human_seat
            assigned_random_human_seat = False
            if human_seat is None:
                human_seat = random.choice(preset.seat_order)
                assigned_random_human_seat = True
            elif human_seat not in preset.seat_order:
                human_seat = random.choice(preset.seat_order)
                assigned_random_human_seat = True
            if assigned_random_human_seat:
                print(f"【法官】随机分配座位：你是{human_seat.upper().replace('P', '')}号位。")
            if participants is None:
                participants = build_participants(
                    preset=preset,
                    seed=seed,
                    config=config,
                    human_seat=human_seat,
                    human_name=args.human_name,
                )
                for adapter in participants.values():
                    if isinstance(adapter, KimiCliParticipant):
                        verify_kimi_cli_ready(adapter)
                    if isinstance(adapter, ClaudeCliParticipant):
                        verify_claude_cli_ready(adapter)
                current_human_seat = human_seat
            elif human_seat != current_human_seat:
                _swap_human_seat(participants, current_human_seat, human_seat, args.human_name, config)
                current_human_seat = human_seat
            if game_number > 1:
                _reset_participants(participants)
                print(bold(f"\n{'=' * 50}"))
                print(bold(f"=== 第 {game_number} 局开始 ==="))
                print(bold(f"{'=' * 50}\n"))
            names = {seat: adapter.name for seat, adapter in participants.items()}
            backgrounds = {seat: adapter.background for seat, adapter in participants.items() if adapter.background}
            state = create_state_from_preset(
                preset_name,
                current_seed,
                names=names,
                backgrounds=backgrounds,
            )
            debug_log_path = logs_dir / f"{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}-game{game_number}-seed{current_seed}-agent-debug.jsonl"
            gateway = ParticipantGateway(
                participants,
                VisibilityCompiler(),
                narration_delay_seconds=narration_delay_seconds,
                learn_history=learn_history,
                previous_games=previous_games,
                debug_logger=AgentDebugLogger(debug_log_path),
            )
            print(dim(f"【诊断】AI 调试日志写入：{debug_log_path}"))
            if narration_delay_seconds > 0:
                print(dim(f"【诊断】当前播报停顿为 {narration_delay_seconds:.1f}s/条；这部分属于程序自身额外耗时。"))
            final_state = GameEngine(
                state,
                gateway,
                max_days=max_days,
                previous_games=previous_games,
                learn_history=learn_history,
                learn_briefing_label=_LEARN_FILE_WHITELIST[0] if learn_history else None,
            ).run()
            print_public_summary(final_state)
            print_gateway_issues(gateway.issues)
            print_gateway_timing_summary(gateway)
            review_text = post_game_review(final_state, participants)
            save_game_record(final_state, review_text, learn_dir, gateway=gateway)
            previous_games.append(build_previous_game_summary(final_state))
            if not _ask_play_again():
                break
    finally:
        _close_participants(participants)
    return 0


def _reset_participants(participants: dict | None) -> None:
    if not participants:
        return
    for adapter in participants.values():
        adapter.reset_state()
        if isinstance(adapter, HumanCliParticipant):
            adapter.last_seen_event_id = 0


def _swap_human_seat(
    participants: dict,
    old_seat: str | None,
    new_seat: str | None,
    human_name: str,
    config: dict,
) -> None:
    if old_seat == new_seat:
        return
    if old_seat is not None and old_seat in participants:
        base_dir = Path(config.get("_base_dir", ".")).resolve()
        participant_configs = config.get("participants", {})
        index = int(old_seat[1:]) if old_seat.startswith("p") and old_seat[1:].isdigit() else 0
        pc = participant_configs.get(old_seat, {"type": "mock", "name": f"模拟玩家{index}"})
        participants[old_seat] = _build_participant_from_config(pc, old_seat, base_dir, index)
    if new_seat is not None and new_seat in participants:
        participants[new_seat] = HumanCliParticipant(name=human_name)


def _close_participants(participants: dict | None) -> None:
    if not participants:
        return
    for adapter in participants.values():
        adapter.close()


def _ask_play_again() -> bool:
    print()
    try:
        answer = input(bold("是否再来一局？(y/n) ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes", "是", "好", "再来")


def build_participants(*, preset, seed: int, config: dict, human_seat: str | None, human_name: str) -> dict:
    base_dir = Path(config.get("_base_dir", ".")).resolve()
    participant_configs = config.get("participants", {})
    ordered_configs = [participant_configs[seat] for seat in preset.seat_order if seat in participant_configs]
    use_pool_assignment = len(ordered_configs) == max(0, len(preset.seat_order) - 1)
    pool_iter = iter(ordered_configs)
    participants = {}
    for index, seat in enumerate(preset.seat_order, start=1):
        if human_seat == seat:
            participants[seat] = HumanCliParticipant(name=human_name)
            continue
        if use_pool_assignment:
            participant_config = next(pool_iter, {"type": "mock", "name": f"模拟玩家{index}"})
        else:
            participant_config = participant_configs.get(seat, {"type": "mock", "name": f"模拟玩家{index}"})
        participants[seat] = _build_participant_from_config(participant_config, seat, base_dir, seed + index)
    return participants


def _build_participant_from_config(participant_config: dict, seat: str, base_dir: Path, seed: int):
    participant_type = participant_config.get("type", "mock")
    name = participant_config.get("name", seat.upper())
    background = participant_config.get("background")
    participant_env = {str(key): str(value) for key, value in participant_config.get("env", {}).items()}
    kimi_share_dir = participant_config.get("share_dir")
    if kimi_share_dir:
        participant_env.setdefault(
            "KIMI_SHARE_DIR",
            resolve_subprocess_cwd(kimi_share_dir, base_dir=str(base_dir)) or kimi_share_dir,
        )
    participant_cwd = resolve_subprocess_cwd(participant_config.get("cwd") or str(base_dir), base_dir=str(base_dir))
    if participant_type == "human_cli":
        return HumanCliParticipant(name=name, background=background)
    if participant_type == "codex_cli":
        return CodexCliParticipant(
            name=name,
            background=background,
            cwd=participant_cwd,
            timeout_seconds=normalize_timeout_seconds(participant_config.get("timeout_seconds")),
            model=participant_config.get("model"),
            profile=participant_config.get("profile"),
            executable=participant_config.get("executable", "codex"),
            sandbox=participant_config.get("sandbox", "read-only"),
            config_overrides=list(participant_config.get("config_overrides", [])),
            extra_args=list(participant_config.get("extra_args", [])),
            env=participant_env,
        )
    if participant_type == "claude_cli":
        return ClaudeCliParticipant(
            name=name,
            background=background,
            cwd=participant_cwd,
            timeout_seconds=normalize_timeout_seconds(participant_config.get("timeout_seconds")),
            model=participant_config.get("model") or "claude-sonnet-4-6",
            effort=participant_config.get("effort", "medium"),
            executable=participant_config.get("executable", "claude"),
            extra_args=list(participant_config.get("extra_args", [])),
            env=participant_env,
        )
    if participant_type == "kimi_cli":
        return KimiCliParticipant(
            name=name,
            background=background,
            cwd=participant_cwd,
            timeout_seconds=normalize_timeout_seconds(participant_config.get("timeout_seconds")),
            model=participant_config.get("model"),
            agent=participant_config.get("agent"),
            config_file=resolve_subprocess_cwd(participant_config.get("config_file"), base_dir=str(base_dir)),
            executable=participant_config.get("executable", "kimi"),
            extra_args=list(participant_config.get("extra_args", [])),
            env=participant_env,
        )
    return MockParticipant(name=name, background=background, seed=seed)


_LEARN_FILE_MAX_CHARS = 4000
_LEARN_TOTAL_MAX_CHARS = 8000
_LEARN_FILE_WHITELIST = ("strategy_guide.md",)


def resolve_learn_dir(base_dir: Path) -> Path:
    base_dir = base_dir.resolve()
    package_root = Path(__file__).resolve().parent.parent
    search_roots: list[Path] = []
    for candidate in (base_dir, Path.cwd().resolve(), package_root):
        if candidate not in search_roots:
            search_roots.append(candidate)

    def _iter_candidates() -> list[Path]:
        candidates: list[Path] = []
        for root in search_roots:
            for current in (root, *root.parents):
                learn_dir = current / "learn"
                if learn_dir not in candidates:
                    candidates.append(learn_dir)
        return candidates

    candidates = _iter_candidates()
    for learn_dir in candidates:
        if not learn_dir.is_dir():
            continue
        if any((learn_dir / filename).is_file() for filename in _LEARN_FILE_WHITELIST):
            return learn_dir
    for learn_dir in candidates:
        if learn_dir.is_dir():
            return learn_dir
    return package_root / "learn"


def resolve_logs_dir(base_dir: Path) -> Path:
    package_root = Path(__file__).resolve().parent.parent
    log_dir = package_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def load_learn_history(learn_dir: Path) -> list[str]:
    if not learn_dir.is_dir():
        return []
    entries = []
    total = 0
    md_files = sorted(
        [learn_dir / filename for filename in _LEARN_FILE_WHITELIST if (learn_dir / filename).is_file()],
        key=lambda path: path.name,
    )
    for md_file in md_files:
        try:
            text = md_file.read_text(encoding="utf-8").strip()
            if not text:
                continue
            if len(text) > _LEARN_FILE_MAX_CHARS:
                text = text[:_LEARN_FILE_MAX_CHARS] + "\n…（内容已截断）"
            if total + len(text) > _LEARN_TOTAL_MAX_CHARS:
                break
            entries.append(text)
            total += len(text)
        except Exception:
            continue
    return entries


def save_game_record(state, review_text: str | None, learn_dir: Path, *, gateway: ParticipantGateway | None = None) -> None:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = learn_dir / f"{ts}-game.md"
    lines = [
        f"# 对局记录 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"预设：{label_preset(state.preset_name)}　胜方：{label_team(state.winner)}",
        "",
        "## 玩家身份",
        "",
    ]
    for seat in state.seat_order:
        player = state.players[seat]
        status = "存活" if player.alive else f"第{player.death_day}天出局"
        lines.append(f"- {label_seat(seat)}: {player.name} — {label_role(player.role)}（{status}）")
    timing_summary = gateway.timing_summary() if gateway is not None else None
    if timing_summary is not None:
        lines.extend(["", "## 等待耗时统计", ""])
        lines.extend(build_gateway_timing_report_lines(timing_summary))
    if gateway is not None and getattr(gateway, "debug_logger", None) is not None:
        lines.extend(["", "## 调试日志", "", f"- AI 调试日志：{gateway.debug_logger.path}"])
    if gateway is not None and gateway.issues:
        lines.extend(["", "## 运行问题详情", ""])
        lines.extend(build_gateway_issue_report_lines(gateway.issues))
    lines.extend(["", "## 完整事件记录", ""])
    for event in state.transcript:
        lines.append(format_event_line(index=event.index, day=event.day, phase=event.phase, channel=event.channel, text=event.text, speaker=event.speaker))
    if review_text:
        lines.extend(["", "## AI 复盘与评分", "", review_text])
    filename.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_config(path: str) -> dict:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["_base_dir"] = str(config_path.parent.resolve())
    return payload


def _color_replay_line(event, raw: str) -> str:
    vis_tag = "公开" if event.visibility.value == "PUBLIC" else "私有"
    prefix = f"[{vis_tag}] "
    if event.channel == "speech" and event.speaker:
        return bold(seat_color(event.speaker, f"{prefix}{raw}"))
    if "投票" in event.text and event.speaker:
        return bold(seat_color(event.speaker, f"{prefix}{raw}"))
    if event.channel == "wolf" and event.speaker:
        return seat_color(event.speaker, f"{prefix}{raw}")
    if event.channel == "wolf":
        return magenta(f"{prefix}{raw}")
    if event.visibility.value == "PRIVATE":
        return dim(f"{prefix}{raw}")
    return yellow(f"{prefix}{raw}")


def print_public_summary(state) -> None:
    print("\n" + "=" * 50)
    print(bold("=== 完整游戏回放 ==="))
    print("=" * 50)
    prev_phase_key: tuple[int, str] | None = None
    for event in state.transcript:
        phase_key = (event.day, event.phase.value)
        if phase_key != prev_phase_key:
            print(bold(f"\n--- 第{event.day}天 · {label_phase(event.phase)} ---"))
            prev_phase_key = phase_key
        raw = format_event_line(index=event.index, day=event.day, phase=event.phase, channel=event.channel, text=event.text, speaker=event.speaker)
        print(_color_replay_line(event, raw))
    print("\n" + "=" * 50)
    print(bold("=== 最终身份 ==="))
    print("=" * 50)
    for seat in state.seat_order:
        player = state.players[seat]
        status = label_status(player.alive, [label_death_cause(cause) for cause in player.death_causes])
        death_day_text = f"，第{player.death_day}天" if player.death_day else ""
        role_colored = red(label_role(player.role)) if player.role.value == "WOLF" else green(label_role(player.role))
        alive_colored = green("存活") if player.alive else dim(f"{status}{death_day_text}")
        print(f"  {label_seat(seat)}: {player.name} -> {role_colored}（{alive_colored}）")
    winner_text = label_team(state.winner)
    winner_colored = red(winner_text) if state.winner and state.winner.value == "WOLF" else green(winner_text)
    print(f"\n胜方：{bold(winner_colored)}")


def _build_review_prompt(state) -> str:
    lines = [
        "你是一位资深狼人杀解说。请对以下完整对局进行复盘总结。",
        "",
        "要求：",
        "1. 先用 2-3 段概述整局走势和关键转折点。",
        "2. 然后对每位玩家逐一点评：给出 1-10 分的评分和简短评语（发言质量、逻辑推理、策略执行）。",
        "3. 最后总结本局的亮点和可改进之处。",
        "4. 全部使用中文。",
        "",
        "=== 对局信息 ===",
        f"预设：{label_preset(state.preset_name)}",
        f"胜方：{label_team(state.winner)}",
        "",
        "=== 玩家身份 ===",
    ]
    for seat in state.seat_order:
        player = state.players[seat]
        status = "存活" if player.alive else f"第{player.death_day}天出局"
        lines.append(f"  {label_seat(seat)}: {player.name} - {label_role(player.role)}（{status}）")
    lines.append("")
    lines.append("=== 完整事件记录 ===")
    for event in state.transcript:
        speaker_text = f" {label_seat(event.speaker)}" if event.speaker else ""
        vis = "公开" if event.visibility.value == "PUBLIC" else "私有"
        lines.append(f"[{vis}][第{event.day}天:{label_phase(event.phase)}:{event.channel}{speaker_text}] {event.text}")
    return "\n".join(lines)


def _find_reviewer(participants: dict):
    for adapter in participants.values():
        if isinstance(adapter, ClaudeCliParticipant):
            args = [
                adapter.executable,
                '-p',
                '--output-format', 'json',
                '--model', adapter.model,
                '--effort', adapter.effort,
                '--session-id', str(uuid4()),
                '--no-session-persistence',
            ]
            return ('claude_json', args, dict(adapter.env), adapter.cwd)
    for adapter in participants.values():
        if isinstance(adapter, KimiCliParticipant):
            args = [
                adapter.executable,
                '--session', f'review-{uuid4().hex}',
                '--print',
                '--input-format', 'text',
                '--output-format', 'stream-json',
            ]
            if adapter.model:
                args.extend(['--model', adapter.model])
            if adapter.agent:
                args.extend(['--agent', adapter.agent])
            if adapter.config_file:
                args.extend(['--config-file', adapter.config_file])
            return ('kimi_stream_json', args, dict(adapter.env), adapter.cwd)
    for adapter in participants.values():
        if isinstance(adapter, CodexCliParticipant):
            args = [adapter.executable, 'exec', '--skip-git-repo-check', '--ephemeral', '--color', 'never', '--sandbox', adapter.sandbox]
            if adapter.model:
                args.extend(['-m', adapter.model])
            args.append('-')
            return ('codex_text', args, dict(adapter.env), adapter.cwd)
    return None


def post_game_review(state, participants: dict) -> str | None:
    reviewer = _find_reviewer(participants)
    if reviewer is None:
        return None
    reviewer_kind, command, env, cwd = reviewer
    prompt = _build_review_prompt(state)
    print("\n" + "=" * 50)
    print(bold(cyan("=== AI 复盘与评分 ===")))
    print("=" * 50)
    print(dim("正在生成复盘分析，请稍候..."))
    try:
        proc_env = build_process_env(env)
        completed = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=120,
            env=proc_env,
            check=False,
        )
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        if reviewer_kind == 'claude_json' and stdout:
            try:
                envelope = json.loads(stdout)
                if isinstance(envelope, dict) and envelope.get('result') is not None:
                    output = str(envelope['result']).strip()
                else:
                    output = stdout
            except json.JSONDecodeError:
                output = stdout
        elif reviewer_kind == 'kimi_stream_json' and stdout:
            from .participants import KimiCliParticipant
            output = KimiCliParticipant(name='review-tmp')._unwrap_stream_json(stdout).strip()
        else:
            output = stdout
        if not output:
            detail = stderr or stdout or 'AI 无返回内容'
            print(red(f"复盘生成失败：{detail}"))
            return None
        print()
        print(output)
        return output
    except subprocess.TimeoutExpired:
        print(red("复盘生成超时，已跳过。"))
        return None
    except Exception as exc:
        print(red(f"复盘生成失败：{exc}"))
        return None


def build_gateway_issue_report_lines(issues: list[dict[str, str]]) -> list[str]:
    lines: list[str] = []
    for issue in issues:
        lines.append(
            f"- [{label_seat(issue['seat'])}:{label_issue_mode(issue['mode'])}:{label_issue_kind(issue['kind'])}] "
            f"{issue['participant']}: {issue['message']}"
        )
        if issue.get('stderr'):
            lines.append(f"  stderr: {issue['stderr']}")
        if issue.get('stdout'):
            lines.append(f"  stdout: {issue['stdout']}")
    return lines


def print_gateway_issues(issues: list[dict[str, str]]) -> None:
    if not issues:
        return
    print("\n=== 运行问题 ===")
    for issue in issues:
        print(f"[{label_seat(issue['seat'])}:{label_issue_mode(issue['mode'])}:{label_issue_kind(issue['kind'])}] {issue['participant']}: {issue['message']}")


def build_gateway_timing_report_lines(summary: dict[str, object]) -> list[str]:
    total_seconds = float(summary["total_seconds"])
    provider_seconds = float(summary["provider_seconds"])
    io_wait_seconds = float(summary["io_wait_seconds"])
    program_seconds = float(summary["program_seconds"])
    pause_seconds = float(summary["pause_seconds"])
    wait_count = int(summary["wait_count"])
    provider_ratio = (provider_seconds / total_seconds * 100.0) if total_seconds > 0 else 0.0
    io_wait_ratio = (io_wait_seconds / total_seconds * 100.0) if total_seconds > 0 else 0.0
    program_ratio = (program_seconds / total_seconds * 100.0) if total_seconds > 0 else 0.0
    lines = [
        f"- 总等待步骤数：{wait_count}",
        f"- 总等待耗时：{total_seconds:.2f}s",
        f"- AI/外部处理总耗时：{provider_seconds:.2f}s（{provider_ratio:.1f}%）",
        f"- 真正等待 AI/IO 总耗时：{io_wait_seconds:.2f}s（{io_wait_ratio:.1f}%）",
        f"- 程序自身额外耗时：{program_seconds:.2f}s（{program_ratio:.1f}%）",
        f"- 播报停顿耗时：{pause_seconds:.2f}s",
        "",
        "### 分步骤耗时",
        "",
    ]
    for record in summary.get("records", []):
        lines.append(
            f"- {record.get('step_label', record.get('kind', '等待步骤'))}：总计 {float(record['total_seconds']):.2f}s，"
            f"AI/外部处理 {float(record['provider_seconds']):.2f}s，"
            f"真正等待 AI/IO {float(record.get('io_wait_seconds', record['provider_seconds'])):.2f}s，"
            f"程序额外 {float(record['program_seconds']):.2f}s，播报停顿 {float(record['pause_seconds']):.2f}s"
        )
    return lines


def print_gateway_timing_summary(gateway: ParticipantGateway) -> None:
    summary = gateway.timing_summary()
    if summary is None:
        return
    total_seconds = float(summary["total_seconds"])
    provider_seconds = float(summary["provider_seconds"])
    io_wait_seconds = float(summary["io_wait_seconds"])
    program_seconds = float(summary["program_seconds"])
    pause_seconds = float(summary["pause_seconds"])
    longest = summary["longest"]
    provider_ratio = (provider_seconds / total_seconds * 100.0) if total_seconds > 0 else 0.0
    io_wait_ratio = (io_wait_seconds / total_seconds * 100.0) if total_seconds > 0 else 0.0
    program_ratio = (program_seconds / total_seconds * 100.0) if total_seconds > 0 else 0.0
    longest_label = str(longest["step_label"])
    print("\n=== 等待耗时汇总 ===")
    print(
        f"共记录 {summary['wait_count']} 次等待，总计 {total_seconds:.2f}s；AI/外部处理 {provider_seconds:.2f}s（{provider_ratio:.1f}%），"
        f"其中真正等待 AI/IO {io_wait_seconds:.2f}s（{io_wait_ratio:.1f}%）；程序额外 {program_seconds:.2f}s（{program_ratio:.1f}%），其中播报停顿 {pause_seconds:.2f}s。"
    )
    print(f"最长一次等待：{longest_label}，耗时 {float(longest['total_seconds']):.2f}s。")
