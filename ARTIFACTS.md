# Experimental Artifacts

This repository ships the execution logs and agent trajectories behind the AgenticRepair paper,
alongside the implementation itself. There are two artifact trees:

- **`results/`** — one directory per run, holding the per-instance agent trajectories and
  generated patches, plus that run's scored reports. This is the complete record.
- **`reports/`** — the same run-level report files under readable names, without the
  per-instance trajectories. Convenient when you only want the verdicts.

Every file in `reports/` is a byte-identical copy of the corresponding file in `results/`, so
nothing is lost by ignoring `reports/` entirely.

## Run index

Instances resolved, by evaluation mode. The `split` column matters: `cve` is the 200 CVE
instances, `eval` is those 200 plus 100 OSS-Fuzz instances.

| `results/` run | Configuration | Model | Split | strict | medium | generous | `reports/` alias |
|---|---|---|---|---|---|---|---|
| `20260101_190935-gpt-5.2-agenticrepair-full-run` | AgenticRepair, all four agents | GPT-5.2 | eval (300) | 266 | 276 | 276 | — |
| `20251229_003254_gpt-5-mini-agenticrepair-full-run` | AgenticRepair, all four agents | GPT-5-mini | cve (200) | 118 | 154 | 156 | `gpt-5-mini-agentmem` |
| `agenticrepair_gpt_5_nano_results/20260126_145051_agentmem_gp5-5-nano_cve` | AgenticRepair, all four agents | GPT-5-nano | cve (200) | 22 | 41 | 42 | nested, see below |
| `20251231_145937-baseline-smolagents-gpt-5.2` | Single-agent baseline | GPT-5.2 | eval (300) | 146 | 251 | 256 | `baseline-gpt-5.2` |
| `20251218_143241-baseline-smolagents-gpt-5-mini` | Single-agent baseline | GPT-5-mini | eval (300) | 116 | 212 | 219 | `baseline-gpt-5-mini` |
| `20260110_225319-baseline-smolagents-gpt-5-nano` | Single-agent baseline | GPT-5-nano | eval (300) | 41 | 67 | 68 | `baseline-gpt-5-nano` |
| `20260111_002133-gpt-5.2-ablation-no-static-analysis` | Static analyzer disabled | GPT-5.2 | cve (200) | 170 | 178 | 179 | `gpt-5.2-agentmem-no-static-analysis` |
| `20260115_212000_gpt-5.2_ablation_no_program_execution` | Dynamic analyzer disabled | GPT-5.2 | cve (200) | 167 | 174 | 174 | — |
| `20260104_235345-gpt-5.2-ablation-no-commit-history` | History miner disabled | GPT-5.2 | cve (200) | 165 | 174 | 176 | `gpt-5.2-agentmem-no-history-miner` |
| `20260108_153605-gpt-5.2-ablation-no-program-analysis-full-run` | Static and dynamic analyzers both disabled | GPT-5.2 | cve (200) | 165 | 175 | 175 | `gpt-5.2-agentmem-no-program-analysis` |
| `20260115_193239_ablation_single_agent_scaffold_full_run` | Single-agent scaffold | GPT-5.2 | cve (200) | 70 | 129 | 131 | `gpt-5.2-agentmem-single-agent-scaffold` |

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
    --input-dir ./results/20260101_190935-gpt-5.2-agenticrepair-full-run \
    --type patch \
    --split eval \
    --agent smolagent \
    --mode all \
    --output-dir ./output/eval/patch
```

Use `--split eval` for the 300-instance runs and `--split cve` for the 200-instance runs.

## Two irregularities worth knowing

`results/agenticrepair_gpt_5_nano_results/` is nested one level deeper than the other runs and
contains two directories: the run itself,
`20260126_145051_agentmem_gp5-5-nano_cve`, and `gpt-5-nano-agentmem`, which is that run's
report files duplicated under a readable name. The duplicate is byte-identical and plays the role
that a `reports/` entry plays for the other runs.

`reports/` covers 8 of the 11 runs. The main GPT-5.2 run, the no-dynamic-analyzer ablation, and
the GPT-5-nano AgenticRepair run have no `reports/` alias; read their report files from
`results/` directly.
