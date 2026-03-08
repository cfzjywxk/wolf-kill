from __future__ import annotations

from collections import Counter

from .gateway import ParticipantGateway
from .localization import label_preset, label_role, label_seat, label_seer_result, label_team
from .models import ActionSpec, ActionType, Audience, DeathCause, EventVisibility, GameState, Phase, Role, SeerInspection, Team


def resolve_vote(votes: dict[str, str | None], candidate_order: tuple[str, ...] | list[str]) -> str | None:
    counts = Counter(target for target in votes.values() if target)
    if not counts:
        return None
    top_score = max(counts.values())
    top_targets = [target for target, score in counts.items() if score == top_score]
    if len(top_targets) != 1:
        return None
    order = list(candidate_order)
    return sorted(top_targets, key=lambda item: order.index(item))[0]


def resolve_wolf_target(votes: dict[str, str | None], candidate_order: tuple[str, ...] | list[str]) -> str | None:
    counts = Counter(target for target in votes.values() if target)
    if not counts:
        return None
    top_score = max(counts.values())
    top_targets = [target for target, score in counts.items() if score == top_score]
    order = list(candidate_order)
    return sorted(top_targets, key=lambda item: order.index(item))[0]


_GOD_ROLES = {Role.SEER, Role.WITCH}


def evaluate_winner(state: GameState) -> Team | None:
    alive_wolves = len(state.wolves(alive_only=True))
    if alive_wolves == 0:
        return Team.VILLAGE
    all_gods = [player for player in state.players.values() if player.role in _GOD_ROLES]
    if all_gods and all(not player.alive for player in all_gods):
        return Team.WOLF
    all_villagers = [player for player in state.players.values() if player.role == Role.VILLAGER]
    if all_villagers and all(not player.alive for player in all_villagers):
        return Team.WOLF
    return None


def build_previous_game_summary(state: GameState) -> str:
    lines = [f"上局结果（胜方：{label_team(state.winner)}）："]
    for seat in state.seat_order:
        player = state.players[seat]
        status = "存活" if player.alive else f"第{player.death_day}天出局"
        lines.append(f"  {label_seat(seat)}（{player.name}）= {label_role(player.role)}（{status}）")
    return "；".join(lines)


class GameEngine:
    def __init__(self, state: GameState, gateway: ParticipantGateway, max_days: int = 12, previous_games: list[str] | None = None, learn_history: list[str] | None = None, learn_briefing_label: str | None = None) -> None:
        self.state = state
        self.gateway = gateway
        self.max_days = max_days
        self.previous_games = previous_games or []
        self.learn_history = learn_history or []
        self.learn_briefing_label = learn_briefing_label

    def run(self) -> GameState:
        self._announce_game_intro()
        while self.state.winner is None and self.state.day <= self.max_days:
            self._run_night()
            if self._finalize_if_winner():
                break
            self._run_day()
            if self._finalize_if_winner():
                break
            self.state.day += 1
        if self.state.winner is None:
            self.state.winner = evaluate_winner(self.state) or Team.DRAW
        self.state.phase = Phase.ENDED
        self._public(f"游戏结束，胜方：{label_team(self.state.winner)}。")
        return self.state

    def _announce_game_intro(self) -> None:
        for seat in self.state.seat_order:
            self._add_event(visibility=EventVisibility.PRIVATE, channel="system", text=self._private_role_intro(seat), recipients=(seat,))
        if self.learn_history:
            if self.learn_briefing_label:
                self._public(f"【系统】已为 AI 玩家加载赛前策略知识库 {self.learn_briefing_label}。")
            else:
                self._public(f"【系统】已为 AI 玩家加载 {len(self.learn_history)} 份赛前策略材料。")
        if self.previous_games:
            self._public(f"【系统】已为 AI 玩家加载 {len(self.previous_games)} 局本轮回顾作为策略参考。")
        self._public(f"游戏开始，使用预设：{label_preset(self.state.preset_name)}。")
        role_counts = Counter(player.role for player in self.state.players.values())
        self._public(f"本局共{len(self.state.seat_order)}名玩家，身份配置：{self._role_distribution_text(role_counts)}。")
        self._public(f"技能说明：{self._role_rules_text(role_counts)}。")
        self._public("【胜利条件】好人阵营：放逐场上全部狼人，好人获胜。狼人阵营有两种获胜方式：①屠边——场上所有平民全部出局；②屠城——场上所有神职全部出局。")

    def _role_distribution_text(self, role_counts: Counter) -> str:
        ordered = [Role.WOLF, Role.SEER, Role.WITCH, Role.VILLAGER]
        return "、".join(f"{label_role(role)}×{role_counts.get(role, 0)}" for role in ordered if role_counts.get(role, 0))

    def _role_rules_text(self, role_counts: Counter) -> str:
        parts = []
        if role_counts.get(Role.WOLF):
            parts.append("狼人：夜间交流并选择刀口")
        if role_counts.get(Role.SEER):
            parts.append("预言家：每晚查验一名存活玩家")
        if role_counts.get(Role.WITCH):
            parts.append("女巫：有一瓶解药和一瓶毒药，每晚最多用一瓶")
        if role_counts.get(Role.VILLAGER):
            parts.append("平民：依靠发言和投票找狼")
        return "；".join(parts)

    def _private_role_intro(self, seat: str) -> str:
        player = self.state.players[seat]
        lines = [f"你的身份是{label_role(player.role)}，所属阵营：{label_team(player.team)}。"]
        if player.role == Role.WOLF:
            teammates = [label_seat(wolf_seat) for wolf_seat in self.state.wolves(alive_only=False) if wolf_seat != seat]
            teammate_text = f"你的狼人队友：{'、'.join(teammates)}。" if teammates else "当前没有可见狼人队友。"
            lines.append(f"你的能力：每晚可与狼人队友交流，并共同选择1名非狼人玩家进行击杀。{teammate_text}")
        elif player.role == Role.SEER:
            lines.append("你的能力：每晚可以查验一名存活玩家，得知其是否为狼人。")
        elif player.role == Role.WITCH:
            lines.append("你的能力：拥有一瓶解药和一瓶毒药，每晚最多使用其中一瓶，且不能自救。")
        else:
            lines.append("你的能力：白天通过发言和投票协助好人阵营找出狼人。")
        return "".join(lines)

    def _run_night(self) -> None:
        self.state.phase = Phase.NIGHT
        self.state.current_night.reset()
        self._public(f"第 {self.state.day} 夜开始。")
        wolves = self.state.wolves(alive_only=True)
        if wolves:
            self.state.phase = Phase.WOLF_CHAT
            recipients = tuple(wolves)
            max_chat_rounds = 1 if len(wolves) == 1 else 5
            for round_idx in range(max_chat_rounds):
                prompt = self._wolf_chat_prompt(round_idx=round_idx, wolf_count=len(wolves))
                round_all_done = True
                for wolf in wolves:
                    text = self.gateway.request_speech(self.state, wolf, Audience.WOLF, prompt)
                    self._add_event(visibility=EventVisibility.PRIVATE, channel="wolf", text=text, speaker=wolf, recipients=recipients)
                    if not self._wolf_chat_done(text):
                        round_all_done = False
                if round_all_done:
                    break
            self.state.phase = Phase.WOLF_ACTION
            wolf_targets = tuple(seat for seat in self.state.living_seats() if seat not in wolves)
            wolf_specs = [ActionSpec(ActionType.NO_OP, description="今晚不击杀")]
            if wolf_targets:
                wolf_specs.insert(0, ActionSpec(ActionType.WOLF_KILL, targets=wolf_targets, requires_target=True, description="选择今晚要击杀的目标座位"))
            for wolf in wolves:
                decision = self.gateway.request_action(self.state, wolf, wolf_specs, "请选择你今晚的狼人行动。")
                self.state.current_night.wolf_votes[wolf] = decision.target
            self.state.current_night.wolf_target = resolve_wolf_target(self.state.current_night.wolf_votes, wolf_targets)
            self._add_event(visibility=EventVisibility.PRIVATE, channel="system", text=self._wolf_action_summary(), recipients=recipients, data={"votes": dict(self.state.current_night.wolf_votes), "target": self.state.current_night.wolf_target})
        seers = self.state.role_seats(Role.SEER, alive_only=True)
        if seers:
            seer = seers[0]
            self.state.phase = Phase.SEER_ACTION
            inspect_targets = tuple(seat for seat in self.state.living_seats() if seat != seer)
            if inspect_targets:
                decision = self.gateway.request_action(self.state, seer, [ActionSpec(ActionType.SEER_INSPECT, targets=inspect_targets, requires_target=True, description="查验一名存活玩家")], "请选择要查验的存活玩家。")
                self.state.current_night.seer_target = decision.target
                result = "WOLF" if self.state.players[decision.target].role == Role.WOLF else "NOT_WOLF"
                self.state.seer_results.setdefault(seer, []).append(SeerInspection(day=self.state.day, target=decision.target, result=result))
                self._add_event(visibility=EventVisibility.PRIVATE, channel="system", text=f"查验结果：{label_seat(decision.target)} 是{label_seer_result(result)}。", recipients=(seer,), data={"target": decision.target, "result": result})
        witches = self.state.role_seats(Role.WITCH, alive_only=True)
        if witches:
            witch = witches[0]
            resources = self.state.witch_resources[witch]
            self.state.phase = Phase.WITCH_ACTION
            witch_specs = [ActionSpec(ActionType.NO_OP, description="今晚不使用技能")]
            wolf_target = self.state.current_night.wolf_target
            can_save = resources.save_available and wolf_target in self.state.living_seats() and wolf_target != witch
            if can_save:
                witch_specs.insert(0, ActionSpec(ActionType.WITCH_SAVE, targets=(wolf_target,), requires_target=True, description="对狼人目标使用解药"))
            poison_targets = tuple(seat for seat in self.state.living_seats() if seat != witch)
            if resources.poison_available and poison_targets:
                witch_specs.append(ActionSpec(ActionType.WITCH_POISON, targets=poison_targets, requires_target=True, description="使用毒药毒杀一名玩家"))
            decision = self.gateway.request_action(self.state, witch, witch_specs, "请选择是否救人、下毒，或什么都不做。")
            self.state.current_night.witch_action = decision
            self._add_event(visibility=EventVisibility.PRIVATE, channel="system", text=self._witch_action_summary(decision), recipients=(witch,), data=decision.to_dict())
            if decision.action_type == ActionType.WITCH_SAVE:
                resources.save_available = False
                self.state.current_night.saved_target = decision.target
            elif decision.action_type == ActionType.WITCH_POISON:
                resources.poison_available = False
                self.state.current_night.poisoned_target = decision.target
        self.state.phase = Phase.DAWN
        deaths: dict[str, set[DeathCause]] = {}
        wolf_target = self.state.current_night.wolf_target
        if wolf_target and wolf_target != self.state.current_night.saved_target:
            deaths.setdefault(wolf_target, set()).add(DeathCause.WOLF)
        poisoned_target = self.state.current_night.poisoned_target
        if poisoned_target:
            deaths.setdefault(poisoned_target, set()).add(DeathCause.POISON)
        dead_seats = self._apply_deaths(deaths)
        if dead_seats:
            summary = "、".join(label_seat(seat) for seat in dead_seats)
            self._public(f"天亮了，死亡玩家：{summary}。")
        else:
            self._public("天亮了，昨夜无人死亡。")

    def _run_day(self) -> None:
        if self.state.winner is not None:
            return
        self.state.phase = Phase.DAY_SPEECH
        self._public(f"第 {self.state.day} 天开始讨论。")
        for seat in self.state.living_seats():
            text = self.gateway.request_speech(self.state, seat, Audience.PUBLIC, "请根据当前局势发表白天发言，长度和风格由你的阵营策略决定。")
            self._add_event(visibility=EventVisibility.PUBLIC, channel="speech", text=text, speaker=seat)
        self.state.phase = Phase.DAY_VOTE
        alive = self.state.living_seats()
        seat_specs: list[tuple[str, list[ActionSpec], str]] = []
        for seat in alive:
            targets = tuple(candidate for candidate in alive if candidate != seat)
            specs = [ActionSpec(ActionType.NO_OP, description="弃票")]
            if targets:
                specs.insert(0, ActionSpec(ActionType.DAY_VOTE, targets=targets, requires_target=True, description="投票放逐一名玩家"))
            seat_specs.append((seat, specs, "请选择你的白天投票。"))
        decisions = self.gateway.request_actions_parallel(self.state, seat_specs)
        votes: dict[str, str | None] = {}
        for seat in alive:
            decision = decisions[seat]
            votes[seat] = decision.target if decision.action_type == ActionType.DAY_VOTE else None
        vote_details = "、".join(f"{label_seat(seat)}→{label_seat(target) if target else '弃票'}" for seat, target in votes.items())
        self._public(f"投票详情：{vote_details}。")
        eliminated = resolve_vote(votes, alive)
        if eliminated is None:
            self._public("投票结果：平票或全部弃票，无人出局。")
            return
        self._apply_deaths({eliminated: {DeathCause.VOTE}})
        self._public(f"投票结果：{label_seat(eliminated)} 被放逐出局。")
        if evaluate_winner(self.state) is None:
            self._last_words(eliminated)

    def _wolf_chat_prompt(self, *, round_idx: int, wolf_count: int) -> str:
        if wolf_count == 1:
            return "你是当前唯一存活的狼人，请分析局势并决定今晚的击杀目标。若你已无补充，可直接回复：无更多讨论。"
        if round_idx == 0:
            return "【第1轮讨论】请分析当前局势，提出今晚的击杀目标建议，并说明理由。如果你目前没有更多要补充的内容，也可以直接回复：无更多讨论。"
        return f"【第{round_idx + 1}轮讨论】请回应队友建议，确认或调整击杀目标；若你已无更多讨论内容，请直接回复：无更多讨论。"

    def _wolf_chat_done(self, text: str) -> bool:
        normalized = ''.join(ch for ch in str(text).strip() if not ch.isspace())
        return normalized in {"无更多讨论", "没有更多讨论", "无更多可讨论", "无更多可补充"}

    def _wolf_action_summary(self) -> str:
        target = self.state.current_night.wolf_target
        if target:
            return f"狼人最终决定击杀{label_seat(target)}。"
        return "狼人今晚决定不击杀任何玩家。"

    def _witch_action_summary(self, decision) -> str:
        if decision.action_type == ActionType.WITCH_SAVE and decision.target:
            return f"女巫使用了解药，救下了{label_seat(decision.target)}。"
        if decision.action_type == ActionType.WITCH_POISON and decision.target:
            return f"女巫使用毒药毒杀了{label_seat(decision.target)}。"
        return "女巫决定今晚不使用技能。"

    def _apply_deaths(self, deaths: dict[str, set[DeathCause]]) -> list[str]:
        dead_seats: list[str] = []
        for seat in self.state.seat_order:
            if seat not in deaths:
                continue
            player = self.state.players[seat]
            if not player.alive:
                continue
            player.alive = False
            player.death_day = self.state.day
            player.death_causes = sorted(deaths[seat], key=lambda cause: cause.value)
            dead_seats.append(seat)
        if dead_seats:
            self.gateway.activate_god_view(self.state)
        return dead_seats

    def _finalize_if_winner(self) -> bool:
        winner = evaluate_winner(self.state)
        if winner is None:
            return False
        self.state.winner = winner
        self._public(f"达成胜利条件：{label_team(winner)}。")
        return True

    def _last_words(self, seat: str) -> None:
        self._public(f"{label_seat(seat)} 被放逐，请发表遗言。")
        text = self.gateway.request_speech(self.state, seat, Audience.PUBLIC, "你被放逐出局了，请根据场上局势发表最后的遗言。")
        self._add_event(visibility=EventVisibility.PUBLIC, channel="speech", text=text, speaker=seat)

    def _public(self, text: str) -> None:
        self._add_event(visibility=EventVisibility.PUBLIC, channel="system", text=text)

    def _add_event(self, *, visibility: EventVisibility, channel: str, text: str, speaker: str | None = None, recipients: tuple[str, ...] = (), data: dict | None = None):
        event = self.state.add_event(visibility=visibility, channel=channel, text=text, speaker=speaker, recipients=recipients, data=data)
        self.gateway.on_event(event)
        return event
