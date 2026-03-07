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

if __name__ == '__main__':
    unittest.main()
