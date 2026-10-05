# AgenticRepair agent library

This directory contains the agent library that AgenticRepair runs on. It is a fork of
[`smolagents`](https://github.com/huggingface/smolagents) v1.23.0 by the Hugging Face team,
used under the Apache License 2.0 (see [LICENSE](LICENSE)). The package is still importable
as `smolagents` so that upstream code paths keep working unchanged.

Upstream documentation, examples, and tests have been removed from this artifact to keep it
focused on reproducing the paper. For general `smolagents` usage, refer to the
[upstream repository](https://github.com/huggingface/smolagents) and its
[documentation](https://huggingface.co/docs/smolagents).

## What AgenticRepair adds

| Path | Purpose |
| --- | --- |
| `src/smolagents/multiagent_scaffold.py` | The multi-agent scaffold: static analyzer, dynamic analyzer, history miner, and fixer-verifier roles, plus prompt-template resolution |
| `src/smolagents/docker_multiagent_runner.py` | Runs the multi-agent scaffold against a benchmark instance inside its Docker environment |
| `src/smolagents/docker_app_runner.py` | Single-agent runner used for the baseline configuration |
| `src/smolagents/cli.py` | The `smolagent secb-run` entry point, including unified-diff extraction and truncation repair |
| `src/smolagents/prompts/multiagent/` | Default prompt templates, one per agent role |
| `src/smolagents/prompts/multiagent_ablation_*/` | Prompt templates for the ablation configurations, which drop the corresponding findings block |
| `src/smolagents/prompts/patch.j2` | Patch-generation prompt for the single-agent baseline |
| `src/smolagents/prompts/patch_single_agent_scaffold_ablation.j2` | Patch prompt for the single-agent-scaffold ablation |

Installation and usage are documented in the [repository README](../README.md).
