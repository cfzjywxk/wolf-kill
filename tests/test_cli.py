from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from wolfkill import cli


class CliTests(unittest.TestCase):
    def test_run_command_assigns_random_human_seat_when_not_specified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / 'config.json'
            config_path.write_text('{"preset":"classic-6","participants":{}}', encoding='utf-8')
            captured: dict[str, object] = {}

            def fake_build_participants(*, preset, seed, config, human_seat, human_name):
                captured['human_seat'] = human_seat
                captured['human_name'] = human_name
                raise SystemExit(0)

            args = argparse.Namespace(
                preset='classic-6',
                seed=None,
                human_seat=None,
                human_name='你',
                config=str(config_path),
                max_days=12,
                narration_delay_seconds=0.0,
            )

            stream = io.StringIO()
            with patch('wolfkill.cli.random.choice', return_value='p4'), patch('wolfkill.cli.build_participants', side_effect=fake_build_participants):
                with redirect_stdout(stream):
                    with self.assertRaises(SystemExit):
                        cli.run_command(args, explicit_flags=set())

            output = stream.getvalue()
            self.assertEqual(captured['human_seat'], 'p4')
            self.assertEqual(captured['human_name'], '你')
            self.assertIn('随机分配座位：你是4号位', output)


    def test_save_game_record_includes_timing_stats(self) -> None:
        from wolfkill.gateway import ParticipantGateway
        from wolfkill.participants import ParticipantAdapter
        from wolfkill.presets import create_state_from_role_map
        from wolfkill.visibility import VisibilityCompiler
        from wolfkill.models import Audience, Role

        class FastParticipant(ParticipantAdapter):
            def speak(self, request):
                return {"text": "测试发言"}

            def decide(self, request):
                return {"action_type": "NO_OP", "target": None}

        state = create_state_from_role_map("classic-6", 7, {"p1": Role.VILLAGER, "p2": Role.WOLF, "p3": Role.WOLF, "p4": Role.SEER, "p5": Role.WITCH, "p6": Role.VILLAGER})
        gateway = ParticipantGateway({"p1": FastParticipant("fast")}, VisibilityCompiler())
        with redirect_stdout(io.StringIO()):
            gateway.request_speech(state, "p1", Audience.PUBLIC, "请发表一句简短的白天发言。")

        with tempfile.TemporaryDirectory() as temp_dir:
            learn_dir = Path(temp_dir)
            cli.save_game_record(state, "复盘内容", learn_dir, gateway=gateway)
            saved_files = list(learn_dir.glob("*-game.md"))
            self.assertEqual(len(saved_files), 1)
            content = saved_files[0].read_text(encoding="utf-8")
            self.assertIn("## 等待耗时统计", content)
            self.assertIn("真正等待 AI/IO 总耗时", content)
            self.assertIn("### 分步骤耗时", content)

    def test_build_participants_uses_ai_pool_when_human_seat_is_random(self) -> None:
        from wolfkill.cli import build_participants
        from wolfkill.participants import CodexCliParticipant, HumanCliParticipant, KimiCliParticipant
        from wolfkill.presets import get_preset

        participants = build_participants(
            preset=get_preset("classic-6"),
            seed=7,
            config={
                "_base_dir": "/tmp/wolfkill-config",
                "participants": {
                    "p2": {"type": "codex_cli", "name": "Codex A"},
                    "p3": {"type": "codex_cli", "name": "Codex B"},
                    "p4": {"type": "kimi_cli", "name": "Kimi A"},
                    "p5": {"type": "kimi_cli", "name": "Kimi B"},
                    "p6": {"type": "codex_cli", "name": "Codex C"},
                },
            },
            human_seat="p5",
            human_name="你",
        )
        self.assertIsInstance(participants["p5"], HumanCliParticipant)
        self.assertIsInstance(participants["p1"], CodexCliParticipant)
        self.assertIsInstance(participants["p2"], CodexCliParticipant)
        self.assertIsInstance(participants["p3"], KimiCliParticipant)
        self.assertIsInstance(participants["p4"], KimiCliParticipant)
        self.assertIsInstance(participants["p6"], CodexCliParticipant)

    def test_verify_kimi_cli_ready_fails_when_model_is_missing(self) -> None:
        from wolfkill.participants import KimiCliParticipant, verify_kimi_cli_ready
        participant = KimiCliParticipant(name='Kimi P2', model='')
        participant.model = None
        with patch('wolfkill.participants.shutil.which', return_value='/usr/bin/kimi'):
            with self.assertRaises(RuntimeError) as ctx:
                verify_kimi_cli_ready(participant)
        self.assertIn('未配置 model', str(ctx.exception))

    def test_verify_kimi_cli_ready_fails_fast_on_invalid_authentication(self) -> None:
        from wolfkill.participants import KimiCliParticipant, verify_kimi_cli_ready
        participant = KimiCliParticipant(name='Kimi P2', model='kimi-code/kimi-for-coding')
        with patch('wolfkill.participants.shutil.which', return_value='/usr/bin/kimi'), patch('wolfkill.participants.subprocess.run', return_value=__import__('subprocess').CompletedProcess(['kimi'], 0, stdout="Error code: 401 - {'error': {'message': 'Invalid Authentication', 'type': 'invalid_authentication_error'}}", stderr='')):
            with self.assertRaises(RuntimeError) as ctx:
                verify_kimi_cli_ready(participant)
        self.assertIn('Invalid Authentication', str(ctx.exception))

    def test_debug2_example_assigns_one_claude_and_one_human(self) -> None:
        from wolfkill.participants import ClaudeCliParticipant, HumanCliParticipant
        config = cli.load_config('examples/debug2-you-plus-1claude.json')
        participants = cli.build_participants(
            preset=cli.get_preset('duel-2'),
            seed=7,
            config=config,
            human_seat='p1',
            human_name='你',
        )
        claude_count = sum(isinstance(adapter, ClaudeCliParticipant) for adapter in participants.values())
        human_count = sum(isinstance(adapter, HumanCliParticipant) for adapter in participants.values())
        self.assertEqual(claude_count, 1)
        self.assertEqual(human_count, 1)

    def test_debug4_example_assigns_human_kimi_claude_codex(self) -> None:
        from wolfkill.participants import ClaudeCliParticipant, CodexCliParticipant, HumanCliParticipant, KimiCliParticipant
        config = cli.load_config('examples/debug4-you-kimi-claude-codex-all-wolves.json')
        participants = cli.build_participants(
            preset=cli.get_preset('all-wolf-4'),
            seed=7,
            config=config,
            human_seat='p1',
            human_name='你',
        )
        self.assertIsInstance(participants['p1'], HumanCliParticipant)
        self.assertIsInstance(participants['p2'], KimiCliParticipant)
        self.assertIsInstance(participants['p3'], ClaudeCliParticipant)
        self.assertIsInstance(participants['p4'], CodexCliParticipant)

    def test_find_reviewer_uses_isolated_claude_review_session(self) -> None:
        from wolfkill.participants import ClaudeCliParticipant
        reviewer = cli._find_reviewer({'p1': ClaudeCliParticipant(name='Claude', env={'http_proxy':'http://127.0.0.1:7890'})})
        self.assertIsNotNone(reviewer)
        kind, command, env, cwd = reviewer
        self.assertEqual(kind, 'claude_json')
        self.assertIn('--session-id', command)
        self.assertIn('--no-session-persistence', command)
        self.assertNotIn('--resume', command)

    def test_find_reviewer_uses_isolated_kimi_review_session(self) -> None:
        from wolfkill.participants import KimiCliParticipant
        reviewer = cli._find_reviewer({'p1': KimiCliParticipant(name='Kimi', model='kimi-code/kimi-for-coding')})
        self.assertIsNotNone(reviewer)
        kind, command, env, cwd = reviewer
        self.assertEqual(kind, 'kimi_stream_json')
        self.assertIn('--session', command)
        self.assertIn('review-', command[command.index('--session') + 1])
        self.assertIn('stream-json', command)

    def test_post_game_review_parses_kimi_stream_json_output(self) -> None:
        import subprocess
        from wolfkill.participants import KimiCliParticipant
        from wolfkill.presets import create_state_from_role_map
        from wolfkill.models import Role, Team
        state = create_state_from_role_map('classic-6', 7, {'p1': Role.WOLF, 'p2': Role.WOLF, 'p3': Role.SEER, 'p4': Role.WITCH, 'p5': Role.VILLAGER, 'p6': Role.VILLAGER})
        state.winner = Team.WOLF
        participant = KimiCliParticipant(name='Kimi', model='kimi-code/kimi-for-coding')
        payload = '{"role":"assistant","content":[{"type":"text","text":"复盘输出"}]}'
        with patch('wolfkill.cli.subprocess.run', return_value=subprocess.CompletedProcess(['kimi'], 0, stdout=payload, stderr='')):
            with redirect_stdout(io.StringIO()):
                output = cli.post_game_review(state, {'p1': participant})
        self.assertEqual(output, '复盘输出')

    def test_load_learn_history_uses_strategy_guide_whitelist_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            learn_dir = Path(temp_dir)
            (learn_dir / '000_狼人杀进阶策略知识库.md').write_text('策略知识', encoding='utf-8')
            (learn_dir / 'strategy_guide.md').write_text('策略指南', encoding='utf-8')
            (learn_dir / '20260307_184850-game.md').write_text('旧对局记录', encoding='utf-8')
            (learn_dir / 'game_20260307_183403.md').write_text('旧风格对局记录', encoding='utf-8')
            history = cli.load_learn_history(learn_dir)
        self.assertEqual(history, ['策略指南'])

    def test_resolve_learn_dir_walks_up_from_examples_base_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            examples_dir = root / 'examples'
            examples_dir.mkdir()
            learn_dir = root / 'learn'
            learn_dir.mkdir()
            (learn_dir / 'strategy_guide.md').write_text('策略指南', encoding='utf-8')

            resolved = cli.resolve_learn_dir(examples_dir)

        self.assertEqual(resolved, learn_dir)

if __name__ == '__main__':
    unittest.main()
