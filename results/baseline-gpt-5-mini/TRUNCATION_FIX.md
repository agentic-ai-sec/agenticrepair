# Patch collection defect and the repaired patches in this folder

## The defect

Patch collection in `agenticrepair/src/smolagents/cli.py` called `.strip()` on the collected
diff. In a unified diff a context line for a blank source line is the single byte `" "`, so
stripping removed such lines from the end of the patch while the hunk header still counted them.
`git apply` then rejected the whole patch with `corrupt patch at line N`, where N is one past the end
of the patch, and the instance was recorded as a failure at the apply step without ever being built
or run.

The agent's own work was not affected: the edits it made in its container were correct, only the
saved copy of them was damaged. Fixed in patch collection (see `cli.py`); this folder
is from a run made before that fix.

## What is in this folder

| File | Contents |
|---|---|
| `output.jsonl` | the raw collection, as produced by the run (unchanged) |
| `<instance>/git_patch.diff` | the patch exactly as the run collected it (unchanged) |
| `<instance>/git_patch.repaired.diff` | present for 40 instances: the same patch with the removed context lines restored |
| `output.repaired.jsonl` | `output.jsonl` with those 40 patches repaired |
| `report_{strict,medium,generous}.jsonl` | the verdicts after repair |
| `truncation_repaired_instances.json` | which instances were repaired, and how many lines each |

The raw files are kept unchanged on purpose: they are the record of what the run produced, defect
included. The pre-repair verdicts are preserved as `reports/baseline-gpt-5-mini/report_{mode}.prerepair.jsonl.gz`.

## The repair

40 patches here were truncated: 37 missing 1 line, 3 missing 2 lines. Every instance in this set failed at the
git-apply step with `corrupt patch at line N` where N is exactly one line past the end of the patch,
and in every case the final hunk header declared more lines than the hunk body contained. All of them
parse cleanly once restored, every hunk matching its header, and each was validated with
`git apply --check`.

Repair is restoration, not editing: the only bytes added are the missing final newline and the
removed blank context lines, and the raw patch is a byte-prefix of the repaired one. Nothing else in
the diff is altered. Two independent detectors — the log fingerprint plus final-hunk shortfall,
and a count-driven scan of every hunk in the diff — select the same
set of instances with the same number of restored lines.

## How the reports were produced

Only the 43 instances whose patch bytes changed were re-evaluated; every other
verdict is carried over unchanged from the pre-repair evaluation, because its patch is byte-identical
and the evaluation is deterministic. That determinism was verified, not assumed: 12
untouched instances were re-evaluated alongside the repaired ones as a control, weighted toward the
slowest emulated builds and toward instances whose PoC actually runs, and every one reproduced its
pre-repair verdict in all three modes.

Re-evaluation used the shipped evaluator, one instance per process, which raises only the
container wall-clock cap — not the 10-second PoC timeout that decides correctness.

## Outcome

| Mode | Before | After | Change |
|---|---|---|---|
| strict | 101/300 | 116/300 | +15 |
| medium | 183/300 | 212/300 | +29 |
| generous | 188/300 | 219/300 | +31 |

Of the 40 repaired patches (strict): 15 now pass,
23 apply but still fail to compile or to fix the PoC, and
2 still fail to apply.

## Instances re-run for a second reason

3 instance(s) in this run carried no usable verdict: the patch applied and the container
then died mid-build, which the report recorded as `exit_code -1`. These were re-evaluated in the same
session as the repaired patches and their rows were replaced too. They are not truncation
recoveries and are excluded from the counts above.

- `matio.cve-2019-20017`: failed on re-evaluation (was recorded as a failure with no usable verdict)
- `php.ossfuzz-42501106`: failed on re-evaluation (was recorded as a failure with no usable verdict)
- `php.ossfuzz-427814456`: failed on re-evaluation (was recorded as a failure with no usable verdict)

## Reproducing

```
python -m secb.evaluator.eval_instances \
    --input-dir ./results/baseline-gpt-5-mini \
    --type patch --split eval --agent smolagent --mode all \
    --output-dir ./output/eval/patch
```

The evaluator reads `output.jsonl`, so copy `output.repaired.jsonl` over it in a scratch copy of
this folder (or point `--input-dir` at such a copy) to reproduce the repaired reports.

Note `--split eval` for all 300 instances; `--split cve` gives the 200 CVE instances only.
