"""请求 / 响应 Pydantic 模型。

结构层面的非法输入（数量越界、ID 不存在、重复 ID 等）在本层校验，FastAPI 返回 422；
记录之间的语义冲突（秒数对不上、换人记录矛盾等）在求解阶段诊断，返回 200 + conflicts。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_PLAYERS = 12
MIN_PLAYERS = 5
LINEUP_SIZE = 5
MAX_SEGMENTS = 16
MAX_RETAINABLE_CLIPS = 6

VerdictStatus = Literal["guaranteed", "possible", "impossible", "infeasible_record"]

PackBuildStatus = Literal[
    "packed", "insufficient_evidence", "rejected", "conflict", "pending_review"
]
RecomputeStatus = Literal[
    "unique", "conflict", "multiple_solutions", "pending_review"
]


class SegmentInput(BaseModel):
    """按换人切分的一个有序片段。"""

    duration_seconds: int = Field(..., ge=1, description="片段时长（整秒），>=1")
    confirmed_players: list[int] = Field(
        default_factory=list, description="该片段已确认在场的球员 ID"
    )
    players_substituted: int = Field(
        ..., ge=0, le=5, description="相对前段的换下人数（首段必须为 0）"
    )
    team_net_points: int = Field(
        ..., description="本队该片段得失分（净胜分，可为负）"
    )

    @field_validator("confirmed_players")
    @classmethod
    def _confirmed_unique(cls, v: list[int]) -> list[int]:
        if len(v) != len(set(v)):
            raise ValueError("confirmed_players 不能包含重复球员")
        if len(v) > LINEUP_SIZE:
            raise ValueError("单片段已确认球员不能超过 5 人")
        return v


class RosterCheckRequest(BaseModel):
    """阵容达标核对请求。"""

    players: list[int] = Field(..., description="本节进入过名单的全部球员 ID（5~12 人）")
    target_lineup: list[int] = Field(..., description="目标五人阵容 ID（恰好 5 人）")
    minimum_seconds: int = Field(..., ge=0, description="最低同场上场时长（整秒）")
    official_seconds: dict[int, int] = Field(
        ..., description="官方逐人本节累计在场秒数，键为球员 ID"
    )
    segments: list[SegmentInput] = Field(..., description="按换人切分的有序片段（1~16 段）")

    @field_validator("players")
    @classmethod
    def _players_unique(cls, v: list[int]) -> list[int]:
        if not (MIN_PLAYERS <= len(v) <= MAX_PLAYERS):
            raise ValueError(f"players 人数必须在 {MIN_PLAYERS}~{MAX_PLAYERS} 之间")
        if len(v) != len(set(v)):
            raise ValueError("players 不能包含重复球员")
        if any(p < 1 for p in v):
            raise ValueError("球员 ID 必须为正整数")
        return v

    @field_validator("target_lineup")
    @classmethod
    def _target_unique(cls, v: list[int]) -> list[int]:
        if len(v) != LINEUP_SIZE:
            raise ValueError("target_lineup 必须恰好为 5 人")
        if len(v) != len(set(v)):
            raise ValueError("target_lineup 不能包含重复球员")
        return v

    @model_validator(mode="after")
    def _cross_check(self) -> RosterCheckRequest:
        roster = set(self.players)

        if not set(self.target_lineup) <= roster:
            raise ValueError("target_lineup 必须全部来自 players")

        if set(self.official_seconds.keys()) != roster:
            raise ValueError("official_seconds 的键必须与 players 完全一致")
        if any(v < 0 for v in self.official_seconds.values()):
            raise ValueError("official_seconds 不能为负")

        if not (1 <= len(self.segments) <= MAX_SEGMENTS):
            raise ValueError(f"segments 数量必须在 1~{MAX_SEGMENTS} 之间")

        for idx, seg in enumerate(self.segments):
            unknown = set(seg.confirmed_players) - roster
            if unknown:
                raise ValueError(f"第 {idx + 1} 段出现名单外球员: {sorted(unknown)}")
            if idx == 0 and seg.players_substituted != 0:
                raise ValueError("首段 players_substituted 必须为 0")
        return self


class SegmentAssignment(BaseModel):
    """见证名单中的逐段结果。"""

    segment_index: int
    duration_seconds: int
    players_on_court: list[int]
    confirmed_players: list[int]
    confirmed_included: bool
    target_all_on_court: bool
    team_net_points: int


class WitnessRoster(BaseModel):
    """一套完整逐段名单及其复核数据。"""

    label: str
    solver_status: str
    shared_seconds: int
    net_points: int
    meets_minimum: bool
    assignments: list[SegmentAssignment]
    player_total_seconds: dict[str, int]
    substitution_counts: list[int]


class NetBounds(BaseModel):
    """净胜分上下界及对应见证名单。"""

    min_net_points: int
    max_net_points: int
    min_witness: WitnessRoster
    max_witness: WitnessRoster


class CheckResponse(BaseModel):
    status: VerdictStatus
    status_label_zh: str
    target_lineup: list[int]
    minimum_seconds: int
    feasible: bool
    shared_seconds_min: int | None = None
    shared_seconds_max: int | None = None
    bounds_proven_optimal: bool = True
    net_bounds_all_feasible: NetBounds | None = None
    net_bounds_among_qualifying: NetBounds | None = None
    verification: str | None = None
    verification_errors: list[str] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    solver_runs: int = 0
    elapsed_ms: float


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str


# --------------------------------------------------------------------------- #
# 证据包：档案员打包 / 接收方复算
# --------------------------------------------------------------------------- #
class EvidenceSegmentInput(BaseModel):
    """技术台记录的单个片段：只保留复算所需的最少字段。"""

    duration_seconds: int = Field(..., ge=1, description="片段时长（整秒），>=1")
    players_substituted: int = Field(
        ..., ge=0, le=5, description="相对前段的换下人数（首段必须为 0）"
    )


class EvidencePackRequest(BaseModel):
    """档案员打包请求：单队、单节。"""

    players: list[int] = Field(..., description="本节进入过名单的全部球员 ID（5~12 人）")
    official_seconds: dict[int, int] = Field(
        ..., description="官方逐人本节累计在场秒数，键为球员 ID"
    )
    segments: list[EvidenceSegmentInput] = Field(
        ..., description="技术台按换人切分的有序片段（1~16 段）"
    )
    manual_lineup: list[list[int]] = Field(
        ...,
        description="人工确认的完整逐段名单，长度必须与 segments 一致，每段恰好 5 人",
    )
    retainable_clips: list[int] = Field(
        default_factory=list,
        description=(
            f"最多 {MAX_RETAINABLE_CLIPS} 个可留存片段号（1 起，按片段顺序），"
            "证据包只会从这些片段里选取"
        ),
    )

    @field_validator("players")
    @classmethod
    def _players_unique(cls, v: list[int]) -> list[int]:
        if not (MIN_PLAYERS <= len(v) <= MAX_PLAYERS):
            raise ValueError(f"players 人数必须在 {MIN_PLAYERS}~{MAX_PLAYERS} 之间")
        if len(v) != len(set(v)):
            raise ValueError("players 不能包含重复球员")
        if any(p < 1 for p in v):
            raise ValueError("球员 ID 必须为正整数")
        return v

    @model_validator(mode="after")
    def _cross_check(self) -> EvidencePackRequest:
        roster = set(self.players)

        if set(self.official_seconds.keys()) != roster:
            raise ValueError("official_seconds 的键必须与 players 完全一致")
        if any(v < 0 for v in self.official_seconds.values()):
            raise ValueError("official_seconds 不能为负")

        if not (1 <= len(self.segments) <= MAX_SEGMENTS):
            raise ValueError(f"segments 数量必须在 1~{MAX_SEGMENTS} 之间")
        if len(self.manual_lineup) != len(self.segments):
            raise ValueError("manual_lineup 长度必须与 segments 一致")

        for idx, seg in enumerate(self.segments):
            if idx == 0 and seg.players_substituted != 0:
                raise ValueError("首段 players_substituted 必须为 0")

        for idx, lineup in enumerate(self.manual_lineup):
            if len(lineup) != LINEUP_SIZE:
                raise ValueError(f"manual_lineup 第 {idx + 1} 段必须恰好 5 人")
            if len(set(lineup)) != len(lineup):
                raise ValueError(f"manual_lineup 第 {idx + 1} 段不能包含重复球员")
            unknown = set(lineup) - roster
            if unknown:
                raise ValueError(
                    f"manual_lineup 第 {idx + 1} 段出现名单外球员: {sorted(unknown)}"
                )

        if len(self.retainable_clips) > MAX_RETAINABLE_CLIPS:
            raise ValueError(f"retainable_clips 最多 {MAX_RETAINABLE_CLIPS} 个")
        if len(set(self.retainable_clips)) != len(self.retainable_clips):
            raise ValueError("retainable_clips 不能重复")
        for clip in self.retainable_clips:
            if not (1 <= clip <= len(self.segments)):
                raise ValueError(
                    f"retainable_clips 中的 {clip} 不是有效片段号（1~{len(self.segments)}）"
                )
        return self


class SegmentLineup(BaseModel):
    """某个片段的一套完整在场名单。"""

    segment_index: int = Field(..., ge=1, description="片段号（1 起）")
    players_on_court: list[int] = Field(..., description="该片段在场 5 人")


class EvidencePackSegment(BaseModel):
    """证据包内的技术台记录（不含任何未入选片段的名单）。"""

    segment_index: int
    duration_seconds: int
    players_substituted: int


class EvidencePack(BaseModel):
    """接收方拿到的全部证据：技术台记录 + 入选片段的完整名单。"""

    format_version: str = Field(default="1.0", description="证据包格式版本")
    players: list[int]
    official_seconds: dict[int, int]
    segments: list[EvidencePackSegment]
    revealed_clips: list[SegmentLineup] = Field(
        default_factory=list, description="入选留存片段的完整在场名单"
    )

    @model_validator(mode="after")
    def _cross_check(self) -> EvidencePack:
        roster = set(self.players)
        if not (MIN_PLAYERS <= len(roster) <= MAX_PLAYERS):
            raise ValueError(f"players 人数必须在 {MIN_PLAYERS}~{MAX_PLAYERS} 之间")
        if len(roster) != len(self.players):
            raise ValueError("players 不能包含重复球员")
        if set(self.official_seconds.keys()) != roster:
            raise ValueError("official_seconds 的键必须与 players 完全一致")
        if any(v < 0 for v in self.official_seconds.values()):
            raise ValueError("official_seconds 不能为负")
        if not (1 <= len(self.segments) <= MAX_SEGMENTS):
            raise ValueError(f"segments 数量必须在 1~{MAX_SEGMENTS} 之间")
        if [s.segment_index for s in self.segments] != list(
            range(1, len(self.segments) + 1)
        ):
            raise ValueError("segments 的 segment_index 必须按 1..N 连续编号")
        if self.segments[0].players_substituted != 0:
            raise ValueError("首段 players_substituted 必须为 0")

        seen: set[int] = set()
        for clip in self.revealed_clips:
            if not (1 <= clip.segment_index <= len(self.segments)):
                raise ValueError(
                    f"revealed_clips 的片段号 {clip.segment_index} 超出范围"
                )
            if clip.segment_index in seen:
                raise ValueError("revealed_clips 不能重复包含同一片段")
            seen.add(clip.segment_index)
            if len(clip.players_on_court) != LINEUP_SIZE:
                raise ValueError(
                    f"revealed_clips 第 {clip.segment_index} 段必须恰好 5 人"
                )
            if len(set(clip.players_on_court)) != LINEUP_SIZE:
                raise ValueError(
                    f"revealed_clips 第 {clip.segment_index} 段不能包含重复球员"
                )
            unknown = set(clip.players_on_court) - roster
            if unknown:
                raise ValueError(
                    f"revealed_clips 第 {clip.segment_index} 段出现名单外球员: "
                    f"{sorted(unknown)}"
                )
        return self


class EvidencePackBuildResponse(BaseModel):
    status: PackBuildStatus
    status_label_zh: str
    message: str
    selected_clips: list[int] = Field(default_factory=list)
    minimum_clips: int | None = Field(
        default=None, description="能唯一复算所需的最少可留存片段数；无解时为 None"
    )
    clips_proven_minimal: bool = False
    pack: EvidencePack | None = None
    alternative_lineup: list[SegmentLineup] | None = Field(
        default=None,
        description=(
            "所有可留存片段都用上仍多解时，给出的一套与人工名单不同、"
            "但完全符合技术台记录的名单（供补查录像）"
        ),
    )
    differing_segments: list[int] = Field(
        default_factory=list, description="备选名单与人工名单不同的片段号"
    )
    manual_verification: str | None = None
    verification_errors: list[str] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    solver_runs: int = 0
    elapsed_ms: float = 0.0
    budget_seconds: float = 0.0
    budget_exhausted: bool = False


class EvidenceRecomputeResponse(BaseModel):
    status: RecomputeStatus
    status_label_zh: str
    message: str
    revealed_clips: list[SegmentLineup] = Field(default_factory=list)
    roster: list[SegmentLineup] | None = Field(
        default=None, description="仅在名单唯一时返回的完整逐段名单"
    )
    solution_count_lower_bound: int | None = Field(
        default=None, description="至少存在多少套完整名单（唯一=1，多解时>=2）"
    )
    suggested_clips: list[int] = Field(
        default_factory=list, description="多解时建议补充留存的片段号"
    )
    verification: str | None = None
    verification_errors: list[str] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    solver_runs: int = 0
    elapsed_ms: float = 0.0
    budget_seconds: float = 0.0
    budget_exhausted: bool = False
