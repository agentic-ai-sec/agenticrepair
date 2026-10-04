# Patch collection defect and the repaired patches in this folder

## The defect

Patch collection in `agenticrepair/src/smolagents/cli.py` called `.strip()` on the collected
diff. In a unified diff a context line for a blank source line is the single byte `" "`, so
stripping removed such lines from the end of the patch while the hunk header still counted
them. `git apply` then rejected the whole patch with `corrupt patch at line N`, where N is one
past the end of the patch, and the instance was recorded as a failure at the apply step
without ever being built or run.

The agent's own work was not affected: the edits it made in its container were correct, only
the saved copy of them was damaged. Fixed in patch collection (see `cli.py`); this folder is
from a run made before that fix.

## What is in this folder

| File | Contents |
|---|---|
| `<instance>/git_patch.diff` | the patch exactly as the run collected it (truncated where the defect applied) |
| `<instance>/git_patch.repaired.diff` | present for 25 instances: the same patch with the removed context lines restored |
| `output.jsonl` | the raw collection, as produced by the run |
| `output.repaired.jsonl` | the patches as evaluated: `output.jsonl` with those 25 repaired |
| `report_{strict,medium,generous}.jsonl` | the verdicts, which come from `output.repaired.jsonl` |
| `truncation_repaired_instances.json` | which instances were repaired, and how many lines each |

The raw files are kept unchanged on purpose: they are the record of what the run produced,
defect included. `output.repaired.jsonl` is what the reports were produced from, so evaluating
it reproduces them.

## The repair

25 patches here were truncated: 25 were missing one context line. All
25 parse cleanly once restored, every hunk matching its header. Only the repaired patches were
re-evaluated; every other verdict is carried over unchanged from the original evaluation.

Repair is restoration, not editing: a patch is only touched when git's error points exactly one
line past its end, the fingerprint of this defect, and the number of missing lines follows from
the final hunk's header. Nothing else in the diff is altered.

## Verdicts (200 instances)

This run had no evaluation report, so no verdict could be carried over: every instance was
evaluated from `output.repaired.jsonl` in one pass.

| Mode | Passed |
|---|---|
| strict | 167 |
| medium | 174 |
| generous | 174 |

Because there is no pre-repair report for this run, the reports here cannot be compared
instance-by-instance with an earlier evaluation; the patches whose bytes changed are listed in
`truncation_repaired_instances.json`, and 18 of those 25 pass in strict mode.

Patch selection here also differs slightly: with no report there is no `corrupt patch at line N`
log to match, so the 25 instances were selected by the structural signature alone, a final hunk
shorter than its own header. The same selection rule reproduces the other runs' repairs exactly.
