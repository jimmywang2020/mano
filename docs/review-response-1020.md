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
