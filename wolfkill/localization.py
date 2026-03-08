from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .models import ActionType, Audience, DeathCause, EventVisibility, Phase, Role, Team


_PHASE_LABELS = {
    Phase.SETUP.value: "准备阶段",
    Phase.NIGHT.value: "夜晚",
    Phase.WOLF_CHAT.value: "狼人密谈",
    Phase.WOLF_ACTION.value: "狼人行动",
    Phase.SEER_ACTION.value: "预言家行动",
    Phase.WITCH_ACTION.value: "女巫行动",
    Phase.DAWN.value: "天亮",
    Phase.DAY_SPEECH.value: "白天发言",
    Phase.DAY_VOTE.value: "白天投票",
    Phase.ENDED.value: "已结束",
}
_CHANNEL_LABELS = {"system": "系统", "speech": "发言", "wolf": "狼人密谈"}
_ROLE_LABELS = {
    Role.WOLF.value: "狼人",
    Role.SEER.value: "预言家",
    Role.WITCH.value: "女巫",
    Role.VILLAGER.value: "平民",
}
_TEAM_LABELS = {Team.WOLF.value: "狼人阵营", Team.VILLAGE.value: "好人阵营", Team.DRAW.value: "平局"}
_ACTION_LABELS = {
    ActionType.WOLF_KILL.value: "狼人击杀",
    ActionType.SEER_INSPECT.value: "查验",
    ActionType.WITCH_SAVE.value: "使用解药",
    ActionType.WITCH_POISON.value: "使用毒药",
    ActionType.DAY_VOTE.value: "白天投票",
    ActionType.NO_OP.value: "不操作",
}
_DEATH_CAUSE_LABELS = {
    DeathCause.WOLF.value: "被狼人击杀",
    DeathCause.POISON.value: "被毒杀",
    DeathCause.VOTE.value: "被放逐",
}
_ISSUE_KIND_LABELS = {
    "timeout": "超时",
    "invalid_response": "非法响应",
    "rate_limit": "限流",
    "adapter_error": "调用异常",
}
_ISSUE_MODE_LABELS = {"speech": "发言", "decision": "行动"}
_VISIBILITY_LABELS = {EventVisibility.PUBLIC.value: "公开", EventVisibility.PRIVATE.value: "私有"}
_AUDIENCE_LABELS = {Audience.PUBLIC.value: "公开", Audience.WOLF.value: "狼人频道"}
_PRESET_LABELS = {"classic-6": "经典六人局", "duel-2": "双狼双人调试局", "all-wolf-4": "四狼四人互通调试局"}


def label_phase(value: Phase | str | None) -> str:
    return _PHASE_LABELS.get(str(value), str(value))


def label_channel(value: str | None) -> str:
    return _CHANNEL_LABELS.get(str(value), str(value))


def label_role(value: Role | str | None) -> str:
    return _ROLE_LABELS.get(str(value), str(value))


def label_team(value: Team | str | None) -> str:
    return _TEAM_LABELS.get(str(value), str(value))


def label_action(value: ActionType | str | None) -> str:
    return _ACTION_LABELS.get(str(value), str(value))


def label_death_cause(value: DeathCause | str | None) -> str:
    return _DEATH_CAUSE_LABELS.get(str(value), str(value))


def label_issue_kind(value: str | None) -> str:
    return _ISSUE_KIND_LABELS.get(str(value), str(value))


def label_issue_mode(value: str | None) -> str:
    return _ISSUE_MODE_LABELS.get(str(value), str(value))


def label_visibility(value: EventVisibility | str | None) -> str:
    return _VISIBILITY_LABELS.get(str(value), str(value))


def label_audience(value: Audience | str | None) -> str:
    return _AUDIENCE_LABELS.get(str(value), str(value))


def label_preset(value: str | None) -> str:
    return _PRESET_LABELS.get(str(value), str(value))


def label_bool(value: bool) -> str:
    return "是" if value else "否"


def label_seer_result(value: str | None) -> str:
    return {"WOLF": "狼人", "NOT_WOLF": "好人"}.get(str(value), str(value))


def label_message(index: int | None) -> str:
    return f"消息#{index}" if index is not None else "消息#?"


def label_seat(seat: str | None) -> str:
    if seat is None:
        return "未知座位"
    match = re.fullmatch(r"[Pp](\d+)", str(seat))
    if match:
        return f"{int(match.group(1))}号位"
    return str(seat)


def label_status(alive: bool, death_causes: list[str] | tuple[str, ...] | None = None) -> str:
    if alive:
        return "存活"
    if death_causes:
        return "出局（" + "、".join(label_death_cause(cause) for cause in death_causes) + "）"
    return "出局"


_PLAYER_REF_RE = re.compile(r"(?:[Pp](\d{1,2})|(\d{1,2})\s*号)")


def highlight_player_refs(text: str) -> str:
    def _repl(match: re.Match) -> str:
        num = match.group(1) or match.group(2)
        return f"[{num}]"
    return _PLAYER_REF_RE.sub(_repl, text)


def format_event_line(*, index: int | None, day: int, phase: Phase | str | None, channel: str | None, text: str, speaker: str | None = None) -> str:
    speaker_text = f" {label_seat(speaker)}" if speaker else ""
    display_text = highlight_player_refs(text)
    return f"[{label_message(index)}][第{day}天:{label_phase(phase)}:{label_channel(channel)}{speaker_text}] {display_text}"


def localize_request(request: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(request)
    if "phase" in payload:
        payload["phase_label"] = label_phase(payload.get("phase"))
    if "audience" in payload:
        payload["audience_label"] = label_audience(payload.get("audience"))
    public_state = payload.get("public_state")
    if isinstance(public_state, dict):
        if "phase" in public_state:
            public_state["phase_label"] = label_phase(public_state.get("phase"))
        if public_state.get("winner") is not None:
            public_state["winner_label"] = label_team(public_state.get("winner"))
        for player in public_state.get("dead_players", []):
            if player.get("role") is not None:
                player["role_label"] = label_role(player.get("role"))
            if player.get("death_causes") is not None:
                player["death_causes_label"] = [label_death_cause(cause) for cause in player.get("death_causes", [])]
        for event in public_state.get("all_public_events", []):
            _annotate_event(event)
        for event in public_state.get("new_public_events", []):
            _annotate_event(event)
    private_view = payload.get("private_view")
    if isinstance(private_view, dict):
        private_view["role_label"] = label_role(private_view.get("role"))
        private_view["team_label"] = label_team(private_view.get("team"))
        private_view["alive_label"] = label_bool(bool(private_view.get("alive")))
        for item in private_view.get("seer_results", []):
            item["result_label"] = label_seer_result(item.get("result"))
        for event in private_view.get("all_visible_events", []):
            _annotate_event(event)
        for event in private_view.get("new_visible_events", []):
            _annotate_event(event)
    for option in payload.get("options", []):
        option["action_label"] = label_action(option.get("action_type"))
    return payload


def _annotate_event(event: dict[str, Any]) -> None:
    event["message_label"] = label_message(event.get("index"))
    event["phase_label"] = label_phase(event.get("phase"))
    event["channel_label"] = label_channel(event.get("channel"))
    if event.get("visibility") is not None:
        event["visibility_label"] = label_visibility(event.get("visibility"))
    data = event.get("data")
    if isinstance(data, dict) and data.get("result") is not None:
        data["result_label"] = label_seer_result(data.get("result"))
