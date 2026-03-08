from __future__ import annotations

import json
import os
import re
import shutil
import readline
import subprocess
import sys
import tempfile
import time
import tomllib
from abc import ABC, abstractmethod
from pathlib import Path
from random import Random
from typing import Any
from uuid import uuid4

from .localization import (
    format_event_line,
    label_action,
    label_bool,
    label_phase,
    label_role,
    label_seat,
    label_seer_result,
    label_team,
    localize_request,
)
from .models import ActionType, Decision, concrete_choices

readline.parse_and_bind("set editing-mode emacs")
readline.parse_and_bind(r'"\C-u": unix-line-discard')
readline.parse_and_bind(r'"\C-k": kill-line')
readline.parse_and_bind(r'"\C-a": beginning-of-line')
readline.parse_and_bind(r'"\C-e": end-of-line')
readline.parse_and_bind(r'"\C-w": unix-word-rubout')


class ParticipantAdapter(ABC):
    def __init__(self, name: str, background: str | None = None) -> None:
        self.name = name
        self.background = background
        self.last_sent_event_id: int = 0
        self.last_call_diagnostics: dict[str, Any] | None = None

    @property
    def has_session(self) -> bool:
        return False

    @abstractmethod
    def speak(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def reset_state(self) -> None:
        self.last_sent_event_id = 0
        self.clear_last_call_diagnostics()

    def clear_last_call_diagnostics(self) -> None:
        self.last_call_diagnostics = None

    def set_last_call_diagnostics(self, **details: Any) -> None:
        self.last_call_diagnostics = details


class ParticipantInvocationError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "adapter_error", stdout: str | None = None, stderr: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.stdout = stdout
        self.stderr = stderr


def normalize_timeout_seconds(value: Any, *, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"", "none", "null", "off", "disable", "disabled"}:
            return None
    return float(value)


def format_timeout_message(label: str, timeout_seconds: float | None) -> str:
    if timeout_seconds is None:
        return f"{label} 调用超时"
    return f"{label} 调用超时（{timeout_seconds:.1f} 秒）"


class HumanCliParticipant(ParticipantAdapter):
    def __init__(self, name: str, background: str | None = None) -> None:
        super().__init__(name=name, background=background)
        self.last_seen_event_id = 0

    def remember_event(self, event_id: int) -> None:
        self.last_seen_event_id = max(self.last_seen_event_id, int(event_id))

    def reset_state(self) -> None:
        super().reset_state()
        self.last_seen_event_id = 0

    def _show_context(self, request: dict[str, Any]) -> None:
        private_view = request["private_view"]
        events = private_view.get("all_visible_events") or private_view.get("new_visible_events") or []
        new_events = [event for event in events if int(event["index"]) > self.last_seen_event_id]
        for event in new_events:
            print(format_event_line(index=int(event["index"]), day=int(event["day"]), phase=event.get("phase"), channel=event.get("channel"), text=str(event.get("text", "")), speaker=event.get("speaker")))
            self.remember_event(event["index"])
        phase_label = request.get("phase_label") or label_phase(request.get("phase"))
        print(f"\n【法官】当前轮到你处理：第{request['day']}天·{phase_label}。")
        print(f"座位：{label_seat(request['seat'])}  名称：{request['name']}")
        print(f"身份：{private_view.get('role_label') or label_role(private_view['role'])}  阵营：{private_view.get('team_label') or label_team(private_view['team'])}  存活：{private_view.get('alive_label') or label_bool(bool(private_view['alive']))}")
        if private_view.get("teammates"):
            print("狼人队友：" + ", ".join(label_seat(item["seat"]) for item in private_view["teammates"]))
        if private_view.get("seer_results"):
            results = ", ".join(f"第{item['day']}天 {label_seat(item['target'])}={item.get('result_label') or label_seer_result(item['result'])}" for item in private_view["seer_results"])
            print(f"查验结果：{results}")
        if private_view.get("witch_resources"):
            res = private_view["witch_resources"]
            print(f"女巫药剂：解药={'可用' if res['save_available'] else '已用'} 毒药={'可用' if res['poison_available'] else '已用'}")
        if private_view.get("night_hint"):
            wolf_target = private_view['night_hint']['wolf_target']
            print(f"夜间提示：狼人选择了 {label_seat(wolf_target)}")
            if private_view.get("role") == "WITCH" and wolf_target == request.get("seat"):
                print("【法官提示】今晚刀口是你自己；本规则女巫不能自救，所以没有解药选项。")
        if self.background:
            print(f"背景设定：{self.background}")
        print(f"【轮到你】{request['prompt']}")

    def speak(self, request: dict[str, Any]) -> dict[str, Any]:
        self._show_context(request)
        print("【法官】请直接输入你的发言内容。")
        text = input("发言> ").strip()
        return {"text": text or "我先不发言。"}

    def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        self._show_context(request)
        print("【法官】可选行动：")
        choices = concrete_choices([_dict_to_decision_spec(item) for item in request["options"]])
        for idx, choice in enumerate(choices, start=1):
            label = label_action(choice.action_type)
            if choice.target:
                label += f" -> {label_seat(choice.target)}"
            print(f"  {idx}. {label}")
        raw = input("行动> ").strip()
        try:
            choice = choices[int(raw) - 1]
        except Exception:
            choice = choices[0]
        return choice.to_dict()


class MockParticipant(ParticipantAdapter):
    def __init__(self, name: str, background: str | None = None, seed: int = 0) -> None:
        super().__init__(name=name, background=background)
        self.rng = Random(seed)

    def speak(self, request: dict[str, Any]) -> dict[str, Any]:
        private_view = request["private_view"]
        public_state = request["public_state"]
        audience = request["audience"]
        alive = public_state["alive_seats"]
        if audience == "WOLF":
            target = self._first_non_teammate(alive, private_view)
            return {"text": f"今晚优先击杀{label_seat(target)}。" if target else "今晚我没有明确目标。"}
        known_wolf = self._known_wolf(private_view, alive)
        if known_wolf:
            return {"text": f"我最怀疑{label_seat(known_wolf)}。"}
        if private_view["role"] == "WOLF":
            target = self._first_non_teammate(alive, private_view)
            if target:
                return {"text": f"我觉得{label_seat(target)}很可疑。"}
        suspects = [seat for seat in alive if seat != request["seat"]]
        return {"text": f"今天先关注{label_seat(suspects[0])}。" if suspects else "我先不发言。"}

    def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        private_view = request["private_view"]
        public_state = request["public_state"]
        seat = request["seat"]
        alive = public_state["alive_seats"]
        options = request["options"]
        option_types = {item["action_type"] for item in options}
        if "WOLF_KILL" in option_types:
            target = self._first_non_teammate(alive, private_view)
            if target and self._is_target_allowed(options, "WOLF_KILL", target):
                return {"action_type": "WOLF_KILL", "target": target}
        if "SEER_INSPECT" in option_types:
            inspected = {item["target"] for item in private_view.get("seer_results", [])}
            for candidate in alive:
                if candidate != seat and candidate not in inspected and self._is_target_allowed(options, "SEER_INSPECT", candidate):
                    return {"action_type": "SEER_INSPECT", "target": candidate}
        if "WITCH_SAVE" in option_types and request["day"] == 1:
            target = (private_view.get("night_hint") or {}).get("wolf_target")
            if target and self._is_target_allowed(options, "WITCH_SAVE", target):
                return {"action_type": "WITCH_SAVE", "target": target}
        if "WITCH_POISON" in option_types:
            known_wolf = self._known_wolf(private_view, alive)
            if known_wolf and self._is_target_allowed(options, "WITCH_POISON", known_wolf):
                return {"action_type": "WITCH_POISON", "target": known_wolf}
        if "DAY_VOTE" in option_types:
            known_wolf = self._known_wolf(private_view, alive)
            if known_wolf and self._is_target_allowed(options, "DAY_VOTE", known_wolf):
                return {"action_type": "DAY_VOTE", "target": known_wolf}
            if private_view["role"] == "WOLF":
                target = self._first_non_teammate(alive, private_view)
                if target and self._is_target_allowed(options, "DAY_VOTE", target):
                    return {"action_type": "DAY_VOTE", "target": target}
            for candidate in alive:
                if candidate != seat and self._is_target_allowed(options, "DAY_VOTE", candidate):
                    return {"action_type": "DAY_VOTE", "target": candidate}
        return self._first_concrete_option(options)

    def _known_wolf(self, private_view: dict[str, Any], alive: list[str]) -> str | None:
        for item in private_view.get("seer_results", []):
            if item["result"] == "WOLF" and item["target"] in alive:
                return item["target"]
        return None

    def _first_non_teammate(self, alive: list[str], private_view: dict[str, Any]) -> str | None:
        teammates = {private_view["seat"]}
        teammates.update(item["seat"] for item in private_view.get("teammates", []))
        for seat in alive:
            if seat not in teammates:
                return seat
        return None

    def _is_target_allowed(self, options: list[dict[str, Any]], action_type: str, target: str) -> bool:
        return any(item["action_type"] == action_type and target in item["targets"] for item in options)

    def _first_concrete_option(self, options: list[dict[str, Any]]) -> dict[str, Any]:
        choices = concrete_choices([_dict_to_decision_spec(item) for item in options])
        choices.sort(key=lambda choice: (choice.action_type == ActionType.NO_OP, choice.action_type.value, choice.target or ""))
        return choices[0].to_dict()


class PromptJsonParticipant(ParticipantAdapter, ABC):
    provider_label = "external_cli"

    def __init__(self, name: str, *, background: str | None = None, cwd: str | None = None, timeout_seconds: float | None = None, extra_args: list[str] | None = None, env: dict[str, str] | None = None) -> None:
        super().__init__(name=name, background=background)
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.extra_args = list(extra_args or [])
        self.env = dict(env or {})
        self._last_run_prompt_meta: dict[str, Any] = {}

    def _set_run_prompt_meta(self, **details: Any) -> None:
        self._last_run_prompt_meta = details

    def _consume_run_prompt_meta(self) -> dict[str, Any]:
        details = dict(self._last_run_prompt_meta)
        self._last_run_prompt_meta = {}
        return details

    def speak(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._invoke("speech", request)

    def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._invoke("decision", request)

    def _invoke(self, mode: str, request: dict[str, Any]) -> dict[str, Any]:
        started_at = time.monotonic()
        prompt_started_at = time.monotonic()
        prompt = build_agent_prompt(self.provider_label, mode, request)
        prompt_build_seconds = time.monotonic() - prompt_started_at
        try:
            provider_started_at = time.monotonic()
            raw_output = self._run_prompt(mode, prompt)
            provider_seconds = time.monotonic() - provider_started_at
        except subprocess.TimeoutExpired as exc:
            raise ParticipantInvocationError(format_timeout_message(self.provider_label, self.timeout_seconds), kind="timeout") from exc
        if not raw_output.strip():
            raise ParticipantInvocationError(f"{self.provider_label} 返回空输出", kind="invalid_response")
        try:
            parse_started_at = time.monotonic()
            parse_mode = "json"
            try:
                response = parse_json_response(raw_output)
            except ValueError:
                response = coerce_non_json_response(raw_output, mode=mode, request=request)
                if response is None:
                    raise
                parse_mode = "text_fallback"
            parse_seconds = time.monotonic() - parse_started_at
            run_prompt_meta = self._consume_run_prompt_meta()
            io_wait_seconds = float(run_prompt_meta.pop("io_wait_seconds", provider_seconds))
            self.set_last_call_diagnostics(
                provider=self.provider_label,
                mode=mode,
                context_mode=str(request.get("context_mode") or "full"),
                prompt_chars=len(prompt),
                response_chars=len(raw_output),
                prompt_build_seconds=prompt_build_seconds,
                provider_seconds=provider_seconds,
                io_wait_seconds=io_wait_seconds,
                parse_seconds=parse_seconds,
                parse_mode=parse_mode,
                total_seconds=time.monotonic() - started_at,
                **run_prompt_meta,
            )
            return response
        except ValueError as exc:
            raise ParticipantInvocationError(f"{self.provider_label} 返回的不是 JSON：{raw_output.strip()[:280]}", kind="invalid_response") from exc

    @abstractmethod
    def _run_prompt(self, mode: str, prompt: str) -> str:
        raise NotImplementedError

    def _raise_process_error(self, completed: subprocess.CompletedProcess[str]) -> None:
        raise _process_error(self.provider_label, completed)


class CodexCliParticipant(PromptJsonParticipant):
    provider_label = "codex_cli"

    def __init__(self, name: str, *, background: str | None = None, cwd: str | None = None, timeout_seconds: float | None = None, model: str | None = None, profile: str | None = None, config_overrides: list[str] | None = None, executable: str = "codex", sandbox: str = "read-only", extra_args: list[str] | None = None, env: dict[str, str] | None = None) -> None:
        super().__init__(name=name, background=background, cwd=cwd, timeout_seconds=timeout_seconds, extra_args=extra_args, env=env)
        self.model = model
        self.profile = profile
        self.config_overrides = list(config_overrides or [])
        self.executable = executable
        self.sandbox = sandbox
        self.thread_id: str | None = None

    @property
    def has_session(self) -> bool:
        return self.thread_id is not None

    def reset_state(self) -> None:
        super().reset_state()
        self.thread_id = None

    def _run_prompt(self, mode: str, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="wolfkill-codex-") as temp_dir:
            output_path = Path(temp_dir) / f"{mode}-response.json"
            schema_path = Path(temp_dir) / f"{mode}-schema.json"
            schema_path.write_text(json.dumps(response_schema(mode), ensure_ascii=False), encoding="utf-8")
            command = [self.executable, "exec", "resume", self.thread_id] if self.thread_id else [self.executable, "exec"]
            if self.model:
                command.extend(["-m", self.model])
            if self.profile:
                command.extend(["-p", self.profile])
            if not self.thread_id and self.cwd:
                command.extend(["-C", self.cwd])
            command.extend(["--skip-git-repo-check", "--color", "never", "--sandbox", self.sandbox])
            for override in self.config_overrides:
                command.extend(["-c", override])
            if not self.thread_id:
                command.extend(["--output-schema", str(schema_path)])
            command.extend(["--json", "-o", str(output_path)])
            command.extend(self.extra_args)
            command.append("-")
            io_wait_started_at = time.monotonic()
            completed = subprocess.run(command, input=prompt, capture_output=True, text=True, cwd=self.cwd, timeout=self.timeout_seconds, env=build_process_env(self.env), check=False)
            io_wait_seconds = time.monotonic() - io_wait_started_at
            self._set_run_prompt_meta(io_wait_seconds=io_wait_seconds)
            self._extract_thread_id(completed.stdout)
            output_text = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
            if completed.returncode != 0 and not output_text.strip():
                if self.thread_id:
                    self.thread_id = None
                    return self._run_prompt(mode, prompt)
                self._raise_process_error(completed)
            if not output_text.strip() and completed.stderr.strip() and not completed.stdout.strip():
                if self.thread_id:
                    self.thread_id = None
                    return self._run_prompt(mode, prompt)
                self._raise_process_error(completed)
            return output_text or self._extract_agent_message(completed.stdout)

    def _extract_thread_id(self, jsonl_output: str) -> None:
        for line in jsonl_output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "thread.started":
                tid = obj.get("thread_id")
                if tid and isinstance(tid, str):
                    self.thread_id = tid
                    return

    def _extract_agent_message(self, jsonl_output: str) -> str:
        for line in reversed(jsonl_output.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "item.completed":
                item = obj.get("item", {})
                if item.get("type") == "agent_message":
                    return item.get("text", "")
        return jsonl_output


CLAUDE_REQUIRED_IP = "154.28.2.59"
_CLAUDE_PREFLIGHT_CACHE: set[tuple[str, str | None, str | None, tuple[tuple[str, str], ...]]] = set()


def _effective_http_proxy(env: dict[str, str]) -> str | None:
    return env.get("http_proxy") or env.get("HTTP_PROXY")


def _curl_ipinfo(env: dict[str, str], *, timeout_seconds: float = 15.0) -> str:
    completed = subprocess.run(
        ["curl", "-s", "--max-time", "10", "https://ipinfo.io/ip"],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=build_process_env(env),
        check=False,
    )
    return completed.stdout.strip()


def verify_claude_cli_ready(adapter: "ClaudeCliParticipant") -> None:
    if shutil.which(adapter.executable) is None:
        raise RuntimeError(f"claude_cli 不可用：找不到可执行文件 {adapter.executable!r}")
    effective_env = dict(adapter.env)
    http_proxy = _effective_http_proxy(effective_env)
    if not http_proxy:
        raise RuntimeError("claude_cli 启动前检查失败：当前启动环境未启用 http_proxy。")
    cache_key = (
        adapter.executable,
        adapter.model,
        adapter.effort,
        tuple(sorted((str(k), str(v)) for k, v in effective_env.items())),
    )
    if cache_key in _CLAUDE_PREFLIGHT_CACHE:
        return
    current_ip = _curl_ipinfo(effective_env)
    if current_ip != CLAUDE_REQUIRED_IP:
        raise RuntimeError(
            f"claude_cli 启动前检查失败：当前出口 IP 为 {current_ip}，必须为 {CLAUDE_REQUIRED_IP}。"
        )
    command = [
        adapter.executable,
        '-p',
        '--output-format', 'json',
        '--input-format', 'text',
        '--model', adapter.model,
        '--effort', adapter.effort,
        '--session-id', str(uuid4()),
        '--no-session-persistence',
        '--json-schema', json.dumps(response_schema('speech'), ensure_ascii=False),
    ]
    try:
        completed = subprocess.run(
            command,
            input='请只返回 {"text":"ok"}',
            capture_output=True,
            text=True,
            timeout=30,
            cwd=adapter.cwd,
            env=build_process_env(effective_env),
            check=False,
        )
    except Exception as exc:
        raise RuntimeError(f"claude_cli 预检失败：{exc}") from exc
    detail = (completed.stderr or completed.stdout).strip()
    lowered = detail.lower()
    if '401' in lowered or 'invalid authentication' in lowered or 'unauthorized' in lowered:
        raise RuntimeError('claude_cli 鉴权失败。请确认当前代理/IP 正确且 Claude 账号凭证有效。')
    if completed.returncode != 0 and not completed.stdout.strip():
        raise RuntimeError(f"claude_cli 预检失败：{detail[:280]}")
    _CLAUDE_PREFLIGHT_CACHE.add(cache_key)


def _default_kimi_config_file() -> str | None:
    path = Path.home() / '.kimi' / 'config.toml'
    return str(path) if path.is_file() else None


_KIMI_PREFLIGHT_CACHE: set[tuple[str, str | None, str | None, tuple[tuple[str, str], ...]]] = set()


def _kimi_default_model(config_file: str | None = None) -> str | None:
    candidates = []
    if config_file:
        candidates.append(Path(config_file).expanduser())
    candidates.append(Path.home() / '.kimi' / 'config.toml')
    for path in candidates:
        try:
            with path.open('rb') as handle:
                data = tomllib.load(handle)
        except Exception:
            continue
        value = data.get('default_model')
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def verify_kimi_cli_ready(adapter: "KimiCliParticipant") -> None:
    if shutil.which(adapter.executable) is None:
        raise RuntimeError(f"kimi_cli 不可用：找不到可执行文件 {adapter.executable!r}")
    if not adapter.model:
        raise RuntimeError(
            "kimi_cli 未配置 model。请在 examples 配置中显式设置 model，"
            "或在 ~/.kimi/config.toml 中设置 default_model。"
        )
    cache_key = (
        adapter.executable,
        adapter.model,
        adapter.config_file,
        tuple(sorted((str(k), str(v)) for k, v in adapter.env.items())),
    )
    if cache_key in _KIMI_PREFLIGHT_CACHE:
        return
    command = [
        adapter.executable,
        "--session", f"wolfkill-preflight-{uuid4().hex}",
        "--print",
        "--input-format", "text",
        "--output-format", "stream-json",
        "--model", adapter.model,
    ]
    if adapter.config_file:
        command.extend(["--config-file", adapter.config_file])
    if adapter.agent:
        command.extend(["--agent", adapter.agent])
    try:
        completed = subprocess.run(
            command,
            input='请只返回 {"ok":true}',
            capture_output=True,
            text=True,
            timeout=20,
            cwd=adapter.cwd,
            env=build_process_env(adapter.env),
            check=False,
        )
    except Exception as exc:
        raise RuntimeError(f"kimi_cli 预检失败：{exc}") from exc
    detail = (completed.stderr or completed.stdout).strip()
    lowered = detail.lower()
    if '401' in lowered or 'invalid authentication' in lowered or 'invalid_authentication_error' in lowered:
        raise RuntimeError('kimi_cli 鉴权失败（401 Invalid Authentication）。请先执行 `kimi login`，或检查 ~/.kimi 下的凭证是否有效。')
    if completed.returncode != 0 and not completed.stdout.strip():
        raise RuntimeError(f"kimi_cli 预检失败：{detail[:280]}")
    _KIMI_PREFLIGHT_CACHE.add(cache_key)


class ClaudeCliParticipant(PromptJsonParticipant):
    provider_label = "claude_cli"

    def __init__(
        self,
        name: str,
        *,
        background: str | None = None,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
        model: str | None = None,
        effort: str = "medium",
        executable: str = "claude",
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(name=name, background=background, cwd=cwd, timeout_seconds=timeout_seconds, extra_args=extra_args, env=env)
        self.model = model or "claude-sonnet-4-6"
        self.effort = effort or "medium"
        self.executable = executable
        self.session_id: str | None = None

    @property
    def has_session(self) -> bool:
        return self.session_id is not None

    def reset_state(self) -> None:
        super().reset_state()
        self.session_id = None

    def _run_prompt(self, mode: str, prompt: str) -> str:
        command = [
            self.executable,
            "-p",
            "--output-format", "json",
            "--input-format", "text",
            "--model", self.model,
            "--effort", self.effort,
            "--json-schema", json.dumps(response_schema(mode), ensure_ascii=False),
        ]
        if self.session_id:
            command.extend(["--resume", self.session_id])
        command.extend(self.extra_args)
        io_wait_started_at = time.monotonic()
        completed = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=self.cwd,
            timeout=self.timeout_seconds,
            env=build_process_env(self.env),
            check=False,
        )
        io_wait_seconds = time.monotonic() - io_wait_started_at
        self._set_run_prompt_meta(io_wait_seconds=io_wait_seconds)
        if completed.returncode != 0 and not completed.stdout.strip():
            self._raise_process_error(completed)
        if not completed.stdout.strip() and completed.stderr.strip():
            self._raise_process_error(completed)
        return self._unwrap_json_envelope(completed.stdout)

    def _unwrap_json_envelope(self, raw: str) -> str:
        stripped = raw.strip()
        if not stripped:
            return raw
        try:
            envelope = json.loads(stripped)
        except json.JSONDecodeError:
            return raw
        if not isinstance(envelope, dict):
            return raw
        session_id = envelope.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        result = envelope.get("result")
        if isinstance(result, str):
            if result.strip():
                return result
        elif result is not None:
            return json.dumps(result, ensure_ascii=False)
        structured_output = envelope.get("structured_output")
        if isinstance(structured_output, dict):
            text_field = structured_output.get("text")
            if isinstance(text_field, str) and text_field.strip():
                return text_field
            if structured_output:
                return json.dumps(structured_output, ensure_ascii=False)
        return raw


class KimiCliParticipant(PromptJsonParticipant):
    provider_label = "kimi_cli"

    def __init__(self, name: str, *, background: str | None = None, cwd: str | None = None, timeout_seconds: float | None = None, model: str | None = None, agent: str | None = None, config_file: str | None = None, executable: str = "kimi", extra_args: list[str] | None = None, env: dict[str, str] | None = None) -> None:
        super().__init__(name=name, background=background, cwd=cwd, timeout_seconds=timeout_seconds, extra_args=extra_args, env=env)
        resolved_config_file = config_file or _default_kimi_config_file()
        self.model = model or _kimi_default_model(resolved_config_file)
        self.agent = agent
        self.config_file = resolved_config_file
        self.executable = executable
        self.session_id = self._new_session_id()

    @property
    def has_session(self) -> bool:
        return self.session_id is not None

    def reset_state(self) -> None:
        super().reset_state()
        self.session_id = self._new_session_id()

    def _new_session_id(self) -> str:
        return f"wolfkill-kimi-{uuid4().hex}"

    def _run_prompt(self, mode: str, prompt: str) -> str:
        command = [
            self.executable,
            "--session", self.session_id,
            "--print",
            "--input-format", "text",
            "--output-format", "stream-json",
        ]
        if self.cwd:
            command.extend(["--work-dir", self.cwd])
        if self.model:
            command.extend(["--model", self.model])
        if self.agent:
            command.extend(["--agent", self.agent])
        if self.config_file:
            command.extend(["--config-file", self.config_file])
        command.extend(self.extra_args)
        io_wait_started_at = time.monotonic()
        completed = subprocess.run(command, input=prompt, capture_output=True, text=True, cwd=self.cwd, timeout=self.timeout_seconds, env=build_process_env(self.env), check=False)
        io_wait_seconds = time.monotonic() - io_wait_started_at
        self._set_run_prompt_meta(io_wait_seconds=io_wait_seconds)
        if completed.returncode != 0 and not completed.stdout.strip():
            self._raise_process_error(completed)
        if not completed.stdout.strip() and completed.stderr.strip():
            self._raise_process_error(completed)
        return self._unwrap_stream_json(completed.stdout)


    def _unwrap_stream_json(self, raw: str) -> str:
        stripped = raw.strip()
        if not stripped:
            return raw
        try:
            envelope = json.loads(stripped)
        except json.JSONDecodeError:
            return raw
        if not isinstance(envelope, dict):
            return raw
        if envelope.get("role") != "assistant":
            return raw
        content = envelope.get("content")
        if not isinstance(content, list):
            return raw
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        return "\n".join(text_parts) if text_parts else raw


def _dict_to_decision_spec(item: dict[str, Any]):
    from .models import ActionSpec
    return ActionSpec(action_type=ActionType(item["action_type"]), targets=tuple(item.get("targets", [])), requires_target=bool(item.get("requires_target", False)), description=item.get("description", ""))


def resolve_subprocess_cwd(path: str | None, *, base_dir: str | None = None) -> str | None:
    if path is None:
        return None
    resolved = Path(path).expanduser()
    if not resolved.is_absolute() and base_dir is not None:
        resolved = Path(base_dir) / resolved
    return str(resolved.resolve())


def build_agent_prompt(provider_label: str, mode: str, request: dict[str, Any]) -> str:
    localized_request = localize_request(request)
    context_mode = request.get("context_mode", "full")
    if context_mode == "incremental":
        response_hint = '只返回JSON：{"text":"..."}' if mode == "speech" else '只返回JSON：{"action_type":"...", "target":"..."}'
        return "\n".join([
            f"【第{localized_request.get('day', '?')}天·{localized_request.get('phase_label', '')}】请根据以下新事件和你记忆中的历史信息进行推理。{response_hint}",
            "这是在线游戏，其他玩家正在等待你；请尽快完成思考并直接返回结果。",
            json.dumps({"mode": mode, "request": localized_request}, ensure_ascii=False, indent=2),
        ])
    response_format = '{"text": "你的中文发言内容"}' if mode == "speech" else '{"action_type": "从 request.options 中选择的合法动作代码", "target": "seat 或 null"}'
    gameplay_lines = _gameplay_instructions(mode, request)
    truncation_notice = _context_truncation_notice(localized_request)
    return "\n".join([
        f"你是通过 {provider_label} 运行的狼人杀参与者，你是一名经验丰富的高水平玩家。",
        "这是在线游戏，其他玩家正在等待你；请尽快完成思考并给出结果，但不要牺牲基本推理质量。",
        "只能依据下面的 JSON 请求行动，绝不能假设任何隐藏信息。",
        "不要调用工具、不要查看文件、不要联网、不要追问问题。",
        "你必须只返回一个 JSON 对象，不要输出 markdown 代码块。",
        "历史消息都带有 index 与 message_label 序号；引用上下文时优先使用这些序号。",
        f"返回格式示例：{response_format}",
        "如果是 decision 模式，action_type 必须从 request.options 中原样选择。",
        "如果是 speech 模式，text 是你的中文发言，不超过 600 个字符。",
        *( ["如果 request.strategy_briefing 存在，你必须先吸收其中的赛前策略知识，再结合当前可见事实推理；策略知识不能替代当前局面，也不能让你假设隐藏信息。"] if localized_request.get("strategy_briefing") else [] ),
        *([truncation_notice] if truncation_notice else []),
        "",
        *gameplay_lines,
        "",
        json.dumps({"mode": mode, "request": localized_request}, ensure_ascii=False, indent=2),
    ])


def _context_truncation_notice(request: dict[str, Any]) -> str:
    public_state = request.get("public_state") or {}
    private_view = request.get("private_view") or {}
    omitted_public = int(public_state.get("omitted_public_event_count") or 0)
    omitted_visible = int(private_view.get("omitted_visible_event_count") or 0)
    if omitted_public <= 0 and omitted_visible <= 0:
        return ""
    omitted = max(omitted_public, omitted_visible)
    return f"注意：为控制上下文长度，request 中只保留最近窗口事件；更早的历史约有 {omitted} 条未展开。请结合当前结构化状态、最近事件与已知规则继续推理。"


def _gameplay_instructions(mode: str, request: dict[str, Any]) -> list[str]:
    phase = str(request.get("phase") or "")
    pv = request.get("private_view") or {}
    role = str(pv.get("role") or "") if isinstance(pv, dict) else ""
    lines: list[str] = []
    lines.append("=== 核心行为准则 ===")
    lines.append("你是一名经验丰富的狼人杀竞技玩家。你必须仔细阅读 private_view 中你的身份信息和所有最近历史事件，结合 public_state 中的存活情况进行推理。")
    lines.append("【读人方法】听逻辑链是否自洽；看发言中有效信息量；抓视角漏洞；观投票行为是否与发言一致；分析发言动机对谁有利。")
    if role == "WOLF":
        lines.append("【狼人铁律】你的一切发言必须伪装成好人视角。绝不能暴露刀口目标、队友身份等私有信息。")
    else:
        lines.append("【好人铁律】你的目标是通过逻辑推理找出狼人。集中票数是好人最大的武器，分散投票和弃票都会帮狼人。")

    if mode == "speech":
        lines.append("")
        if phase == "WOLF_CHAT":
            lines.append("=== 狼人密谈指导 ===")
            lines.extend([
                "与队友讨论以下要点，直接给出具体建议：",
                "1. 谁最可能是预言家/女巫？分析每个玩家的发言和行为。",
                "2. 今晚刀谁？优先级：已跳预言家 > 疑似女巫 > 强逻辑好人。",
                "3. 白天谁来悍跳/对跳？谁深水？谁冲锋站边？如何配合？",
                "4. 可以使用的狼人技术：悍跳、垫飞、狼踩狼、倒钩、深水、冲锋。",
                "5. 禁止空话，必须给出明确的刀口建议和理由。",
                "6. 如果你已经没有新的补充，请直接回复：无更多讨论。法官会在全员都这么回复后进入行动确认。",
            ])
        elif phase == "DAY_SPEECH":
            lines.append("=== 白天发言指导 ===")
            lines.extend([
                "高水平发言必须满足：有逻辑、有观点、有互动、能抓破绽。",
                "1. 基于发言内容、投票记录、死亡顺序、查验结果进行推理，不凭感觉。",
                "2. 明确表达你怀疑谁、信任谁、推荐今天投谁出局，并给出具体理由。",
                "3. 直接回应前面玩家的发言，最好引用消息编号（message_label）佐证。",
                "4. 重点关注：是否有人暴露狼人视角、是否有人前后矛盾、是否有人回避关键问题。",
            ])
            if role == "WOLF":
                lines.extend([
                    "5.【狼人伪装】像一个积极找狼的好人一样发言。可用战术：",
                    "  - 悍跳：假冒预言家对跳，制造二选一。",
                    "  - 垫飞：故意做低真预言家或给队友做身份。",
                    "  - 狼踩狼：公开质疑队友，制造不可能同阵营的假象。",
                    "  - 倒钩：查杀或踩队友，博取信任。",
                    "  - 深水：低调但不空洞，减少被关注。",
                    "  - 冲锋：强站边悍跳队友，攻击真预言家漏洞。",
                ])
            else:
                lines.append("5.【好人找狼】分析谁在划水、谁逻辑链断裂、谁排除了不该排除的人、谁投票和发言不一致。")
            lines.extend([
                "【绝对禁止的发言】不要只说“我先观望/暂时没有看法/大家怎么看”这类空话。",
                "你的每一句发言都应该包含信息量和明确立场。",
            ])
        else:
            lines.append("请根据当前阶段进行有针对性的发言，言之有物。")
    else:
        lines.append("")
        lines.append("=== 决策指导 ===")
        if phase == "WOLF_ACTION":
            lines.extend([
                "击杀目标优先级：已确认/高度疑似预言家 > 疑似女巫 > 强逻辑好人。",
                "结合白天发言、神职暴露情况做决定。",
            ])
        elif phase == "SEER_ACTION":
            lines.extend([
                "查验目标优先级：发言最可疑、争议最大、对跳位。",
                "不要浪费查验在已高度确认身份的人身上。",
            ])
        elif phase == "WITCH_ACTION":
            lines.extend([
                "用药要慎重：不能自救。被刀者若是关键神职且你有把握，可果断救。",
                "毒药只在高度确信某人是狼人时使用。毒错好人代价极大。",
            ])
        elif phase == "DAY_VOTE":
            lines.extend([
                "投票必须回顾所有发言再投。",
                "绝不要弃票；弃票等于帮助狼人。",
                "集中票数，和场上最有逻辑的方向保持一致。",
                "如果你是狼人：投票要配合伪装，可以推无关紧要的好人或踩队友做身份。",
            ])
        else:
            lines.append("请严格从 request.options 中选择最合理的合法动作。")
    return lines


def response_schema(mode: str) -> dict[str, Any]:
    if mode == "speech":
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action_type": {"type": "string"},
            "target": {"type": ["string", "null"]},
        },
        "required": ["action_type", "target"],
    }


def parse_json_response(raw_text: str) -> dict[str, Any]:
    candidates = [candidate for candidate in _json_candidates(raw_text) if candidate]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"无法从输出中解析 JSON 对象：{raw_text[:280]}")


def coerce_non_json_response(raw_text: str, *, mode: str, request: dict[str, Any]) -> dict[str, Any] | None:
    text = _normalize_freeform_response(raw_text)
    if not text or _looks_like_provider_error_text(text):
        return None
    if mode == "speech":
        return {"text": text[:600]}
    return _coerce_decision_response(text, request)


def _json_candidates(raw_text: str) -> list[str]:
    stripped = raw_text.strip()
    if not stripped:
        return []
    candidates = [stripped]
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL):
        fenced = match.group(1).strip()
        if fenced:
            candidates.append(fenced)
    candidates.extend(line.strip() for line in reversed(stripped.splitlines()) if line.strip())
    start = stripped.find("{")
    end = stripped.rfind("}")
    if 0 <= start < end:
        candidates.append(stripped[start:end + 1].strip())
    return candidates


def _normalize_freeform_response(raw_text: str) -> str:
    stripped = raw_text.strip()
    if not stripped:
        return ""
    fenced_matches = list(re.finditer(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL))
    if len(fenced_matches) == 1:
        fenced = fenced_matches[0].group(1).strip()
        if fenced:
            stripped = fenced
    text_wrapper = _unwrap_text_field_wrapper(stripped)
    return text_wrapper if text_wrapper else stripped


def _looks_like_provider_error_text(text: str) -> bool:
    lowered = text.lower().strip()
    error_markers = (
        "error code:",
        "invalid authentication",
        "invalid_authentication_error",
        "llm not set",
        "traceback",
        "exception:",
        "rate limit",
        "too many requests",
        "quota",
        "timed out",
        "timeout",
        "unauthorized",
        "forbidden",
        "connection refused",
        "network error",
        "api key",
    )
    return any(marker in lowered for marker in error_markers)


def _unwrap_text_field_wrapper(text: str) -> str | None:
    match = re.match(r'^\{\s*"text"\s*:\s*"(?P<body>.*)"\s*\}\s*$', text, flags=re.DOTALL)
    if not match:
        return None
    body = match.group('body').strip()
    if not body:
        return ""
    body = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), body)
    body = body.replace('\\n', '\n')
    body = body.replace('\\r', '\r')
    body = body.replace('\\t', '\t')
    body = body.replace('\\"', '"')
    return body.replace('\\\\', '\\')

_SEAT_CAPTURE_PATTERN = r"(?:\[(?P<bracket>\d+)\]|p(?P<prefix>\d+)|(?P<plain>\d+)号位)"


def _coerce_decision_response(text: str, request: dict[str, Any]) -> dict[str, Any] | None:
    options = request.get("options")
    if not isinstance(options, list) or not options:
        return None
    explicit = _extract_explicit_action_and_target(text)
    if explicit is not None:
        action_type, target = explicit
        if _decision_matches_options(options, action_type, target):
            return {"action_type": action_type, "target": target}

    no_op_option = next((item for item in options if item.get("action_type") == ActionType.NO_OP.value), None)
    if no_op_option and _mentions_no_op(text):
        return {"action_type": ActionType.NO_OP.value, "target": None}

    for action_type in _infer_action_types(text, options):
        target = _extract_target_for_action(text, action_type)
        if target is None:
            target = _infer_target_from_request_context(action_type, request, options)
        if _decision_matches_options(options, action_type, target):
            return {"action_type": action_type, "target": target}

    unique_seats = _extract_unique_seats(text)
    if len(unique_seats) == 1:
        seat = unique_seats[0]
        target_matches = [item for item in options if item.get("requires_target") and seat in item.get("targets", [])]
        if len(target_matches) == 1:
            return {"action_type": str(target_matches[0]["action_type"]), "target": seat}
    return None


def _extract_explicit_action_and_target(text: str) -> tuple[str, str | None] | None:
    action_match = re.search(r"\b(WOLF_KILL|SEER_INSPECT|WITCH_SAVE|WITCH_POISON|DAY_VOTE|NO_OP)\b", text)
    if not action_match:
        return None
    action_type = action_match.group(1)
    target_match = re.search(r"\btarget\b\s*[:=]\s*(null|\"?p\d+\"?)", text, flags=re.IGNORECASE)
    if target_match:
        raw_target = target_match.group(1).strip('"').lower()
        return action_type, None if raw_target == "null" else raw_target
    return action_type, _extract_target_for_action(text, action_type)


def _infer_action_types(text: str, options: list[dict[str, Any]]) -> list[str]:
    lowered = text.lower()
    action_types = {str(item.get("action_type")) for item in options}
    ordered: list[str] = []
    keyword_map = [
        (ActionType.WITCH_SAVE.value, ("开药救", "用解药", "解药", "救人", "救", "save", "heal")),
        (ActionType.WITCH_POISON.value, ("下毒", "毒药", "毒杀", "毒", "poison")),
        (ActionType.DAY_VOTE.value, ("票投向", "投票给", "投给", "今天投", "票给", "主推", "vote", "push")),
        (ActionType.WOLF_KILL.value, ("刀口", "击杀", "今晚刀", "主刀", "刀", "kill")),
        (ActionType.SEER_INSPECT.value, ("查验", "验人", "验", "查", "inspect")),
        (ActionType.NO_OP.value, ("不操作", "不使用", "不用", "跳过", "skip", "no op", "no-op", "弃票", "空刀")),
    ]
    for action_type, keywords in keyword_map:
        if action_type in action_types and any(keyword in lowered for keyword in keywords):
            ordered.append(action_type)
    if not ordered:
        non_noop = [str(item.get("action_type")) for item in options if item.get("action_type") != ActionType.NO_OP.value]
        if len(set(non_noop)) == 1:
            ordered.append(non_noop[0])
    return ordered


def _extract_target_for_action(text: str, action_type: str) -> str | None:
    action_patterns: dict[str, tuple[str, ...]] = {
        ActionType.WITCH_SAVE.value: (
            rf"(?:开药救|用解药(?:救)?|解药(?:救)?|save|heal|救(?:人)?)\s*(?:->|给|向|to|for)?\s*{_SEAT_CAPTURE_PATTERN}",
        ),
        ActionType.WITCH_POISON.value: (
            rf"(?:下毒|用毒(?:药)?|毒杀|毒(?:死)?|poison)\s*(?:->|给|向|to|for)?\s*{_SEAT_CAPTURE_PATTERN}",
        ),
        ActionType.DAY_VOTE.value: (
            rf"(?:票投向|投票给|投给|今天投|票给|主推|vote(?:\s+for)?|push)\s*(?:->|给|向|to|for)?\s*{_SEAT_CAPTURE_PATTERN}",
        ),
        ActionType.WOLF_KILL.value: (
            rf"(?:今晚刀|主刀|刀口(?:定)?(?:在)?|击杀|刀|kill)\s*(?:->|给|向|to|for)?\s*{_SEAT_CAPTURE_PATTERN}",
        ),
        ActionType.SEER_INSPECT.value: (
            rf"(?:查验|验人|验|查|inspect)\s*(?:->|给|向|to|for)?\s*{_SEAT_CAPTURE_PATTERN}",
        ),
    }
    for pattern in action_patterns.get(action_type, ()):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _seat_from_match(match)
    return None


def _infer_target_from_request_context(action_type: str, request: dict[str, Any], options: list[dict[str, Any]]) -> str | None:
    if action_type != ActionType.WITCH_SAVE.value:
        return None
    private_view = request.get("private_view") if isinstance(request.get("private_view"), dict) else {}
    night_hint = private_view.get("night_hint") if isinstance(private_view, dict) else None
    wolf_target = night_hint.get("wolf_target") if isinstance(night_hint, dict) else None
    if not isinstance(wolf_target, str):
        return None
    if _decision_matches_options(options, action_type, wolf_target):
        return wolf_target
    return None


def _extract_unique_seats(text: str) -> list[str]:
    seats: list[str] = []
    for match in re.finditer(_SEAT_CAPTURE_PATTERN, text, flags=re.IGNORECASE):
        seat = _seat_from_match(match)
        if seat not in seats:
            seats.append(seat)
    return seats


def _seat_from_match(match: re.Match[str]) -> str:
    number = match.group("bracket") or match.group("prefix") or match.group("plain")
    return f"p{int(number)}"


def _decision_matches_options(options: list[dict[str, Any]], action_type: str, target: str | None) -> bool:
    for item in options:
        if str(item.get("action_type")) != action_type:
            continue
        requires_target = bool(item.get("requires_target"))
        targets = [str(value) for value in item.get("targets", [])]
        if not requires_target:
            return target is None
        if isinstance(target, str) and target in targets:
            return True
    return False


def _mentions_no_op(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("不操作", "不使用", "不用", "跳过", "skip", "no op", "no-op", "弃票", "空刀", "今晚不使用技能"))


def _process_error(provider_label: str, completed: subprocess.CompletedProcess[str]) -> ParticipantInvocationError:
    stderr = completed.stderr.strip()
    stdout = completed.stdout.strip()
    detail = stderr or stdout or f"exit code {completed.returncode}"
    return ParticipantInvocationError(f"{provider_label} 调用失败：{detail[:280]}", kind=_issue_kind(detail), stdout=completed.stdout, stderr=completed.stderr)


def _issue_kind(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ("rate limit", "too many requests", "quota", "429", "限流", "配额")):
        return "rate_limit"
    if "timed out" in lowered or "timeout" in lowered or "超时" in lowered:
        return "timeout"
    if any(token in lowered for token in ("json", "parse", "empty output", "invalid", "解析", "为空", "无效")):
        return "invalid_response"
    return "adapter_error"


def build_process_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in (extra_env or {}).items():
        env[str(key)] = str(value)
    return env
