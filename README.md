<div align="center">
  <img src="imgs/agenticrepair_icon.png" alt="AgenticRepair logo" width="220" />
  <p><strong>AgenticRepair</strong></p>
</div>

# AgenticRepair: Multi-Faceted Program Context Engineering for Agentic Vulnerability Repair

<div align="center">
  <img src="imgs/agenticrepair_usage.png" alt="The AgenticRepair Framework" width="100%" />
</div>

---

## Overview

<div align="center">
  <img src="imgs/overview.png" alt="AgenticRepair Architecture Overview" width="100%" />
  <p><em>AgenticRepair architecture overview</em></p>
</div>

<details>
  <summary><strong>Quick Navigation</strong></summary>

  <ul>
    <li><a href="#environment-setup">Environment Setup</a></li>
    <li><a href="#configure-agenticrepair">Configure AgenticRepair</a></li>
    <li><a href="#run-agenticrepair">Run AgenticRepair</a></li>
    <li><a href="#run-ablation-studies">Run Ablation Studies</a></li>
    <li><a href="#outputs">Outputs</a></li>
    <li><a href="#evaluate-results">Evaluate Results</a></li>
    <li><a href="#experimental-artifacts">Experimental Artifacts</a></li>
    <li><a href="#customization">Customization</a></li>
    <li><a href="#citation">Citation</a></li>
  </ul>
</details>

---

## Environment Setup

- **OS:** Linux or Windows with WSL
- **Python:** 3.10 or later
- **Docker:** installed and running
  - Pre-built Docker images (`hwiwonlee/secb.eval.x86_64.*`) are pulled automatically from DockerHub on first run.
  - Images are x86_64-based. On Apple Silicon (ARM) Macs, Docker will emulate via Rosetta/QEMU, which works but runs significantly slower.

### 1. Create and activate a virtual environment

```bash
cd AgenticRepair
python -m venv .venv
source .venv/bin/activate
```

### 2. Install the local `smolagents` fork

AgenticRepair depends on the custom `smolagents` fork included in this repository under `agenticrepair/`.

Do **not** install `smolagents` from PyPI for this project. The public package does not include our custom tools and AgenticRepair-specific multi-agent workflow.

Install the local fork in editable mode:

```bash
cd AgenticRepair
pip install -e "./agenticrepair[secb,docker,litellm]"
```

This ensures the `smolagent` CLI resolves to the local implementation in this repository.

---

## Configure AgenticRepair

The main configuration file is:

- `config_patch_agenticrepair.toml`

Edit this file before running.

### Model configuration

```toml
[model_analyzer]
type = "LiteLLMModel"
model_id = "YOUR_ANALYZER_MODEL_ID"
api_key = "YOUR_API_KEY_HERE"

[model_fixer_verifier]
type = "LiteLLMModel"
model_id = "YOUR_FIXER_VERIFIER_MODEL_ID"
api_key = "YOUR_API_KEY_HERE"
```

### Dataset configuration

```toml
[dataset]
name = "SEC-bench/SEC-bench"
split = "cve"  # available splits: eval, cve, or oss
instance_ids = ["gpac.cve-2023-4754"]
```

`name` is the HuggingFace dataset identifier. Set `split` to one of `eval`, `cve`, or `oss`. Use `instance_ids` to run on specific instances, or remove the line to run on the entire split.

### Output configuration

```toml
[output]
output_dir = "/absolute/path/to/AgenticRepair/results"
```

> **Important:** `output_dir` must be an **absolute path**. Docker volume mounts do not accept relative paths. For example: `/home/user/AgenticRepair/results`.

Results will be written into a timestamped subdirectory under the configured output directory.

---

## Run AgenticRepair

After the environment is activated, the local fork is installed, and `config_patch_agenticrepair.toml` is configured, run:

```bash
cd AgenticRepair
source .venv/bin/activate
./run_patch_agenticrepair.sh
```

This script runs `smolagent secb-run --config config_patch_agenticrepair.toml`. You can also invoke the command directly.

Example with more workers:

```bash
smolagent secb-run --config config_patch_agenticrepair.toml --num-workers 2
```

Example for a single instance:

```bash
smolagent secb-run --config config_patch_agenticrepair.toml --instance-id gpac.cve-2023-4754
```

---

## Run Ablation Studies

AgenticRepair ablations can be run by editing the main configuration file:

- `config_patch_agenticrepair.toml`

The simplest way is to disable analyzers in:

```toml
[analyzer_enabled]
enable_static_analyzer = true
enable_dynamic_analyzer = true
enable_history_miner = true
```

Set any analyzer to `false` to remove that source of contextual insight from the pipeline.

Examples:

- Disable static analysis:

```toml
[analyzer_enabled]
enable_static_analyzer = false
enable_dynamic_analyzer = true
enable_history_miner = true
```

- Disable dynamic analysis:

```toml
[analyzer_enabled]
enable_static_analyzer = true
enable_dynamic_analyzer = false
enable_history_miner = true
```

- Disable history mining:

```toml
[analyzer_enabled]
enable_static_analyzer = true
enable_dynamic_analyzer = true
enable_history_miner = false
```

- Single-agent-style ablation:

```toml
[analyzer_enabled]
enable_static_analyzer = false
enable_dynamic_analyzer = false
enable_history_miner = false

[prompt_templates]
fixer_verifier = "patch_single_agent_scaffold_ablation.j2"
```

For the single-agent-style ablation, you should disable all three analyzers and also set the `fixer_verifier` prompt template to:

- `agenticrepair/src/smolagents/prompts/patch_single_agent_scaffold_ablation.j2`

After editing the config, run the same command:

```bash
./run_patch_agenticrepair.sh
```

or:

```bash
smolagent secb-run --config config_patch_agenticrepair.toml
```

---

## Outputs

For each run, AgenticRepair creates a timestamped output directory containing:

- Per-instance directories with generated artifacts
- `git_patch.diff` for patch outputs
- agent trajectories and summaries under `artifacts/`
- a root-level `output.jsonl`

If a run fails, the per-instance directory will contain an `error.txt` file.

---

## Evaluate Results

After a run completes, you can evaluate the generated patches and view the results using `eval_and_view_patch.sh`.

First, open the script and set the result folder name to your run's timestamped directory:

```bash
python -m secb.evaluator.eval_instances \
    --input-dir ./results/<YOUR_RESULT_FOLDER> \
    --type patch \
    --split cve \
    --agent smolagent \
    --mode all \
    --output-dir ./output/eval/patch
```

Replace `<YOUR_RESULT_FOLDER>` with the timestamped directory name generated by your run (e.g., `20260317_192107`). Adjust `--split` to match the split you ran on (`eval`, `cve`, or `oss`).

Then view the results:

```bash
python -m secb.evaluator.view_patch_results \
    --agent smolagent \
    --input-dir ./output/eval/patch
```

Both commands are included in `eval_and_view_patch.sh`. Edit the script with your result folder path and run:

```bash
./eval_and_view_patch.sh
```

---

## Experimental Artifacts

The execution logs and agent trajectories behind the paper are included in this repository:

- `results/` — one directory per run, with per-instance agent trajectories, generated patches, and that run's scored reports
- `reports/` — the same run-level report files under readable names, without the trajectories

See [ARTIFACTS.md](ARTIFACTS.md) for the run index: which directory corresponds to which
configuration, how many instances each resolved under the three evaluation modes, and how to
re-run the evaluator to reproduce a run's reports.

The baseline runs were produced with the single-agent configuration in `config_patch.toml`
(`[scaffold] type = "single"`), as opposed to `config_patch_agenticrepair.toml`, which is the
multi-agent AgenticRepair configuration described above.

---

## Customization

To change the behavior of AgenticRepair, edit the main configuration file:

- `config_patch_agenticrepair.toml`

This file controls:

- model selection
- enabled tools
- analyzer settings
- dataset split and instance selection
- output directory
- runtime limits

Prompt templates live under:

- `agenticrepair/src/smolagents/prompts/`

---

## Citation

```bibtex
Under Review
```
