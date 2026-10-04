#!/usr/bin/env python
# coding=utf-8

# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse
import importlib.resources
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Template


try:
    import tomli
except ImportError:
    try:
        import tomllib as tomli  # type: ignore  # Python 3.11+
    except ImportError:
        raise ImportError("Please install 'tomli' package: pip install tomli")

# datasets is imported lazily in run_secb_evaluation to avoid import errors when not using secb-run

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.table import Table

from smolagents import (
    CodeAgent,
    InferenceClientModel,
    LiteLLMModel,
    Model,
    OpenAIModel,
    Tool,
    ToolCallingAgent,
    TransformersModel,
)
from smolagents.default_tools import TOOL_MAPPING
from smolagents.monitoring import AgentLogger, LogLevel
from smolagents.remote_executors import DockerAgentRuntime
from smolagents.multiagent_scaffold import MultiAgentOrchestrator, run_multiagent_scaffold


console = Console()

leopard_prompt = "How many seconds would it take for a leopard at full speed to run through Pont des Arts?"


def parse_arguments():
    parser = argparse.ArgumentParser(description="Run a CodeAgent with all specified parameters")
    parser.add_argument(
        "prompt",
        type=str,
        nargs="?",
        default=None,
        help="The prompt to run with the agent. If no prompt is provided, interactive mode will be launched to guide user through agent setup",
    )
    parser.add_argument(
        "--model-type",
        type=str,
        default="InferenceClientModel",
        help="The model type to use (e.g., InferenceClientModel, OpenAIModel, LiteLLMModel, TransformersModel)",
    )
    parser.add_argument(
        "--action-type",
        type=str,
        default="code",
        help="The action type to use (e.g., code, tool_calling)",
    )
    parser.add_argument(
        "--model-id",
        type=str,
        default="Qwen/Qwen3-Next-80B-A3B-Thinking",
        help="The model ID to use for the specified model type",
    )
    parser.add_argument(
        "--imports",
        nargs="*",  # accepts zero or more arguments
        default=[],
        help="Space-separated list of imports to authorize (e.g., 'numpy pandas')",
    )
    parser.add_argument(
        "--tools",
        nargs="*",
        default=["web_search"],
        help="Space-separated list of tools that the agent can use (e.g., 'tool1 tool2 tool3')",
    )
    parser.add_argument(
        "--verbosity-level",
        type=int,
        default=1,
        help="The verbosity level, as an int in [0, 1, 2].",
    )
    group = parser.add_argument_group("api options", "Options for API-based model types")
    group.add_argument(
        "--provider",
        type=str,
        default=None,
        help="The inference provider to use for the model",
    )
    group.add_argument(
        "--api-base",
        type=str,
        help="The base URL for the model",
    )
    group.add_argument(
        "--api-key",
        type=str,
        help="The API key for the model",
    )
    subparsers = parser.add_subparsers(dest="subcommand")

    # SEC-bench batch runner
    secbench = subparsers.add_parser("secb-run", help="Run multiple SEC-bench instances across Docker images")
    secbench.add_argument("--config", required=True, help="Local path to agent config TOML file")
    secbench.add_argument("--output-dir", help="Output directory for evaluation results")
    secbench.add_argument("--num-workers", type=int, default=1, help="Number of parallel workers")
    secbench.add_argument("--instance-id", help="Run evaluation for a specific instance ID only")

    return parser.parse_args()


def interactive_mode():
    """Run the CLI in interactive mode"""
    console.print(
        Panel.fit(
            "[bold magenta]🤖 SmolaGents CLI[/]\n[dim]Intelligent agents at your service[/]", border_style="magenta"
        )
    )

    console.print("\n[bold yellow]Welcome to smolagents![/] Let's set up your agent step by step.\n")

    # Get user input step by step
    console.print(Rule("[bold yellow]⚙️  Configuration", style="bold yellow"))

    # Get agent action type
    action_type = Prompt.ask(
        "[bold white]What action type would you like to use? 'code' or 'tool_calling'?[/]",
        default="code",
        choices=["code", "tool_calling"],
    )

    # Show available tools
    tools_table = Table(title="[bold yellow]🛠️  Available Tools", show_header=True, header_style="bold yellow")
    tools_table.add_column("Tool Name", style="bold yellow")
    tools_table.add_column("Description", style="white")

    for tool_name, tool_class in TOOL_MAPPING.items():
        # Get description from the tool class if available
        try:
            tool_instance = tool_class()
            description = getattr(tool_instance, "description", "No description available")
        except Exception:
            description = "Built-in tool"
        tools_table.add_row(tool_name, description)

    console.print(tools_table)
    console.print(
        "\n[dim]You can also use HuggingFace Spaces by providing the full path (e.g., 'username/spacename')[/]"
    )

    console.print("[dim]Enter tool names separated by spaces (e.g., 'web_search python_interpreter')[/]")
    tools_input = Prompt.ask("[bold white]Select tools for your agent[/]", default="web_search")
    tools = tools_input.split()

    # Get model configuration
    console.print("\n[bold yellow]Model Configuration:[/]")
    model_type = Prompt.ask(
        "[bold]Model type[/]",
        default="InferenceClientModel",
        choices=["InferenceClientModel", "OpenAIServerModel", "LiteLLMModel", "TransformersModel"],
    )

    model_id = Prompt.ask("[bold white]Model ID[/]", default="Qwen/Qwen2.5-Coder-32B-Instruct")

    # Optional configurations
    provider = None
    api_base = None
    api_key = None
    imports = []
    action_type = "code"

    if Confirm.ask("\n[bold white]Configure advanced options?[/]", default=False):
        if model_type in ["InferenceClientModel", "OpenAIServerModel", "LiteLLMModel"]:
            provider = Prompt.ask("[bold white]Provider[/]", default="")
            api_base = Prompt.ask("[bold white]API Base URL[/]", default="")
            api_key = Prompt.ask("[bold white]API Key[/]", default="", password=True)

        imports_input = Prompt.ask("[bold white]Additional imports (space-separated)[/]", default="")
        if imports_input:
            imports = imports_input.split()

    # Get prompt
    prompt = Prompt.ask(
        "[bold white]Now the final step; what task would you like the agent to perform?[/]", default=leopard_prompt
    )

    return prompt, tools, model_type, model_id, provider, api_base, api_key, imports, action_type


def load_model(
    model_type: str,
    model_id: str,
    api_base: str | None = None,
    api_key: str | None = None,
    provider: str | None = None,
    service_tier: str | None = None,
) -> Model:
    if model_type == "OpenAIModel":
        return OpenAIModel(
            api_key=api_key or os.getenv("FIREWORKS_API_KEY"),
            api_base=api_base or "https://api.fireworks.ai/inference/v1",
            model_id=model_id,
        )
    elif model_type == "LiteLLMModel":
        return LiteLLMModel(
            model_id=model_id,
            api_key=api_key,
            api_base=api_base,
            service_tier=service_tier or "default",
        )
    elif model_type == "TransformersModel":
        return TransformersModel(model_id=model_id, device_map="auto")
    elif model_type == "InferenceClientModel":
        return InferenceClientModel(
            model_id=model_id,
            token=api_key or os.getenv("HF_API_KEY"),
            provider=provider,
        )
    else:
        raise ValueError(f"Unsupported model type: {model_type}")


def run_smolagent(
    prompt: str,
    tools: list[str],
    model_type: str,
    model_id: str,
    api_base: str | None = None,
    api_key: str | None = None,
    imports: list[str] | None = None,
    provider: str | None = None,
    action_type: str = "code",
) -> None:
    load_dotenv()

    model = load_model(model_type, model_id, api_base=api_base, api_key=api_key, provider=provider)

    available_tools = []

    for tool_name in tools:
        if "/" in tool_name:
            space_name = tool_name.split("/")[-1].lower().replace("-", "_").replace(".", "_")
            description = f"Tool loaded from Hugging Face Space: {tool_name}"
            available_tools.append(Tool.from_space(space_id=tool_name, name=space_name, description=description))
        else:
            if tool_name in TOOL_MAPPING:
                available_tools.append(TOOL_MAPPING[tool_name]())
            else:
                raise ValueError(f"Tool {tool_name} is not recognized either as a default tool or a Space.")

    if action_type == "code":
        agent: CodeAgent | ToolCallingAgent = CodeAgent(
            tools=available_tools,
            model=model,
            additional_authorized_imports=imports,
            stream_outputs=True,
        )
    elif action_type == "tool_calling":
        agent = ToolCallingAgent(tools=available_tools, model=model, stream_outputs=True)
    else:
        raise ValueError(f"Unsupported action type: {action_type}")

    agent.run(prompt)


def run_secb_evaluation(args) -> None:
    """Run SEC-bench evaluation using Docker containers."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    load_dotenv()

    # Load TOML config
    config_path = Path(args.config)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("rb") as f:
        config = tomli.load(f)

    # Extract configuration
    dataset_config = config.get("dataset", {})
    docker_config = config.get("docker", {})
    task_config = config.get("task", {})
    output_config = config.get("output", {})

    # Load dataset - import datasets lazily to avoid import errors when not using secb-run
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError as e:
        raise ImportError(
            "Please install 'datasets' package for SEC-bench evaluation: pip install 'smolagents[secb]'"
        ) from e

    dataset_name = dataset_config.get("name", "SEC-bench/SEC-bench")
    dataset_split = dataset_config.get("split", "eval")  # eval, cve, or oss
    selected_ids = dataset_config.get("instance_ids", [])

    console.print(f"[bold]Loading dataset: {dataset_name} (split: {dataset_split})[/]")
    dataset = load_dataset(dataset_name, split=dataset_split)

    if selected_ids:
        # Filter by instance IDs if specified
        dataset = dataset.filter(lambda x: x["instance_id"] in selected_ids)
        console.print(f"[bold]Filtered to {len(selected_ids)} instance(s)[/]")

    # Filter by specific instance_id if provided via CLI
    if args.instance_id:
        dataset = dataset.filter(lambda x: x["instance_id"] == args.instance_id)
        console.print(f"[bold]Running for instance: {args.instance_id}[/]")

    # Convert to list for easier processing
    instances = list(dataset)

    if not instances:
        console.print("[bold red]No instances found matching the criteria[/]")
        return

    console.print(f"[bold green]Found {len(instances)} instance(s) to evaluate[/]")

    # Setup output directory with timestamp-based subdirectory
    # Priority: CLI arg > config file > default
    base_output_dir = (
        Path(args.output_dir) if args.output_dir else Path(output_config.get("output_dir", "./secb_results"))
    )
    # Create timestamp-based subdirectory for this run session
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = base_output_dir / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]Output directory: {output_dir}[/]")

    # Process instances
    if args.num_workers <= 1:
        # Sequential processing
        for instance in instances:
            _process_secb_instance(
                instance,
                config,
                output_dir,
                docker_config,
                task_config,
            )
    else:
        # Parallel processing
        with ThreadPoolExecutor(max_workers=args.num_workers) as executor:
            futures = {
                executor.submit(
                    _process_secb_instance,
                    instance,
                    config,
                    output_dir,
                    docker_config,
                    task_config,
                ): instance
                for instance in instances
            }
            for future in as_completed(futures):
                instance = futures[future]
                try:
                    future.result()
                    console.print(f"[green]Completed: {instance['instance_id']}[/]")
                except Exception as e:
                    console.print(f"[red]Failed {instance['instance_id']}: {e}[/]")


def _process_secb_instance(
    instance: dict[str, Any],
    config: dict[str, Any],
    output_dir: Path,
    docker_config: dict[str, Any],
    task_config: dict[str, Any],
) -> None:
    """Process a single SEC-bench instance."""
    instance_id = instance["instance_id"]
    task_type = task_config.get("type", "patch")  # patch, poc-repo, poc-desc, poc-san
    
    # Check scaffold type - "single" (default) or "multiagent"
    scaffold_config = config.get("scaffold", {})
    scaffold_type = scaffold_config.get("type", "single")

    console.print(f"[bold]Processing instance: {instance_id} (task: {task_type}, scaffold: {scaffold_type})[/]")

    # Create task prompt
    task_prompt = _create_task_prompt(instance, task_type, config)

    # Create agent config JSON
    agent_config_json = _create_agent_config_json(config)

    # Create temporary directory for this instance
    instance_output_dir = output_dir / instance_id
    instance_output_dir.mkdir(parents=True, exist_ok=True)

    # Determine Docker image name
    image_prefix = docker_config.get("image_prefix", "hwiwonlee/secb.eval.x86_64")
    image_tag = "poc" if task_type.startswith("poc") else "patch"
    docker_image = f"{image_prefix}.{instance_id}:{image_tag}"

    # Create logger
    logger = AgentLogger(level=LogLevel.INFO)

    # Setup Docker runtime
    docker_kwargs = docker_config.get("run_kwargs", {})
    # Increased to 16g for CodeQL database building which can be memory-intensive
    docker_kwargs.setdefault("mem_limit", "16g")
    docker_kwargs.setdefault("network_mode", "host")
    docker_kwargs.setdefault("auto_remove", True)

    # Use work_dir from instance as default workdir, fallback to /app if not present
    workdir = instance.get("work_dir", "/app")

    # For patch tasks, don't mount /testcase to preserve PoC files in the Docker image
    # For PoC tasks, mount /testcase to capture generated PoC
    mount_testcase = task_type.startswith("poc")

    runtime = DockerAgentRuntime(
        image_name=docker_image,
        workdir=workdir,
        artifacts_dir=str(instance_output_dir),
        runtime_logger=logger,
        docker_run_kwargs=docker_kwargs,
        mount_testcase=mount_testcase,
    )

    try:
        # Start container
        runtime.start()

        # Install smolagents - try local first, then fall back to git
        # Get installation config from docker_config
        smolagents_git_url = docker_config.get("smolagents_git_url", "https://github.com/SEC-bench/smolagents.git")
        smolagents_git_branch = docker_config.get("smolagents_git_branch")

        # Try to find local repository root (assuming cli.py is in src/smolagents/)
        # Check if we're running from a local development repo
        repo_root = Path(__file__).parent.parent.parent
        pyproject_path = repo_root / "pyproject.toml"
        src_dir_path = repo_root / "src"
        local_repo_root = (
            str(repo_root) if (pyproject_path.exists() and src_dir_path.exists() and src_dir_path.is_dir()) else None
        )

        # Use unified installation method that handles both local and git
        runtime.install_smolagents(
            host_repo_root=local_repo_root,
            git_url=smolagents_git_url,
            git_branch=smolagents_git_branch,
        )

        # Determine which runner to use based on scaffold type
        if scaffold_type == "multiagent":
            # Multi-agent scaffold
            runner_script_path = Path(__file__).parent / "docker_multiagent_runner.py"
            if not runner_script_path.exists():
                raise FileNotFoundError(f"Multi-agent runner script not found: {runner_script_path}")
            runtime.copy_into_container(str(runner_script_path), "/app/runner.py")

            # Create multiagent config (includes full config)
            multiagent_config_json = _create_multiagent_config_json(config)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump(multiagent_config_json, f)
                config_path = f.name
            runtime.copy_into_container(config_path, "/app/agent_config.json")
            os.unlink(config_path)

            # Copy instance data
            instance_json = {
                "instance_id": instance_id,
                "work_dir": instance.get("work_dir", ""),
                "bug_description": instance.get("bug_description", ""),
                "sanitizer_report": instance.get("sanitizer_report", ""),
                "base_commit": instance.get("base_commit", ""),
                "repo": instance.get("repo", ""),
            }
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump(instance_json, f)
                instance_path = f.name
            runtime.copy_into_container(instance_path, "/app/instance.json")
            os.unlink(instance_path)

        else:
            # Single-agent scaffold (default)
            runner_script_path = Path(__file__).parent / "docker_app_runner.py"
            if not runner_script_path.exists():
                raise FileNotFoundError(f"Runner script not found: {runner_script_path}")
            runtime.copy_into_container(str(runner_script_path), "/app/runner.py")

            # Copy agent config
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump(agent_config_json, f)
                agent_config_path = f.name
            runtime.copy_into_container(agent_config_path, "/app/agent_config.json")
            os.unlink(agent_config_path)

            # Copy task
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
                f.write(task_prompt)
                task_path = f.name
            runtime.copy_into_container(task_path, "/app/task.txt")
            os.unlink(task_path)

        # Set environment variables
        # Pass API keys from config to container environment
        model_config = config.get("model", {})
        codeql_config = config.get("codeql", {})
        env_vars = {
            "SMOLAGENTS_CONFIG_PATH": "/app/agent_config.json",
            "SMOLAGENTS_ARTIFACTS_DIR": "/app/artifacts",
            "SMOLAGENTS_SECB_RUN": "1",  # Disable sandbox checks for SEC-bench (runs in Docker)
        }

        if scaffold_type == "multiagent":
            env_vars["SMOLAGENTS_INSTANCE_PATH"] = "/app/instance.json"
            for agent_key in ["model_analyzer", "model_fixer", "model_verifier", "model_fixer_verifier"]:
                if agent_config := config.get(agent_key, {}):
                    if api_key := agent_config.get("api_key"):
                        env_vars["OPENAI_API_KEY"] = api_key
                        break  # Just need one key set
            # The agent runs inside the container, so the host environment is not
            # visible to it. Forward the host's key when the config does not set one,
            # so the key never has to be written into a config file.
            if "OPENAI_API_KEY" not in env_vars and os.environ.get("OPENAI_API_KEY"):
                env_vars["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
        else:
            env_vars["SMOLAGENTS_TASK_PATH"] = "/app/task.txt"
            # Add API keys to environment if provided in config
            if api_key := model_config.get("api_key"):
                model_type = model_config.get("type", "")
                if model_type == "InferenceClientModel":
                    env_vars["HF_API_KEY"] = api_key
                elif model_type in ["OpenAIModel", "LiteLLMModel"]:
                    env_vars["OPENAI_API_KEY"] = api_key

        # Add CodeQL RAM limit if configured
        if codeql_ram := codeql_config.get("ram"):
            env_vars["CODEQL_RAM"] = str(codeql_ram)

        runtime.environment.update(env_vars)

        # Run agent
        timeout_seconds = task_config.get("timeout_seconds", 7200)  # 2 hours default
        # The runner script reads config and task from environment variables
        exit_code = runtime.run_agent(
            agent_runner_path_in_container="/app/runner.py",
            agent_config_path_in_container="/app/agent_config.json",
            task=task_prompt if scaffold_type != "multiagent" else "",
            max_steps=config.get("agent", {}).get("max_steps", 20),
            timeout_seconds=timeout_seconds,
            stream=True,
        )

        # Collect artifacts
        _collect_artifacts(instance, instance_output_dir, task_type, runtime)

        # Save result
        _save_result(instance_id, instance_output_dir, output_dir, exit_code, task_type)

    except Exception as e:
        console.print(f"[red]Error processing {instance_id}: {e}[/]")
        raise
    finally:
        runtime.cleanup()


def _create_task_prompt(instance: dict[str, Any], task_type: str, config: dict[str, Any] | None = None) -> str:
    """Create task prompt based on task type using Jinja2 templates."""
    # Map task types to template files
    template_map = {
        "patch": "patch.j2",
        "poc-repo": "poc-repo.j2",
        "poc-desc": "poc-desc.j2",
        "poc-san": "poc-san.j2",
    }

    template_name = template_map.get(task_type)
    if not template_name:
        raise ValueError(f"Unknown task type: {task_type}")

    # Load template from package resources
    try:
        template_content = (
            importlib.resources.files("smolagents.prompts").joinpath(template_name).read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError) as e:
        raise FileNotFoundError(
            f"Template file '{template_name}' not found in smolagents.prompts package. "
            f"Make sure the package is properly installed. Original error: {e}"
        ) from e

    template = Template(template_content)

    # Get available tools from config
    available_tools = []
    if config:
        agent_config = config.get("agent", {})
        available_tools = agent_config.get("tools", [])

    # Prepare context variables
    context = {
        "work_dir": instance.get("work_dir", ""),
        "bug_report": instance.get("bug_report", ""),
        "bug_description": instance.get("bug_description", ""),
        "sanitizer_report": instance.get("sanitizer_report", ""),
        "base_commit": instance.get("base_commit", ""),
        "repo": instance.get("repo", ""),  # Repository name in format 'owner/repo'
        "has_valgrind": "valgrind" in available_tools,
        "has_pydriller": "pydriller" in available_tools,
        "has_codeql": "codeql" in available_tools,
    }

    return template.render(**context)


def _create_agent_config_json(config: dict[str, Any]) -> dict[str, Any]:
    """Create agent configuration JSON from TOML config."""
    model_config = config.get("model", {})
    agent_config = config.get("agent", {})
    return {
        "model": {
            "type": model_config.get("type", "InferenceClientModel"),
            "model_id": model_config.get("model_id", ""),
            "api_key": model_config.get("api_key"),
            "api_base": model_config.get("api_base"),
            "provider": model_config.get("provider"),
            "temperature": model_config.get("temperature"),
            "service_tier": model_config.get("service_tier", "default"),
        },
        "agent_type": agent_config.get("type", "ToolCallingAgent"),
        "tools": agent_config.get("tools", []),
        "max_steps": agent_config.get("max_steps", 20),
        "max_cost": agent_config.get("max_cost", 0.0),  # Max cost in USD (0 = disabled)
        "verbosity_level": agent_config.get("verbosity_level", 1),
        "additional_imports": agent_config.get("additional_imports", []),
    }


def _create_multiagent_config_json(config: dict[str, Any]) -> dict[str, Any]:
    """Create multi-agent configuration JSON from TOML config.
    
    New architecture: 4 parallel analyzers → fixer+verifier
    """
    agent_config = config.get("agent", {})
    scaffold_config = config.get("scaffold", {})
    agent_steps = config.get("agent_steps", {})
    analyzer_enabled = config.get("analyzer_enabled", {})
    prompt_templates = config.get("prompt_templates", {})
    
    # Helper function to extract model config
    def get_model_config(key: str) -> dict[str, Any]:
        model_cfg = config.get(key, config.get("model", {}))
        return {
            "type": model_cfg.get("type", "LiteLLMModel"),
            "model_id": model_cfg.get("model_id", ""),
            "api_key": model_cfg.get("api_key"),
            "api_base": model_cfg.get("api_base"),
            "provider": model_cfg.get("provider"),
            "temperature": model_cfg.get("temperature", 0.0),
            "service_tier": model_cfg.get("service_tier", "default"),
            "reasoning_effort": model_cfg.get("reasoning_effort"),
        }
    
    default_steps = agent_config.get("max_steps", 30)
    
    result = {
        "scaffold": {
            "type": scaffold_config.get("type", "multiagent"),
        },
        "model": get_model_config("model"),
        "model_analyzer": get_model_config("model_analyzer"),
        "model_fixer_verifier": get_model_config("model_fixer_verifier"),
        "agent": {
            "type": agent_config.get("type", "ToolCallingAgent"),
            "tools": agent_config.get("tools", []),
            "max_steps": default_steps,
            "max_cost": agent_config.get("max_cost", 0.0),
            "verbosity_level": agent_config.get("verbosity_level", 1),
        },
        "agent_steps": {
            "static_analyzer_max_steps": agent_steps.get("static_analyzer_max_steps", 25),
            "dynamic_analyzer_max_steps": agent_steps.get("dynamic_analyzer_max_steps", 30),
            "history_miner_max_steps": agent_steps.get("history_miner_max_steps", 15),
            "fixer_verifier_max_steps": agent_steps.get("fixer_verifier_max_steps", 50),
        },
    }
    
    # Include analyzer_enabled section if present
    if analyzer_enabled:
        result["analyzer_enabled"] = {
            "enable_static_analyzer": analyzer_enabled.get("enable_static_analyzer", True),
            "enable_dynamic_analyzer": analyzer_enabled.get("enable_dynamic_analyzer", True),
            "enable_history_miner": analyzer_enabled.get("enable_history_miner", True),
        }
    
    # Include prompt_templates section if present
    if prompt_templates:
        result["prompt_templates"] = prompt_templates
    
    return result


def _collect_artifacts(
    instance: dict[str, Any],
    instance_output_dir: Path,
    task_type: str,
    runtime: DockerAgentRuntime,
) -> None:
    """Collect artifacts from container based on task type."""
    work_dir = instance.get("work_dir", "")

    if runtime.container is None:
        return

    container = runtime.container

    if task_type == "patch":
        # Collect git patch - execute git diff and capture output.
        # The evaluator (`secb patch`) applies /testcase/repo_changes.diff, the benchmark's
        # own build fixes, before the model patch. `secb build` also applies it inside this
        # container, so diffing against the base commit would put those changes into the
        # patch and they would be applied twice, failing `git apply`. Diff instead against
        # the evaluator's starting tree: the base commit plus repo_changes.diff, built in a
        # temporary index so the working tree is not touched. If repo_changes.diff does not
        # apply to the base commit, the evaluator skips it too, and so does this. Files that
        # repo_changes.diff changes but that are still identical to the base commit here
        # (e.g. the agent never ran `secb build`) are left out, so the patch never reverts
        # the benchmark's fixes: it only ever contains the agent's own changes.
        base = instance.get("base_commit", "HEAD")
        script = f"""
cd {work_dir} && git config --global core.pager '' && git add -A || exit 1
base={base}
exclude=()
if [ -f /testcase/repo_changes.diff ]; then
    idx=/tmp/agenticrepair_base_index.$$
    rm -f "$idx"
    if GIT_INDEX_FILE="$idx" git read-tree {base} \\
        && GIT_INDEX_FILE="$idx" git apply --cached /testcase/repo_changes.diff 2>/dev/null; then
        base=$(GIT_INDEX_FILE="$idx" git write-tree)
        while IFS= read -r f; do
            git diff --cached --quiet {base} -- "$f" && exclude+=(":(exclude)$f")
        done < <(git apply --numstat /testcase/repo_changes.diff | cut -f3-)
    fi
    rm -f "$idx"
fi
git diff --no-color --cached "$base" -- '*.c' '*.cpp' '*.h' '*.hpp' '*.cc' '*.hh' "${{exclude[@]}}"
"""
        exec_result = container.exec_run(
            ["bash", "-c", script],
            workdir=runtime.workdir,
        )
        if exec_result.exit_code == 0:
            # Do NOT strip: in a unified diff a context line for a blank source line
            # is the single byte " ", and stripping removes such trailing lines while
            # the hunk header still counts them, producing "corrupt patch at line N".
            patch_content = exec_result.output.decode("utf-8", errors="ignore")
            if patch_content and not patch_content.endswith("\n"):
                patch_content += "\n"
        else:
            patch_content = ""

        # Save patch
        with (instance_output_dir / "git_patch.diff").open("w") as f:
            f.write(patch_content)

    elif task_type.startswith("poc"):
        # Collect PoC artifact (base64 encoded tar.gz)
        # Compress and encode PoC
        runtime.exec(
            [
                "bash",
                "-c",
                'tar --exclude="base_commit_hash" -czf /tmp/poc.tar.gz -C /testcase . 2>/dev/null || echo ""',
            ]
        )

        # Encode to base64 and save to artifacts directory (which is mounted)
        exec_result = container.exec_run(
            [
                "bash",
                "-c",
                "cat /tmp/poc.tar.gz | base64 -w 0 > /app/artifacts/poc.tar.gz.base64 2>/dev/null || echo ''",
            ],
            workdir=runtime.workdir,
        )

        # Read base64 content from mounted artifacts directory
        # Note: artifacts_dir is set to instance_output_dir
        # In remote_executors.py, artifacts_subdir = artifacts_dir / "artifacts" = instance_output_dir / "artifacts"
        # /app/artifacts is mounted to artifacts_subdir
        poc_file = instance_output_dir / "artifacts" / "poc.tar.gz.base64"
        if poc_file.exists():
            with poc_file.open() as f:
                poc_content = f.read().strip()
        else:
            poc_content = ""

        # Save PoC artifact
        with (instance_output_dir / "poc_artifact.txt").open("w") as f:
            f.write(poc_content)


def _save_result(
    instance_id: str,
    instance_output_dir: Path,
    root_output_dir: Path,
    exit_code: int,
    task_type: str,
) -> None:
    """Save evaluation result in compatible format."""
    result: dict[str, Any] = {
        "instance_id": instance_id,
    }

    if task_type == "patch":
        # Read git patch
        patch_file = instance_output_dir / "git_patch.diff"
        git_patch: str = ""
        if patch_file.exists():
            with patch_file.open() as f:
                git_patch = f.read()

        result["test_result"] = {
            "git_patch": git_patch,
        }
    else:  # poc tasks
        # Read PoC artifact
        poc_file = instance_output_dir / "poc_artifact.txt"
        poc_artifact: str = ""
        if poc_file.exists():
            with poc_file.open() as f:
                poc_artifact = f.read().strip()

        result["test_result"] = {
            "poc_artifact": poc_artifact,
        }

    # Append to comprehensive output.jsonl in root directory
    output_file = root_output_dir / "output.jsonl"
    with output_file.open("a") as f:
        f.write(json.dumps(result) + "\n")


def main() -> None:
    args = parse_arguments()

    # Handle secb-run subcommand
    if args.subcommand == "secb-run":
        run_secb_evaluation(args)
        return

    # Check if we should run in interactive mode
    # Interactive mode is triggered when no prompt is provided
    if args.prompt is None:
        prompt, tools, model_type, model_id, provider, api_base, api_key, imports, action_type = interactive_mode()
    else:
        prompt = args.prompt
        tools = args.tools
        model_type = args.model_type
        model_id = args.model_id
        provider = args.provider
        api_base = args.api_base
        api_key = args.api_key
        imports = args.imports
        action_type = args.action_type

    run_smolagent(
        prompt,
        tools,
        model_type,
        model_id,
        provider=provider,
        api_base=api_base,
        api_key=api_key,
        imports=imports,
        action_type=action_type,
    )


if __name__ == "__main__":
    main()