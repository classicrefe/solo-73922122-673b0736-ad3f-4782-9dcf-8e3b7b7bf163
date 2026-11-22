"""证据包打包 / 复算的端到端测试。

用例覆盖：
  - 人工名单与记录不符 → 退回；
  - 最少片段数选择（含 0 片段的平凡情形）；
  - 可留存片段不足 → 给出与人工名单不同的合规名单；
  - 复算唯一 / 无解 / 多解；
  - 同一请求内共用 10 秒预算 → 预算耗尽标为待核；
  - 证据包只含入选片段名单 + 技术台记录，不泄漏其余片段。
"""
from __future__ import annotations

import itertools
import json
import random
import time

import pytest
from fastapi.testclient import TestClient

from app.evidence import (
    _check_uniqueness,
    _synthetic_request,
    build_pack,
    recompute_pack,
)
from app.main import app
from app.schemas import EvidencePack, EvidencePackRequest, EvidenceSegmentInput
from app.solver import Budget, BudgetExhausted, RosterCpSolver
from app.verifier import verify_assignments

client = TestClient(app)

# 9 人、3 段各 200 秒：1~3 打满全场，其余 6 人各打一段，相邻两段名单不相交。
# 整节名单 = 把 6 名轮换球员分成 3 对并分配 → 共 6! / 2^3 = 90 套。
THREE_SEG_REQUEST = {
    "players": [1, 2, 3, 4, 5, 6, 7, 8, 9],
    "official_seconds": {
        "1": 600, "2": 600, "3": 600,
        "4": 200, "5": 200, "6": 200,
        "7": 200, "8": 200, "9": 200,
    },
    "segments": [
        {"duration_seconds": 200, "players_substituted": 0},
        {"duration_seconds": 200, "players_substituted": 2},
        {"duration_seconds": 200, "players_substituted": 2},
    ],
    "manual_lineup": [[1, 2, 3, 4, 5], [1, 2, 3, 6, 7], [1, 2, 3, 8, 9]],
}

# 5 人、1 段：记录已完全确定名单，0 个可留存片段即可复算。
TRIVIAL_REQUEST = {
    "players": [1, 2, 3, 4, 5],
    "official_seconds": {"1": 300, "2": 300, "3": 300, "4": 300, "5": 300},
    "segments": [{"duration_seconds": 300, "players_substituted": 0}],
    "manual_lineup": [[1, 2, 3, 4, 5]],
}


def _pack_request(**overrides):
    data = json.loads(json.dumps(THREE_SEG_REQUEST))
    return {**data, "retainable_clips": [1, 2, 3], **overrides}


def _as_evidence_pack(payload: dict) -> EvidencePack:
    return EvidencePack.model_validate(payload)


# --------------------------------------------------------------------------- #
# 打包
# --------------------------------------------------------------------------- #
def test_pack_selects_minimum_two_clips_and_recomputes_uniquely() -> None:
    resp = client.post("/api/v1/evidence/pack", json=_pack_request())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "packed"
    assert body["minimum_clips"] == 2
    assert len(body["selected_clips"]) == 2
    assert body["clips_proven_minimal"] is True
    assert body["verification_errors"] == []
    assert body["budget_exhausted"] is False

    pack = body["pack"]
    assert pack is not None
    # 证据包只含入选片段的名单
    assert [c["segment_index"] for c in pack["revealed_clips"]] == body["selected_clips"]
    # 技术台记录齐全但没有任何多余字段
    assert [s["segment_index"] for s in pack["segments"]] == [1, 2, 3]
    assert all(
        set(s) == {"segment_index", "duration_seconds", "players_substituted"}
        for s in pack["segments"]
    )
    for clip in pack["revealed_clips"]:
        assert clip["players_on_court"] == sorted(
            THREE_SEG_REQUEST["manual_lineup"][clip["segment_index"] - 1]
        )

    # 接收方只提交证据包
    recomputed = client.post("/api/v1/evidence/recompute", json=pack)
    assert recomputed.status_code == 200, recomputed.text
    rb = recomputed.json()
    assert rb["status"] == "unique"
    assert rb["solution_count_lower_bound"] == 1
    assert rb["roster"] is not None
    assert [
        c["players_on_court"] for c in rb["roster"]
    ] == [sorted(x) for x in THREE_SEG_REQUEST["manual_lineup"]]
    assert rb["verification"].startswith("已通过独立复核")
    assert rb["verification_errors"] == []


def test_pack_minimum_is_really_minimal() -> None:
    """最少性：最小片段数小于总数时，任意更小子集都必须仍多解。"""
    resp = client.post("/api/v1/evidence/pack", json=_pack_request())
    body = resp.json()
    assert body["status"] == "packed"
    minimum = body["minimum_clips"]
    assert body["clips_proven_minimal"] is True
    for clip in body["selected_clips"]:
        assert 1 <= clip <= 3
    # 暴力对照：固定 size < minimum 的任意子集，必然多解
    request = EvidencePackRequest.model_validate(_pack_request())
    synthetic = _synthetic_request(
        request.players,
        request.official_seconds,
        request.segments,
        request.manual_lineup[0],
    )
    solver = RosterCpSolver(synthetic)
    for size in range(minimum):
        for combo in itertools.combinations([1, 2, 3], size):
            fixed = {c - 1: request.manual_lineup[c - 1] for c in combo}
            check = _check_uniqueness(
                solver, request.manual_lineup, fixed, Budget(10.0)
            )
            assert check.kind == "multiple", (size, combo)


def test_pack_all_clip_subsets_consistent_with_minimum() -> None:
    """逐个固定单个片段：都必须仍多解；任意两个片段：必须唯一。"""
    resp = client.post("/api/v1/evidence/pack", json=_pack_request())
    body = resp.json()
    assert body["status"] == "packed"
    request = EvidencePackRequest.model_validate(_pack_request())
    synthetic = _synthetic_request(
        request.players,
        request.official_seconds,
        request.segments,
        request.manual_lineup[0],
    )
    solver = RosterCpSolver(synthetic)
    for size in (1, 2):
        results = []
        for combo in itertools.combinations([1, 2, 3], size):
            fixed = {c - 1: request.manual_lineup[c - 1] for c in combo}
            check = _check_uniqueness(solver, request.manual_lineup, fixed, Budget(10.0))
            results.append(check.kind)
        expected = "multiple" if size < body["minimum_clips"] else "unique"
        assert results == [expected] * len(results)


def test_pack_without_clips_when_record_is_deterministic() -> None:
    resp = client.post(
        "/api/v1/evidence/pack", json={**TRIVIAL_REQUEST, "retainable_clips": []}
    )
    body = resp.json()
    assert body["status"] == "packed"
    assert body["selected_clips"] == []
    assert body["minimum_clips"] == 0
    assert body["pack"]["revealed_clips"] == []

    rb = client.post("/api/v1/evidence/recompute", json=body["pack"]).json()
    assert rb["status"] == "unique"
    assert rb["roster"][0]["players_on_court"] == [1, 2, 3, 4, 5]


def test_pack_rejects_manual_lineup_mismatch() -> None:
    bad = _pack_request()
    bad["manual_lineup"] = [[1, 2, 3, 4, 6], [1, 2, 3, 6, 7], [1, 2, 3, 8, 9]]
    body = client.post("/api/v1/evidence/pack", json=bad).json()
    assert body["status"] == "rejected"
    assert body["verification_errors"]
    assert body["pack"] is None
    assert any("累计秒数" in e or "换人" in e or "变动" in e for e in body["verification_errors"])


def test_pack_rejects_manual_lineup_with_wrong_substitutions() -> None:
    bad = _pack_request()
    # 第 2 段换人记录是 2，但人工名单里 4 号换下、8 号换上 -> 只变动 2 人次，仍合规；
    # 把第 2 段名单改成只有 1 人不同 -> 变动 2 人次也合规，因此改官方秒数制造不符。
    bad["official_seconds"] = {str(p): 600 for p in range(1, 10)}
    body = client.post("/api/v1/evidence/pack", json=bad).json()
    assert body["status"] == "rejected"


def test_insufficient_clips_returns_compliant_alternative() -> None:
    resp = client.post("/api/v1/evidence/pack", json=_pack_request(retainable_clips=[1]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "insufficient_evidence"
    assert body["pack"] is None
    assert body["minimum_clips"] is None
    assert body["clips_proven_minimal"] is False
    assert body["differing_segments"]
    assert set(body["differing_segments"]) <= {2, 3}

    # 备选名单必须与人工名单不同、且完全符合技术台记录
    alt = [c["players_on_court"] for c in body["alternative_lineup"]]
    assert alt != THREE_SEG_REQUEST["manual_lineup"]
    assert verify_assignments(
        _synthetic_request(
            THREE_SEG_REQUEST["players"],
            THREE_SEG_REQUEST["official_seconds"],
            [EvidenceSegmentInput(**s) for s in THREE_SEG_REQUEST["segments"]],
            THREE_SEG_REQUEST["manual_lineup"][0],
        ),
        alt,
    ) == []
    # 差异片段必须真的不同
    assert all(
        set(alt[t - 1]) != set(THREE_SEG_REQUEST["manual_lineup"][t - 1])
        for t in body["differing_segments"]
    )
    # 第 1 段是可留存片段，故备选名单必须在不可留存的片段上不同
    assert 1 in [c["segment_index"] for c in body["alternative_lineup"]]


def test_pack_rejects_structurally_invalid_input() -> None:
    bad = _pack_request()
    bad["retainable_clips"] = [1, 2, 3, 4]  # 4 超出片段数
    assert client.post("/api/v1/evidence/pack", json=bad).status_code == 422

    bad = _pack_request()
    bad["retainable_clips"] = [1, 1]
    assert client.post("/api/v1/evidence/pack", json=bad).status_code == 422

    bad = _pack_request()
    bad["retainable_clips"] = [1, 2, 3, 4, 5, 6, 7]  # 超过 6 个
    assert client.post("/api/v1/evidence/pack", json=bad).status_code == 422

    bad = _pack_request()
    bad["manual_lineup"] = [[1, 2, 3, 4]] * 3  # 每段不足 5 人
    assert client.post("/api/v1/evidence/pack", json=bad).status_code == 422

    bad = _pack_request()
    bad["manual_lineup"] = bad["manual_lineup"][:2]  # 段数不符
    assert client.post("/api/v1/evidence/pack", json=bad).status_code == 422

    bad = _pack_request()
    bad["segments"][0]["players_substituted"] = 1  # 首段非 0
    assert client.post("/api/v1/evidence/pack", json=bad).status_code == 422


# --------------------------------------------------------------------------- #
# 复算
# --------------------------------------------------------------------------- #
def _pack_payload(revealed: list[int]) -> dict:
    return {
        "format_version": "1.0",
        "players": THREE_SEG_REQUEST["players"],
        "official_seconds": THREE_SEG_REQUEST["official_seconds"],
        "segments": [
            {"segment_index": t + 1, **s} for t, s in enumerate(THREE_SEG_REQUEST["segments"])
        ],
        "revealed_clips": [
            {
                "segment_index": clip,
                "players_on_court": THREE_SEG_REQUEST["manual_lineup"][clip - 1],
            }
            for clip in revealed
        ],
    }


def test_recompute_unique_only_when_sufficient() -> None:
    for revealed, expected in (([1, 2], "unique"), ([2, 3], "unique"), ([1], "multiple_solutions")):
        body = client.post("/api/v1/evidence/recompute", json=_pack_payload(revealed)).json()
        assert body["status"] == expected, revealed
        if expected == "unique":
            assert body["roster"] is not None
            assert body["suggested_clips"] == []
        else:
            assert body["roster"] is None
            assert body["solution_count_lower_bound"] >= 2
            assert body["suggested_clips"]
            assert set(body["suggested_clips"]) <= {2, 3}


def test_recompute_conflict_when_pack_unsatisfiable() -> None:
    pack = _pack_payload([1])
    # 把第 1 段名单改成 3 号不在场（其它记录仍按 3 号打满全场）
    pack["revealed_clips"][0]["players_on_court"] = [1, 2, 4, 5, 6]
    body = client.post("/api/v1/evidence/recompute", json=pack).json()
    assert body["status"] == "conflict"
    assert body["roster"] is None
    assert body["conflicts"]
    assert body["conflicts"][0]["code"] == "EVIDENCE_PACK_INFEASIBLE"


def test_recompute_rejects_malformed_pack() -> None:
    pack = _pack_payload([1])
    pack["revealed_clips"][0]["players_on_court"] = [1, 2, 3, 4]  # 不足 5 人
    assert client.post("/api/v1/evidence/recompute", json=pack).status_code == 422

    pack = _pack_payload([1])
    pack["revealed_clips"][0]["segment_index"] = 9  # 越界
    assert client.post("/api/v1/evidence/recompute", json=pack).status_code == 422

    pack = _pack_payload([1])
    pack["segments"][0]["players_substituted"] = 1  # 首段非 0
    assert client.post("/api/v1/evidence/recompute", json=pack).status_code == 422


def test_recompute_does_not_leak_roster_when_ambiguous() -> None:
    body = client.post("/api/v1/evidence/recompute", json=_pack_payload([1])).json()
    assert body["status"] == "multiple_solutions"
    assert body["roster"] is None
    # 即便多解，也只回显证据包里已有的片段
    assert [c["segment_index"] for c in body["revealed_clips"]] == [1]


# --------------------------------------------------------------------------- #
# 共用预算
# --------------------------------------------------------------------------- #
class _HungrySolver(RosterCpSolver):
    """把每次求解拖到预算上限，用来验证预算耗尽路径。"""

    def _solve(self, model, objective_var, maximize=True, x=None, *, budget=None):
        if budget is not None:
            time.sleep(min(0.25, max(0.0, budget.remaining())))
        return super()._solve(
            model, objective_var, maximize=maximize, x=x, budget=budget
        )


def test_pack_budget_exhaustion_marks_pending(monkeypatch) -> None:
    import app.evidence as evidence_module

    monkeypatch.setattr(evidence_module, "RosterCpSolver", _HungrySolver)
    request = EvidencePackRequest.model_validate(_pack_request())
    body = build_pack(request, Budget(total_seconds=0.6))
    assert body.status == "pending_review"
    assert body.budget_exhausted is True
    assert body.budget_seconds == 0.6
    assert body.elapsed_ms < 2000


def test_recompute_budget_exhaustion_marks_pending(monkeypatch) -> None:
    import app.evidence as evidence_module

    monkeypatch.setattr(evidence_module, "RosterCpSolver", _HungrySolver)
    pack = EvidencePack.model_validate(_pack_payload([1]))
    body = recompute_pack(pack, Budget(total_seconds=0.4))
    assert body.status == "pending_review"
    assert body.budget_exhausted is True
    assert body.roster is None


def test_budget_is_shared_between_solves() -> None:
    """预算对象在多个求解间累计消耗，不会每次重置。"""
    budget = Budget(total_seconds=0.4)
    time.sleep(0.3)
    assert 0 < budget.remaining() < 0.15
    with pytest.raises(BudgetExhausted):
        budget.request_seconds()


# --------------------------------------------------------------------------- #
# 与随机生成实例的对照：打包结果复算必须还原人工名单
# --------------------------------------------------------------------------- #
def _random_instance(rng: random.Random) -> dict:
    P = rng.randint(5, 9)
    T = rng.randint(1, 5)
    players = list(range(1, P + 1))
    lineups = [set(rng.sample(players, 5))]
    subs = [0]
    for _ in range(1, T):
        k = rng.randint(0, min(3, P - 5))
        cur = set(lineups[-1])
        if k == 0:
            lineups.append(set(cur))
            subs.append(0)
            continue
        outs = rng.sample(sorted(cur), k)
        ins = rng.sample(sorted(set(players) - cur), k)
        cur -= set(outs)
        cur |= set(ins)
        lineups.append(cur)
        subs.append(k)
    durations = [rng.randint(2, 8) * 100 for _ in range(T)]
    official = {p: 0 for p in players}
    for t, lineup in enumerate(lineups):
        for p in lineup:
            official[p] += durations[t]
    clips = sorted(rng.sample(range(1, T + 1), rng.randint(0, min(6, T))))
    return {
        "players": players,
        "official_seconds": {str(p): v for p, v in official.items()},
        "segments": [
            {"duration_seconds": durations[t], "players_substituted": subs[t]}
            for t in range(T)
        ],
        "manual_lineup": [sorted(x) for x in lineups],
        "retainable_clips": clips,
    }


def test_evidence_openapi_examples_are_callable() -> None:
    spec = client.get("/openapi.json").json()
    expected = {
        "/api/v1/evidence/pack": {"packable_demo", "ambiguous_demo"},
        "/api/v1/evidence/recompute": {"unique_demo", "multiple_demo"},
    }
    for path, names in expected.items():
        examples = spec["paths"][path]["post"]["requestBody"]["content"][
            "application/json"
        ]["examples"]
        assert set(examples) == names
        for name, example in examples.items():
            resp = client.post(path, json=example["value"])
            assert resp.status_code == 200, (path, name, resp.text)
            assert resp.json()["status"] in {
                "packed", "insufficient_evidence", "unique", "multiple_solutions",
                "conflict", "pending_review", "rejected",
            }


def test_large_instance_respects_budget_and_recompute_works() -> None:
    """12 人 × 16 段的规模上限：打包不得超出 10 秒预算；能给包就必须可复算。"""
    rng = random.Random(11)
    P, T = 12, 16
    players = list(range(1, P + 1))
    lineups = [set(rng.sample(players, 5))]
    subs = [0]
    for _ in range(1, T):
        k = rng.randint(1, 4)
        cur = set(lineups[-1])
        outs = rng.sample(sorted(cur), k)
        ins = rng.sample(sorted(set(players) - cur), k)
        cur -= set(outs)
        cur |= set(ins)
        lineups.append(cur)
        subs.append(k)
    durations = [rng.randint(30, 180) for _ in range(T)]
    official = {p: 0 for p in players}
    for t, lineup in enumerate(lineups):
        for p in lineup:
            official[p] += durations[t]
    request = {
        "players": players,
        "official_seconds": {str(p): v for p, v in official.items()},
        "segments": [
            {"duration_seconds": durations[t], "players_substituted": subs[t]}
            for t in range(T)
        ],
        "manual_lineup": [sorted(x) for x in lineups],
        "retainable_clips": [1, 4, 8, 12, 15, 16],
    }

    started = time.perf_counter()
    body = client.post("/api/v1/evidence/pack", json=request).json()
    elapsed = time.perf_counter() - started
    assert elapsed < 11.5, f"打包耗时 {elapsed:.2f}s 超出 10 秒预算（含收尾余量）"
    assert body["status"] in {"packed", "pending_review", "insufficient_evidence"}
    assert body["verification_errors"] == []

    if body["pack"] is not None:
        recomputed = client.post("/api/v1/evidence/recompute", json=body["pack"]).json()
        assert recomputed["status"] == "unique"
        assert [c["players_on_court"] for c in recomputed["roster"]] == [
            sorted(x) for x in lineups
        ]


def test_random_instances_pack_then_recompute() -> None:
    rng = random.Random(20261122)
    packed = failed = 0
    for _ in range(30):
        instance = _random_instance(rng)
        resp = client.post("/api/v1/evidence/pack", json=instance)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["verification_errors"] == []
        if body["status"] == "pending_review":
            continue
        if body["status"] == "insufficient_evidence":
            failed += 1
            alt = [c["players_on_court"] for c in body["alternative_lineup"]]
            assert alt != instance["manual_lineup"]
            continue
        assert body["status"] == "packed"
        packed += 1
        assert body["minimum_clips"] == len(body["selected_clips"])
        assert body["minimum_clips"] <= len(instance["retainable_clips"])
        rb = client.post("/api/v1/evidence/recompute", json=body["pack"]).json()
        assert rb["status"] == "unique"
        assert [c["players_on_court"] for c in rb["roster"]] == instance["manual_lineup"]

    assert packed > 5
    assert packed + failed > 10
