#!/usr/bin/env python
# coding=utf-8
"""
Multi-Agent Scaffold for SEC-bench Vulnerability Patching.

This module implements a sequential multi-agent workflow:
1. Three Analyzer Agents run sequentially:
   - Static Analyzer: Runs CodeQL
   - Dynamic Analyzer: Runs GDB/Valgrind/UBSan/MSan
   - History Miner: Uses PyDriller for commit history
2. Fixer+Verifier Agent: Implements and verifies the fix

No looping - single pass from analysis to fix.
"""

import importlib.resources
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from jinja2 import Template

from smolagents.models import LiteLLMModel, Model
from smolagents.tools import Tool
from smolagents.agents import ToolCallingAgent
from smolagents.monitoring import AgentLogger, LogLevel


__all__ = [
    "AgentRole",
    "AgentOutput",
    "TrajectoryStep",
    "AgentTrajectory",
    "MultiAgentResult",
    "MultiAgentOrchestrator",
    "run_multiagent_scaffold",
    "load_model",
    "parse_agent_output",
]


class AgentRole(Enum):
    """Roles for agents in the multi-agent scaffold."""
    STATIC_ANALYZER = "static_analyzer"
    DYNAMIC_ANALYZER = "dynamic_analyzer"
    HISTORY_MINER = "history_miner"
    FIXER_VERIFIER = "fixer_verifier"


@dataclass
class TrajectoryStep:
    """A single step in the agent trajectory."""
    step_number: int
    agent_role: str
    timing: dict[str, float]
    model_input_messages: Optional[list[dict]] = None
    model_output: Optional[str] = None
    tool_calls: Optional[list[dict]] = None
    observations: Optional[list[str]] = None
    error: Optional[str] = None
    token_usage: Optional[dict[str, int]] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "step_number": self.step_number,
            "agent_role": self.agent_role,
            "timing": self.timing,
        }
        if self.model_input_messages:
            result["model_input_messages"] = self.model_input_messages
        if self.model_output:
            result["model_output"] = self.model_output
        if self.tool_calls:
            result["tool_calls"] = self.tool_calls
        if self.observations:
            result["observations"] = self.observations
        if self.error:
            result["error"] = self.error
        if self.token_usage:
            result["token_usage"] = self.token_usage
        return result


@dataclass
class AgentTrajectory:
    """Trajectory for a single agent run."""
    agent_role: str
    task: str
    steps: list[TrajectoryStep] = field(default_factory=list)
    total_duration: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0
    final_state: str = "unknown"

    def add_step(self, step: TrajectoryStep):
        """Add a step to the trajectory."""
        self.steps.append(step)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "agent_role": self.agent_role,
            "task": self.task[:500] if self.task else "",  # Truncate task for summary
            "total_steps": len(self.steps),
            "total_duration": self.total_duration,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost": self.total_cost,
            "final_state": self.final_state,
        }

    def to_jsonl(self) -> list[str]:
        """Convert trajectory to JSONL format lines."""
        lines = []
        # Add task as first entry
        lines.append(json.dumps({"task": self.task}))
        # Add each step
        for step in self.steps:
            lines.append(json.dumps(step.to_dict()))
        # Add summary as last entry
        summary = {
            "_type": "summary",
            "agent_role": self.agent_role,
            "total_cost_usd": self.total_cost,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_steps": len(self.steps),
            "state": self.final_state,
            "total_duration_seconds": self.total_duration,
        }
        lines.append(json.dumps(summary))
        return lines


@dataclass
class AgentOutput:
    """Structured output from an agent."""
    role: AgentRole
    raw_output: str
    parsed_output: dict[str, Any] = field(default_factory=dict)
    success: bool = False
    error: Optional[str] = None
    token_usage: Optional[dict[str, int]] = None
    timing: Optional[dict[str, float]] = None
    trajectory: Optional[AgentTrajectory] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "role": self.role.value,
            "raw_output": self.raw_output,
            "parsed_output": self.parsed_output,
            "success": self.success,
            "error": self.error,
            "token_usage": self.token_usage,
            "timing": self.timing,
            "trajectory": self.trajectory.to_dict() if self.trajectory else None,
        }


@dataclass
class MultiAgentResult:
    """Result of a complete multi-agent run."""
    instance_id: str
    static_analyzer_output: Optional[AgentOutput] = None
    dynamic_analyzer_output: Optional[AgentOutput] = None
    history_miner_output: Optional[AgentOutput] = None
    fixer_verifier_output: Optional[AgentOutput] = None
    final_status: str = "unknown"
    total_token_usage: dict[str, int] = field(default_factory=dict)
    timing: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "instance_id": self.instance_id,
            "final_status": self.final_status,
            "static_analyzer_output": self._output_to_dict(self.static_analyzer_output),
            "dynamic_analyzer_output": self._output_to_dict(self.dynamic_analyzer_output),
            "history_miner_output": self._output_to_dict(self.history_miner_output),
            "fixer_verifier_output": self._output_to_dict(self.fixer_verifier_output),
            "total_token_usage": self.total_token_usage,
            "timing": self.timing,
        }

    def _output_to_dict(self, output: Optional[AgentOutput]) -> Optional[dict]:
        if output is None:
            return None
        return output.to_dict()

    def get_all_trajectories(self) -> dict[str, AgentTrajectory]:
        """Get all agent trajectories."""
        trajectories = {}
        for output, name in [
            (self.static_analyzer_output, "static_analyzer"),
            (self.dynamic_analyzer_output, "dynamic_analyzer"),
            (self.history_miner_output, "history_miner"),
            (self.fixer_verifier_output, "fixer_verifier"),
        ]:
            if output and output.trajectory:
                trajectories[name] = output.trajectory
        return trajectories


def load_model(model_config: dict[str, Any]) -> Model:
    """Load a model from configuration."""
    model_type = model_config.get("type", "LiteLLMModel")
    model_id = model_config.get("model_id", "")
    api_key = model_config.get("api_key")
    api_base = model_config.get("api_base")
    temperature = model_config.get("temperature", 0.0)
    service_tier = model_config.get("service_tier", "default")
    reasoning_effort = model_config.get("reasoning_effort")

    model_kwargs = {}
    if reasoning_effort is not None:
        model_kwargs["reasoning_effort"] = reasoning_effort

    if model_type == "LiteLLMModel":
        return LiteLLMModel(
            model_id=model_id,
            api_key=api_key,
            api_base=api_base,
            temperature=temperature,
            service_tier=service_tier,
            **model_kwargs,
        )
    else:
        return LiteLLMModel(
            model_id=model_id,
            api_key=api_key,
            api_base=api_base,
            temperature=temperature,
            service_tier=service_tier,
            **model_kwargs,
        )


def load_prompt_template(agent_role: AgentRole, prompt_templates: Optional[dict[str, str]] = None) -> str:
    """Load the prompt template for a specific agent role.
    
    Args:
        agent_role: The role of the agent
        prompt_templates: Optional dict mapping agent roles to template file paths.
                         If provided, overrides default template paths.
                         Keys should be: "static_analyzer", "dynamic_analyzer", "history_miner", "fixer_verifier"
                         Values should be relative paths from smolagents.prompts (e.g., "multiagent/static_analyzer.j2")
    """
    # Default template map
    default_template_map = {
        AgentRole.STATIC_ANALYZER: "multiagent/static_analyzer.j2",
        AgentRole.DYNAMIC_ANALYZER: "multiagent/dynamic_analyzer.j2",
        AgentRole.HISTORY_MINER: "multiagent/history_miner.j2",
        AgentRole.FIXER_VERIFIER: "multiagent/fixer_verifier.j2",
    }
    
    # Use custom templates if provided, otherwise use defaults
    if prompt_templates:
        role_key = agent_role.value  # e.g., "static_analyzer"
        if role_key in prompt_templates:
            template_name = prompt_templates[role_key]
        else:
            template_name = default_template_map[agent_role]
    else:
        template_name = default_template_map[agent_role]

    try:
        template_content = (
            importlib.resources.files("smolagents.prompts").joinpath(template_name).read_text(encoding="utf-8")
        )
        return template_content
    except (FileNotFoundError, ModuleNotFoundError) as e:
        raise FileNotFoundError(f"Template '{template_name}' not found: {e}") from e


def render_prompt(
    agent_role: AgentRole,
    instance: dict[str, Any],
    static_analyzer_output: Optional[str] = None,
    dynamic_analyzer_output: Optional[str] = None,
    history_miner_output: Optional[str] = None,
    prompt_templates: Optional[dict[str, str]] = None,
) -> str:
    """Render the prompt for a specific agent with context.
    
    Args:
        agent_role: The role of the agent
        instance: Instance data for rendering
        static_analyzer_output: Output from static analyzer (for fixer_verifier)
        dynamic_analyzer_output: Output from dynamic analyzer (for fixer_verifier)
        history_miner_output: Output from history miner (for fixer_verifier)
        prompt_templates: Optional dict mapping agent roles to template file paths
    """
    template_content = load_prompt_template(agent_role, prompt_templates)
    template = Template(template_content)

    context = {
        "work_dir": instance.get("work_dir", ""),
        "bug_description": instance.get("bug_description", ""),
        "sanitizer_report": instance.get("sanitizer_report", ""),
        "base_commit": instance.get("base_commit", ""),
        "repo": instance.get("repo", ""),
    }

    # Add analyzer outputs for fixer_verifier
    if agent_role == AgentRole.FIXER_VERIFIER:
        context["static_analyzer_output"] = static_analyzer_output or "No data available"
        context["dynamic_analyzer_output"] = dynamic_analyzer_output or "No data available"
        context["history_miner_output"] = history_miner_output or "No data available"

    return template.render(**context)


def parse_agent_output(raw_output: str) -> dict[str, Any]:
    """Parse JSON output from an agent's final_answer."""
    output = raw_output.strip()

    # Try to find JSON in the output
    json_start = -1
    json_end = -1

    for i, char in enumerate(output):
        if char == "{" and json_start == -1:
            json_start = i
        elif char == "}" and json_start != -1:
            json_end = i + 1

    if json_start != -1 and json_end != -1:
        try:
            json_str = output[json_start:json_end]
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        pass

    return {"raw": output, "parse_error": True}


def _extract_trajectory_from_agent(
    agent: ToolCallingAgent,
    role: AgentRole,
    task: str,
    result: Any,
    start_time: float,
    end_time: float,
) -> AgentTrajectory:
    """Extract trajectory from an agent's memory after running."""
    trajectory = AgentTrajectory(agent_role=role.value, task=task)

    # Extract steps from agent memory
    if hasattr(agent, 'memory') and hasattr(agent.memory, 'steps'):
        for i, step in enumerate(agent.memory.steps):
            # Skip non-action steps
            if not hasattr(step, 'step_number'):
                continue

            # Extract timing
            step_timing = {}
            if hasattr(step, 'timing') and step.timing:
                step_timing = {
                    "start_time": getattr(step.timing, 'start_time', 0),
                    "end_time": getattr(step.timing, 'end_time', 0),
                    "duration": getattr(step.timing, 'duration', 0),
                }

            # Extract tool calls
            tool_calls = None
            if hasattr(step, 'tool_calls') and step.tool_calls:
                tool_calls = []
                for tc in step.tool_calls:
                    tc_dict = {
                        "name": getattr(tc, 'name', ''),
                        "arguments": getattr(tc, 'arguments', {}),
                    }
                    tool_calls.append(tc_dict)

            # Extract observations
            observations = None
            if hasattr(step, 'observations') and step.observations:
                # Truncate long observations
                observations = [str(obs)[:1000] for obs in step.observations]

            # Extract token usage for step
            step_token_usage = None
            if hasattr(step, 'token_usage') and step.token_usage:
                step_token_usage = {
                    "input_tokens": getattr(step.token_usage, 'input_tokens', 0),
                    "output_tokens": getattr(step.token_usage, 'output_tokens', 0),
                }

            traj_step = TrajectoryStep(
                step_number=getattr(step, 'step_number', i),
                agent_role=role.value,
                timing=step_timing,
                model_output=getattr(step, 'model_output', None),
                tool_calls=tool_calls,
                observations=observations,
                token_usage=step_token_usage,
            )
            trajectory.add_step(traj_step)

    # Set trajectory summary
    trajectory.total_duration = end_time - start_time
    if result and hasattr(result, 'token_usage') and result.token_usage:
        trajectory.total_input_tokens = result.token_usage.input_tokens
        trajectory.total_output_tokens = result.token_usage.output_tokens

    # Try to get cost from agent monitor
    if hasattr(agent, 'monitor') and hasattr(agent.monitor, 'total_cost'):
        trajectory.total_cost = agent.monitor.total_cost

    return trajectory


class MultiAgentOrchestrator:
    """Orchestrates the sequential multi-agent vulnerability patching workflow."""

    def __init__(
        self,
        config: dict[str, Any],
        tools: list[Tool],
        logger: Optional[AgentLogger] = None,
    ):
        self.config = config
        self.tools = tools
        self.logger = logger or AgentLogger(level=LogLevel.INFO)

        # Extract agent config
        agent_config = config.get("agent", {})
        self.verbosity_level = agent_config.get("verbosity_level", 1)

        # Extract per-agent step limits
        agent_steps = config.get("agent_steps", {})
        default_max_steps = agent_config.get("max_steps", 30)

        self.static_analyzer_max_steps = agent_steps.get("static_analyzer_max_steps", default_max_steps)
        self.dynamic_analyzer_max_steps = agent_steps.get("dynamic_analyzer_max_steps", default_max_steps)
        self.history_miner_max_steps = agent_steps.get("history_miner_max_steps", default_max_steps)
        self.fixer_verifier_max_steps = agent_steps.get("fixer_verifier_max_steps", default_max_steps)

        # Extract enable/disable flags for each analyzer (default: all enabled)
        analyzer_enabled = config.get("analyzer_enabled", {})
        self.enable_static_analyzer = analyzer_enabled.get("enable_static_analyzer", True)
        self.enable_dynamic_analyzer = analyzer_enabled.get("enable_dynamic_analyzer", True)
        self.enable_history_miner = analyzer_enabled.get("enable_history_miner", True)

        # Extract prompt template paths (optional)
        self.prompt_templates = config.get("prompt_templates", None)

        # Load models
        self._load_models()

    def _load_models(self):
        """Load models for each agent from config."""
        # All analyzers share the same model config
        analyzer_config = self.config.get("model_analyzer", self.config.get("model", {}))
        self.analyzer_model = load_model(analyzer_config)

        # Fixer+Verifier can have its own model
        fixer_verifier_config = self.config.get("model_fixer_verifier", self.config.get("model", {}))
        self.fixer_verifier_model = load_model(fixer_verifier_config)

    def _create_agent(self, role: AgentRole) -> ToolCallingAgent:
        """Create an agent for a specific role."""
        # Analyzers share the same model
        if role in [AgentRole.STATIC_ANALYZER, AgentRole.DYNAMIC_ANALYZER, AgentRole.HISTORY_MINER]:
            model = self.analyzer_model
        else:
            model = self.fixer_verifier_model

        max_steps_map = {
            AgentRole.STATIC_ANALYZER: self.static_analyzer_max_steps,
            AgentRole.DYNAMIC_ANALYZER: self.dynamic_analyzer_max_steps,
            AgentRole.HISTORY_MINER: self.history_miner_max_steps,
            AgentRole.FIXER_VERIFIER: self.fixer_verifier_max_steps,
        }

        return ToolCallingAgent(
            tools=self.tools,
            model=model,
            max_steps=max_steps_map[role],
            verbosity_level=LogLevel(self.verbosity_level),
            logger=self.logger,
        )

    def _run_agent(self, role: AgentRole, task: str) -> AgentOutput:
        """Run a single agent and capture its output with trajectory."""
        start_time = time.time()
        agent = self._create_agent(role)

        try:
            self.logger.log(f"\n{'='*50}", level=LogLevel.INFO)
            self.logger.log(f"Starting {role.value.upper()}", level=LogLevel.INFO)
            self.logger.log(f"{'='*50}\n", level=LogLevel.INFO)

            result = agent.run(task, return_full_result=True)
            end_time = time.time()

            raw_output = str(result.output) if result.output else ""
            parsed_output = parse_agent_output(raw_output)

            # For fixer_verifier, check if repro_success is true
            success = True
            if role == AgentRole.FIXER_VERIFIER:
                verification = parsed_output.get("verification", {})
                success = verification.get("repro_success", False)

            token_usage = None
            if result.token_usage:
                token_usage = {
                    "input_tokens": result.token_usage.input_tokens,
                    "output_tokens": result.token_usage.output_tokens,
                }

            # Extract trajectory from agent
            trajectory = _extract_trajectory_from_agent(
                agent, role, task, result, start_time, end_time
            )
            trajectory.final_state = "success" if success else "failed"

            return AgentOutput(
                role=role,
                raw_output=raw_output,
                parsed_output=parsed_output,
                success=success,
                token_usage=token_usage,
                timing={"start_time": start_time, "end_time": end_time, "duration": end_time - start_time},
                trajectory=trajectory,
            )

        except Exception as e:
            end_time = time.time()
            self.logger.log(f"Error in {role.value}: {e}", level=LogLevel.ERROR)
            
            # Create error trajectory
            trajectory = AgentTrajectory(agent_role=role.value, task=task)
            trajectory.total_duration = end_time - start_time
            trajectory.final_state = "error"
            
            return AgentOutput(
                role=role,
                raw_output="",
                parsed_output={},
                success=False,
                error=str(e),
                timing={"start_time": start_time, "end_time": end_time, "duration": end_time - start_time},
                trajectory=trajectory,
            )

    def run(self, instance: dict[str, Any]) -> MultiAgentResult:
        """Run the complete multi-agent workflow for an instance."""
        overall_start = time.time()
        instance_id = instance.get("instance_id", "unknown")

        result = MultiAgentResult(instance_id=instance_id)

        self.logger.log("\n" + "=" * 70, level=LogLevel.INFO)
        self.logger.log(f"MULTI-AGENT SCAFFOLD: {instance_id}", level=LogLevel.INFO)
        self.logger.log("=" * 70 + "\n", level=LogLevel.INFO)

        # Phase 1: Run 3 analyzers sequentially (only enabled ones)
        self.logger.log("PHASE 1: Sequential Analysis", level=LogLevel.INFO)

        analyzer_roles = []
        if self.enable_static_analyzer:
            analyzer_roles.append(AgentRole.STATIC_ANALYZER)
        if self.enable_dynamic_analyzer:
            analyzer_roles.append(AgentRole.DYNAMIC_ANALYZER)
        if self.enable_history_miner:
            analyzer_roles.append(AgentRole.HISTORY_MINER)

        analyzer_outputs = {}

        for role in analyzer_roles:
            try:
                prompt = render_prompt(role, instance, prompt_templates=self.prompt_templates)
                output = self._run_agent(role, prompt)
                analyzer_outputs[role] = output
                self.logger.log(f"✓ {role.value} completed", level=LogLevel.INFO)
            except Exception as e:
                self.logger.log(f"✗ {role.value} failed: {e}", level=LogLevel.ERROR)
                analyzer_outputs[role] = AgentOutput(
                    role=role, raw_output="", parsed_output={}, success=False, error=str(e)
                )

        # Store analyzer outputs
        result.static_analyzer_output = analyzer_outputs.get(AgentRole.STATIC_ANALYZER)
        result.dynamic_analyzer_output = analyzer_outputs.get(AgentRole.DYNAMIC_ANALYZER)
        result.history_miner_output = analyzer_outputs.get(AgentRole.HISTORY_MINER)

        # Convert outputs to strings for fixer_verifier
        def output_to_str(output: Optional[AgentOutput]) -> str:
            if output is None or output.error:
                return "Analysis failed or unavailable"
            return json.dumps(output.parsed_output, indent=2)

        static_str = output_to_str(result.static_analyzer_output) if self.enable_static_analyzer else "Static analyzer disabled"
        dynamic_str = output_to_str(result.dynamic_analyzer_output) if self.enable_dynamic_analyzer else "Dynamic analyzer disabled"
        history_str = output_to_str(result.history_miner_output) if self.enable_history_miner else "History miner disabled"

        # Phase 2: Run Fixer+Verifier
        self.logger.log("\nPHASE 2: Fix + Verify", level=LogLevel.INFO)

        fixer_prompt = render_prompt(
            AgentRole.FIXER_VERIFIER,
            instance,
            static_analyzer_output=static_str,
            dynamic_analyzer_output=dynamic_str,
            history_miner_output=history_str,
            prompt_templates=self.prompt_templates,
        )

        fixer_output = self._run_agent(AgentRole.FIXER_VERIFIER, fixer_prompt)
        result.fixer_verifier_output = fixer_output

        # Determine final status
        if fixer_output.success:
            result.final_status = "success"
            self.logger.log("\n✓ VERIFICATION SUCCESSFUL!", level=LogLevel.INFO)
        else:
            result.final_status = "failed"
            self.logger.log("\n✗ VERIFICATION FAILED", level=LogLevel.INFO)

        # Calculate total token usage
        total_input = 0
        total_output = 0

        for output in [
            result.static_analyzer_output,
            result.dynamic_analyzer_output,
            result.history_miner_output,
            result.fixer_verifier_output,
        ]:
            if output and output.token_usage:
                total_input += output.token_usage.get("input_tokens", 0)
                total_output += output.token_usage.get("output_tokens", 0)

        result.total_token_usage = {"input_tokens": total_input, "output_tokens": total_output}
        result.timing["total_duration"] = time.time() - overall_start

        self.logger.log("\n" + "=" * 70, level=LogLevel.INFO)
        self.logger.log(f"Status: {result.final_status}", level=LogLevel.INFO)
        self.logger.log(f"Duration: {result.timing['total_duration']:.2f}s", level=LogLevel.INFO)
        self.logger.log("=" * 70 + "\n", level=LogLevel.INFO)

        return result


def run_multiagent_scaffold(
    instance: dict[str, Any],
    config: dict[str, Any],
    tools: list[Tool],
    output_dir: Optional[Path] = None,
    logger: Optional[AgentLogger] = None,
) -> MultiAgentResult:
    """Run the multi-agent scaffold for a single instance."""
    orchestrator = MultiAgentOrchestrator(config, tools, logger)
    result = orchestrator.run(instance)

    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        instance_id = instance.get("instance_id", "unknown")

        # Save main result
        result_file = output_dir / "multiagent_result.json"
        with result_file.open("w") as f:
            json.dump(result.to_dict(), f, indent=2)

        # Save trajectories for each agent
        trajectories = result.get_all_trajectories()
        for agent_name, trajectory in trajectories.items():
            traj_file = output_dir / f"{agent_name}_trajectory.jsonl"
            with traj_file.open("w") as f:
                for line in trajectory.to_jsonl():
                    f.write(line + "\n")

    return result
