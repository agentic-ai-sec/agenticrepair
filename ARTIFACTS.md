# Experimental Artifacts

This repository ships the execution logs and agent trajectories behind the AgenticRepair paper,
alongside the implementation itself. There are two artifact trees:

- **`results/`** — one directory per run, holding the per-instance agent trajectories and
  generated patches, plus that run's scored reports. This is the complete record.
- **`reports/`** — the same run-level report files, under the same directory names, without the
  per-instance trajectories. Convenient when you only want the verdicts.

Every file in `reports/` is a byte-identical copy of the corresponding file in `results/`, so
nothing is lost by ignoring `reports/` entirely.

## Run index

Instances resolved, by evaluation mode. The `split` column matters: `cve` is the 200 CVE
instances, `eval` is those 200 plus 100 OSS-Fuzz instances.

| Directory | Configuration | Model | Split | strict | medium | generous | In `reports/` |
|---|---|---|---|---|---|---|---|
| `agenticrepair-gpt-5.2` | AgenticRepair, all four agents | GPT-5.2 | eval (300) | 266 | 276 | 276 | yes |
| `agenticrepair-gpt-5-mini` | AgenticRepair, all four agents | GPT-5-mini | cve (200) | 118 | 154 | 156 | yes |
| `agenticrepair-gpt-5-nano` | AgenticRepair, all four agents | GPT-5-nano | cve (200) | 22 | 41 | 42 | yes |
| `baseline-gpt-5.2` | Single-agent baseline | GPT-5.2 | eval (300) | 146 | 251 | 256 | yes |
| `baseline-gpt-5-mini` | Single-agent baseline | GPT-5-mini | eval (300) | 116 | 212 | 219 | yes |
| `baseline-gpt-5-nano` | Single-agent baseline | GPT-5-nano | eval (300) | 41 | 67 | 68 | yes |
| `agenticrepair-gpt-5.2-no-static-analysis` | Static analyzer disabled | GPT-5.2 | cve (200) | 170 | 178 | 179 | yes |
| `agenticrepair-gpt-5.2-no-dynamic-analysis` | Dynamic analyzer disabled | GPT-5.2 | cve (200) | 167 | 174 | 174 | no |
| `agenticrepair-gpt-5.2-no-history-miner` | History miner disabled | GPT-5.2 | cve (200) | 165 | 174 | 176 | yes |
| `agenticrepair-gpt-5.2-no-program-analysis` | Static and dynamic analyzers both disabled | GPT-5.2 | cve (200) | 165 | 175 | 175 | yes |
| `agenticrepair-gpt-5.2-single-agent-scaffold` | Single-agent scaffold | GPT-5.2 | cve (200) | 70 | 129 | 131 | yes |

The configuration column is not inferred from directory names; it reflects which analyzer
trajectories each run actually contains and which prompt its fixer agent received.

### Comparing the main run against the ablations

The main GPT-5.2 run was evaluated on the 300-instance `eval` split, while the ablations were
evaluated on the 200-instance `cve` split. Do not compare 266/300 against 165/200. Restricted to
the same 200 CVE instances, the main run resolves **180 strict, 180 medium, 180 generous**, which
is the number comparable to the ablation rows above.

### Evaluation modes

Set by `--mode` in `secb.evaluator.eval_instances`:

- `strict` — only exit code 0 is accepted.
- `medium` — the exit code must match the one recorded in the dataset.
- `generous` — any non-timeout exit without sanitizer errors is accepted.

## Per-instance layout

Each `results/<run>/<instance_id>/` directory contains the patch the agent produced as
`git_patch.diff`, plus an `artifacts/` directory. Instances that crashed or timed out carry an
`error.txt` or `.timeout_flag` instead of trajectories.

Multi-agent runs (AgenticRepair, all ablations, and the GPT-5-nano baseline) use:

| File | Contents |
|---|---|
| `artifacts/static_analyzer_trajectory.jsonl` | static analyzer's step-by-step trajectory |
| `artifacts/dynamic_analyzer_trajectory.jsonl` | dynamic analyzer's trajectory |
| `artifacts/history_miner_trajectory.jsonl` | history miner's trajectory |
| `artifacts/fixer_verifier_trajectory.jsonl` | fixer/verifier's trajectory |
| `artifacts/multiagent_result.json` | each agent's final output; disabled agents are `null` |
| `artifacts/summary.json` | final status, token usage, wall-clock duration |

Only the enabled agents' trajectory files are present, so the file list identifies the
configuration directly. The GPT-5-mini and GPT-5.2 baselines instead ran through the plain
single-agent path and use `artifacts/trajectory.jsonl`, `artifacts/output.json`, and
`artifacts/meta.json`.

The GPT-5-nano baseline is a single-agent baseline that happened to be executed through the
multi-agent runner with all three analyzers disabled. It therefore has the multi-agent file
layout with only `fixer_verifier_trajectory.jsonl`, but its agent received the same task prompt,
byte for byte, as the other two baselines.

## Run-level files

| File | Contents |
|---|---|
| `output.jsonl` | one row per instance, exactly as the run produced it |
| `output.repaired.jsonl` | `output.jsonl` with truncated patches restored; this is what was evaluated |
| `report_{strict,medium,generous}.jsonl` | per-instance verdicts, derived from `output.repaired.jsonl` |
| `truncation_repaired_instances.json` | which instances were repaired, and how many lines each |
| `TRUNCATION_FIX.md` | full description of the patch-collection defect and the repair |
| `report_*.prerepair.jsonl.gz` | verdicts before the repair, kept for reference (`reports/` only) |

Patch collection in an earlier version of `agenticrepair/src/smolagents/cli.py` called `.strip()`
on the collected diff, which deleted trailing blank context lines and made `git apply` reject the
whole patch. The agents' actual edits were unaffected; only the saved copies were damaged. Each
run directory's `TRUNCATION_FIX.md` records how many patches this affected and what the verdicts
were before and after. The raw `output.jsonl` is deliberately left unrepaired as the record of
what the run produced.

## Reproducing a run's reports

The evaluator reads `output.jsonl`, so copy `output.repaired.jsonl` over it in a scratch copy of
the run directory, then point `--input-dir` at that copy:

```bash
python -m secb.evaluator.eval_instances \
    --input-dir ./results/agenticrepair-gpt-5.2 \
    --type patch \
    --split eval \
    --agent smolagent \
    --mode all \
    --output-dir ./output/eval/patch
```

Use `--split eval` for the 300-instance runs and `--split cve` for the 200-instance runs.

`results/` and `reports/` use the same directory name for a run. `reports/` has every run except
`agenticrepair-gpt-5.2-no-dynamic-analysis`; that run's report files are only under `results/`.
