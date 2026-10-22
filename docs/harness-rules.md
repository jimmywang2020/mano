# Harness Rules（Mano 项目）

> 本项目踩坑/有效做法的沉淀。标签见每条末尾 `<!-- [tag] [scope] -->`。
> `[global]` 规则需同步到 `~/.claude/CLAUDE.md` 与个人 instructions（见「待镜像」）。
> 最后更新：2026-10-22

## observability（勘探 / 排查）

- OB-11：测试 fake / mock 使用某个回调参数的属性前，先 grep 真实调用点确认该参数的
  **实际类型**——参数名会骗人。本项目实测：`Pack.verify(*, approved_intent, fresh_observation)`
  的 `approved_intent` 形参虽叫 "intent"，runner 的 `_safe_verify` 实际传的是
  `decision.semantic_target`（一个 `str`，非 `ResolvedAction`）。测试 fake 里写
  `approved_intent.resolved_action` → `str` 无该属性 → `AttributeError` → 被 fail-closed
  包装器 `_safe_verify` 吞成 verdict=BLOCK/`reason="verify_error"`，于是测试报
  `assert 'verify_error' == 'terminate_success'`，把脚手架 bug 伪装成 verify 阶段产品失败。
  判据：fake 要访问回调参数的 `.x` 前，grep 该回调在产品代码里的唯一调用点（本项目
  `_safe_verify(verify, decision.semantic_target, reval)` + `verify(approved_intent=...,
  fresh_observation=...)`），以实参表达式而非形参名判类型。是 OB-01/CE-01（名 ≠ 实际值/类型）
  与 OB-09（脚手架 bug 伪装成产品 bug）在「回调参数类型」面的合流。
  <!-- [observability] [global] -->

- OB-12：产品里的 fail-closed 包装器（把任意异常/错误返回收敛成稳定错误码，如
  `_safe_resolve`→`resolve_error`、`_safe_verify`→`verify_error`）会把测试 fake 抛的异常
  一并掩盖成「产品错误码」。新加/新改的测试意外得到某个 fail-closed 错误码时，排查优先级：
  先怀疑 fake 抛了被包装器吞掉的异常（打开包装器看 `except` 分支、或临时在 fake 里不吞异常
  直跑），再怀疑产品缺陷。本项目实测即 OB-11 现场：`verify_error` 实为 fake 的 `AttributeError`。
  <!-- [observability] [global] -->

## harness-design（回路 / 接口设计）

- HD-10：要给「紧邻的下一次模型调用」注入一次性反馈、又不想拓宽已冻结的 Pack 公共 ABI
  （`Pack.instruction(history)`）、也不能污染 Pack 规划用的 `history` 时，在**传输层组合**而非
  改 ABI：runner 先用干净 `history` 调 `pack.instruction(history)`，再把反馈追加到返回的
  instruction **字符串**（本项目 `PREVIOUS_ACTION_REJECTED <stable-json>`），下一轮顶端消费后立即
  清空（一次性，可被替换）。效果：Pack 永不见被拒动作（history 干净、ABI 不变），模型却能看到
  纠正事实。配套：用 `json.dumps(..., sort_keys=True, ensure_ascii=False)` 稳定序列化、`reason`
  定长截断 + 空值默认、审计落 `replan_feedback`（产出步）/`replan_feedback_consumed`（消费步）。
  "不拓宽 ABI，在传输层组合"。
  <!-- [harness-design] [project] -->

- HD-11：薄 Core 新增「有界自动纠正」类反馈时，反馈只能携带**通用运行时事实**
  （source / reason / rejected_action / semantic_target），Core 不得解释 App 语义；上界复用既有
  `max_reobserve`，到限仍以既有 `result` 收尾；且所有 fail-closed 窗口（BLOCK、resolve/verify
  异常或未知 verdict、whitelist/anti-swap/文本后端拒绝、approve 否决、ADB 失败、崩溃/模糊执行窗口、
  Pack 声明不可重试）**一律不置反馈**——只在「已证设备动作未发生」的零副作用分支置位。验证正反两面：
  正向（resolve/verify REOBSERVE → 下一次模型输入含 reason + rejected_action）与反向
  （BLOCK/异常/未知 verdict → record 的 `replan_feedback is None` 且 0 ADB、journal 0 记录）。
  <!-- [harness-design] [project] -->

## 已镜像（[global] 规则）

- OB-11、OB-12 已镜像到 `~/.claude/CLAUDE.md`（排查与勘探方法论段，HD-07 之后）与个人
  instructions（排查方法论（精要）段）。2026-10-22。
