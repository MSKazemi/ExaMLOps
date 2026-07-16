# Continuous evaluation & the regression gate (C2 · C3)

ExaMLOps evaluates model/prompt versions with **eval suites** (deterministic + LLM-as-judge
evaluators) and blocks promotion when a version **regresses** against the incumbent or
violates an absolute floor.

Design: ADR 0007 (C2) + ADR 0008 (C3) · specs `design/vision/specs/C2-continuous-eval.md`
and `C3-eval-regression-gate.md`.

## C2 — eval suites

An **evaluator** scores one item → `{metric, score, detail}`. A **suite** bundles
evaluators and runs them over items, persisting the mean per-metric score to
`platform_db.eval_suite_results` (idempotent per suite × version × run × metric).

Deterministic evaluators: `ExactMatch`, `Regex`, `NumericTolerance`, `JSONValid`.
LLM-as-judge: `LLMJudge(judge_fn, rubric, judge_model, prompt_version)` — runs at
temperature 0 and records the judge model + prompt version per result (governance).

```bash
# items.jsonl: one {"output": "...", "reference": "...", "prompt": "..."} per line
exa eval run smoke --model JPCP --items ./eval/items.jsonl --version 18
exa eval run smoke --model JPCP --items ./eval/items.jsonl --sample 20   # sample by request_hash
```

`exa eval run` exits non-zero only on **execution error** — never on low scores (that's
the gate's job, C3). Aggregate scores feed A2's `eval_score` facet, C6 SLOs, D1 evidence,
and the dashboard Evaluation page.

### Judge calibration

`judge_calibration(judge_scores, human_labels)` computes judge↔human agreement (+ MAE)
over a labelled set, so a judge's reliability is measured before it gates anything.

## C3 — regression gate

A gate is per-model config: `{suite, baseline_alias, metrics:[{name, min?, max_drop?}],
mode ∈ {block, warn}}`. A metric **fails** if it regresses beyond `max_drop` versus the
baseline alias, or violates `min`. Error metrics (rmse) use `--lower-is-better`.

```bash
exa eval gate set JPCP --suite smoke \
    --metric accuracy:min=0.8:max_drop=0.01 --metric groundedness:min=0.8 --mode block
exa eval gate show JPCP
exa eval gate run  JPCP 18            # exit 1 in block mode on failure — CI-safe
```

### Enforcement in `promote`

`exa pipeline promote` runs the gate before moving an alias. In `block` mode a failure
**refuses** the promotion and audits `promotion_blocked_by_gate`; `--force` overrides and
audits `eval_gate_override` with the failing metrics (D4). The latency SLA check in
`validate-model` continues to run as an additional, independent gate.

```bash
exa pipeline promote jpcp --if-rmse-lt 5.0            # gate-checked
exa pipeline promote jpcp --if-rmse-lt 5.0 --force    # override a failing gate (audited)
```

Every gate run persists a structured report (`platform_db.gate_reports`) shown on the
dashboard Promotion page.
