"""证据包：档案员打包 / 接收方复算。

打包侧：
  1. 用现有独立复核器校验人工确认的完整逐段名单；不符即退回；
  2. 在“可留存片段”里找能唯一确定整节名单的最小子集：
     单调性保证“固定更多片段只会更容易唯一”，于是先用全集判定是否足够，
     再降序收缩得到唯一候选，最后用已知反例的“差异片段命中集”下界逐层验证最少性；
  3. 若把全部可留存片段都用上仍多解，给出一套与人工名单不同的合规名单。

复算侧：
  1. 仅凭证据包（技术台记录 + 入选片段名单）重建约束；
  2. 0 套 → 冲突；恰好 1 套 → 返回完整名单；≥2 套 → 提示补证；
  3. 同一请求内所有求解共用 10 秒预算，耗尽且结论未定时标为待核。
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

from .schemas import (
    EvidencePack,
    EvidencePackBuildResponse,
    EvidencePackRequest,
    EvidencePackSegment,
    EvidenceRecomputeResponse,
    EvidenceSegmentInput,
    RosterCheckRequest,
    SegmentInput,
    SegmentLineup,
)
from .solver import Budget, BudgetExhausted, RosterCpSolver
from .verifier import verify_assignments

REQUEST_BUDGET_SECONDS = 10.0
INFEASIBLE = {"INFEASIBLE"}
UNDETERMINED = {"UNKNOWN"}

PACK_STATUS_ZH = {
    "packed": "已打包",
    "insufficient_evidence": "可留存片段不足",
    "rejected": "人工名单与记录不符，已退回",
    "conflict": "技术台记录自身冲突",
    "pending_review": "求解预算耗尽，待核",
}

RECOMPUTE_STATUS_ZH = {
    "unique": "名单唯一",
    "conflict": "复算无解：证据包与记录冲突",
    "multiple_solutions": "名单不唯一，请补充证据",
    "pending_review": "求解预算耗尽，待核",
}


def _synthetic_request(
    players: list[int],
    official_seconds: dict[int, int],
    segments: list[EvidenceSegmentInput],
    target_lineup: list[int],
) -> RosterCheckRequest:
    """把证据数据包装成本项目现有的请求模型，以便复用求解器与复核器。

    证据包不包含“已确认球员”与“得失分”，因此这两项填空值；
    校验仍覆盖每段 5 人、人员范围、官方秒数与换人记录。
    """
    return RosterCheckRequest(
        players=players,
        target_lineup=target_lineup,
        minimum_seconds=0,
        official_seconds=official_seconds,
        segments=[
            SegmentInput(
                duration_seconds=s.duration_seconds,
                confirmed_players=[],
                players_substituted=s.players_substituted,
                team_net_points=0,
            )
            for s in segments
        ],
    )


def _lineup_views(lineups: list[list[int]]) -> list[SegmentLineup]:
    return [
        SegmentLineup(segment_index=t + 1, players_on_court=sorted(lineup))
        for t, lineup in enumerate(lineups)
    ]


@dataclass
class _RevealCheck:
    """一次“固定若干片段后是否唯一”的判定结果。"""

    kind: str  # unique / multiple / undetermined
    alternatives: list[list[list[int]]] = field(default_factory=list)


def _check_uniqueness(
    solver: RosterCpSolver,
    reference: list[list[int]],
    fixed: dict[int, list[int]],
    budget: Budget,
) -> _RevealCheck:
    """判定固定这些片段后，参考名单是否唯一。

    判为多解时顺便收集至多 3 套其他名单，供命中集剪枝使用（一次建模多次求解）。
    """
    try:
        found, status = solver.find_alternatives(
            reference,
            budget=budget,
            fixed_lineups=fixed,
            hint_lineups=reference,
        )
    except BudgetExhausted:
        return _RevealCheck("undetermined")
    if status in INFEASIBLE and not found:
        return _RevealCheck("unique")
    if found:
        return _RevealCheck("multiple", alternatives=found)
    return _RevealCheck("undetermined")


def _min_hitting_set_size(sets: list[frozenset[int]], universe: list[int]) -> int:
    """所有已知备选方案的差异片段集合的最小命中集大小（整数下界）。"""
    if not sets:
        return 0
    for size in range(len(universe) + 1):
        for combo in itertools.combinations(universe, size):
            chosen = set(combo)
            if all(chosen & s for s in sets):
                return size
    return len(universe)


def _is_hitting(subset: set[int], sets: list[frozenset[int]]) -> bool:
    return all(subset & s for s in sets)


def build_pack(
    req: EvidencePackRequest, budget: Budget | None = None
) -> EvidencePackBuildResponse:
    started = time.perf_counter()
    budget = budget or Budget(total_seconds=REQUEST_BUDGET_SECONDS)
    synthetic = _synthetic_request(
        req.players, req.official_seconds, req.segments, req.manual_lineup[0]
    )
    solver = RosterCpSolver(synthetic)

    def response(**kwargs) -> EvidencePackBuildResponse:
        kwargs.setdefault("status_label_zh", PACK_STATUS_ZH[kwargs["status"]])
        return EvidencePackBuildResponse(
            elapsed_ms=(time.perf_counter() - started) * 1000,
            budget_seconds=budget.total_seconds,
            solver_runs=solver.runs,
            **kwargs,
        )

    # ---------- 第 1 步：沿用现有独立复核器校验人工名单 ----------
    manual_errors = verify_assignments(synthetic, req.manual_lineup)
    if manual_errors:
        return response(
            status="rejected",
            message="人工确认的逐段名单与技术台记录不符，已退回；请修正后重新打包",
            manual_verification="未通过独立复核",
            verification_errors=manual_errors,
            conflicts=solver.precheck().conflicts,
        )

    # ---------- 第 2 步：记录有效性（人工名单已通过复核，通常无冲突） ----------
    pre_diag = solver.precheck()
    if pre_diag.has_conflict:
        return response(
            status="conflict",
            message="技术台记录自身存在冲突，无法复算",
            manual_verification="人工名单通过复核，但记录存在结构性冲突",
            conflicts=pre_diag.conflicts,
        )

    # ---------- 第 3 步：在可留存片段里找最少片段数的最小子集 ----------
    # 单调性：子集唯一 ⇒ 其任何超集唯一；子集多解 ⇒ 其任何子集多解。
    #   a) 先判定全部可留存片段是否足以唯一（固定片段最多，通常最快）；
    #      如果不足，直接给备选名单；
    #   b) 从全集出发逐个尝试去掉片段，快速得到一个“不含冗余”的唯一候选；
    #   c) 每发现一套其他名单，其差异片段集合必须被所选片段命中，
    #      因此命中集大小是所需片段数的下界；从下界起逐层验证，
    #      第一个唯一子集即为最少片段数解（clips_proven_minimal=True）；
    #   d) 预算耗尽仍无法确认最少性 → 待核，并附上已得到的候选片段。
    clips = sorted(set(req.retainable_clips))
    # 每套已知其他名单：(与人工名单不同的片段集合, 该名单本身)
    alternatives: list[tuple[frozenset[int], list[list[int]]]] = []

    def fixed_for(subset: tuple[int, ...]) -> dict[int, list[int]]:
        return {clip - 1: req.manual_lineup[clip - 1] for clip in subset}

    def check(subset: tuple[int, ...]) -> _RevealCheck:
        result = _check_uniqueness(
            solver, req.manual_lineup, fixed_for(subset), budget
        )
        for alternative in result.alternatives:
            alternatives.append(
                (
                    frozenset(
                        t + 1
                        for t in range(len(alternative))
                        if set(alternative[t]) != set(req.manual_lineup[t])
                    ),
                    alternative,
                )
            )
        return result

    def lower_bound() -> int:
        return _min_hitting_set_size([a[0] for a in alternatives], clips)

    full = check(tuple(clips))
    if full.kind == "undetermined":
        return _pending_response(response, req, tuple(clips), confirmed_unique=False)
    if full.kind == "multiple":
        return _insufficient_response(response, req, clips, alternatives)

    # b) 降序收缩，得到不含冗余的唯一候选
    kernel = list(clips)
    for clip in reversed(clips):
        if len(kernel) <= 1:
            break
        trial = tuple(c for c in kernel if c != clip)
        result = check(trial)
        if result.kind == "undetermined":
            return _pending_response(response, req, tuple(kernel), confirmed_unique=True)
        if result.kind == "unique":
            kernel = list(trial)

    # c) 用已知反例的命中集下界逐层验证最少性
    size = lower_bound()
    while size < len(kernel):
        found: tuple[int, ...] | None = None
        for combo in itertools.combinations(clips, size):
            if not _is_hitting(set(combo), [a[0] for a in alternatives]):
                continue  # 已知存在未命中的其他名单，必然不唯一
            result = check(combo)
            if result.kind == "undetermined":
                return _pending_response(
                    response, req, tuple(kernel), confirmed_unique=True
                )
            if result.kind == "unique":
                found = combo
                break
        if found is not None:
            kernel = list(found)
            break
        size = max(size + 1, lower_bound())

    return _packed_response(response, req, sorted(kernel), proven_minimal=True)


def _pending_response(
    response,
    req: EvidencePackRequest,
    candidate: tuple[int, ...],
    *,
    confirmed_unique: bool,
) -> EvidencePackBuildResponse:
    """预算耗尽：如实标注待核。

    若候选片段已被证实能唯一复算（confirmed_unique=True），仍把证据包交给档案员，
    但明确说明“未证实片段数最少”；否则只给候选片段号。
    """
    return response(
        status="pending_review",
        message=(
            "共用 10 秒求解预算已耗尽，尚未证实所选片段数是最少的；"
            + (
                "evidence_pack 中的证据已确认可唯一复算，可先行使用；"
                "请稍后重试以取得最少性证明"
                if confirmed_unique
                else "请稍后重试"
            )
        ),
        manual_verification="已通过独立复核",
        selected_clips=list(candidate),
        clips_proven_minimal=False,
        pack=_build_pack_payload(req, list(candidate)) if confirmed_unique else None,
        budget_exhausted=True,
    )


def _packed_response(
    response, req: EvidencePackRequest, selected: list[int], *, proven_minimal: bool
) -> EvidencePackBuildResponse:
    return response(
        status="packed",
        message=(
            f"已选出最少 {len(selected)} 个可留存片段，"
            "接收方据此可唯一复算出整节名单"
            if selected
            else "无需任何录像片段，仅凭技术台记录即可唯一复算出整节名单"
        ),
        manual_verification="已通过独立复核",
        selected_clips=selected,
        minimum_clips=len(selected),
        clips_proven_minimal=proven_minimal,
        pack=_build_pack_payload(req, selected),
    )


def _insufficient_response(
    response,
    req: EvidencePackRequest,
    clips: list[int],
    alternatives: list[tuple[frozenset[int], list[list[int]]]],
) -> EvidencePackBuildResponse:
    """全部可留存片段仍多解：给出一套与人工名单不同的合规名单供补查。"""
    usable = [a for a in alternatives if a[0]]
    if not usable:
        return response(
            status="pending_review",
            message="未能取得与人工名单不同的备选名单，请重试",
            manual_verification="已通过独立复核",
            selected_clips=clips,
            budget_exhausted=True,
        )
    # 取差异片段最少的一套，便于档案员只补查少量录像
    differing, alternative = min(usable, key=lambda a: (len(a[0]), sorted(a[0])))
    return response(
        status="insufficient_evidence",
        message=(
            f"全部 {len(clips)} 个可留存片段都用上后整节名单仍不唯一；"
            "已给出一套不同于人工名单的合规名单，供补查录像锁定差异片段"
        ),
        manual_verification="已通过独立复核",
        selected_clips=clips,
        clips_proven_minimal=False,
        alternative_lineup=_lineup_views(alternative),
        differing_segments=sorted(differing),
    )


def _build_pack_payload(req: EvidencePackRequest, selected: list[int]) -> EvidencePack:
    return EvidencePack(
        players=req.players,
        official_seconds=req.official_seconds,
        segments=[
            EvidencePackSegment(
                segment_index=t + 1,
                duration_seconds=s.duration_seconds,
                players_substituted=s.players_substituted,
            )
            for t, s in enumerate(req.segments)
        ],
        revealed_clips=[
            SegmentLineup(
                segment_index=clip,
                players_on_court=sorted(req.manual_lineup[clip - 1]),
            )
            for clip in selected
        ],
    )


def recompute_pack(
    pack: EvidencePack, budget: Budget | None = None
) -> EvidenceRecomputeResponse:
    started = time.perf_counter()
    budget = budget or Budget(total_seconds=REQUEST_BUDGET_SECONDS)

    segments = [
        EvidenceSegmentInput(
            duration_seconds=s.duration_seconds,
            players_substituted=s.players_substituted,
        )
        for s in pack.segments
    ]
    # 目标阵容只用于构造合成请求，证据包本身不含目标阵容；任取前 5 人。
    synthetic = _synthetic_request(
        pack.players, pack.official_seconds, segments, pack.players[:5]
    )
    solver = RosterCpSolver(synthetic)

    def response(**kwargs) -> EvidenceRecomputeResponse:
        kwargs.setdefault("status_label_zh", RECOMPUTE_STATUS_ZH[kwargs["status"]])
        return EvidenceRecomputeResponse(
            elapsed_ms=(time.perf_counter() - started) * 1000,
            budget_seconds=budget.total_seconds,
            solver_runs=solver.runs,
            revealed_clips=[
                SegmentLineup(segment_index=c.segment_index, players_on_court=c.players_on_court)
                for c in pack.revealed_clips
            ],
            **kwargs,
        )

    fixed: dict[int, list[int]] = {}
    for clip in pack.revealed_clips:
        fixed[clip.segment_index - 1] = clip.players_on_court

    # ---------- 第 1 步：至少存在一套名单吗？ ----------
    try:
        base = solver.feasible(budget=budget, fixed_lineups=fixed)
    except BudgetExhausted:
        return response(
            status="pending_review",
            message="共用求解预算已耗尽，尚未能判定证据包是否有解",
            budget_exhausted=True,
        )
    if base.status_name in INFEASIBLE:
        conflicts = solver.precheck().conflicts or [
            {
                "code": "EVIDENCE_PACK_INFEASIBLE",
                "message": (
                    "证据包中的技术台记录与入选片段名单无法同时成立，"
                    "不存在任何完整名单与之相符"
                ),
            }
        ]
        return response(
            status="conflict",
            message="复算无解：证据包内部冲突，无法得到任何完整名单",
            conflicts=conflicts,
        )
    if base.status_name in UNDETERMINED or base.assignments is None:
        return response(
            status="pending_review",
            message="求解器未能给出确定结论，请重试",
            budget_exhausted=True,
        )

    # ---------- 第 2 步：名单是否唯一？ ----------
    try:
        alt = solver.has_alternative(
            base.assignments, budget=budget, fixed_lineups=fixed
        )
    except BudgetExhausted:
        return response(
            status="pending_review",
            message="共用求解预算已耗尽，尚未能证实名单是否唯一",
            budget_exhausted=True,
        )
    if alt.status_name in UNDETERMINED:
        return response(
            status="pending_review",
            message="求解器未能证实名单唯一性，请重试",
            budget_exhausted=True,
        )

    if alt.status_name in INFEASIBLE:
        # 唯一：用独立复核器再核对一遍
        errors = verify_assignments(synthetic, base.assignments)
        if errors:
            return response(
                status="conflict",
                message="复算得到的唯一名单未通过独立复核",
                conflicts=[
                    {
                        "code": "RECOMPUTED_ROSTER_FAILED_VERIFICATION",
                        "message": "复核器发现求解结果与记录不一致",
                        "errors": errors,
                    }
                ],
            )
        return response(
            status="unique",
            message="证据包足以唯一复算整节名单",
            roster=_lineup_views(base.assignments),
            solution_count_lower_bound=1,
            verification="已通过独立复核：唯一名单满足技术台记录与入选片段",
        )

    # ---------- 第 3 步：多解，只提示补证，不返回完整名单 ----------
    assert alt.assignments is not None
    revealed = {c.segment_index for c in pack.revealed_clips}
    differing = [
        t + 1
        for t in range(len(alt.assignments))
        if set(alt.assignments[t]) != set(base.assignments[t])
    ]
    suggested = [t for t in differing if t not in revealed]
    return response(
        status="multiple_solutions",
        message=(
            "证据包仍不足以唯一复算，至少存在两套名单；"
            "请补充留存建议片段后再提交（未返回完整名单）"
        ),
        solution_count_lower_bound=2,
        suggested_clips=suggested,
    )
