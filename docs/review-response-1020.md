# Commit B review response — 5 协议缺陷修复 + 16 测试映射

> Baseline: `cbec22e`（179 passed）。本轮只修 reviewer 列出的 5 个协议缺陷、补 16 类
> 测试、同步文档，未新增状态/接口/恢复机制，未扩展范围（采购助手接线 / 真机验收 /
> 默认后端切换均未开始）。所有 pytest / git 由用户执行；下方 commit hash 留占位符。

## 验收方式（reviewer 要求：先证 16 测试在 `cbec22e` 失败）

新增的 16 类测试全部位于 `tests/test_runner_journal.py` 第 G 节
（`# G. Reviewer-requested hardening`）。要证明它们在旧实现上失败，用户可：

```bash
# 1) 暂存本轮生产代码修复，只保留新测试（证旧实现 red）
git stash push -- mano/runner.py mano/action_journal.py mano/pack.py
uv run --extra dev pytest -q tests/test_runner_journal.py -k "persisted or incompatible or unstable or protocol or write_failure or hook_raises or finalize or not_replayable or unavailable or unknown_recovery or restart_attempt or missing_in_flight or reconcile_pack"
# 预期：多条 FAIL（旧实现用当前 pack 的 hook_recovery、只比 schema、吞 journal 写失败等）
git stash pop

# 2) 全量绿
uv run --extra dev pytest -q
git diff --check
```

> 注意：第 1 步 stash 了生产修复后，部分“既有”D/F 恢复用例也会因 `ReconcilePack`
> 新增 `pack_id` 而联动——这是预期的，真正要看的是第 G 节用例由 red→green。

## 规范 — 测试 — 实现 映射表

| # | 规范（缺陷/契约） | 实现（真实函数） | 正向测试 | 反向测试 |
|---|---|---|---|---|
| 1 | 恢复必须按**落盘时**的 `hook_recovery` 关闭，而非当前 pack 的；新加的 IDEMPOTENT hook 不得回放旧动作 | `runner._run_recovery` EXECUTED 分支（`persisted_hr = rec.hook_recovery`；`incompatible = persisted==IDEMPOTENT and current!=IDEMPOTENT`） | `test_recovery_persisted_not_applicable_never_runs_new_idempotent_hook`（持久 NOT_APPLICABLE + 当前加 IDEMPOTENT → hook 0、FINALIZED） | `test_recovery_persisted_idempotent_rejected_when_current_pack_has_no_hook`、`..._when_current_not_replayable`、`test_recovery_persisted_not_replayable_never_calls_hook` |
| 2 | reconcile 兼容必须双向比对 `pack_id`+`schema`；缺失/不同身份一律 PACK_INCOMPATIBLE，不得关闭 | `runner._reconcile_config_error`（preflight 强制非空 pack_id+schema）+ `runner._pack_reconcile_fact`（比对持久与当前两侧的 pack_id+schema） | `test_reconcile_pack_missing_pack_id_fails_config_before_any_action`、`..._missing_schema_...` | `test_recovery_different_pack_id_same_schema_is_incompatible`（同 schema 不同 pack_id → PACK_INCOMPATIBLE、reconcile 不调用、不关闭） |
| 3 | 不稳定的恢复观察只作证据，不得驱动 verdict / 关闭动作 | `runner._run_recovery` DISPATCHING/OUTCOME_UNKNOWN 分支（`stable = obs.stability=="stable"`；unstable+AMBIGUOUS → verdict=UNKNOWN，跳过 reconcile） | — | `test_recovery_unstable_observation_does_not_reconcile_or_close`（reconcile 0、OUTCOME_UNKNOWN 保持、证据 ref 追加） |
| 4 | runner 只能依赖 `ActionJournal` Protocol（prepare/transition/load_open_actions/get_record）；in-flight 记录丢失视为 JOURNAL_CORRUPT | `ActionJournal` Protocol 新增 `get_record`；`runner._require_record` | `test_runner_uses_only_protocol_journal_methods`（仅暴露 4 方法仍跑通） | `test_missing_in_flight_record_is_journal_corrupt`（get_record→None 抛 JournalCorruptError） |
| 5 | journal 写失败绝不被吞；分类/收尾 transition 失败必须显式 `journal_error` | `runner.run` 执行块显式 `except ActionJournalError`（置 `journal_error_code`、关 jsonl、返回 `journal_error`）；hook 前 `_require_record` | `test_happy_path_order_prepared_dispatching_executed_finalized`（正常序列仍绿） | `test_outcome_unknown_write_failure_is_journal_error`、`test_not_executed_write_failure_is_journal_error`、`test_executed_write_failure_leaves_dispatching_and_no_hook`、`test_hook_raises_leaves_executed_and_never_replays_adb`、`test_finalize_write_failure_after_hook_leaves_executed` |
| 决策矩阵 | NOT_REPLAYABLE 恢复决策矩阵；hook 永不回放、动作不关闭 | `_run_recovery` EXECUTED 分支（NOT_REPLAYABLE → invalid_recovery_decision；None/MANUAL/TERMINATE 各自码） | — | `test_not_replayable_recovery_decision_matrix`（None/MANUAL/TERMINATE/CONTINUE/REPLAN 五档） |
| 报告区分 | PACK_INCOMPATIBLE 与 RECONCILIATION_UNAVAILABLE 分别上报 | `_pack_reconcile_fact` + `_emit` 的 `core_fact` | `test_pack_incompatible_vs_reconciliation_unavailable_reported_separately`（两条 report.core_fact 分别断言） | — |
| 证据追加 | 多次 UNKNOWN 恢复，证据 ref 只追加不覆盖 | `transition` patch 对 `recovery_observation_refs` 做 `extend`（action_journal L774） | — | `test_repeated_unknown_recovery_appends_evidence_only`（两轮 → 2 条、首条保留、保持 OUTCOME_UNKNOWN） |
| 证据隔离 | 重启的新 attempt 目录不覆盖旧 PNG/XML/steps.jsonl | `runner.run`：journal 模式 `work_dir = run_dir/attempts/<uuid12>` | — | `test_restart_attempt_dir_does_not_overwrite_prior_evidence`（两次运行 → 两个隔离目录、首目录 steps.jsonl 不变） |

> 既有 A–F 节（`journal=None` 行为不变、preflight、固定执行序、异常分类、D/F 恢复 +
> SIGKILL 子进程）继续覆盖未被上表单列的契约，本轮未改其语义。

## 文档同步

- `mano/pack.py`：`on_action_executed` 双 ABI 段补“恢复按**落盘**声明关闭”；
  `reconcile_action` 段把 `pack_id` + `reconciliation_schema_version` 从“可选”改为
  **必填非空**，并说明恢复需两侧相等。
- `docs/adding-a-pack.md`：新增“Durable action journal + crash recovery (optional)”
  章节，Non-goals 移除“crash recovery / durable journaling”（改为 opt-in），
  `on_action_executed` 段跨崩溃语义指向新章节。
- `docs/action-journal-design.md`：冻结于 `d556840`，未改。

## 交付给用户（服务器/本地执行者）

1. 跑验证（red 证明 + 全量绿 + diff check）：见上方命令块。
2. 暂存（**排除** Duolingo 运行产物与 lock）：
   ```bash
   git add mano/action_journal.py mano/pack.py mano/runner.py \
           tests/test_runner_journal.py docs/adding-a-pack.md docs/review-response-1020.md
   git status --short   # 确认未混入 packs/duolingo/*, uv.lock 等
   ```
3. commit + push + `git rev-parse HEAD`。
4. 回填 `HANDOFF_NEXT_SESSION.md` 的 commit：`<执行者 push 后回填>`。

---

## Round 2（基于 `2833eb9` 复核）— 1 阻塞 + 1 审计缺口

Reviewer 复核 `2833eb9` 核销了上一轮 5 项，但发现 EXECUTED IDEMPOTENT hook 恢复
**绕过 Pack 身份校验**（阻塞），以及 `_obs_ref()` 证据缺时间戳（审计）。均属原冻结
“逐动作耐久 journal / 恢复兼容性 / 证据契约”范围内，未扩范围。

| 规范 | 实现 | 测试（均 reviewer 必需） |
|---|---|---|
| EXECUTED 的 IDEMPOTENT hook 回放前须证**同一 Pack 身份**（pack_id+schema 两侧非空且相等），否则 PACK_INCOMPATIBLE、不调 hook、不转 FINALIZED、保持 EXECUTED、拒 CONTINUE/REPLAN | `runner._identity_compatible`（reconcile 与 hook 共用）；EXECUTED 分支 `incompatible = persisted==IDEMPOTENT and (current!=IDEMPOTENT or not _identity_compatible)` | 正: `test_recovery_idempotent_hook_same_identity_replays_once_and_finalizes`；反: `..._different_pack_id_...`（reviewer 复现场景）、`..._different_schema_...`、`..._missing_persisted_identity_...` |
| IDEMPOTENT hook 的 Pack 也必须在 preflight 声明非空身份 | `runner._hook_identity_config_error` 接入 run() preflight | `test_idempotent_hook_pack_missing_identity_fails_preflight`（零 ADB） |
| observation refs 带捕获时间/`skew_ms`/`stability`，不复制原始证据；追加语义不变 | `runner._obs_ref` 增补 `capture_started_at`/`capture_finished_at`/`skew_ms`/`stability` | `test_observation_refs_carry_capture_timing`（pre-execution + recovery 两侧均含时间字段） |

文档：`mano/pack.py`、`docs/adding-a-pack.md` 同步“IDEMPOTENT 回放须同身份”“refs 带时间”。
`docs/action-journal-design.md` 仍冻结未改。

验收标准（reviewer 指定）：既有 198 全绿 + 新组合测试全绿；错误 Pack 复现
`core_fact=PACK_INCOMPATIBLE / hook_replays=0 / state=EXECUTED`；`git diff --check` 无输出。

---

## Round 3（基于 `2789339` 复核）— 无设备副作用失败后的有界自动纠正规划

Reviewer 要求：为 Mano Core 增加「零设备副作用 REOBSERVE 后，把失败事实有界地带入
下一次模型规划」的通用薄反馈，不进 Action Journal、不执行/回放 ADB、Core 不含任何
App 语义。复用现有 `ResolveDecision.reason` / `VerificationResult.reason` / `max_reobserve`，
不新增公共 ABI。所有 pytest / git 由用户执行；commit hash 留占位符。

### 设计（薄、通用、有界）

- 一个循环局部变量 `replan_feedback: Optional[dict]`（`runner.run` 循环初始化）。
- 在**四个**零副作用 REOBSERVE 分支置位：`resolve` REOBSERVE、`verify` REOBSERVE、
  Core 前台切换（`reval.foreground_changed`）、legacy raw-XML 变化回退。载荷
  `{source, reason, rejected_action, semantic_target?}`。
- 下一轮循环顶端构造 instruction 时**消费一次**：先调 `pack.instruction(history)`
  （history 干净，拒绝动作永不入 history），再把 `PREVIOUS_ACTION_REJECTED <json>`
  （`json.dumps(..., sort_keys=True, ensure_ascii=False)`）追加到该次 instruction，
  随即清空（一次性；若下步又 REOBSERVE 则替换）。
- 审计：产出步记 `replan_feedback`，消费步记 `replan_feedback_consumed`。
- `reason` 截断 1024 字符、UTF-8 安全、空则默认 `reobserve_required`。
- 有界：沿用 `max_reobserve`；到限仍以既有 `result="reobserve_required"` 结束，
  并受 `max_steps`/timeout 约束；成功执行允许动作后清零连续 REOBSERVE 计数。
- Fail-closed（**不**纠正）：`BLOCK`、resolve/verify 异常或未知 verdict、非法
  `semantic_target`、whitelist / anti-swap / 文本后端拒绝、approve 否决、ADB 执行
  失败、模糊/崩溃执行窗口、Pack 声明不可重试 —— 这些分支一律不置 `replan_feedback`。

### 规范 — 实现 — 测试 映射表

| 项 | 规范 | 实现（真实函数） | 测试 |
|---|---|---|---|
| A | resolve REOBSERVE：把 `reason` + 被拒 `proposed_action` 带入下一次模型调用 | `runner.run` resolve REOBSERVE 分支置 `_replan_feedback("resolve", decision.reason, action)` | `test_resolve_reobserve_carries_rejected_action_into_next_model_call` |
| B | verify REOBSERVE：把 `verify` reason + 回退的 resolved 动作 + `semantic_target` 带入下一次 | verify REOBSERVE 分支 `_replan_feedback("verify", verification.reason, resolved, semantic_target)` | `test_verify_reobserve_carries_rejected_action_into_next_model_call` |
| C | 有界：持续 REOBSERVE 仍受 `max_reobserve` 终止、零 ADB | 四分支共用 `if reobserve_streak >= max_reobserve: result="reobserve_required"; break` | `test_replan_feedback_is_bounded_by_max_reobserve` |
| D | `BLOCK` 永不触发纠正 | BLOCK 分支不置 `replan_feedback` | `test_block_verdict_sets_no_replan_feedback` |
| E | 非法 verdict / resolve 异常 fail-closed，不纠正 | `_safe_resolve` 异常→BLOCK；未知 verdict→`invalid_resolve_verdict`（均不置位） | `test_illegal_verdict_and_resolve_exception_set_no_replan_feedback` |
| F | 一次性：仅紧邻的下一次模型调用可见 | 顶端消费后 `replan_feedback = None` | `test_replan_feedback_is_one_shot` |
| G | 拒绝动作不污染 Pack 规划用 `history` | 先 `pack.instruction(history)` 后追加反馈；history 永不含拒绝动作 | `test_replan_feedback_does_not_pollute_pack_history` |
| H | `reason` 有界/截断/Unicode 安全/默认 | `runner._replan_feedback`（`text[:1024]`，空→`reobserve_required`）+ `_format_replan_feedback`（`ensure_ascii=False, sort_keys=True`） | `test_replan_reason_is_bounded_truncated_and_unicode_safe`、`test_replan_reason_defaults_when_empty` |
| I | journal 边界：零副作用 REOBSERVE 绝不 prepare/提交 journal 动作 | 四分支均在 `prepare` 之前；journal 模式同样零记录 | `test_resolve_reobserve_creates_no_journal_record`（journal 实盘 `actions` 表为空） |
| J | 既有行为回归：无 REOBSERVE 的 run 永不发反馈 | 默认 `replan_feedback=None`，record 默认 `replan_feedback/_consumed=None` | `test_no_reobserve_run_never_emits_replan_feedback` |

> A/B/C/D/E/F/G/H/J 位于 `tests/test_runner.py`（薄 runner 通用回路）；I 位于
> `tests/test_runner_journal.py`（真实 SQLite journal）。

### 文档同步

- `docs/adding-a-pack.md`：resolve/verify 回路节新增三点 —— ①临时不可用目标应
  `REOBSERVE`（安全边界仍必须 `BLOCK`）；②Core 把有界 `reason`/`rejected_action`
  一次性带入下一次模型规划（不入 history、不入 journal、不碰 ADB、Core 不含 App 语义）；
  ③所有 fail-closed 窗口永不自动纠正。
- `docs/action-journal-design.md`：冻结于 `d556840`，未改。
- Core 内零 App 语义（无「转转 / 容量 / 128G / 筛选栏目」等）。

### 交付给用户（执行者/本地）

1. 跑验证：
   ```bash
   uv run --extra dev pytest -q
   git diff --check
   ```
2. 暂存（**排除** Duolingo 运行产物与 lock）：
   ```bash
   git add mano/runner.py tests/test_runner.py tests/test_runner_journal.py \
           docs/adding-a-pack.md docs/review-response-1020.md docs/harness-rules.md
   git status --short   # 确认未混入 packs/duolingo/*, uv.lock 等
   ```
3. commit + push + `git rev-parse HEAD`（hash：`47aaa09841ba04e99f40fd25ee814c485016cb31`）。

验收标准（reviewer 指定）：全量 pytest 绿 + 新测试绿；REOBSERVE 路径 0 ADB / 0 journal
动作；`rejected_action` 不入成功 history；`max_reobserve` 仍能终止；`BLOCK` 与所有不确定
窗口永不自动重试；Core 无 App 语义；`git diff --check` 无输出。

> 落地：本轮已以 `47aaa09841ba04e99f40fd25ee814c485016cb31` push（`2789339..47aaa09`，
> 6 files changed）；采购助手生产回执（2026-10-23）确认该 pin 本机 `215 passed`、并已把
> Mano 提升为默认采样后端（V-01 保留为显式回退）。

---

## Round 4（基于 `47aaa09` 生产回执）— resolve-ALLOW 越界坐标纳入有界重规划

采购助手真机回执：模型给出 y 超出坐标空间（`0..1000`）的 ALLOW 动作时，Core 在
resolve 后的 `validate_executable_action` 返回 `bad_coordinate`/`bad_coordinate2`，
旧路径直接以 `resolve_invalid_action` 结束整次运行，**未**进入已有的零副作用有界重
规划（Round 3 机制）。本轮把且仅把这两个越界坐标码接入既有重规划回路，其余校验码
保持 fail-closed。复用 Round 3 的 `replan_feedback` / `max_reobserve`，不新增公共 ABI，
Core 不含 App 语义，不 clamp / scale / guess / 执行非法坐标。

### 设计（薄、仅限越界坐标、有界）

- 模块常量 `_REPLAN_RESOLVE_INVALID_CODES = frozenset({"bad_coordinate", "bad_coordinate2"})`。
- resolve-ALLOW 校验块：`invalid = validate_executable_action(adb, resolved)`；
  `invalid in _REPLAN_RESOLVE_INVALID_CODES` → 走有界重规划（`reobserve_streak`/
  `reobserves` 自增、`record["note"]="reobserve_required"`、`record["resolve_reason"]=invalid`、
  `replan_feedback = _replan_feedback("resolve_invalid_action", invalid, resolved,
  decision.semantic_target, extra={"coordinate_space": "0..1000 per axis"})`、
  到 `max_reobserve` 以 `result="reobserve_required"` 终止，否则 `continue`）；
  **其余任何码**保持原 `result="resolve_invalid_action"; break`（文本码仍 mirror）。
- 该分支结构上位于 journal prepare/approve/verify/ADB **之前** → 非法轮次零设备、零 journal。
- 新稳定 `source="resolve_invalid_action"`（区别于既有 `resolve`/`verify`/
  `foreground_changed`/`legacy_xml_changed`）。
- `_replan_feedback(..., *, extra=None)`：`extra` 为 dict 时仅合并尚不存在的字符串键，
  绝不覆盖核心键（`source`/`reason`/`rejected_action`/`semantic_target`）。

### 规范 — 实现 — 测试 映射表

| 项 | 规范 | 实现（真实函数） | 测试 |
|---|---|---|---|
| K | `bad_coordinate`（click y 越界）→ 有界重规划；载荷含 `coordinate_space` 提示 | resolve-ALLOW 分支 `_replan_feedback("resolve_invalid_action", "bad_coordinate", resolved, semantic_target, extra=...)` | `test_bad_coordinate_resolve_routes_into_bounded_replan`（`tests/test_runner.py`） |
| L | `bad_coordinate2`（swipe 终点越界）→ 同样重规划，`swipe_calls==1` 仅发生在后续合法轮 | 同上，`invalid=="bad_coordinate2"` | `test_bad_coordinate2_resolve_routes_into_bounded_replan` |
| M | 持续越界仍受 `max_reobserve` 终止、零 tap | 共用 `if reobserve_streak >= max_reobserve: result="reobserve_required"; break` | `test_bad_coordinate_replan_is_bounded_by_max_reobserve`（`max_reobserve=3`→`reobserves==3`、`tap==0`） |
| N | 一次性且不污染 history | 顶端消费后清空；`rejected_action` 永不入 history | `test_bad_coordinate_replan_is_one_shot_and_keeps_history_clean` |
| O | 非坐标校验码（文本不被后端支持）仍 fail-closed，不重规划 | 其余码保持 `result="resolve_invalid_action"; break` | `test_text_unsupported_still_ends_not_replanned`（`type "中文"`→`resolve_invalid_action`、`replan_feedback is None`） |
| J' | journal 边界：越界重规划零 journal 动作 | 分支在 `prepare` 之前 | `test_bad_coordinate_replan_creates_no_journal_record`（`tests/test_runner_journal.py`，`actions` 表为空、`tap==0`） |
| R' | 既有越界测试随新语义改写（原 fails-closed → 现 bounded replan） | 同一分支 | `test_resolve_out_of_range_coordinate_routes_into_bounded_replan`（`tests/test_runner_observation.py`，重写自旧 `..._fails_closed`；`reobserves==3`、`tap_calls==0`、首记录 `resolve_reason=="bad_coordinate"`、`replan_feedback["coordinate_space"]=="0..1000 per axis"`） |

> 旧 `test_resolve_out_of_range_coordinate_fails_closed` 在新实现下因第二次模型调用
> 脚本耗尽报 `IndexError` —— 这正是「行为已按预期改变」的正向证据（非新 bug），
> 据此重写为 `..._routes_into_bounded_replan`。

### 文档同步

- `docs/adding-a-pack.md`：「Bounded replan feedback (one-shot, generic)」条新增越界坐标
  （`bad_coordinate`/`bad_coordinate2`，携 `coordinate_space` 提示）为第 5 个零副作用
  重规划来源；明确 Core 绝不 clamp / scale / guess / 执行非法坐标；fail-closed 清单
  改述为「除上述可重规划的越界坐标外的任何校验码，如 `text_unsupported_by_backend`」。
- `docs/action-journal-design.md`：冻结未改。

### 交付给用户（执行者/本地）

1. 验证：`uv run --extra dev pytest -q`（`221 passed`）+ `git diff --check`。
2. 暂存（**排除** Duolingo 运行产物与 lock）：
   ```bash
   git add mano/runner.py tests/test_runner.py tests/test_runner_journal.py \
           tests/test_runner_observation.py docs/adding-a-pack.md docs/review-response-1020.md
   git status --short   # 确认未混入 packs/duolingo/*, uv.lock 等
   ```
3. commit + push + `git rev-parse HEAD`（hash：`9acb4b57947aca1da4c6049029b687aabf8e8449`）。

验收标准：全量 `221 passed`；越界坐标 → 有界重规划（0 ADB / 0 journal 动作）；非坐标
校验码仍 fail-closed；`rejected_action` 不入 history；`max_reobserve` 仍能终止；
`git diff --check` 无输出。

> 落地：本轮已以 `9acb4b57947aca1da4c6049029b687aabf8e8449` push，tag **`v0.1.1`**
> （不可移动，`v0.1.1^{commit}==9acb4b5`）。采购助手生产回执（2026-10-28）确认：
> non-editable Git 安装、`direct_url.json` `requested_revision=v0.1.1` +
> `commit_id=9acb4b5`、采购助手 commit `8692c47`、`286 passed`；真机：单 SKU 10/10
> `COMPLETED_AUTONOMOUS`、串行双 SKU 各 10/10，且旧 `bad_coordinate` 任务以 v0.1.1
> 重跑 → `COMPLETED`（29 journal `FINALIZED`，证明越界有界自纠正在真实页面生效）。

---

## 发布：v0.1.2（纯打包元数据，无运行代码改动）

采购助手回执（2026-10-28）指出：v0.1.0 与 v0.1.1 构建出的 wheel 均为
`mano-0.1.0-…`，因 `pyproject`/`__init__` 的 version 从未从 `0.1.0` 提升。本次只修
版本元数据，不动任何运行代码。

- 改动：`pyproject.toml` `version` 与 `mano/__init__.py` `__version__` 同步 `0.1.0 → 0.1.2`
  （跳过 0.1.1 以避免移动已存在的 `v0.1.1` tag；该 tag 的 pyproject 仍写 0.1.0）。
- 验证：`git diff --cached` 仅两行版本串；`uv run --extra dev pytest -q` → `221 passed`；
  `uv build --wheel` → `mano-0.1.2-py3-none-any.whl`。
- 发布 commit `02a19738f2847266b3e87c3fe9a24c1da0207667`（`9acb4b5..02a1973`，main），
  tag **`v0.1.2`**（不可移动，`v0.1.2^{commit}==02a1973`）。
- 复核未被动：`v0.1.1^{commit}==9acb4b5`、`v0.1.0^{commit}==47aaa09`。
- 因纯元数据、运行代码零改动 → 采购助手仅需重装依赖 + 跑全量测试，**无需**重做
  单 SKU / 批量真机验收。
