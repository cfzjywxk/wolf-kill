from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Team(StrEnum):
    WOLF = "WOLF"
    VILLAGE = "VILLAGE"
    DRAW = "DRAW"


class Role(StrEnum):
    WOLF = "WOLF"
    SEER = "SEER"
    WITCH = "WITCH"
    VILLAGER = "VILLAGER"


class Phase(StrEnum):
    SETUP = "SETUP"
    NIGHT = "NIGHT"
    WOLF_CHAT = "WOLF_CHAT"
    WOLF_ACTION = "WOLF_ACTION"
    SEER_ACTION = "SEER_ACTION"
    WITCH_ACTION = "WITCH_ACTION"
    DAWN = "DAWN"
    DAY_SPEECH = "DAY_SPEECH"
    DAY_VOTE = "DAY_VOTE"
    ENDED = "ENDED"


class ActionType(StrEnum):
    WOLF_KILL = "WOLF_KILL"
    SEER_INSPECT = "SEER_INSPECT"
    WITCH_SAVE = "WITCH_SAVE"
    WITCH_POISON = "WITCH_POISON"
    DAY_VOTE = "DAY_VOTE"
    NO_OP = "NO_OP"


class Audience(StrEnum):
    PUBLIC = "PUBLIC"
    WOLF = "WOLF"


class EventVisibility(StrEnum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"


class DeathCause(StrEnum):
    WOLF = "WOLF"
    POISON = "POISON"
    VOTE = "VOTE"


def team_for_role(role: Role) -> Team:
    return Team.WOLF if role == Role.WOLF else Team.VILLAGE


def json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


@dataclass(frozen=True)
class ActionSpec:
    action_type: ActionType
    targets: tuple[str, ...] = ()
    requires_target: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type.value,
            "targets": list(self.targets),
            "requires_target": self.requires_target,
            "description": self.description,
        }


@dataclass(frozen=True)
class Decision:
    action_type: ActionType
    target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"action_type": self.action_type.value, "target": self.target}


@dataclass
class PlayerState:
    seat: str
    name: str
    role: Role
    background: str | None = None
    alive: bool = True
    death_day: int | None = None
    death_causes: list[DeathCause] = field(default_factory=list)

    @property
    def team(self) -> Team:
        return team_for_role(self.role)


@dataclass
class WitchResources:
    save_available: bool = True
    poison_available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"save_available": self.save_available, "poison_available": self.poison_available}


@dataclass
class SeerInspection:
    day: int
    target: str
    result: str

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day, "target": self.target, "result": self.result}


@dataclass
class NightState:
    wolf_votes: dict[str, str | None] = field(default_factory=dict)
    wolf_target: str | None = None
    seer_target: str | None = None
    witch_action: Decision | None = None
    saved_target: str | None = None
    poisoned_target: str | None = None

    def reset(self) -> None:
        self.wolf_votes.clear()
        self.wolf_target = None
        self.seer_target = None
        self.witch_action = None
        self.saved_target = None
        self.poisoned_target = None


@dataclass
class TranscriptEvent:
    index: int
    day: int
    phase: Phase
    visibility: EventVisibility
    channel: str
    text: str
    speaker: str | None = None
    recipients: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)

    def visible_to(self, seat: str) -> bool:
        if self.visibility == EventVisibility.PUBLIC:
            return True
        return seat in self.recipients

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "day": self.day,
            "phase": self.phase.value,
            "visibility": self.visibility.value,
            "channel": self.channel,
            "text": self.text,
            "speaker": self.speaker,
            "recipients": list(self.recipients),
            "data": json_value(self.data),
        }


@dataclass
class GameState:
    preset_name: str
    seed: int
    players: dict[str, PlayerState]
    seat_order: tuple[str, ...]
    phase: Phase = Phase.SETUP
    day: int = 1
    transcript: list[TranscriptEvent] = field(default_factory=list)
    winner: Team | None = None
    witch_resources: dict[str, WitchResources] = field(default_factory=dict)
    seer_results: dict[str, list[SeerInspection]] = field(default_factory=dict)
    current_night: NightState = field(default_factory=NightState)
    next_event_id: int = 1

    def living_seats(self) -> list[str]:
        return [seat for seat in self.seat_order if self.players[seat].alive]

    def living_players(self) -> list[PlayerState]:
        return [self.players[seat] for seat in self.living_seats()]

    def role_seats(self, role: Role, alive_only: bool = False) -> list[str]:
        return [
            seat for seat in self.seat_order
            if self.players[seat].role == role and (not alive_only or self.players[seat].alive)
        ]

    def wolves(self, alive_only: bool = False) -> list[str]:
        return self.role_seats(Role.WOLF, alive_only=alive_only)

    def add_event(
        self,
        *,
        visibility: EventVisibility,
        channel: str,
        text: str,
        speaker: str | None = None,
        recipients: tuple[str, ...] = (),
        data: dict[str, Any] | None = None,
    ) -> TranscriptEvent:
        event = TranscriptEvent(
            index=self.next_event_id,
            day=self.day,
            phase=self.phase,
            visibility=visibility,
            channel=channel,
            text=text,
            speaker=speaker,
            recipients=recipients,
            data=data or {},
        )
        self.next_event_id += 1
        self.transcript.append(event)
        return event

    def public_transcript(self, limit: int | None = None) -> list[TranscriptEvent]:
        events = [event for event in self.transcript if event.visibility == EventVisibility.PUBLIC]
        return events if limit is None else events[-limit:]


def concrete_choices(specs: list[ActionSpec]) -> list[Decision]:
    choices: list[Decision] = []
    for spec in specs:
        if spec.requires_target:
            choices.extend(Decision(spec.action_type, target) for target in spec.targets)
        else:
            choices.append(Decision(spec.action_type))
    return choices
