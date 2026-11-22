# 篮球五人阵容达标核对 API

根据**官方逐人在场秒数**、**按换人切分的有序片段**（时长 / 已确认球员 / 相对前段换下人数 / 本队得失分），
用约束规划（Google OR-Tools CP-SAT）判定一套目标五人阵容能否达到最低同场上场时长，
并在达标名单中计算净胜分上下界，每个边界都附一套完整的逐段名单供独立复核。

## 判定语义

| 状态 | 含义 |
| --- | --- |
| `guaranteed` 必定达标 | 所有满足原始记录的名单，目标五人同场秒数都达到阈值 |
| `possible` 可能达标 | 存在达标名单，也存在不达标名单 |
| `impossible` 无法达标 | 原始记录可行，但没有任何名单达到阈值 |
| `infeasible_record` 原始记录冲突 | 约束无法同时成立；只返回冲突说明，不给达标结论 |

## 建模口径

- 变量 `x[t,p]`：片段 `t` 球员 `p` 是否在场（布尔）。
- 硬约束：
  1. 每段恰有 5 人，且包含该段全部已确认球员；
  2. 逐人累计在场秒数 `Σ duration[t]·x[t,p]` 等于官方秒数；
  3. 相邻片段变动人次 `Σ (x[t,p] XOR x[t-1,p])` 等于 `2 × 换下人数`
     （换下与换上人数相等，按双边人次计数；首段换下人数必须为 0）。
- 派生：
  - `z[t]`：目标五人是否在片段 `t` 同场；
  - 同场秒数 `S = Σ duration[t]·z[t]`；
  - 净胜分 `N = Σ team_net_points[t]·z[t]`（只累计目标五人同场的片段）。
- 对 `S`、`N` 分别最小化 / 最大化得到严格边界；达标名单的净胜分边界通过追加
  `S ≥ minimum_seconds` 约束后再次优化得到。
- 规模上限：12 名球员、16 个片段。

## 响应里的复核材料

每个净胜分边界都附带一套**完整逐段见证名单**，包含：

- 每段在场 5 人、已确认球员是否都在、目标五人是否同场；
- 逐人累计秒数（可直接与 `official_seconds` 对账）；
- 逐段实际换下人数（可与 `players_substituted` 对账）；
- 该名单的同场秒数与净胜分。

服务端在返回前会用**不依赖求解器的独立复核器**（纯 Python 重算全部约束）校验每套见证名单，
结果写在 `verification` / `verification_errors` 字段。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 交互式文档（含可直接调用的请求示例）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 阵容核对：`POST /api/v1/roster/check`
- 证据包打包：`POST /api/v1/evidence/pack`
- 证据包复算：`POST /api/v1/evidence/recompute`

## 证据包：少留录像，仍可复算整节名单

场景：档案员手上有一份**人工确认的完整逐段名单**，但只愿意长期保留少量录像片段。
证据包的目标是——只交出**入选片段的名单 + 技术台记录**，接收方就能把整节名单唯一复算出来。

### 打包 `POST /api/v1/evidence/pack`

请求（单队、单节）：

| 字段 | 说明 |
| --- | --- |
| `players` | 本节进入过名单的全部球员 ID（5~12 人） |
| `official_seconds` | 官方逐人本节累计在场秒数 |
| `segments` | 逐段 `duration_seconds` + `players_substituted`（换下人数） |
| `manual_lineup` | 人工确认的完整逐段名单，每段恰好 5 人 |
| `retainable_clips` | 最多 6 个**可留存片段号**（1 起），证据包只从这些片段里选 |

处理顺序：

1. 用项目现有的**独立复核器**核对人工名单与技术台记录；不符即 `rejected` 退回，附具体错误；
2. 记录本身有结构性冲突时返回 `conflict`；
3. 在 `retainable_clips` 中找**片段数最少**的一组，使整节名单唯一；
   同时验证更小子集都仍多解，因此 `clips_proven_minimal=true` 是已证明的最少性；
4. 全部可留存片段都用上仍多解时返回 `insufficient_evidence`，
   并给出一套**与人工名单不同、但完全符合技术台记录**的合规名单（`alternative_lineup`）
   及差异片段号（`differing_segments`），供档案员补查录像；
5. 同一请求内所有求解共用 **10 秒**预算；耗尽且结论未定时返回 `pending_review`
   （`budget_exhausted=true`）。若候选片段已确认可唯一复算，证据包仍会一并给出。

响应 `pack` 字段即接收方拿到的全部内容：

```json
{
  "format_version": "1.0",
  "players": [1, 2, 3],
  "official_seconds": {"1": 600},
  "segments": [{"segment_index": 1, "duration_seconds": 200, "players_substituted": 0}],
  "revealed_clips": [{"segment_index": 1, "players_on_court": [1, 2, 3, 4, 5]}]
}
```

`segments` 只保留时长与换下人数，**不含任何未入选片段的名单**。

### 复算 `POST /api/v1/evidence/recompute`

请求体就是上面整个证据包（`EvidencePack`）。结果：

| 状态 | 含义 |
| --- | --- |
| `unique` | 证据足以唯一确定整节名单；`roster` 返回完整逐段名单，并附独立复核结论 |
| `conflict` | 证据包内部矛盾，无任何名单与之相符；只返回冲突说明 |
| `multiple_solutions` | 仍多解；**不返回完整名单**，给出 `suggested_clips` 建议补存的片段号 |
| `pending_review` | 10 秒预算耗尽，尚未能证实唯一性 |

唯一性判定方法是“排除已知解后是否还能再解出一套”：
若在固定入选片段、且要求与找到的名单至少一处不同的前提下不可满足，则该名单唯一。

### 建模口径补充

证据包不含“已确认球员”和“得失分”，复算时把它们当作空值 / 0，
其余约束（每段 5 人、人员范围、逐人官方秒数、相邻段换人记录）与阵容核对完全一致，
并可继续复用 `app/verifier.py` 的独立复核器交叉验证。

### 示例数据（`/docs` 中可直接调用）

- `packable_demo`：9 人 3 段，保留任意 2 段即可唯一复算 → `packed`，`minimum_clips=2`；
- `ambiguous_demo`：只允许保留 1 段 → `insufficient_evidence`，附合规备选名单；
- `unique_demo`：证据包含 2 段名单 → `unique`，返回整节名单；
- `multiple_demo`：证据包只含 1 段名单 → `multiple_solutions`，`roster=null`。

## 本地开发（无 Docker 时）

```bash
python3 -m venv --without-pip .venv
# 若系统没有 pip，可用 https://bootstrap.pypa.io/get-pip.py 引导
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
```

## 请求示例

```json
{
  "players": [1, 2, 3, 4, 5, 6, 7],
  "target_lineup": [2, 3, 5, 6, 7],
  "minimum_seconds": 100,
  "official_seconds": {"1": 300, "2": 600, "3": 600, "4": 300, "5": 300, "6": 300, "7": 600},
  "segments": [
    {"duration_seconds": 300, "confirmed_players": [2, 7], "players_substituted": 0, "team_net_points": 4},
    {"duration_seconds": 300, "confirmed_players": [3, 5, 7], "players_substituted": 2, "team_net_points": -3}
  ]
}
```

## 冲突诊断

记录无法同时成立时，`conflicts` 数组给出带错误码的精确定位，例如：

- `TOTAL_SECONDS_MISMATCH`：官方秒数总和不等于 5 人在场总人秒；
- `CONFIRMED_PLAYER_NOT_ENOUGH_SECONDS`：确认球员的官方秒数不够覆盖该片段；
- `SUBSTITUTION_CONFIRMED_CONFLICT`：确认球员的强制变动超过换人记录允许值；
- `NOT_ENOUGH_BENCH_PLAYERS`：换下人数超过板凳容量；
- `PLAYER_SECONDS_OUT_OF_REACH`：某球员官方秒数不在结构允许的可达区间内（附区间）；
- `JOINT_SECONDS_INFEASIBLE`：单人秒数都可达，但联合换人结构使它们无法同时满足；
- `STRUCTURAL_INFEASIBLE`：忽略秒数后五人 / 确认 / 换人结构本身就不成立。

## 测试

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests/ -q
```

测试策略：

- `tests/brute_force.py`：小规模实例的暴力枚举真值（独立于 CP-SAT 的第二套实现）；
- 250 个随机实例（含可行 / 扰动冲突变体）逐项对照状态、同场秒数与净胜分边界；
- 四种判定状态的冻结夹具、阈值边界、422 结构校验；
- 12 人 × 16 段规模上限的性能与正确性（秒级完成，边界均证明最优）；
- 独立复核器对各类人为篡改的检出测试；
- `tests/test_evidence.py`：证据包打包 / 复算全链路——最少片段数与暴力对照一致、
  人工名单不符即退回、片段不足给出不同合规名单、复算唯一 / 无解 / 多解、
  证据包不泄漏未入选片段、共用预算耗尽标为待核、随机实例打包后复算能还原人工名单。
