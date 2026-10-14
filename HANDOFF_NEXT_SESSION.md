# 新会话开场包 · mano 渐进抽取（最薄通用 Runner）

> 过渡 handoff，**勿提交到 mano 公开仓**（放这里只为新会话读取）。
> 来源：codex 评估 `/Users/wangjinxiang/Desktop/mano调用评估_1013_1130.md` + 上一会话实读 mano 现状。
> 诚实边界（HD-07）：本包内所有“已验证/已通过”默认视为**未执行**，以你实跑的 pytest / 真机输出为准；不编造 commit hash。

---

## 0. 你的任务与铁律

**在现有 mano 仓库里，只抽取一个最薄的通用循环**，让 mano 能被第二个 App（转转）和采购助手调用。

- ❌ 不重写 Core、不从 0 造引擎、不把 `mcore0_spike` 扩成第二套实现。
- ❌ 不把 `packs/duolingo/run.py` 整段搬进 Core（它混了通用循环 + Duolingo 守卫 + solver + 提示词 + 结束条件 + 证据格式）。
- ✅ 复用现有 `Adb`/`GuiPlus`/`execute_action`/`action_codec`/`ui_xml`/`media`，只抽**最小通用循环**。
- ✅ 先保证 Duolingo 不退化（原 `run.py` 留作回归基线），再接转转，最后采购助手 Adapter 调用。

需求已判定为 **b + c**（不用再选 a/b/c）：
- **b**：第二个 App 接入，而不是复制 Duolingo 整段 `run.py`；
- **c**：采购助手要自主运行，保留**最低限度**的不重放 / 失败可解释 / 恢复；
- **a**（统一 CLI/安装体验）后置，当前不做。

---

## 1. 渐进四步（codex 定；每步有验收）

```
现有 Mano 模块 → 最薄通用 Runner → Duolingo Pack 适配 → 转转 Pack 接入 → 采购助手 ManoSamplingAdapter
```

| 步 | 做什么 | 验收（机器证据） |
|---|---|---|
| 1 | **不改** `packs/duolingo/run.py`，留作回归基线 | 现有 `python3 packs/duolingo/run.py run` 仍能跑完一节课 |
| 2 | 新增 `mano/runner.py` + `mano/pack.py`（最薄循环 + Pack 协议） | 单测：fake Pack + fake Adb，一步一动作、执行后重观察、结构化结果 |
| 3 | Duolingo 通过**薄适配器**调新 Runner | 同一节课用新 Runner 跑通，`summary.json` 的 `result` 与基线同类；无退化 |
| 4 | 转转 Pack **独立私有仓**；`ManoSamplingAdapter` 留采购助手 | 采购助手经 Adapter 调本地 editable mano，`import mano` PASS |

> 第四步纠正“全部回 mano 仓”的绝对说法：通用 Runner + Duolingo Pack 在 mano 仓；**转转 Pack / Adapter / SKU/样本/报价/审核/队列 都不进 mano**。

---

## 2. run.py 循环切分（上一会话实读 404 行；**以你实读定稿**）

**通用 → 抽进 `mano/runner.py`：**
- 设备就绪/重连：`_ensure_adb_ready` / `_try_reconnect` / `_is_local_tcp_serial` / `_capture_frame`（引擎级 ADB 健康）
- 每步：截图 → `adb.ui_dump()` XML → 证据落盘（`steps.jsonl` + `step-NN.png` + `step-NN.xml`）
- 一步一动作主循环 `for step in range(1, MAX_STEPS+1)` + `STEP_TIMEOUT_S` 截止
- **可靠性守卫（= 需求 c）**：`action_signature` 精确重复（`repeat>=2` 拦）+ `_loop_sig`/`recent_loop` A-B-A-B 振荡拦 + `wait_streak>=4` 空等拦
- VLM 单步：`call_for_action_duo` 里的通用部分 = 3 次解析自纠 + `ManoNetworkError` 指数退避重试（**prompt 装配留 pack**）
- `execute_action`（引擎级，内部再拒未知动作/越界坐标）
- 执行后 `time.sleep(SETTLE_S)` 重观察 + `terminate` 处理
- `summary.json` 结构化输出（result/steps/vlm_calls/tokens/wall）

**Duolingo 专属 → 留 `packs/duolingo/`：**
- `PACKAGE="com.duolingo"`
- `prompts.duolingo_system_prompt / duolingo_instruction`
- `guards.foreground_pkg / paywall_detected / energy_depleted / bonus_offer_detected`
- `matching_solver` / `ordering_solver`（确定性 solver 路由）
- 结束语义：`left_app / paywall / energy_depleted / lesson_done`

**直接复用不改：** `mano.action_codec`(action_signature/mapped_action/parse_tool_call)、`mano.adb_executor`(Adb/execute_action)、`mano.errors`(ManoError/ManoNetworkError)、`mano.gui_plus_client`(GuiPlus)、`mano.media`(image_data_url/png_size)。

---

## 3. 最薄 Runner / Pack 接口（草案，起步用，**非冻结 ABI**）

```python
# mano/pack.py —— Pack 只报告“看到什么/该做什么”，不碰 ADB/坐标/证据
class Pack(Protocol):
    package: str
    def system_prompt(self, width: int, height: int) -> str: ...
    def instruction(self, history: list[dict]) -> str: ...
    def guard(self, xml: str, foreground: str | None) -> str | None: ...   # 停止原因或 None
    def route(self, xml: str) -> "Solver | None": ...                      # 确定性 solver 或 None（走 VLM）
    # 结束动作默认看 action["action"]=="terminate"；需要可加 is_terminal(action)

# mano/runner.py —— 通用循环；所有 Duolingo 字样为 0
def run(adb, client, pack: Pack, run_dir, *,
        max_steps=40, step_timeout_s=900, settle_s=1.0) -> RunResult: ...
```

Duolingo 适配 = 新增 `packs/duolingo/pack.py` 把现有 `prompts/guards/solver` 包成 `DuolingoPack`，`run.py` 的新路径变成 `runner.run(adb, client, DuolingoPack(), ...)`。**旧 `run.py` 不动**，可加 `--engine new` 开关或新建 `run_via_runner.py` 做对照。

> 禁止（codex 第二步“不要一开始加入”）：自动导航特殊状态、deterministic plan、Pack Registry 自动发现、LangSmith、JSON-lines 子进程、可替换 JournalStore、三种证据模式、完整 GC/密钥轮换。

---

## 4. 可靠性最小集（b+c 的底线）

**本阶段必须保留**（已在 run.py，抽进 Runner 即可，别丢）：
- 一次 advance/step = 一个动作；执行后重新观察，不盲目连发
- 重复 / 振荡 / 空等守卫（防死循环烧 VLM、防重放）
- 失败可解释：结构化 `result` + 每步 `steps.jsonl`（exec_error / blocked_repeat / oscillating）

**本阶段暂缓，但标记“本地参考实现不做”≠“不需要”**（mano 成为采购助手**唯一自主采样后端前**是准入条件）：
- 设备租约 **fencing**（否则旧进程失租后仍操作设备）
- 生产**证据加密/脱敏**（否则截图/XML 明文存账号、地址、商品）

> 需要这两项的设计细节时，按需读 `/Users/wangjinxiang/Desktop/mano-core-design/M_CORE_0_SCOPE.md`（§5 不变量 I1–I12、§6 故障矩阵 FM-1–FM-10）。**只借思想，不复制那套架构。**

---

## 5. mcore0_spike 处置

路径 `/Users/wangjinxiang/Desktop/mano-core-design/mcore0_spike/`。
- 停止新增功能，不要扩成第二个引擎；
- 有价值的是**故障矩阵与测试思想**（FM-1 并发 advance 0-journal 等）→ 需要时把测试逐步移植到 mano 的单测，不复制全套新架构；
- 确认无独有价值前**不删**（删除交人工决定）。

---

## 6. 相关绝对路径（按需 read，跨工作区用绝对路径）

- mano 仓（你的工作区）：`/Users/wangjinxiang/Desktop/Projects/mano`
- codex 评估原文：`/Users/wangjinxiang/Desktop/mano调用评估_1013_1130.md`
- 远期 Core 设计（只借思想）：`/Users/wangjinxiang/Desktop/mano-core-design/M_CORE_0_SCOPE.md`
- spike：`/Users/wangjinxiang/Desktop/mano-core-design/mcore0_spike/`

---

## 7. 第一步具体动作（建议）

1. 新建工作分支（你走 git；人类或你执行）：`git checkout -b feat/thin-runner`
2. 实读 `packs/duolingo/run.py` + `mano/*.py` 公共接口，确认 §2 切分（以真实代码为准，别凭本包）
3. 写 `mano/pack.py`（Protocol）+ `mano/runner.py`（最薄循环），**Duolingo 字样为 0**
4. 写单测：fake Pack + fake Adb（计次），断言一步一动作 / 执行后重观察 / 守卫拦截 / 结构化结果（每个断言**先写死预期**）
5. Duolingo 薄适配器调新 Runner，真机跑一节课对照基线，证明不退化
6. 把“已通过”的结论附上你实跑的命令与输出（HD-07）

---

## 8. 本次会话进展（2026-10-13；下次从这里继续）

> 诚实边界（HD-07）：以下 commit 号、测试计数、A/B 输出均来自人类终端实跑，非编造。本文件**勿 push 到 mano 公开仓**，保持本地。

### 8.1 已完成（步骤 1–4 全绿）
- **步骤 1 实读核对**：§2「通用 vs Duolingo」切分与真实代码一致；5 处按真实代码细化（solver 是带内部循环的宏步骤、foreground 获取归属、history 窗口归 pack、两条 terminate 路径、fencing/加密暂缓非取消）。
- **决策（采纳 codex 评估 1013_1214）**：
  - **不**把 solver 提升为 Pack ABI（否则 Pack 持有 ADB、一步多动作，违反"一步一动作/Pack 不碰 ADB"）。Runner v0 只走 VLM 单步；solver 留旧 `run.py` 当基线，后续改成 `next_action(xml,history)->至多一个动作` 适配器再接。
  - foreground 获取上收为 `Adb.foreground_package()`（通用），`pack.guard(xml,foreground)` 只做语义判定。
  - Runner 只记原始用量（tokens/vlm_calls/retries/actions_executed/wall），**成本计算留调用方**。
  - 守卫是**运行时防循环**，**不**声称崩溃恢复/不重放。action 字典作 0.x 兼容边界，仍由 `execute_action` 校验。Pack 协议标 0.x 非冻结。
- **代码产物**（分支 `feat/thin-runner`，旧 `packs/duolingo/run.py` 一字未改）：
  - `mano/pack.py`：Pack 协议（package/system_prompt/instruction/guard），无 solver、无 ADB、Duolingo 字样 0。
  - `mano/runner.py`：最薄循环，Duolingo 字样 0。
  - `mano/adb_executor.py`：新增 `Adb.foreground_package()`（additive；旧 `guards.foreground_pkg` 保留不删，以免破坏基线——刻意偏离 codex"移走"建议，理由=铁律"旧 run.py 不改"）。
  - `packs/duolingo/pack.py`：`DuolingoPack` 薄适配器（包现有 prompts+guards，无 solver）。
  - `packs/duolingo/run_via_runner.py`：A/B harness，走新 Runner，证据布局+summary.json 字段对齐基线。
  - `tests/test_runner.py`（6）+ `tests/test_duolingo_pack.py`（9）。
- **提交**：`71e5dab`（runner+pack+tests）、`25c50a1`（DuolingoPack+harness+pack tests）。
- **离线测试**：`uv run --extra dev pytest -q` → `15 passed`（用 uv，**不**用系统 py3.9）。
- **设备探测**：`eval "$(bash scripts/mumu-adb.sh)"` 自动连，别硬编码 serial、别写 .env。

### 8.2 真机 A/B（本阶段**未判定通过**，选 B 挂起）
- 基线 `run.py`：`RESULT=max_steps steps=40`（518s/$0.23）——**基线自己也没跑完**；证据：steps 31–33 `ordering_solver` 把 "The plane will land soon" 摆成错词序，检查不过→反复重摆→耗尽。
- 新 Runner `run_via_runner.py`：`RESULT=repeat_click_blocked steps=15`（90s/$0.13）。
- **判定**：基线未完成 → 无"完成类"基准 → 不退化**无法判定**。但 Runner 机制经证据验证正确：steps 1–13 正常推进填空/翻译/选择多题型（executed:true），第 15 步重复守卫按设计拦截；无 exec_refused/vlm_error/崩溃，且比基线更早更省地受控停机。
- **卡点性质已坐实（读 step-13/15 截图）**：是 **grounding 错误**不是决策错误——VLM 正确选了"never been there before"，但坐标 y=676(→1731px) 落在两选项空隙，始终点不中目标选项（step-15 选项2 变灰、答题气泡仍空、继续仍灰）。

### 8.3 下次起点（按优先级）
1. **补完成对照（了结步骤 5）**：挑一节**简单、无组句/对话题**的基础课重跑 A/B，让基线先拿 `lesson_done`/`terminate_success`，再看新 Runner 是否同类。命令见 §8.1 的 uv 两条。
2. **grounding 候选修法（治 8.2 卡点，优先于引新模型）**：感知环节输出选项**包围盒(bounds)**，点**框中心**而非目测坐标（matching/ordering solver 不翻车就是这么做）；叠加"状态变化信号"（选项变灰）重判 + 同坐标 N 次无变化抖动/兜底。先核该屏 XML 的选项容器有无带 bounds 的可点击节点。
3. **solver 接回**：按 codex，改成 `next_action(xml,history)->至多一个动作` 的逐步适配器塞进 Pack，Runner 执行，别让 Pack 跑设备。
4. **（暂缓非取消，别删标记）**：设备租约 fencing、生产证据加密/脱敏——mano 成采购助手唯一自主采样后端前的准入条件。
5. **Jev / 感知-决策分工讨论**：本次已评估并暂停。结论存档：Jev 是**纯文本判别式分类器（无视觉/无坐标）**，治"选错答案"，治不了我们实际撞到的 grounding；更适合采购助手的文本判定+置信度门控，不是 GUI 决策层。重启讨论前先量"决策错误率 vs grounding 错误率"。

### 8.4 git 待办（交人类/执行者，本地）
- 分支 `feat/thin-runner` 两次提交已在本地；按需 `git push -u origin feat/thin-runner`（**仅推代码分支**）。
- 本 handoff 文件按首部约定**不进公开仓**，保持本地，勿 git add。

---

## 9. 本次会话进展（2026-10-14；闭环 resolve/verify 落地）

> 诚实边界（HD-07）：以下 commit 号、pytest 计数**待人类终端实跑回填**，占位符未执行。我（builder）只写文件，不跑 git、不跑 pytest、不碰真机。

### 9.1 这次做了什么（一次性交付完整闭环，非分步）
实现定稿《mano迭代细节回复_1014_1430》A–E 的完整闭环：
**模型原始动作 → Pack 解析为语义动作 → 项目批准语义动作 → 获取最新页面 → Pack 验证同一语义动作 → Core 执行最新物理动作**。

**固定每步顺序（Core 强制）：**
```
capture Observation            # 单次 XML dump 的有界包；model+guard 共享
 -> Pack.guard                 # 确定性停止
 -> model 提原始动作
 -> Pack.resolve               # -> ResolveDecision(verdict, semantic_target, resolved_action)
 -> 对 RESOLVED 动作分类        # 所有防循环守卫 key 在 resolved 动作上
 -> Consumer approve(ResolvedAction, Observation)
 -> capture RevalidationView   # 前后台三明治重读最新页
 -> Core 前台安全检查           # 前台变 -> 零 ADB，重观察
 -> Pack.verify  或  legacy 兜底
 -> 执行最终物理动作
```

### 9.2 改了哪些文件（旧 `packs/duolingo/run.py` 仍一字未改）
| 文件 | 核心改动 |
|---|---|
| `mano/pack.py` | 新增 `ALLOW/REOBSERVE/BLOCK` 常量；`ResolvedAction`(frozen)、`ResolveDecision`、`VerificationResult` 三个 dataclass；`Pack` 协议新增**强制** `resolve(...)`，`verify` 文档化为**可选**（不入 Protocol，靠 `getattr`+`callable` 探测）。`REPLAN` 明确禁止。 |
| `mano/observation.py` | 观察**诚实非原子化**：单次 XML dump，顺序 `fg_before→screenshot→XML→fg_after`，稳定判据 = 截图 ok + xml ok + `fg_before==fg_after` + `skew_ms<=max_skew_ms`。原始 XML 指纹**仅存证不作稳定门**（轮播/计时器不再误判 unstable）。`FreshnessCheck`→`RevalidationView`、`check_freshness`→`capture_revalidation`（保留同名后向别名）。删除 xml_before/after 双指纹双时间戳字段。 |
| `mano/runner.py` | 主循环改为上述固定顺序；`_MUTABLE_ACTION_KEYS={coordinate,coordinate2}`（verify 只能改坐标，改其它字段→`verify_semantic_swap_blocked`）；新 helper `_safe_resolve`/`_safe_verify`（fail-closed BLOCK）/`_safe_approve`/`_frozen_action_fields`；`approve` 签名变 `(ResolvedAction, Observation)`；守卫/分类全部 key 在 resolved 动作；`verification_mode` 记 `pack_verify`/`legacy_raw_xml_fallback`。 |
| `packs/duolingo/pack.py` | 新增 `resolve`（迁移期 passthrough：`ResolveDecision(ALLOW, _legacy_semantic_target(a), dict(a))`），**不实现** verify → 走 legacy 兜底。 |
| `docs/adding-a-pack.md` §7 | 重写：Core=机制/Pack=应用判断/Consumer=业务授权；观察非原子、页面变化≠动作失效；记录 resolve/verify/RevalidationView/verification_mode；legacy 兜底=仅迁移；声明 0.x 非目标。 |
| `tests/test_runner_observation.py` | `ScriptedAdb`/`FakePack` 重构（phase 翻前台、resolve_fn/verify_fn 注入）；A/A2/B/F/M 适配单 dump；新增 codex 验收 1–11 十个用例 + `_track_phase` + resolve 助手（terminate/wait 透传修复）。 |
| `tests/test_runner.py` | `FakePack` 加 `resolve` passthrough；`ui_dump_calls` 断言 `8→5`（单 dump：步1 cap+reval=2、步2 cap+reval=2、步3 terminate cap=1）。 |

### 9.3 关键接口定义（供下一会话/转转 Pack 对齐）
```python
# mano/pack.py
ALLOW, REOBSERVE, BLOCK = "ALLOW", "REOBSERVE", "BLOCK"   # REPLAN 禁止

@dataclass(frozen=True)
class ResolvedAction:
    semantic_target: str
    resolved_action: dict
    metadata: dict = field(default_factory=dict)

@dataclass
class ResolveDecision:
    verdict: str                 # ALLOW / REOBSERVE / BLOCK
    semantic_target: str
    resolved_action: dict
    reason: str = ""
    metadata: dict = field(default_factory=dict)

@dataclass
class VerificationResult:
    verdict: str                 # ALLOW / REOBSERVE / BLOCK
    resolved_action: dict | None = None   # 仅允许重解析 coordinate/coordinate2
    reason: str = ""
    metadata: dict = field(default_factory=dict)

class Pack(Protocol):
    package: str
    def system_prompt(self, width, height) -> str: ...
    def instruction(self, history) -> str: ...
    def guard(self, xml, foreground) -> str | None: ...
    def resolve(self, *, proposed_action: dict, observation, model_text: str) -> ResolveDecision: ...
    # 可选：def verify(self, *, approved_intent: str, fresh_observation) -> VerificationResult
```

### 9.4 给人类的验收命令（我不跑）
```bash
cd /Users/wangjinxiang/Desktop/Projects/mano
uv run --extra dev pytest -q        # 用 uv，不用系统 py3.9
```
预期：全绿。12 点验收映射（codex 1–11 + 既有前台竞态=12）对应 `tests/test_runner_observation.py` 中：
1. `test_verify_allows_with_latest_coordinates` · 2 `test_verify_reobserve_when_target_gone` · 3&4 `test_verify_uses_new_coordinate_for_same_target` · 5 `test_core_foreground_check_precedes_verify` · 7 `test_wait_resolved_to_click_goes_through_approve_and_verify` · 8 `test_verify_cannot_swap_action_type` · 9 `test_dynamic_xml_churn_is_not_unstable` · 10 `test_legacy_fallback_executes_and_logs_mode` · 11 `test_model_may_wait_on_non_atomic_capture` · 12 既有前台翻转 `test_*foreground*`。

### 9.5 未实现能力（本轮明确不做；非遗漏）
崩溃恢复 / 持久化 journal、租约 fencing、XML 规范化与 stability_fingerprint、视觉↔XML 一致性检测、LangGraph、转转语义进 Core、转转真机。**legacy 兜底不得重新引入 XML 规范化。** 转转 Pack 需下一轮实现真正的 resolve+verify（本轮不做）。**完成自动测试后先停止，不做转转真机操作。**

### 9.6 git 待办（交人类/执行者；我不跑 git、不编造 hash — HD-07）
```bash
cd /Users/wangjinxiang/Desktop/Projects/mano
# 先实跑 uv run --extra dev pytest -q 确认全绿，再提交
git add mano/pack.py mano/observation.py mano/runner.py \
        packs/duolingo/pack.py docs/adding-a-pack.md \
        tests/test_runner.py tests/test_runner_observation.py
git commit -m "feat(runner): resolve/verify closed loop + non-atomic observation"
# git push -u origin feat/thin-runner   # 按需
```
提交后 commit hash 回填此处：`<执行者 push 后回填>`；pytest 结果回填：`<人类实跑后回填>`。

---

## 10. 第一批硬化（2026-10-14；A1–A4 + B2/B3/B5，离线可验证）

> 定稿《mano迭代细节回复_1014_1536》。本批只做**离线可验证的 Core 硬化**；第二批
> （Duolingo 真 resolve/verify，从答案选项 XML 文本节点 hit-test）**明确延后**。
> 诚实边界（HD-07）：commit hash / pytest 计数**待人类终端实跑回填**，占位符未执行。
> 我（builder）只写文件，不跑 git、不跑 pytest、不碰真机。

### 10.1 修了什么（问题 → 机制）
| 编号 | 问题 | 机制 |
|---|---|---|
| A1 | resolve/verify 返回非法动作（越界坐标/未知动作/非 dict）会在 `action_signature`/`execute_action` 处未捕获崩溃 | ALLOW 后、算签名前用 `validate_action` **fail-closed** 校验（resolve→`resolve_invalid_action`，verify→`verify_invalid_action`，零 ADB）；`execute_action` 入口再校验一次做纵深防御 |
| A2 | `recent_loop` 在判决门前**无条件** append，REOBSERVE/被拦步污染动作历史 | 只有**成功执行的设备动作**才进 `recent_loop`/`repeat`/`last_sem_sig` |
| A3 | `wait` 不重置 `reobserve_streak`，正常空等会把重观察计数推到上限误停 | wait 成功后 `reobserve_streak=0` |
| B2 | 物理坐标去重：同一控件像素漂移时精确签名失配、重复动作漏拦 | 新增 `semantic_signature=(semantic_target, 动作类型, 非坐标副作用)`，**排除坐标**；repeat/oscillation 守卫 key 在语义签名上 |
| B3 | “冻结所有非坐标字段”过脆（diagnostic/metadata 也被冻） | 改为 `execution_payload` **白名单**（动作类型 + 指定副作用字段），metadata/coordinate 排除；未知字段直接拒 |
| B5 | skew 用墙钟（wall-clock），NTP 回拨可伪造负/巨大跨度 | skew 用 `time.monotonic_ns`（`_mono_ms`）算 duration；epoch 时间戳仅存证 |

### 10.2 改了哪些文件（旧 `packs/duolingo/run.py` 仍一字未改；Duolingo 仍是 legacy 退化）
| 文件 | 核心改动 |
|---|---|
| `mano/action_codec.py` | 新增单一真源：`ALLOWED_ACTIONS`、`TYPE_TEXT_RE`、`KEYEVENT_CODES`、`_COORD_FIELDS`、`_EXEC_FIELDS`、`_ALLOWED_META`、`_SEMANTIC_LOOP_ACTIONS`；函数 `validate_action(action)->str|None`（稳定错误码）、`execution_payload(action)->dict`、`semantic_signature(target, action)->tuple|None`。 |
| `mano/adb_executor.py` | `type_text`/`keyevent` 复用 `TYPE_TEXT_RE`/`KEYEVENT_CODES`（删除内联副本）；`execute_action` 入口加 `validate_action` 纵深防御。 |
| `mano/observation.py` | 新增 `_mono_ms()`；capture 用单调钟算 `skew_ms`（保留 epoch 存证字段）。 |
| `mano/runner.py` | 主循环重写：ALLOW 后先 `validate_action`→失败零 ADB break；`semantic_signature` 去重 + 新振荡公式（`len(recent_loop)>=3 and sem==[-2] and [-1]==[-3] and sem!=[-1]`）；`recent_loop`/`last_sem_sig`/`repeat` 仅执行后更新；wait 重置 `reobserve_streak`；verify 候选先 `validate_action` 再 `execution_payload` 反交换；记录 `physical_signature`/`semantic_signature`；删除 `_loop_sig`/`_frozen_action_fields`/`_MUTABLE_ACTION_KEYS`。 |
| `tests/test_runner.py` | `FakePack.resolve` 的 `semantic_target` 内嵌坐标（否则去坐标签名后 A/B 点击不可分）。 |
| `tests/test_runner_observation.py` | G 用例改 monkeypatch `_mono_ms`；新增 G2(单调/回拨)、语义去重、REOBSERVE 不污染、wait 重置重观察、resolve/verify 越界坐标、resolve 未知动作、verify 非 dict，及 `action_codec` 直测（validate/execution_payload/semantic_signature/terminate meta）。 |
| `docs/adding-a-pack.md` §7 | 记录 `validate_action` 单一白名单（fail-closed）、`execution_payload` 反交换、`semantic_signature` 去坐标去重、monotonic skew；诚实标注 legacy 退化=坐标内嵌、Duolingo 仍 legacy、真 resolve 延后第二批。 |

### 10.3 关键接口（供第二批 / 转转 Pack 对齐）
```python
# mano/action_codec.py —— 动作面单一真源
ALLOWED_ACTIONS = {"click","swipe","type","key","system_button","wait","terminate"}

def validate_action(action) -> str | None:
    # None=通过；否则稳定错误码之一：
    #   not_a_dict / unknown_action / unknown_field / bad_meta_field /
    #   bad_coordinate / bad_coordinate2 / bad_text / bad_key / bad_button /
    #   bad_time / bad_status
    ...

def execution_payload(action) -> dict:
    # {"action": kind} + 非坐标副作用字段（text/button/status/...）
    # 排除 coordinate/coordinate2 与 diagnostic metadata —— 反交换比较用此

def semantic_signature(semantic_target, action) -> tuple | None:
    # (semantic_target, *sorted(execution_payload(action).items()))；坐标排除
    # wait/terminate 返回 None（不入循环历史）
```

### 10.4 12 项验收 → 测试名映射（人类实跑 `uv run --extra dev pytest -q`）
全量跑两个测试文件；下表为本批第一批铁律的逐条映射：

| # | 验收项（用户逐字） | 测试名（`tests/test_runner_observation.py`，除注明） |
|---|---|---|
| 1 | 全量 pytest 真实执行通过 | 人类实跑：`<待回填>`（builder 不跑） |
| 2 | resolve 非法输出零 ADB | `test_resolve_out_of_range_coordinate_fails_closed` · `test_resolve_unknown_action_fails_closed` |
| 3 | verify 非法输出零 ADB | `test_verify_out_of_range_coordinate_fails_closed` · `test_verify_non_dict_fails_closed` |
| 4 | foreground race 零 ADB | `test_core_foreground_check_precedes_verify` · `test_foreground_flip_during_freshness_dump_zero_action` |
| 5 | 语义目标稳定但坐标漂移 → FakePack 能识别语义重复 | `test_semantic_dedup_blocks_despite_coordinate_drift` · `tests/test_runner.py::test_oscillation_guard_blocks` |
| 6 | REOBSERVE 不污染动作历史 | `test_reobserve_does_not_pollute_action_history` |
| 7 | anti-swap 只允许坐标更新 | `test_verify_cannot_swap_action_type`（改副作用被拦）· `test_verify_uses_new_coordinate_for_same_target`（仅坐标放行） |
| 8 | duration/skew 用 monotonic 计算 | `test_skew_is_monotonic_not_wallclock` · `test_slow_capture_over_skew_bound_is_unstable` |
| 9 | wait 重置重观察计数（A3） | `test_wait_resets_reobserve_streak` |
| 10 | 单一白名单 / 未知字段拒 / terminate meta（B3） | `test_validate_action_accepts_whitelisted_actions` · `test_validate_action_rejects_bad_input` · `test_terminate_meta_field_is_allowed_and_excluded_from_identity` · `test_execution_payload_excludes_coordinate` |
| 11 | 语义签名去坐标、区分副作用（B2） | `test_semantic_signature_ignores_coordinate_drift` · `test_semantic_signature_distinguishes_side_effect_change` |
| 12 | HANDOFF 诚实标明 Duolingo 仍 legacy 退化 | 本节 10.2 / 10.5 + `docs/adding-a-pack.md` §7 legacy 限制段 |

> 既有 codex 1–11 + 前台竞态用例（§9.4）全部保留，本批未删除任一既有验收。

### 10.5 诚实边界 / 延后项
- **Duolingo 仍是 legacy passthrough**：`_legacy_semantic_target` 内嵌坐标（`xy=x,y`），其语义去重**退化为物理去重**——同控件像素漂移不算重复，靠 `max_steps`/wait-stall 兜底停。这是迁移路径的诚实退化，**非 Core 功能**，第二批真 resolve/verify 消除。
- **行为收紧（已记文档）**：`terminate` 若无法恢复出合法 status，现在被判 `resolve_invalid_action`（而非静默执行 `terminate_None`）。
- **第二批延后**：从 Duolingo 答案选项 XML 做真 resolve/verify（选项节点无 `resource-id`/`clickable`，需文本节点 hit-test），本批不做。
- **本批后停止**：完成文件编辑 + 自查后**不跑 git、不跑真机**；只回传人类需执行的 pytest 命令。

### 10.6 自查结果（builder，仅静态 + 手工走查，不等于 pytest）
- `get_errors` 对 `mano/action_codec.py`、`mano/adb_executor.py`、`mano/observation.py`、`mano/runner.py`、`tests/test_runner.py`、`tests/test_runner_observation.py` **均无报错**。
- 手工走查：oscillation/repeat/wait/semantic-dedup/REOBSERVE-no-pollution/wait-reset/validate-fail-closed/anti-swap/monotonic-skew 各路径 tap 次数与 result 已逐步推演（见 §10.4 映射）。
- ⚠️ 上述**不能代替 pytest**：`get_errors` 干净 + 心算 ADB 次数 ≠ 真实执行通过。以人类 `uv run --extra dev pytest -q` 输出为准。

### 10.7 git 待办（交人类/执行者；我不跑 git、不编造 hash — HD-07）
```bash
cd /Users/wangjinxiang/Desktop/Projects/mano
# 先实跑 uv run --extra dev pytest -q 确认全绿，再提交
git add mano/action_codec.py mano/adb_executor.py mano/observation.py mano/runner.py \
        tests/test_runner.py tests/test_runner_observation.py \
        docs/adding-a-pack.md HANDOFF_NEXT_SESSION.md
git commit -m "feat(core): batch1 hardening (validate/exec-payload/semantic-sig/monotonic skew)"
```
提交后 commit hash 回填此处：`<执行者 push 后回填>`；pytest 结果回填：`<人类实跑后回填>`。

