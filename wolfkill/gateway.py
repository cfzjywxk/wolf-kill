from __future__ import annotations

import sys
import threading
import time
from typing import Any

from .colors import bold, cyan, dim, green, magenta, red, seat_color, yellow
from .debug_logging import AgentDebugLogger
from .localization import format_event_line, label_action, label_seat, label_visibility, localize_request
from .models import ActionSpec, ActionType, Audience, Decision, concrete_choices
from .observer_visibility import ObserverVisibilityPolicy
from .participants import HumanCliParticipant, ParticipantAdapter
from .visibility import VisibilityCompiler


class ParticipantGateway:
    def __init__(self, participants: dict[str, ParticipantAdapter], visibility: VisibilityCompiler, observer_seat: str | None = None, narration_delay_seconds: float = 0.0, learn_history: list[str] | None = None, previous_games: list[str] | None = None, debug_logger: AgentDebugLogger | None = None) -> None:
        self.participants = participants
        self.visibility = visibility
        self.request_counter = 1
        self.issues: list[dict[str, str]] = []
        self.wait_records: list[dict[str, Any]] = []
        self.observer_seat = observer_seat or self._default_observer_seat()
        self.god_view_active = False
        self.narration_delay_seconds = max(0.0, float(narration_delay_seconds))
        self.total_pause_seconds = 0.0
        self.learn_history = list(learn_history or [])
        self.previous_games = list(previous_games or [])
        self.debug_logger = debug_logger
        self._current_wait_key: tuple[str, str] | None = None
        self._hidden_night_announced_day: int | None = None
        self._hidden_night_clock_day: int | None = None

    def bootstrap_sessions(self, state) -> None:
        ai_items = [(seat, adapter) for seat, adapter in self.participants.items() if not isinstance(adapter, HumanCliParticipant)]
        if not ai_items:
            return
        lock = threading.Lock()

        def _bootstrap_one(seat: str, adapter: ParticipantAdapter) -> None:
            adapter.clear_last_call_diagnostics()
            adapter.clear_last_call_exchange()
            request = self._build_base_request(state, seat, "这是赛前初始化同步。请阅读并记住当前身份、规则、座位顺序与已有历史；后续游戏只会增量提供新事件。请只返回 JSON：{\"text\":\"已同步\"}。")
            request["audience"] = Audience.PUBLIC.value
            issue_message = None
            issue_kind = None
            fallback_used = False
            try:
                response = adapter.speak(request)
                final_response = {"text": str(response.get("text", "")).strip() or "已同步"}
            except Exception as exc:
                issue_message = str(exc).strip() or exc.__class__.__name__
                issue_kind = getattr(exc, "kind", None) or self._issue_kind(issue_message)
                fallback_used = True
                adapter.reset_state()
                with lock:
                    self._record_exception(seat, "speech", exc)
                final_response = {"text": "初始化失败"}
            diagnostics_snapshot = dict(adapter.last_call_diagnostics or {})
            exchange_snapshot = dict(adapter.last_call_exchange or {})
            adapter.clear_last_call_exchange()
            self._log_agent_call(seat=seat, adapter=adapter, mode="bootstrap", request=request, diagnostics=diagnostics_snapshot, exchange=exchange_snapshot, final_response=final_response, issue_message=issue_message, issue_kind=issue_kind, fallback_used=fallback_used)

        threads = []
        for seat, adapter in ai_items:
            thread = threading.Thread(target=_bootstrap_one, args=(seat, adapter))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()

    def request_speech(self, state, seat: str, audience: Audience, prompt: str) -> str:
        started_at = time.monotonic()
        pause_before = self.total_pause_seconds
        request_started_at = time.monotonic()
        request = self._build_base_request(state, seat, prompt)
        request["audience"] = audience.value
        request_build_seconds = time.monotonic() - request_started_at
        adapter = self.participants[seat]
        adapter.clear_last_call_exchange()
        wait_started_at = time.monotonic()
        self._announce_wait(state, seat, "speech", request)
        wait_announce_seconds = time.monotonic() - wait_started_at
        show_clock = not isinstance(adapter, HumanCliParticipant)
        hidden_wait = self._should_hide_request_details(state, seat, request)
        if show_clock:
            self._start_wait_clock(hidden_wait=hidden_wait, day=int(state.day))
        adapter.clear_last_call_diagnostics()
        adapter_started_at = time.monotonic()
        issue_message = None
        issue_kind = None
        fallback_used = False
        try:
            response = adapter.speak(request)
            text = str(response.get("text", "")).strip()
            if not text:
                issue_message = "发言内容为空"
                issue_kind = "invalid_response"
                fallback_used = True
                self._record_issue(seat, "speech", "发言内容为空", kind="invalid_response")
                self._announce_issue(state, seat, "speech", "发言内容为空，已使用默认发言。")
                text = "我先不发言。"
        except Exception as exc:
            issue_message = str(exc).strip() or exc.__class__.__name__
            issue_kind = getattr(exc, "kind", None) or self._issue_kind(issue_message)
            fallback_used = True
            self._record_exception(seat, "speech", exc)
            self._announce_issue(state, seat, "speech", f"发言失败，已使用默认发言：{self._exception_brief(exc)}")
            text = "我先不发言。"
        finally:
            adapter_seconds = time.monotonic() - adapter_started_at
            exchange_snapshot = dict(adapter.last_call_exchange or {})
            diagnostics_snapshot = dict(adapter.last_call_diagnostics or {})
            adapter.clear_last_call_exchange()
            if show_clock:
                self._stop_wait_clock(hidden_wait=hidden_wait)
        self._log_agent_call(seat=seat, adapter=adapter, mode="speech", request=request, diagnostics=diagnostics_snapshot, exchange=exchange_snapshot, final_response={"text": text}, issue_message=issue_message, issue_kind=issue_kind, fallback_used=fallback_used)
        if show_clock:
            record = self._record_wait_timing(state=state, seat=seat, mode="speech", request=request, adapter=adapter, total_seconds=time.monotonic() - started_at, adapter_seconds=adapter_seconds, request_build_seconds=request_build_seconds, wait_announce_seconds=wait_announce_seconds, done_announce_seconds=0.0, pause_seconds=self.total_pause_seconds - pause_before)
            done_started_at = time.monotonic()
            self._announce_done(state, seat, "speech", details=text[:80] or None, elapsed_seconds=float(record["total_seconds"]))
            record["done_announce_seconds"] = time.monotonic() - done_started_at
        return text[:600] or "我先不发言。"

    def request_action(self, state, seat: str, specs: list[ActionSpec], prompt: str) -> Decision:
        started_at = time.monotonic()
        pause_before = self.total_pause_seconds
        request_started_at = time.monotonic()
        request = self._build_base_request(state, seat, prompt)
        request["options"] = [spec.to_dict() for spec in specs]
        request_build_seconds = time.monotonic() - request_started_at
        adapter = self.participants[seat]
        adapter.clear_last_call_exchange()
        wait_started_at = time.monotonic()
        self._announce_wait(state, seat, "decision", request)
        wait_announce_seconds = time.monotonic() - wait_started_at
        show_clock = not isinstance(adapter, HumanCliParticipant)
        hidden_wait = self._should_hide_request_details(state, seat, request)
        if show_clock:
            self._start_wait_clock(hidden_wait=hidden_wait, day=int(state.day))
        adapter.clear_last_call_diagnostics()
        adapter_started_at = time.monotonic()
        issue_message = None
        issue_kind = None
        fallback_used = False
        try:
            response = adapter.decide(request)
            decision, issue = self._validate_decision(response, specs)
            if issue:
                issue_message = issue
                issue_kind = "invalid_response"
                fallback_used = True
                self._record_issue(seat, "decision", issue, kind="invalid_response")
                self._announce_issue(state, seat, "decision", f"行动无效，已回退到默认动作：{issue}")
        except Exception as exc:
            issue_message = str(exc).strip() or exc.__class__.__name__
            issue_kind = getattr(exc, "kind", None) or self._issue_kind(issue_message)
            fallback_used = True
            self._record_exception(seat, "decision", exc)
            self._announce_issue(state, seat, "decision", f"行动失败，已回退到默认动作：{self._exception_brief(exc)}")
            decision = self._fallback_decision(specs)
        finally:
            adapter_seconds = time.monotonic() - adapter_started_at
            exchange_snapshot = dict(adapter.last_call_exchange or {})
            diagnostics_snapshot = dict(adapter.last_call_diagnostics or {})
            adapter.clear_last_call_exchange()
            if show_clock:
                self._stop_wait_clock(hidden_wait=hidden_wait)
        self._log_agent_call(seat=seat, adapter=adapter, mode="decision", request=request, diagnostics=diagnostics_snapshot, exchange=exchange_snapshot, final_response=decision.to_dict(), issue_message=issue_message, issue_kind=issue_kind, fallback_used=fallback_used)
        details = self._describe_decision(decision, specs)
        if show_clock:
            record = self._record_wait_timing(state=state, seat=seat, mode="decision", request=request, adapter=adapter, total_seconds=time.monotonic() - started_at, adapter_seconds=adapter_seconds, request_build_seconds=request_build_seconds, wait_announce_seconds=wait_announce_seconds, done_announce_seconds=0.0, pause_seconds=self.total_pause_seconds - pause_before, details=details)
            done_started_at = time.monotonic()
            self._announce_done(state, seat, "decision", details=details, elapsed_seconds=float(record["total_seconds"]))
            record["done_announce_seconds"] = time.monotonic() - done_started_at
        return decision

    def request_actions_parallel(self, state, seat_specs: list[tuple[str, list[ActionSpec], str]]) -> dict[str, Decision]:
        results: dict[str, Decision] = {}
        human_items = []
        ai_items = []
        for seat, specs, prompt in seat_specs:
            adapter = self.participants[seat]
            if isinstance(adapter, HumanCliParticipant):
                human_items.append((seat, specs, prompt))
            else:
                ai_items.append((seat, specs, prompt))
        for seat, specs, prompt in human_items:
            results[seat] = self.request_action(state, seat, specs, prompt)
        if not ai_items:
            return results
        lock = threading.Lock()
        per_seat_records: dict[str, dict[str, Any]] = {}
        started_at = time.monotonic()
        pause_before = self.total_pause_seconds

        def _do_request(seat: str, specs: list[ActionSpec], prompt: str) -> None:
            request_started_at = time.monotonic()
            request = self._build_base_request(state, seat, prompt)
            request["options"] = [spec.to_dict() for spec in specs]
            request_build_seconds = time.monotonic() - request_started_at
            adapter = self.participants[seat]
            adapter.clear_last_call_diagnostics()
            adapter.clear_last_call_exchange()
            adapter_started_at = time.monotonic()
            issue_message = None
            issue_kind = None
            fallback_used = False
            try:
                response = adapter.decide(request)
                decision, issue = self._validate_decision(response, specs)
                if issue:
                    issue_message = issue
                    issue_kind = "invalid_response"
                    fallback_used = True
                    with lock:
                        self._record_issue(seat, "decision", issue, kind="invalid_response")
            except Exception as exc:
                issue_message = str(exc).strip() or exc.__class__.__name__
                issue_kind = getattr(exc, "kind", None) or self._issue_kind(issue_message)
                fallback_used = True
                with lock:
                    self._record_exception(seat, "decision", exc)
                decision = self._fallback_decision(specs)
            adapter_seconds = time.monotonic() - adapter_started_at
            exchange_snapshot = dict(adapter.last_call_exchange or {})
            diagnostics_snapshot = dict(adapter.last_call_diagnostics or {})
            adapter.clear_last_call_exchange()
            self._log_agent_call(seat=seat, adapter=adapter, mode="decision", request=request, diagnostics=diagnostics_snapshot, exchange=exchange_snapshot, final_response=decision.to_dict(), issue_message=issue_message, issue_kind=issue_kind, fallback_used=fallback_used)
            info = self._adapter_diagnostics(adapter, fallback_seconds=adapter_seconds, request=request)
            with lock:
                results[seat] = decision
                per_seat_records[seat] = {
                    "seat": seat,
                    "participant": adapter.name,
                    "provider": info["provider"],
                    "provider_seconds": info["provider_seconds"],
                    "io_wait_seconds": info["io_wait_seconds"],
                    "context_mode": info["context_mode"],
                    "prompt_chars": info["prompt_chars"],
                    **self._request_feed_stats(request),
                    "request_build_seconds": request_build_seconds,
                    "total_seconds": request_build_seconds + adapter_seconds,
                    "details": self._describe_decision(decision, specs),
                }

        self._emit(dim("【法官】正在等待所有玩家同时投票..."), pace=False)
        self._start_clock()
        threads = []
        for seat, specs, prompt in ai_items:
            thread = threading.Thread(target=_do_request, args=(seat, specs, prompt))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        self._stop_clock()
        total_seconds = time.monotonic() - started_at
        for seat, specs, prompt in ai_items:
            details = self._describe_decision(results[seat], specs)
            self._announce_done(state, seat, "decision", details=details, elapsed_seconds=float(per_seat_records[seat].get("total_seconds", per_seat_records[seat]["provider_seconds"])))
        record = self._record_parallel_wait_timing(total_seconds=total_seconds, pause_seconds=self.total_pause_seconds - pause_before, seat_records=[per_seat_records[seat] for seat, _, _ in ai_items])
        return results

    def close(self) -> None:
        for adapter in self.participants.values():
            adapter.close()

    def activate_god_view(self, state) -> None:
        if self.god_view_active or self.observer_seat is None:
            return
        observer = state.players.get(self.observer_seat)
        if observer is None or observer.alive:
            return
        self.god_view_active = True
        self._emit(yellow(f"【法官】{label_seat(self.observer_seat)}已出局，现在切换为上帝视角；接下来将继续播报全部步骤、等待对象与结算结果。"), pace=True)
        last_seen = self._observer_last_seen_event_id()
        for event in state.transcript:
            if event.index <= last_seen:
                continue
            self._announce_live_event(event, judge_mode=True)
            self._remember_observer_event(event.index)

    def on_event(self, event) -> None:
        if self.debug_logger is not None:
            self.debug_logger.log("event", event=event.to_dict())
        policy = self._observer_visibility_policy()
        if not policy.can_observer_receive_event(event):
            return
        self._announce_live_event(event, judge_mode=self.god_view_active)
        self._remember_observer_event(event.index)

    def _build_base_request(self, state, seat: str, prompt: str) -> dict[str, Any]:
        adapter = self.participants[seat]
        since = adapter.last_sent_event_id if adapter.has_session else 0
        request = {
            "request_id": f"req-{self.request_counter}",
            "seat": seat,
            "name": state.players[seat].name,
            "day": state.day,
            "phase": state.phase.value,
            "prompt": prompt,
            "background": adapter.background,
            "context_mode": "incremental" if since > 0 else "full",
            "public_state": self.visibility.public_state(state, since_event_id=since),
            "private_view": self.visibility.private_view(state, seat, since_event_id=since),
        }
        if since == 0 and (self.learn_history or self.previous_games):
            request["strategy_briefing"] = {
                "learn_history": list(self.learn_history),
                "previous_games": list(self.previous_games),
            }
        if state.transcript:
            adapter.last_sent_event_id = state.transcript[-1].index
        self.request_counter += 1
        return localize_request(request)

    def _log_agent_call(self, *, seat: str, adapter: ParticipantAdapter, mode: str, request: dict[str, Any], diagnostics: dict[str, Any], exchange: dict[str, Any], final_response: dict[str, Any], issue_message: str | None, issue_kind: str | None, fallback_used: bool) -> None:
        if self.debug_logger is None or isinstance(adapter, HumanCliParticipant):
            return
        self.debug_logger.log(
            "agent_call",
            seat=seat,
            participant=adapter.name,
            provider=getattr(adapter, "provider_label", adapter.__class__.__name__.lower()),
            mode=mode,
            request_id=request.get("request_id"),
            phase=request.get("phase"),
            context_mode=request.get("context_mode"),
            prompt=request.get("prompt"),
            request=request,
            diagnostics=diagnostics,
            prompt_text=exchange.get("prompt"),
            raw_output=exchange.get("raw_output"),
            parsed_response=exchange.get("parsed_response"),
            parse_mode=exchange.get("parse_mode") or diagnostics.get("parse_mode"),
            error=exchange.get("error"),
            stdout=exchange.get("stdout"),
            stderr=exchange.get("stderr"),
            final_response=final_response,
            issue={"kind": issue_kind, "message": issue_message} if issue_message else None,
            fallback_used=bool(fallback_used),
        )

    def _validate_decision(self, response: dict[str, Any], specs: list[ActionSpec]) -> tuple[Decision, str | None]:
        raw_action = response.get("action_type") or response.get("action") or response.get("type")
        try:
            action_type = ActionType(str(raw_action))
        except Exception:
            return self._fallback_decision(specs), f"非法 action_type={raw_action!r}"
        matching = next((spec for spec in specs if spec.action_type == action_type), None)
        if matching is None:
            return self._fallback_decision(specs), f"未提供该动作：{action_type.value}"
        target = response.get("target")
        if matching.requires_target:
            if target not in matching.targets:
                return self._fallback_decision(specs), f"非法 target={target!r}，动作 {action_type.value}"
            return Decision(action_type, str(target)), None
        return Decision(action_type), None

    def _fallback_decision(self, specs: list[ActionSpec]) -> Decision:
        choices = concrete_choices(specs)
        no_op = next((choice for choice in choices if choice.action_type == ActionType.NO_OP), None)
        if no_op is not None:
            return no_op
        choices.sort(key=lambda item: (item.action_type.value, item.target or ""))
        return choices[0]

    def _record_exception(self, seat: str, mode: str, exc: Exception) -> None:
        message = str(exc).strip() or exc.__class__.__name__
        kind = getattr(exc, "kind", None) or self._issue_kind(message)
        self._record_issue(
            seat,
            mode,
            message,
            kind=kind,
            stdout=getattr(exc, "stdout", None),
            stderr=getattr(exc, "stderr", None),
        )

    def _record_issue(
        self,
        seat: str,
        mode: str,
        message: str,
        *,
        kind: str,
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> None:
        self.issues.append(
            {
                "seat": seat,
                "participant": self.participants[seat].name,
                "mode": mode,
                "kind": kind,
                "message": message[:280],
                "stdout": self._debug_excerpt(stdout),
                "stderr": self._debug_excerpt(stderr),
            }
        )

    def _debug_excerpt(self, text: str | None, *, limit: int = 600) -> str | None:
        if not text:
            return None
        stripped = text.strip()
        if not stripped:
            return None
        if len(stripped) > limit:
            return stripped[:limit] + "\n…（已截断）"
        return stripped

    def _exception_brief(self, exc: Exception, *, limit: int = 160) -> str:
        message = str(exc).strip() or exc.__class__.__name__
        if len(message) > limit:
            return message[:limit] + "…"
        return message

    def _adapter_diagnostics(self, adapter: ParticipantAdapter, *, fallback_seconds: float, request: dict[str, Any]) -> dict[str, Any]:
        info = dict(adapter.last_call_diagnostics or {})
        adapter.clear_last_call_diagnostics()
        provider_seconds = info.get("provider_seconds")
        if provider_seconds is None:
            provider_seconds = info.get("subprocess_seconds")
        if provider_seconds is None:
            provider_seconds = info.get("total_seconds")
        if provider_seconds is None:
            provider_seconds = fallback_seconds
        prompt_chars = info.get("prompt_chars")
        if prompt_chars is None:
            prompt_chars = info.get("payload_chars")
        provider = info.get("provider") or getattr(adapter, "provider_label", adapter.__class__.__name__.lower())
        io_wait_seconds = info.get("io_wait_seconds")
        if io_wait_seconds is None:
            io_wait_seconds = provider_seconds
        return {
            **info,
            "provider": str(provider),
            "provider_seconds": float(provider_seconds),
            "io_wait_seconds": float(io_wait_seconds),
            "context_mode": str(info.get("context_mode") or request.get("context_mode") or "full"),
            "prompt_chars": int(prompt_chars or 0),
            "response_chars": int(info.get("response_chars") or 0),
        }

    def _request_feed_stats(self, request: dict[str, Any]) -> dict[str, Any]:
        public_state = request.get("public_state") or {}
        private_view = request.get("private_view") or {}
        public_events = public_state.get("new_public_events") or public_state.get("all_public_events") or []
        private_events = private_view.get("new_visible_events") or private_view.get("all_visible_events") or []
        extras: list[str] = ["角色", "阵营", "存活列表"]
        if public_state.get("dead_players"):
            extras.append("死亡信息")
        if private_view.get("teammates"):
            extras.append("狼人队友")
        if private_view.get("seer_results"):
            extras.append("查验结果")
        if private_view.get("witch_resources"):
            extras.append("女巫药剂")
        if private_view.get("night_hint"):
            extras.append("夜间提示")
        return {
            "public_events": len(public_events),
            "private_events": len(private_events),
            "event_count": len(public_events) + len(private_events),
            "omitted_public_events": int(public_state.get("omitted_public_event_count") or 0),
            "omitted_private_events": int(private_view.get("omitted_visible_event_count") or 0),
            "extras": extras,
        }

    def _request_event_count(self, request: dict[str, Any]) -> int:
        return int(self._request_feed_stats(request)["event_count"])

    def _record_wait_timing(self, *, state, seat: str, mode: str, request: dict[str, Any], adapter: ParticipantAdapter, total_seconds: float, adapter_seconds: float, request_build_seconds: float, wait_announce_seconds: float, done_announce_seconds: float, pause_seconds: float, details: str | None = None) -> dict[str, Any]:
        info = self._adapter_diagnostics(adapter, fallback_seconds=adapter_seconds, request=request)
        record = {
            "kind": "single",
            "seat": seat,
            "participant": adapter.name,
            "mode": mode,
            "details": details,
            "step_label": f"{label_seat(seat)}{'发言' if mode == 'speech' else '行动'}",
            "provider": info["provider"],
            "context_mode": info["context_mode"],
            "prompt_chars": info["prompt_chars"],
            **self._request_feed_stats(request),
            "hidden": self._should_hide_request_details(state, seat, request),
            "total_seconds": float(total_seconds),
            "provider_seconds": float(info["provider_seconds"]),
            "io_wait_seconds": float(info["io_wait_seconds"]),
            "pause_seconds": float(max(0.0, pause_seconds)),
            "request_build_seconds": float(request_build_seconds),
            "wait_announce_seconds": float(wait_announce_seconds),
            "done_announce_seconds": float(done_announce_seconds),
        }
        record["program_seconds"] = max(0.0, record["total_seconds"] - record["io_wait_seconds"])
        record["provider_overhead_seconds"] = max(0.0, record["provider_seconds"] - record["io_wait_seconds"])
        self.wait_records.append(record)
        return record

    def _record_parallel_wait_timing(self, *, total_seconds: float, pause_seconds: float, seat_records: list[dict[str, Any]]) -> dict[str, Any]:
        provider_seconds = max((float(item["provider_seconds"]) for item in seat_records), default=0.0)
        io_wait_seconds = max((float(item.get("io_wait_seconds", item["provider_seconds"])) for item in seat_records), default=0.0)
        record = {
            "kind": "parallel_decision",
            "step_label": "并行投票",
            "total_seconds": float(total_seconds),
            "provider_seconds": provider_seconds,
            "provider_sum_seconds": sum(float(item["provider_seconds"]) for item in seat_records),
            "io_wait_seconds": io_wait_seconds,
            "io_wait_sum_seconds": sum(float(item.get("io_wait_seconds", item["provider_seconds"])) for item in seat_records),
            "pause_seconds": float(max(0.0, pause_seconds)),
            "seat_records": seat_records,
        }
        record["program_seconds"] = max(0.0, record["total_seconds"] - record["io_wait_seconds"])
        record["provider_overhead_seconds"] = max(0.0, record["provider_seconds"] - record["io_wait_seconds"])
        self.wait_records.append(record)
        return record

    def _announce_wait_timing(self, record: dict[str, Any]) -> None:
        mode_label = "发言" if record["mode"] == "speech" else "行动"
        if record["hidden"]:
            self._emit(
                dim(
                    f"【诊断】本次{mode_label}等待 {record['total_seconds']:.2f}s：AI/外部处理 {record['provider_seconds']:.2f}s，"
                    f"其中真正等待 AI/IO {record['io_wait_seconds']:.2f}s；程序额外 {record['program_seconds']:.2f}s（播报停顿 {record['pause_seconds']:.2f}s）；"
                    f"喂给 AI 的上下文：{record['context_mode']}，公开 {record['public_events']} 条，私有 {record['private_events']} 条。"
                ),
                pace=False,
            )
            return
        seat_label = label_seat(record["seat"])
        context_label = f"{record['provider']}/{record['context_mode']}"
        prompt_text = f"，prompt≈{record['prompt_chars']}字" if record["prompt_chars"] else ""
        omitted_parts = []
        if record["omitted_public_events"]:
            omitted_parts.append(f"更早公开 {record['omitted_public_events']} 条")
        if record["omitted_private_events"]:
            omitted_parts.append(f"更早私有 {record['omitted_private_events']} 条")
        omitted_text = f"，裁剪掉 {'、'.join(omitted_parts)}" if omitted_parts else ""
        extras_text = "、".join(record["extras"])
        self._emit(
            dim(
                f"【诊断】{seat_label}（{record['participant']}）本次{mode_label}等待 {record['total_seconds']:.2f}s："
                f"AI/外部处理 {record['provider_seconds']:.2f}s，其中真正等待 AI/IO {record['io_wait_seconds']:.2f}s；程序额外 {record['program_seconds']:.2f}s"
                f"（播报停顿 {record['pause_seconds']:.2f}s）；{context_label}{prompt_text}；"
                f"喂给 AI：公开 {record['public_events']} 条、私有 {record['private_events']} 条{omitted_text}；"
                f"附带信息：{extras_text}。"
            ),
            pace=False,
        )

    def _announce_parallel_wait_timing(self, record: dict[str, Any]) -> None:
        breakdown = "；".join(
            f"{label_seat(item['seat'])}/{item['provider']} {float(item['provider_seconds']):.2f}s（公开{item['public_events']} 私有{item['private_events']}）"
            for item in sorted(record["seat_records"], key=lambda item: item["seat"])
        )
        self._emit(dim(f"【诊断】并行投票等待 {record['total_seconds']:.2f}s：最慢 AI/外部处理 {record['provider_seconds']:.2f}s，其中真正等待 AI/IO {record['io_wait_seconds']:.2f}s；程序额外 {record['program_seconds']:.2f}s（播报停顿 {record['pause_seconds']:.2f}s）；各座位 {breakdown}。"), pace=False)

    def timing_summary(self) -> dict[str, Any] | None:
        if not self.wait_records:
            return None
        total_seconds = sum(float(record["total_seconds"]) for record in self.wait_records)
        provider_seconds = sum(float(record["provider_seconds"]) for record in self.wait_records)
        io_wait_seconds = sum(float(record.get("io_wait_seconds", record["provider_seconds"])) for record in self.wait_records)
        program_seconds = sum(float(record["program_seconds"]) for record in self.wait_records)
        pause_seconds = sum(float(record["pause_seconds"]) for record in self.wait_records)
        longest = max(self.wait_records, key=lambda record: float(record["total_seconds"]))
        return {
            "wait_count": len(self.wait_records),
            "total_seconds": total_seconds,
            "provider_seconds": provider_seconds,
            "io_wait_seconds": io_wait_seconds,
            "program_seconds": program_seconds,
            "pause_seconds": pause_seconds,
            "longest": longest,
            "records": [dict(record) for record in self.wait_records],
        }

    def _announce_wait(self, state, seat: str, mode: str, request: dict[str, Any]) -> None:
        hidden = self._should_hide_request_details(state, seat, request)
        if hidden:
            if self._hidden_night_announced_day == int(state.day):
                return
            self._hidden_night_announced_day = int(state.day)
            self._current_wait_key = ("hidden_night", str(state.day))
            self._emit(dim(self._hidden_wait_message(seat, mode)), pace=False)
            return
        self._stop_hidden_night_clock()
        wait_key = (seat, mode)
        if self._current_wait_key == wait_key:
            return
        self._current_wait_key = wait_key
        self._emit(dim(self._judge_wait_message(seat, mode, request)), pace=self._should_pause_for_request(state, seat))

    def _announce_request_feed_summary(self, state, seat: str, mode: str, request: dict[str, Any]) -> None:
        mode_label = "发言" if mode == "speech" else "行动"
        stats = self._request_feed_stats(request)
        session_text = "会话复用" if request.get("context_mode") == "incremental" else "同局首轮"
        if self._should_hide_request_details(state, seat, request):
            self._emit(
                dim(
                    f"【提示】即将向一位玩家同步{mode_label}上下文：{session_text}/{request.get('context_mode', 'full')}，"
                    f"公开 {stats['public_events']} 条，私有 {stats['private_events']} 条。"
                ),
                pace=False,
            )
            return
        seat_label = label_seat(seat)
        omitted_parts = []
        if stats["omitted_public_events"]:
            omitted_parts.append(f"更早公开 {stats['omitted_public_events']} 条")
        if stats["omitted_private_events"]:
            omitted_parts.append(f"更早私有 {stats['omitted_private_events']} 条")
        omitted_text = f"，裁剪掉 {'、'.join(omitted_parts)}" if omitted_parts else ""
        extras_text = "、".join(stats["extras"])
        self._emit(
            dim(
                f"【提示】即将喂给 {seat_label}（{self.participants[seat].name}）用于{mode_label}："
                f"{session_text}/{request.get('context_mode', 'full')} 上下文，公开 {stats['public_events']} 条、私有 {stats['private_events']} 条{omitted_text}；"
                f"附带信息：{extras_text}。"
            ),
            pace=False,
        )

    def _announce_done(self, state, seat: str, mode: str, details: str | None = None, elapsed_seconds: float | None = None) -> None:
        if not (isinstance(self._current_wait_key, tuple) and self._current_wait_key and str(self._current_wait_key[0]).startswith("hidden")):
            self._current_wait_key = None
        if elapsed_seconds is None:
            return
        duration_text = f"（{elapsed_seconds:.1f}s）"
        if self._should_hide_request_details(state, seat):
            return
        seat_label = label_seat(seat)
        name = self.participants[seat].name
        if mode == "speech":
            return
        action_text = details or "不操作"
        self._emit(green(f"【结果】{duration_text}{seat_label}（{name}）行动：{action_text}"), pace=False)

    def _announce_issue(self, state, seat: str, mode: str, message: str) -> None:
        if self._should_hide_request_details(state, seat):
            return
        seat_label = label_seat(seat)
        result_label = self._judge_result_label(mode)
        self._emit(red(f"【异常】{seat_label}（{self.participants[seat].name}）的{result_label}出现问题：{message}"), pace=self._should_pause_for_request(state, seat))

    def _default_observer_seat(self) -> str | None:
        return next((seat for seat, adapter in self.participants.items() if isinstance(adapter, HumanCliParticipant)), None)

    def _observer_last_seen_event_id(self) -> int:
        if self.observer_seat is None:
            return 0
        observer = self.participants.get(self.observer_seat)
        if isinstance(observer, HumanCliParticipant):
            return observer.last_seen_event_id
        return 0

    def _remember_observer_event(self, event_id: int) -> None:
        if self.observer_seat is None:
            return
        observer = self.participants.get(self.observer_seat)
        if isinstance(observer, HumanCliParticipant):
            observer.remember_event(event_id)

    def _announce_live_event(self, event, *, judge_mode: bool) -> None:
        self._stop_hidden_night_clock()
        raw = format_event_line(index=event.index, day=event.day, phase=event.phase, channel=event.channel, text=event.text, speaker=event.speaker)
        if judge_mode:
            if event.channel in {"speech", "wolf"} and event.speaker:
                body = seat_color(event.speaker, raw)
            elif "投票" in event.text and event.speaker:
                body = seat_color(event.speaker, raw)
            elif event.channel == "wolf":
                body = magenta(raw)
            else:
                body = yellow(raw)
            line = f"{dim(f'【法官记录/{label_visibility(event.visibility)}】')}{body}"
        elif event.channel == "speech" and event.speaker:
            line = bold(seat_color(event.speaker, f"【发言】{raw}"))
        elif "投票" in event.text and event.speaker:
            line = bold(seat_color(event.speaker, f"【投票】{raw}"))
        elif event.channel == "wolf" and event.speaker:
            line = seat_color(event.speaker, f"【密谈】{raw}")
        elif event.channel == "wolf":
            line = magenta(f"【密谈】{raw}")
        else:
            line = yellow(f"【播报】{raw}")
        self._emit(line, pace=True)

    def _describe_decision(self, decision: Decision, specs: list[ActionSpec]) -> str:
        details = [label_action(decision.action_type)]
        if decision.target:
            details.append(f"目标：{label_seat(decision.target)}")
        matching = next((spec for spec in specs if spec.action_type == decision.action_type), None)
        if matching and matching.description:
            details.append(matching.description)
        return "；".join(details)

    def _judge_wait_message(self, seat: str, mode: str, request: dict[str, Any]) -> str:
        seat_label = label_seat(seat)
        participant_name = self.participants[seat].name
        day = request.get("day")
        phase = str(request.get("phase") or "")
        phase_label = str(request.get("phase_label") or request.get("phase") or "当前阶段")
        if mode == "speech":
            templates = {
                "WOLF_CHAT": f"【法官】现在是第{day}天夜里，狼人请依次交流；请{seat_label}（{participant_name}）发言。",
                "DAY_SPEECH": f"【法官】现在进入第{day}天白天讨论；请{seat_label}（{participant_name}）发言。",
            }
            return templates.get(phase, f"【法官】现在是{phase_label}；请{seat_label}（{participant_name}）发言。")
        templates = {
            "WOLF_ACTION": f"【法官】狼人请确认今晚刀口；请{seat_label}（{participant_name}）给出行动。",
            "SEER_ACTION": f"【法官】预言家请睁眼；请{seat_label}（{participant_name}）选择查验目标。",
            "WITCH_ACTION": f"【法官】女巫请睁眼；请{seat_label}（{participant_name}）决定是否用药。",
            "DAY_VOTE": f"【法官】现在开始第{day}天放逐投票；请{seat_label}（{participant_name}）投票。",
        }
        return templates.get(phase, f"【法官】现在是{phase_label}；请{seat_label}（{participant_name}）给出行动。")

    def _observer_visibility_policy(self) -> ObserverVisibilityPolicy:
        return ObserverVisibilityPolicy(observer_seat=self.observer_seat, god_view_active=self.god_view_active)

    def _judge_result_label(self, mode: str) -> str:
        return "发言" if mode == "speech" else "选择"

    def _should_hide_request_details(self, state, seat: str, request: dict[str, Any] | None = None) -> bool:
        return self._observer_visibility_policy().should_hide_request_details(state, seat, request)

    def _should_pause_for_request(self, state, seat: str) -> bool:
        return self._observer_visibility_policy().should_pause_for_request(seat)

    def _hidden_wait_message(self, seat: str, mode: str) -> str:
        return "【法官】夜间流程进行中，等待夜间发言和角色行动。"

    def _hidden_done_message(self, mode: str) -> str:
        label = "夜间发言" if mode == "speech" else "夜间行动"
        return f"已记录一位玩家的{label}。"

    def _start_wait_clock(self, *, hidden_wait: bool, day: int) -> None:
        if hidden_wait:
            if self._hidden_night_clock_day == day:
                return
            self._hidden_night_clock_day = day
            self._start_clock()
            return
        self._stop_hidden_night_clock()
        self._start_clock()

    def _stop_wait_clock(self, *, hidden_wait: bool) -> None:
        if hidden_wait:
            return
        self._stop_clock()

    def _stop_hidden_night_clock(self) -> None:
        if self._hidden_night_clock_day is None:
            return
        self._hidden_night_clock_day = None
        self._stop_clock()

    def _start_clock(self) -> None:
        is_tty = getattr(sys.stdout, "isatty", None)
        if not (callable(is_tty) and is_tty()):
            return
        self._clock_stop = threading.Event()
        self._clock_start = time.monotonic()
        self._clock_thread = threading.Thread(target=self._clock_tick, daemon=True)
        self._clock_thread.start()

    def _stop_clock(self) -> None:
        stop = getattr(self, "_clock_stop", None)
        thread = getattr(self, "_clock_thread", None)
        if stop is None or thread is None:
            return
        stop.set()
        thread.join(timeout=2)
        self._clock_thread = None
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def _clock_tick(self) -> None:
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        index = 0
        while not self._clock_stop.wait(0.1):
            elapsed = time.monotonic() - self._clock_start
            frame = frames[index % len(frames)]
            index += 1
            sys.stdout.write(f"\r{frame} 已等待 {elapsed:.1f}s")
            sys.stdout.flush()

    def _emit(self, *lines: str, pace: bool = False) -> None:
        for line in lines:
            if pace:
                self._pause()
            ts = time.strftime("%H:%M:%S")
            print(f"{line} [{ts}]", flush=True)

    def _pause(self) -> None:
        if self.narration_delay_seconds <= 0:
            return
        is_tty = getattr(sys.stdout, "isatty", None)
        if callable(is_tty) and not is_tty():
            return
        delay = self.narration_delay_seconds
        time.sleep(delay)
        self.total_pause_seconds += delay

    def _issue_kind(self, message: str) -> str:
        lowered = message.lower()
        if any(token in lowered for token in ("rate limit", "too many requests", "quota", "429", "限流", "配额")):
            return "rate_limit"
        if "timed out" in lowered or "timeout" in lowered or "超时" in lowered:
            return "timeout"
        if any(token in lowered for token in ("json", "parse", "invalid", "illegal", "empty", "解析", "无效", "非法", "为空")):
            return "invalid_response"
        return "adapter_error"
