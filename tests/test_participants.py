from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from wolfkill.cli import build_participants, load_config
from wolfkill.gateway import ParticipantGateway
from wolfkill.participants import ClaudeCliParticipant, CodexCliParticipant, KimiCliParticipant, ParticipantInvocationError, verify_claude_cli_ready
from wolfkill.presets import create_state_from_role_map, get_preset
from wolfkill.visibility import VisibilityCompiler
from wolfkill.models import EventVisibility, Phase, Role


class ParticipantAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        intro_event = {"index": 1, "day": 1, "phase": "SETUP", "visibility": "PUBLIC", "channel": "system", "text": "游戏开始，测试规则说明。", "speaker": None, "recipients": [], "data": {}}
        private_intro_event = {"index": 2, "day": 1, "phase": "SETUP", "visibility": "PRIVATE", "channel": "system", "text": "你的身份是平民，所属阵营：好人阵营。", "speaker": None, "recipients": ["p1"], "data": {"role": "VILLAGER"}}
        self.speech_request = {"seat": "p1", "name": "P1", "day": 1, "phase": "DAY_SPEECH", "prompt": "Say something.", "background": "Test background", "audience": "PUBLIC", "public_state": {"alive_seats": ["p1", "p2"]}, "private_view": {"seat": "p1", "role": "VILLAGER", "team": "VILLAGE", "alive": True, "all_visible_events": [intro_event, private_intro_event]}}
        self.decision_request = {**self.speech_request, "prompt": "Vote now.", "options": [{"action_type": "DAY_VOTE", "targets": ["p2"], "requires_target": True, "description": "Vote out a seat."}, {"action_type": "NO_OP", "targets": [], "requires_target": False, "description": "Skip."}]}

    def test_codex_cli_participant_uses_output_file_schema(self) -> None:
        participant = CodexCliParticipant(name="Codex P1", cwd="/tmp/wolfkill-codex", timeout_seconds=12.0, model="gpt-test", profile="default", config_overrides=['model_reasoning_effort="low"'])
        captured: list[list[str]] = []

        def fake_run(command, **kwargs):
            captured.append(command)
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_text('{"action_type": "NO_OP", "target": null}\n', encoding='utf-8')
            if "resume" in command:
                return subprocess.CompletedProcess(command, 0, stdout='{"type":"item.completed","item":{"type":"agent_message","text":"{\"action_type\": \"NO_OP\", \"target\": null}"}}\n', stderr="")
            return subprocess.CompletedProcess(command, 0, stdout='{"type":"thread.started","thread_id":"thread-1"}\n', stderr="")

        with patch("wolfkill.participants.subprocess.run", side_effect=fake_run):
            first = participant.decide(self.decision_request)
            second = participant.decide(self.decision_request)

        self.assertEqual(first["action_type"], "NO_OP")
        self.assertEqual(second["action_type"], "NO_OP")
        self.assertIn("--output-schema", captured[0])
        self.assertIn("resume", captured[1])
        self.assertIn("thread-1", captured[1])

    def test_claude_cli_participant_uses_required_model_and_effort(self) -> None:
        participant = ClaudeCliParticipant(name="Claude P4", cwd="/tmp/wolfkill-claude")
        captured: list[list[str]] = []

        def fake_run(command, **kwargs):
            import json
            captured.append(command)
            payload = json.dumps({"session_id": "claude-session-1", "result": json.dumps({"text": "ok"})})
            return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")
        with patch("wolfkill.participants.subprocess.run", side_effect=fake_run):
            response = participant.speak(self.speech_request)

        self.assertEqual(response["text"], "ok")
        self.assertIn("--model", captured[0])
        self.assertIn("claude-sonnet-4-6", captured[0])
        self.assertIn("--effort", captured[0])
        self.assertIn("medium", captured[0])
        self.assertEqual(participant.session_id, "claude-session-1")

    def test_claude_cli_participant_resumes_same_session(self) -> None:
        participant = ClaudeCliParticipant(name="Claude Resume", cwd="/tmp/wolfkill-claude")
        participant.session_id = "claude-session-1"
        captured: list[list[str]] = []
        def fake_run(command, **kwargs):
            import json
            captured.append(command)
            payload = json.dumps({"session_id": "claude-session-1", "result": json.dumps({"action_type": "NO_OP", "target": None})})
            return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

        with patch("wolfkill.participants.subprocess.run", side_effect=fake_run):
            response = participant.decide(self.decision_request)

        self.assertEqual(response["action_type"], "NO_OP")
        self.assertIn("--resume", captured[0])
        self.assertIn("claude-session-1", captured[0])

    def test_verify_claude_cli_ready_requires_http_proxy_and_required_ip(self) -> None:
        participant = ClaudeCliParticipant(
            name="Claude Guard",
            model="claude-sonnet-4-6",
            env={"http_proxy": "http://127.0.0.1:7890"},
        )
        calls = 0

        def fake_run(command, **kwargs):
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(command, 0, stdout='154.28.2.59\n', stderr='')

        with patch('wolfkill.participants.shutil.which', return_value='/usr/bin/claude'), patch('wolfkill.participants.subprocess.run', side_effect=fake_run):
            verify_claude_cli_ready(participant)
        self.assertEqual(calls, 1)

    def test_kimi_cli_participant_reuses_same_session_id_across_calls(self) -> None:
        participant = KimiCliParticipant(name="Kimi P2", cwd="/tmp/wolfkill-kimi", timeout_seconds=9.0, model="moonshot-test", agent="default")
        captured: list[list[str]] = []

        def fake_run(command, **kwargs):
            captured.append(command)
            import json
            payload = json.dumps({"role": "assistant", "content": [{"type": "think", "think": "..."}, {"type": "text", "text": json.dumps({"text": "p1 says hello"}, ensure_ascii=False)}]}, ensure_ascii=False)
            return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

        with patch("wolfkill.participants.subprocess.run", side_effect=fake_run):
            first = participant.speak(self.speech_request)
            second = participant.speak(self.speech_request)

        self.assertEqual(first["text"], "p1 says hello")
        self.assertEqual(second["text"], "p1 says hello")
        self.assertTrue(participant.has_session)
        first_session = captured[0][captured[0].index("--session") + 1]
        second_session = captured[1][captured[1].index("--session") + 1]
        self.assertEqual(first_session, second_session)

    def test_kimi_prompt_includes_wolf_strategy_and_online_game_hint(self) -> None:
        participant = KimiCliParticipant(name="Kimi Strategy")
        request = {**self.speech_request, "phase": "DAY_SPEECH", "private_view": {**self.speech_request["private_view"], "role": "WOLF", "team": "WOLF"}}

        def fake_run(command, **kwargs):
            prompt = kwargs["input"]
            self.assertIn("悍跳", prompt)
            self.assertIn("狼踩狼", prompt)
            self.assertIn("倒钩", prompt)
            self.assertIn("在线游戏", prompt)
            return subprocess.CompletedProcess(command, 0, stdout='{"text":"ok"}', stderr="")

        with patch("wolfkill.participants.subprocess.run", side_effect=fake_run):
            response = participant.speak(request)

        self.assertEqual(response["text"], "ok")

    def test_kimi_stream_json_unwrap_extracts_text_payload(self) -> None:
        participant = KimiCliParticipant(name="Kimi Stream JSON")
        import json
        raw = json.dumps({"role": "assistant", "content": [{"type": "think", "think": "..."}, {"type": "text", "text": json.dumps({"text": "ok"}, ensure_ascii=False)}]}, ensure_ascii=False)
        self.assertEqual(participant._unwrap_stream_json(raw), '{"text": "ok"}')

    def test_kimi_cli_uses_default_model_from_config_file(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "kimi.toml"
            config_file.write_text('default_model = "kimi-code/kimi-for-coding"\n', encoding="utf-8")
            participant = KimiCliParticipant(name="Kimi Model", config_file=str(config_file))
            captured: list[list[str]] = []

            def fake_run(command, **kwargs):
                captured.append(command)
                return subprocess.CompletedProcess(command, 0, stdout='{"text":"ok"}', stderr="")

            with patch("wolfkill.participants.subprocess.run", side_effect=fake_run):
                response = participant.speak(self.speech_request)

            self.assertEqual(response["text"], "ok")
            self.assertIn("--model", captured[0])
            self.assertIn("kimi-code/kimi-for-coding", captured[0])

    def test_kimi_cli_defaults_to_home_config_file(self) -> None:
        participant = KimiCliParticipant(name="Kimi Home Config")
        if participant.config_file is None:
            self.skipTest("No ~/.kimi/config.toml in this environment")
        captured: list[list[str]] = []

        def fake_run(command, **kwargs):
            captured.append(command)
            return subprocess.CompletedProcess(command, 0, stdout='{"text":"ok"}', stderr="")

        with patch("wolfkill.participants.subprocess.run", side_effect=fake_run):
            response = participant.speak(self.speech_request)

        self.assertEqual(response["text"], "ok")
        self.assertIn("--config-file", captured[0])
        self.assertIn(participant.config_file, captured[0])

    def test_kimi_cli_reset_state_rotates_session_id(self) -> None:
        participant = KimiCliParticipant(name="Kimi Reset")
        old_session_id = participant.session_id
        participant.last_sent_event_id = 123
        participant.reset_state()
        self.assertNotEqual(participant.session_id, old_session_id)
        self.assertEqual(participant.last_sent_event_id, 0)

    def test_build_participants_supports_mvp_provider_types_without_default_timeout(self) -> None:
        participants = build_participants(preset=get_preset("classic-6"), seed=7, config={"_base_dir": "/tmp/wolfkill-config", "participants": {"p1": {"type": "codex_cli", "name": "Codex Seat"}, "p2": {"type": "kimi_cli", "name": "Kimi Seat", "share_dir": "./.kimi-test"}}}, human_seat=None, human_name="You")
        self.assertIsInstance(participants["p1"], CodexCliParticipant)
        self.assertIsInstance(participants["p2"], KimiCliParticipant)
        self.assertIsNone(participants["p1"].timeout_seconds)
        self.assertIsNone(participants["p2"].timeout_seconds)
        self.assertTrue(participants["p2"].env["KIMI_SHARE_DIR"].endswith(".kimi-test"))

    def test_gateway_includes_strategy_briefing_only_on_full_context(self) -> None:
        state = create_state_from_role_map("classic-6", 7, {"p1": Role.VILLAGER, "p2": Role.WOLF, "p3": Role.WOLF, "p4": Role.SEER, "p5": Role.WITCH, "p6": Role.VILLAGER})
        state.phase = Phase.DAY_SPEECH
        state.add_event(visibility=EventVisibility.PUBLIC, channel="system", text="开局公开信息")
        participant = KimiCliParticipant(name="Kimi Briefing")
        gateway = ParticipantGateway(
            {"p1": participant},
            VisibilityCompiler(),
            learn_history=["learn-a", "learn-b"],
            previous_games=["prev-a"],
        )

        first_request = gateway._build_base_request(state, "p1", "请发言")
        state.add_event(visibility=EventVisibility.PUBLIC, channel="speech", text="新的公开发言", speaker="p2")
        second_request = gateway._build_base_request(state, "p1", "请继续")

        self.assertIn("strategy_briefing", first_request)
        self.assertNotIn("strategy_briefing", second_request)
        self.assertEqual(first_request["strategy_briefing"]["learn_history"], ["learn-a", "learn-b"])
        self.assertEqual(first_request["strategy_briefing"]["previous_games"], ["prev-a"])

    def test_gateway_builds_incremental_request_for_session_participant(self) -> None:
        state = create_state_from_role_map("classic-6", 7, {"p1": Role.VILLAGER, "p2": Role.WOLF, "p3": Role.WOLF, "p4": Role.SEER, "p5": Role.WITCH, "p6": Role.VILLAGER})
        state.phase = Phase.DAY_SPEECH
        state.add_event(visibility=EventVisibility.PUBLIC, channel="system", text="开局公开信息")
        participant = KimiCliParticipant(name="Kimi Incremental")
        gateway = ParticipantGateway({"p1": participant}, VisibilityCompiler())

        first_request = gateway._build_base_request(state, "p1", "请发言")
        state.add_event(visibility=EventVisibility.PUBLIC, channel="speech", text="新的公开发言", speaker="p2")
        second_request = gateway._build_base_request(state, "p1", "请继续")

        self.assertEqual(first_request["context_mode"], "full")
        self.assertEqual(second_request["context_mode"], "incremental")
        self.assertIn("new_public_events", second_request["public_state"])
        self.assertEqual(len(second_request["public_state"]["new_public_events"]), 1)
        self.assertIn("new_visible_events", second_request["private_view"])
        self.assertEqual(len(second_request["private_view"]["new_visible_events"]), 1)

    def test_kimi_preflight_passes_config_file_to_cli(self) -> None:
        import tempfile
        from wolfkill.participants import verify_kimi_cli_ready
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / 'kimi.toml'
            config_file.write_text('default_model = "kimi-code/kimi-for-coding"\n', encoding='utf-8')
            participant = KimiCliParticipant(name='Kimi Config', config_file=str(config_file))
            captured = {}

            def fake_run(command, **kwargs):
                captured['command'] = command
                return subprocess.CompletedProcess(command, 0, stdout='{"role":"assistant","content":[{"type":"text","text":"ok"}]}', stderr='')

            with patch('wolfkill.participants.shutil.which', return_value='/usr/bin/kimi'), patch('wolfkill.participants.subprocess.run', side_effect=fake_run):
                verify_kimi_cli_ready(participant)

            self.assertIn('--config-file', captured['command'])
            self.assertIn(str(config_file), captured['command'])

    def test_kimi_cli_reuses_same_session_id_across_speak_and_decide(self) -> None:
        participant = KimiCliParticipant(name="Kimi Mixed", cwd="/tmp/wolfkill-kimi", model="moonshot-test")
        captured: list[list[str]] = []

        def fake_run(command, **kwargs):
            captured.append(command)
            if 'Vote now.' in str(kwargs['input']):
                return subprocess.CompletedProcess(command, 0, stdout='{"action_type":"NO_OP","target":null}', stderr="")
            return subprocess.CompletedProcess(command, 0, stdout='{"text":"ok"}', stderr="")

        with patch("wolfkill.participants.subprocess.run", side_effect=fake_run):
            speech = participant.speak(self.speech_request)
            decision = participant.decide(self.decision_request)

        self.assertEqual(speech["text"], "ok")
        self.assertEqual(decision["action_type"], "NO_OP")
        first_session = captured[0][captured[0].index("--session") + 1]
        second_session = captured[1][captured[1].index("--session") + 1]
        self.assertEqual(first_session, second_session)

    def test_kimi_cli_plaintext_error_is_marked_invalid_response(self) -> None:
        participant = KimiCliParticipant(name="Kimi Broken", model="moonshot-test")

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout='LLM not set', stderr='')

        with patch("wolfkill.participants.subprocess.run", side_effect=fake_run):
            with self.assertRaises(ParticipantInvocationError) as ctx:
                participant.speak(self.speech_request)

        self.assertEqual(ctx.exception.kind, "invalid_response")
        self.assertIn("不是 JSON", str(ctx.exception))

    def test_gateway_returns_full_context_again_after_kimi_reset(self) -> None:
        state = create_state_from_role_map("classic-6", 7, {"p1": Role.VILLAGER, "p2": Role.WOLF, "p3": Role.WOLF, "p4": Role.SEER, "p5": Role.WITCH, "p6": Role.VILLAGER})
        state.phase = Phase.DAY_SPEECH
        state.add_event(visibility=EventVisibility.PUBLIC, channel="system", text="开局公开信息")
        participant = KimiCliParticipant(name="Kimi Reset Full")
        gateway = ParticipantGateway({"p1": participant}, VisibilityCompiler())
        first_request = gateway._build_base_request(state, "p1", "请发言")
        state.add_event(visibility=EventVisibility.PUBLIC, channel="speech", text="新的公开发言", speaker="p2")
        incremental_request = gateway._build_base_request(state, "p1", "请继续")
        participant.reset_state()
        full_again_request = gateway._build_base_request(state, "p1", "重新开始")

        self.assertEqual(first_request["context_mode"], "full")
        self.assertEqual(incremental_request["context_mode"], "incremental")
        self.assertEqual(full_again_request["context_mode"], "full")
        self.assertIn("all_visible_events", full_again_request["private_view"])

    def test_mixed_agents_example_assigns_three_codex_two_claude_plus_human(self) -> None:
        config = load_config('examples/classic6-you-plus-5mixed-agents.json')
        participants = build_participants(
            preset=get_preset("classic-6"),
            seed=7,
            config=config,
            human_seat='p3',
            human_name='你',
        )
        codex_count = sum(isinstance(adapter, CodexCliParticipant) for adapter in participants.values())
        claude_count = sum(isinstance(adapter, ClaudeCliParticipant) for adapter in participants.values())
        human_count = sum(adapter.__class__.__name__ == 'HumanCliParticipant' for adapter in participants.values())
        mock_count = sum(adapter.__class__.__name__ == 'MockParticipant' for adapter in participants.values())
        self.assertEqual(codex_count, 3)
        self.assertEqual(claude_count, 2)
        self.assertEqual(human_count, 1)
        self.assertEqual(mock_count, 0)



if __name__ == "__main__":
    unittest.main()
