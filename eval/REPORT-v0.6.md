# RepoPilot v0.6 Evaluation Report

**SWE-bench Verified subset · official Docker judge · containerized inference · dollar-metered budgets · 3-round majority vote**

Date: 2026-08-03 · Model: `deepseek-chat` · Agent commit: `90c36a1`

---

## 中文摘要

在官方 SWE-bench Docker 判分器、官方逐题镜像内推理、按美元结算预算（$0.10/题）、每配置 3 轮多数票的口径下，对 RepoPilot 完整版（full：Reviewer + Explorer + 停机策略）与朴素 ReAct 基线（baseline-b）做了 dev-15 + test-30 全量对比（后者为冻结配置后的确认集，未做任何调参）：

1. **解题率打平（噪声内），但 full 花约 2 倍的钱。** dev：6/15 vs 7/15（噪声底噪 3.0）；test：12/25 vs 11/25（噪声底噪 6.0）。两个划分上效应量均小于噪声。中位 token 1.9 倍，实际美元 2.0 倍。
2. **full 买到的是"从不说谎"。** 两个划分合计，full 声称修好 7 次、7 次全部被官方判分证实（100%）；baseline 声称 51 次、只有约 57% 是真的。对"产出能否免人工复查直接用"这个问题，两者是天壤之别。
3. **代价是巨量漏报，且主犯已锁定：Reviewer 的终审否决正确率仅 52%（35 杀对 / 32 冤杀），在 test 上低于抛硬币。** full 实际被官方判修好 62 次，只敢声称 7 次；32 份正确补丁死在自家审查员手里。
4. **天花板是模型能力，不是预算。** 4 倍预算探针 0/2 转绿，且两题都只花了不到 2/3 的钱就自行放弃——"继续加钱"这条优化路线正式关闭。
5. **判分基建的翻案价值实测：** venv 判分曾把 full 在 pytest-5631 上的三轮正确补丁全部误判为失败；官方 gold 校准把可判性从 9/15 修复到 15/15（dev）与 25/30（test，5 题官方 gold 亦不过、按测量剔除）。

下一步（v0.7）：改报告层而非能力层——审查三态化（否决不再湮灭结论）+ 预算收尾协议，预期把声称覆盖率从 7/62 拉到 5 倍以上，且不增加任何推理成本。

---

## 1. Setup

**Task.** Repository-level bug fixing on SWE-bench Verified instances: the agent receives an issue and a repo at `base_commit`, and must produce a patch. Hidden tests (FAIL_TO_PASS / PASS_TO_PASS) never reach the inference process (AST-enforced isolation, input-side leak scan = 0 hits across all runs).

**Configurations.**
- `full`: planner/executor state machine + independent Reviewer (merge gate) + Explorer + halting policy.
- `baseline-b`: plain ReAct loop, single fix attempt, no reviewer/explorer/halting. Identical budget, tools, model, prompts scaffold.

**Splits (frozen before any inference, checksummed).** dev-15 for iteration; test-30 as a confirmatory set run once under the dev-frozen config; holdout-10 untouched.

**Environment & judge (new in v0.6).**
- Inference runs inside the official per-instance SWE-bench Docker images via an executor seam: commands `docker exec` into a long-lived container; git and file I/O stay host-side on a bind-mounted worktree extracted from the image (`/testbed`, byte-identical to the judge's tree, image-side edits sealed in an env-baseline commit).
- Primary verdicts come from the official SWE-bench harness (`swebench.harness.run_evaluation`, prebuilt images). The local in-container grader is retained as a diagnostic; agreement with the official judge: 247/270 runs (91.5%), disagreements concentrated in 7 instances.
- Official gold calibration gates the certifiable denominator: dev 15/15 pass; test 25/30 (the 5 failures stay in the denominator, reported as judge-untrusted; a recheck found 2 of 5 flaky, 3 stable failures).

**Budgets.** $0.10 per instance (dollar-metered with cache-hit-aware pricing; 92% of billed tokens are cache hits at 1/4 price, which is why token ceilings stopped measuring anything), 1800s soft wall clock, 100 tool calls, 3 rounds per configuration. Majority vote per instance; per-configuration flip rate × n is the noise floor no effect below which is interpreted.

**Hardware.** dev rounds: WSL2 laptop (8GB, 2 workers). test rounds: cloud vhp-12c-24gb (24GB, 6 workers). Each split's comparison is within a single machine.

## 2. Headline results

### dev-15 (3 rounds, official judge, n=15)

| | majority resolved | exclusive wins | claim precision | median tokens | total cost (45 runs) |
|---|---:|---:|---:|---:|---:|
| full | 6/15 | 0 | 3/3 = 100% | 615,552 | $3.51 |
| baseline-b | 7/15 | 1 | 14/26 ≈ 54% | 315,280 | $1.55 |

Noise floor: 3.0 instances → **the 6-vs-7 gap is not interpretable.**

### test-30 confirmatory (3 rounds, frozen config, n=25 certifiable)

| | majority resolved | exclusive wins | claim precision | median tokens | total cost (90 runs) |
|---|---:|---:|---:|---:|---:|
| full | 12/25 | 2 | 3/3 = 100% | 644,388 | $6.43 |
| baseline-b | 11/25 | 1 | 16/25 = 64% | 347,709 | $3.47 |

Noise floor: 6.0 instances → **again no interpretable resolve-rate difference.** The confirmatory set reproduces the dev conclusion.

## 3. The claims ledger (what the resolve rate hides)

Per-run accounting against official verdicts, both splits combined (135 full runs, 135 baseline runs):

| outcome | full | baseline-b |
|---|---:|---:|
| claimed fixed, officially correct | 7 | 32 |
| **claimed fixed, officially wrong (lied)** | **0** | **27** |
| resolved but never claimed (silent) | 55 | 26 |
| honest failure | 73 | 50 |

- full's word is perfect: **7/7 claims true, zero lies across 135 runs.** baseline lies in ~46% of its claims — its "done" is close to a coin flip.
- But full resolves 62 runs and claims 7: **89% of its successes are silent.** Silent-success attribution: Reviewer wrongful veto 32, budget/wall-clock clipping ~18, other loop exits ~5.

### The Reviewer's confusion matrix (final vetoes, official ground truth)

| split | final vetoes | correct kills | wrongful kills | precision |
|---|---:|---:|---:|---:|
| dev | 20 | 13 | 7 | 65% |
| test | 47 | 22 | 25 | **47%** |
| combined | 67 | 35 | 32 | 52% |

The Reviewer is simultaneously the source of the 0-lie record and — at final-veto time — a coin flip that destroyed 32 correct patches' claims. On the harder test set its veto precision drops below chance: patches that survive to final review are mostly good, so a conservative gate kills mostly-good patches.

## 4. Ceiling probe: capability, not budget

The two instances never resolved by full and majority-clipped by budget (seaborn-3069, pylint-4551) were rerun at 4× budget ($0.25 / 5400s):

- 0/2 converted. Both runs ended in Reviewer rejection having spent only **49% and 61% of the allowed money** — the agent ran out of ideas, not budget, and the officially-graded patches were indeed wrong (two correct kills).
- Conclusion: for this model and architecture, raising budgets buys nothing on these instances. The "spend more" optimization line is closed; failure-attribution tables are the next place to look.

## 5. What the judge migration was worth

- The venv judge systematically under-credited full: on pytest-5631 all three v0.5.1 full-round patches were officially correct but locally graded failed (the sole systematic local/official disagreement in the v0.5.1 regrade, 3/3 rounds).
- Official gold calibration turned "judgeable" from an assumption into a measurement: dev 9/15 (venv era) → 15/15; test 25/30 with the 5 failures explicitly quarantined (2 flaky, 3 stable).
- Environment recovery: containerized prepare succeeded on 45/45 instances (the venv path had permanently lost 4/15 on dev), giving the first full-denominator evaluation in the project's history.

## 6. Costs

| item | amount |
|---|---:|
| inference, dev sweep (90 runs incl. salvaged round) | ≈ $5.1 |
| inference, test sweep (180 runs) | $9.91 |
| ceiling probe | $0.28 |
| smoke tests | ≈ $0.15 |
| **API total** | **≈ $15.4** |
| cloud server (~14h × $0.197) | ≈ $2.8 |

## 7. Threats to validity

1. **Single model** (deepseek-chat). Conclusions about the architecture are conditioned on it; no cross-model robustness check this round.
2. **Small n.** 15 + 30 instances, 7 repos; noise floors (3.0 / 6.0) are honest but wide. Only effects larger than the floor are claimed — which is why the headline is a null result plus a precision result.
3. **Contamination.** The model may have seen SWE-bench repos/patches in training. This biases absolute resolve rates upward for both configurations; the paired comparison and the claims analysis are less exposed.
4. **Judge residuals.** 5/30 test instances fail official gold (2 flaky); they stay in denominators but carry no trusted verdicts. Local/official judge disagreement is 8.5% overall, concentrated in 7 instances.
5. **Machine heterogeneity across splits** (dev on a laptop, test on cloud). Every compared pair ran on the same machine; cross-split level differences (e.g., absolute resolve rate) should not be over-read.
6. **Timeout sensitivity.** 1800s soft / test-command 900s; slow suites (seaborn) sit near the edge, contributing to instability.

## 8. v0.7 roadmap (evidence-ranked)

1. **Three-state review verdict** — a veto no longer annihilates the claim; rejected-but-submitted patches carry an explicit "reviewer holds reservations" tier. Paper calculation from existing data: recovers all 32 wrongful-kill claims into a separately-audited tier at zero inference cost, leaves the 0-lie confident tier untouched.
2. **Budget wind-down protocol** — at 90% budget, stop exploring, run one scoped verification, emit a verdict from the last evidence; recovers most clipping-type silent successes (~18 runs).
3. **Evidence-only vetoes** — the Reviewer may only veto on observed failures (test output, regressions), not hypothesized risks; its test-set veto precision (47%) is the argument.
4. Grading parallelism should scale with machine memory (the cloud run wasted ~2h judging at laptop-era concurrency).
5. Re-run the paired `no-reviewer` ablation under v0.6 conditions to price the Reviewer's contribution end-to-end.

---

*Everything in this report is reproducible from the repo: frozen splits (`eval/splits/`), manifests and per-run artifacts (`eval/runs/v0.6-*`), aggregations (`REPEATS-v0.6-dev.md`, `REPEATS-v0.6-test.md`), gold calibrations (`eval/envs/*/official_gold.json`), and the official harness reports (`eval/runs/official/`).*
