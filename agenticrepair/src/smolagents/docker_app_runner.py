"""Runner script for executing agents inside Docker containers for SEC-bench evaluation."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Union

# Import smolagents - should be installed locally via install_local_smolagents before this script runs
from smolagents import CodeAgent, ToolCallingAgent
from smolagents.default_tools import TOOL_MAPPING
from smolagents.memory import ActionStep
from smolagents.models import InferenceClientModel, LiteLLMModel, OpenAIModel, TransformersModel
from smolagents.monitoring import LogLevel
from smolagents.utils import AgentError


class MaxCostExceededError(AgentError):
    """Raised when the maximum cost limit is exceeded."""
    pass


def _create_cost_check_callback(max_cost: float, agent_ref: list):
    """Create a callback function that checks if max cost has been exceeded.
    
    Args:
        max_cost: Maximum cost in USD
        agent_ref: A mutable list containing a reference to the agent (to be set later)
    
    Returns:
        A callback function for step callbacks
    """
    def cost_check_callback(step_log: ActionStep, agent=None):
        """Check if the total cost has exceeded max_cost and interrupt if so."""
        if agent is None:
            agent = agent_ref[0] if agent_ref else None
        if agent is None:
            return
        
        total_cost = agent.monitor.total_cost
        if total_cost > max_cost:
            print(f"\n{'='*80}")
            print(f"MAX COST EXCEEDED: ${total_cost:.4f} > ${max_cost:.4f}")
            print(f"Terminating agent run...")
            print(f"{'='*80}\n")
            agent.interrupt()
            raise MaxCostExceededError(
                f"Maximum cost limit of ${max_cost:.4f} exceeded (current: ${total_cost:.4f})",
                agent.logger
            )
    
    return cost_check_callback


def _build_model(model_config: dict[str, Any]) -> Any:
    """Build a model from configuration."""
    model_type = model_config.get("type", "InferenceClientModel")
    model_id = model_config.get("model_id", "")
    
    # Extract temperature and reasoning_effort if provided
    temperature = model_config.get("temperature")
    reasoning_effort = model_config.get("reasoning_effort")
    model_kwargs = {}
    if temperature is not None:
        model_kwargs["temperature"] = temperature
    if reasoning_effort is not None:
        model_kwargs["reasoning_effort"] = reasoning_effort
    
    # Configure LiteLLM to drop unsupported parameters for providers like xAI
    if model_type == "LiteLLMModel":
        try:
            import litellm
            litellm.drop_params = True
        except ImportError:
            pass

    if model_type == "InferenceClientModel":
        return InferenceClientModel(
            model_id=model_id,
            token=model_config.get("api_key") or os.getenv("HF_API_KEY"),
            provider=model_config.get("provider"),
            **model_kwargs,
        )
    elif model_type == "OpenAIModel":
        return OpenAIModel(
            model_id=model_id,
            api_key=model_config.get("api_key") or os.getenv("OPENAI_API_KEY"),
            api_base=model_config.get("api_base"),
            **model_kwargs,
        )
    elif model_type == "LiteLLMModel":
        return LiteLLMModel(
            model_id=model_id,
            api_key=model_config.get("api_key") or os.getenv("OPENAI_API_KEY"),
            api_base=model_config.get("api_base"),
            service_tier=model_config.get("service_tier", "default"),
            **model_kwargs,
        )
    elif model_type == "TransformersModel":
        return TransformersModel(
            model_id=model_id,
            device_map="auto",
            **model_kwargs,
        )
    else:
        raise ValueError(f"Unsupported model type: {model_type}")


def _build_tools(tool_names: list[str]) -> list[Any]:
    """Build tools from tool names."""
    tools = []
    for tool_name in tool_names:
        if tool_name in TOOL_MAPPING:
            tools.append(TOOL_MAPPING[tool_name]())
        else:
            raise ValueError(f"Unknown tool: {tool_name}")
    return tools


def _install_codeql() -> tuple[bool, str]:
    """
    Install CodeQL CLI if not already installed.
    
    Returns:
        Tuple of (success: bool, message: str)
    """
    import subprocess
    
    # Check if already installed
    check = subprocess.run(["which", "codeql"], capture_output=True, text=True)
    if check.returncode == 0:
        version_check = subprocess.run(["codeql", "--version"], capture_output=True, text=True, timeout=10)
        return True, f"CodeQL already installed: {version_check.stdout.strip()}"
    
    print("="*80)
    print("CodeQL CLI not found. Installing...")
    print("="*80)
    
    try:
        # Detect architecture
        arch_result = subprocess.run(["uname", "-m"], capture_output=True, text=True)
        arch = arch_result.stdout.strip()
        print(f"Detected architecture: {arch}")
        
        # Map architecture to CodeQL bundle name
        if arch in ["x86_64", "amd64"]:
            codeql_arch = "linux64"
        elif arch in ["aarch64", "arm64"]:
            codeql_arch = "linux-arm64"  
        else:
            return False, f"Unsupported architecture: {arch}"
        
        # Download latest CodeQL CLI bundle
        codeql_version = "v2.23.7"
        download_url = f"https://github.com/github/codeql-cli-binaries/releases/download/{codeql_version}/codeql-{codeql_arch}.zip"
        
        install_dir = "/opt"
        codeql_dir = f"{install_dir}/codeql"
        
        print(f"Installing CodeQL {codeql_version} for {codeql_arch}")
        print(f"Download URL: {download_url}")
        
        # Step 1: Install dependencies
        print("\n[1/6] Installing dependencies (unzip, curl, wget)...")
        deps_result = subprocess.run(
            "apt-get update -qq && apt-get install -qq -y unzip curl wget ca-certificates 2>&1 | tail -5",
            shell=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if deps_result.returncode != 0:
            print(f"Warning: apt-get might have failed: {deps_result.stderr[:200]}")
        else:
            print("Dependencies installed")
        
        # Step 2: Create install directory
        print(f"\n[2/6] Creating install directory: {install_dir}")
        subprocess.run(f"mkdir -p {install_dir}", shell=True, check=True)
        
        # Step 3: Download CodeQL
        print(f"\n[3/6] Downloading CodeQL...")
        download_success = False
        
        # Try curl first
        try:
            print(f"  Trying curl...")
            curl_result = subprocess.run(
                f"curl -sSL --connect-timeout 30 --max-time 300 -o /tmp/codeql.zip {download_url}",
                shell=True,
                capture_output=True,
                text=True,
                timeout=320,
            )
            if curl_result.returncode == 0 and os.path.exists("/tmp/codeql.zip"):
                size = os.path.getsize("/tmp/codeql.zip")
                print(f"  Downloaded successfully via curl ({size} bytes)")
                download_success = True
        except Exception as e:
            print(f"  Curl failed: {e}")
        
        # Try wget as fallback
        if not download_success:
            try:
                print(f"  Trying wget...")
                wget_result = subprocess.run(
                    f"wget -q --timeout=300 -O /tmp/codeql.zip {download_url}",
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=320,
                )
                if wget_result.returncode == 0 and os.path.exists("/tmp/codeql.zip"):
                    size = os.path.getsize("/tmp/codeql.zip")
                    print(f"  Downloaded successfully via wget ({size} bytes)")
                    download_success = True
            except Exception as e:
                print(f"  Wget failed: {e}")
        
        if not download_success:
            return False, "Failed to download CodeQL: curl and wget both failed. Check network connectivity."
        
        # Step 4: Extract CodeQL
        print(f"\n[4/6] Extracting CodeQL to {install_dir}...")
        extract_result = subprocess.run(
            f"unzip -qq -o /tmp/codeql.zip -d {install_dir}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if extract_result.returncode != 0:
            return False, f"Failed to extract CodeQL: {extract_result.stderr}"
        print("  Extracted successfully")
        
        # Step 5: Create symlink
        print(f"\n[5/6] Creating symlink...")
        if os.path.exists(f"{codeql_dir}/codeql"):
            subprocess.run(f"ln -sf {codeql_dir}/codeql /usr/local/bin/codeql", shell=True, check=True)
            print(f"  Symlink created: /usr/local/bin/codeql -> {codeql_dir}/codeql")
        else:
            return False, f"CodeQL binary not found at {codeql_dir}/codeql after extraction"
        
        # Step 6: Clean up
        print(f"\n[6/6] Cleaning up...")
        subprocess.run("rm -f /tmp/codeql.zip", shell=True)
        
        # Verify installation
        print(f"\nVerifying CodeQL installation...")
        verify = subprocess.run(
            ["codeql", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "PATH": f"/usr/local/bin:{os.environ.get('PATH', '')}"}
        )
        if verify.returncode == 0:
            version_info = verify.stdout.strip()
            print(f"✓ CodeQL installed successfully!")
            print(f"  Version: {version_info}")
            
            # Download CodeQL C++ query pack and create qlpack for custom queries
            print(f"\n[7/8] Setting up CodeQL query pack for C++...")
            try:
                # Create a qlpack directory for our queries
                qlpack_dir = "/opt/codeql-queries"
                os.makedirs(qlpack_dir, exist_ok=True)
                
                # Write qlpack.yml with compatible cpp-all version
                qlpack_yml = os.path.join(qlpack_dir, "qlpack.yml")
                with open(qlpack_yml, 'w') as f:
                    f.write("""name: secbench-queries
version: 0.0.1
dependencies:
  codeql/cpp-all: ~1.4.0
""")
                print(f"  Created qlpack at {qlpack_dir}")
                
                # Install dependencies using codeql pack install
                print(f"\n[8/8] Installing C++ query pack dependencies...")
                install_result = subprocess.run(
                    ["codeql", "pack", "install"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    cwd=qlpack_dir,
                    env={**os.environ, "PATH": f"/usr/local/bin:{os.environ.get('PATH', '')}"}
                )
                if install_result.returncode == 0:
                    print(f"  ✓ C++ query pack dependencies installed")
                    print(install_result.stdout[-500:] if len(install_result.stdout) > 500 else install_result.stdout)
                else:
                    print(f"  Warning: Pack install may have issues: {install_result.stderr[:300]}")
                
                # Set environment variable for qlpack location
                os.environ["CODEQL_QLPACK_PATH"] = qlpack_dir
                
            except Exception as e:
                print(f"  Warning: Failed to set up query pack: {e}")
                print(f"  Continuing anyway - queries may have limited functionality")
            
            print("="*80)
            return True, f"CodeQL installed: {version_info}"
        else:
            return False, f"CodeQL installation verification failed: {verify.stderr}"
            
    except subprocess.TimeoutExpired:
        return False, "CodeQL installation timed out - download or extraction took too long"
    except Exception as e:
        import traceback
        return False, f"Failed to install CodeQL: {str(e)}\n{traceback.format_exc()}"


def _build_codeql_database(work_dir: str, db_path: str = "/codeql-db") -> tuple[bool, str]:
    """
    Build CodeQL database for the project before agent analysis.
    
    Args:
        work_dir: Path to the source code directory
        db_path: Path where the CodeQL database should be created
    
    Returns:
        Tuple of (success: bool, message: str)
    """
    import subprocess
    import shutil
    
    try:
        # Check if codeql is available, install if not
        codeql_check = subprocess.run(
            ["which", "codeql"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if codeql_check.returncode != 0:
            # Try to install CodeQL
            install_success, install_msg = _install_codeql()
            if not install_success:
                return False, f"CodeQL not available and installation failed: {install_msg}"
            print(install_msg)
        
        # Remove existing database if present
        if os.path.exists(db_path):
            shutil.rmtree(db_path)
        
        # Detect language - for SEC-bench, most projects are C/C++
        # SEC-bench Docker images use 'secb build' command which knows how to build the project
        # We try 'secb build' first, then fall back to auto-detection
        build_cmd = None
        
        # Check if secb helper script exists (SEC-bench Docker environment)
        secb_helper = "/app/secb_helper.sh"
        if os.path.exists(secb_helper):
            # Use secb build which is designed for this environment
            build_cmd = "secb build"
            print("Detected SEC-bench Docker environment, using 'secb build'")
        # Check for various build systems in priority order
        elif os.path.exists(os.path.join(work_dir, "autogen.sh")):
            # Projects with autogen.sh (generates configure)
            build_cmd = "chmod +x autogen.sh && ./autogen.sh && ./configure && make -j$(nproc)"
        elif os.path.exists(os.path.join(work_dir, "configure.ac")) or os.path.exists(os.path.join(work_dir, "configure.in")):
            # Projects need autoconf/automake
            build_cmd = "autoreconf -fi && ./configure && make -j$(nproc)"
        elif os.path.exists(os.path.join(work_dir, "configure")):
            # Projects with configure script
            build_cmd = "chmod +x configure && ./configure && make -j$(nproc)"
        elif os.path.exists(os.path.join(work_dir, "CMakeLists.txt")):
            # CMake projects
            build_cmd = "mkdir -p build && cd build && cmake .. && make -j$(nproc)"
        elif os.path.exists(os.path.join(work_dir, "build.sh")):
            # Custom build script
            build_cmd = "chmod +x build.sh && ./build.sh"
        elif os.path.exists(os.path.join(work_dir, "Makefile")):
            # Direct Makefile (last resort)
            build_cmd = "make -j$(nproc)"
        
        # Build CodeQL database
        # Use --overwrite to handle any partial builds
        # RAM limit can be configured via CODEQL_RAM env var (in MB), default 8GB
        ram_mb = os.environ.get("CODEQL_RAM", "8192")
        codeql_cmd = [
            "codeql", "database", "create",
            db_path,
            "--language=cpp",
            "--source-root", work_dir,
            "--overwrite",
            f"--ram={ram_mb}",
        ]
        
        if build_cmd:
            codeql_cmd.extend(["--command", build_cmd])
        else:
            # Try autobuild for C/C++
            codeql_cmd.append("--no-run-unnecessary-builds")
        
        print(f"Building CodeQL database at {db_path}...")
        if build_cmd:
            print(f"Build command: {build_cmd}")
        print(f"Full command: {' '.join(codeql_cmd)}")
        
        # Install build dependencies that might be needed
        print("Installing build dependencies (autoconf, automake, libtool)...")
        subprocess.run(
            "apt-get install -qq -y autoconf automake libtool pkg-config 2>&1 | tail -3",
            shell=True,
            timeout=120,
        )
        
        result = subprocess.run(
            codeql_cmd,
            capture_output=True,
            text=True,
            cwd=work_dir,
            timeout=600,  # 10 minute timeout for database creation
            env={**os.environ, "CODEQL_ENABLE_EXPERIMENTAL_FEATURES": "true"}
        )
        
        if result.returncode != 0:
            # Show error details
            print(f"Initial build failed with return code {result.returncode}")
            print(f"STDERR (last 500 chars): {result.stderr[-500:] if result.stderr else 'none'}")
            print(f"STDOUT (last 500 chars): {result.stdout[-500:] if result.stdout else 'none'}")
            
            # Try with autobuild as fallback
            print("\nTrying with CodeQL autobuild...")
            codeql_cmd_fallback = [
                "codeql", "database", "create",
                db_path,
                "--language=cpp",
                "--source-root", work_dir,
                "--overwrite",
            ]
            
            print(f"Fallback command: {' '.join(codeql_cmd_fallback)}")
            
            result = subprocess.run(
                codeql_cmd_fallback,
                capture_output=True,
                text=True,
                cwd=work_dir,
                timeout=600,
                env={**os.environ, "CODEQL_ENABLE_EXPERIMENTAL_FEATURES": "true"}
            )
            
            if result.returncode != 0:
                error_msg = f"CodeQL database creation failed with both manual build and autobuild:\n"
                error_msg += f"Return code: {result.returncode}\n"
                error_msg += f"STDERR (last 1000 chars): {result.stderr[-1000:] if result.stderr else 'none'}\n"
                error_msg += f"STDOUT (last 1000 chars): {result.stdout[-1000:] if result.stdout else 'none'}"
                return False, error_msg
            else:
                print("Autobuild succeeded!")
        else:
            print("Manual build succeeded!")
        
        # Verify database was created AND is complete (has dbscheme)
        if not os.path.exists(db_path):
            return False, "CodeQL database directory was not created"
        
        # Check for db-cpp directory and dbscheme file
        db_cpp_path = os.path.join(db_path, "db-cpp")
        if not os.path.exists(db_cpp_path):
            return False, f"CodeQL database missing db-cpp directory at {db_cpp_path}"
        
        # Check for dbscheme file (indicates successful extraction)
        dbscheme_files = [f for f in os.listdir(db_cpp_path) if f.endswith('.dbscheme')]
        if not dbscheme_files:
            # Database was created but no code was extracted - common when build fails silently
            print(f"Warning: No dbscheme file found in {db_cpp_path}")
            print("Contents of db-cpp:", os.listdir(db_cpp_path) if os.path.exists(db_cpp_path) else "N/A")
            return False, "CodeQL database was created but is incomplete (no dbscheme). The build likely failed to compile any code."
        
        print(f"✓ Database verification passed: found {dbscheme_files[0]}")
        
        # Set environment variable for the tool to find the database
        os.environ["CODEQL_DATABASE_PATH"] = db_path
        
        print(f"CodeQL database successfully built at {db_path}")
        return True, f"CodeQL database created at {db_path}"
        
    except subprocess.TimeoutExpired:
        return False, "CodeQL database creation timed out after 10 minutes"
    except Exception as e:
        return False, f"Error building CodeQL database: {str(e)}"


def _write_meta_json(artifacts_dir: str, agent: Union[ToolCallingAgent, CodeAgent], result: Any) -> None:
    """Write metadata JSON file for the agent run."""
    try:
        # Ensure artifacts directory exists
        out_dir = Path(artifacts_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Extract model name
        model_name = getattr(agent.model, "model_id", None) or agent.model.__class__.__name__

        # Extract agent name
        agent_name = agent.__class__.__name__

        # Extract tool names
        tools_attr = getattr(agent, "tools", {})
        if isinstance(tools_attr, dict):
            tool_names = sorted(list(tools_attr.keys()))
        elif isinstance(tools_attr, list):
            tool_names = sorted([getattr(t, "name", t.__class__.__name__) for t in tools_attr])
        else:
            tool_names = []

        # Count steps and aggregate token usage from agent.memory.steps
        steps_count = 0
        input_tokens = 0
        output_tokens = 0

        for step in agent.memory.steps:
            # Count steps that have step_number (ActionStep)
            if hasattr(step, "step_number"):
                steps_count += 1

            # Extract token usage from step
            tu = getattr(step, "token_usage", None)
            if tu is not None:
                input_tokens += getattr(tu, "input_tokens", 0)
                output_tokens += getattr(tu, "output_tokens", 0)

        # If result has token_usage, prefer that (more accurate aggregation)
        if result is not None and hasattr(result, "token_usage") and result.token_usage is not None:
            input_tokens = result.token_usage.input_tokens
            output_tokens = result.token_usage.output_tokens

        # Use actual cost tracked by monitor (from LiteLLM built-in cost tracking)
        cost = agent.monitor.total_cost if hasattr(agent.monitor, "total_cost") else 0.0
        
        # If monitor cost is 0, fall back to calculating using litellm
        if cost == 0.0:
            try:
                from litellm import completion_cost

                fake_response = {
                    "model": model_name.split("/")[-1],
                    "usage": {"prompt_tokens": int(input_tokens), "completion_tokens": int(output_tokens)},
                }
                cost = float(completion_cost(fake_response))
            except Exception:
                # If litellm is not available or cost calculation fails, cost remains 0.0
                pass

        # Build metadata dictionary
        meta = {
            "model": model_name,
            "agent": agent_name,
            "tools": tool_names,
            "steps": steps_count,
            "cost": cost,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

        # Add docker_image if available
        docker_image = os.getenv("DOCKER_IMAGE")
        if docker_image:
            meta["docker_image"] = docker_image

        # Write meta.json using Path.write_text (more robust)
        meta_path = out_dir / "meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=False), encoding="utf-8")

    except Exception as e:
        # Log warning but don't fail the entire run
        import traceback

        print(f"Warning: failed to write meta.json: {e}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)


def main() -> None:
    """Main entry point for the Docker container runner."""
    # Parse command-line arguments (fallback to environment variables)
    parser = argparse.ArgumentParser(description="Run smolagents application inside container")
    parser.add_argument("--config", help="Path to agent config JSON inside container")
    parser.add_argument("--task", help="Task string to run")
    parser.add_argument("--artifacts-dir", help="Directory to write trajectory and outputs")
    parser.add_argument("--max-steps", type=int, help="Optional max steps override")
    args = parser.parse_args()

    # Load agent config (prefer CLI arg, fallback to env var, then default)
    config_path = args.config or os.environ.get("SMOLAGENTS_CONFIG_PATH", "/app/agent_config.json")
    with open(config_path, "r") as f:
        agent_config = json.load(f)

    # Load task (prefer CLI arg, fallback to env var, then default)
    if args.task:
        task = args.task
    else:
        task_path = os.environ.get("SMOLAGENTS_TASK_PATH", "/app/task.txt")
        with open(task_path, "r") as f:
            task = f.read()

    # Create model
    model_config = agent_config.get("model", {})
    model = _build_model(model_config)

    # Get tool names
    tool_names = agent_config.get("tools", [])
    
    # If CodeQL is enabled, build the database before creating tools
    if "codeql" in tool_names:
        # Get work_dir from environment or default
        work_dir = os.environ.get("SMOLAGENTS_WORK_DIR", "/src")
        # Check if a more specific source directory exists
        if os.path.exists("/src") and os.path.isdir("/src"):
            # Find the actual project directory (usually a subdirectory)
            src_contents = os.listdir("/src")
            for item in src_contents:
                item_path = os.path.join("/src", item)
                if os.path.isdir(item_path) and not item.startswith('.'):
                    # Check if it looks like a source directory
                    if any(os.path.exists(os.path.join(item_path, f)) for f in 
                           ["Makefile", "CMakeLists.txt", "configure", "configure.ac", "setup.py", "Cargo.toml"]):
                        work_dir = item_path
                        break
        
        print(f"CodeQL tool enabled. Building database for {work_dir}...")
        success, message = _build_codeql_database(work_dir)
        if success:
            print(f"CodeQL: {message}")
        else:
            print(f"Warning: CodeQL database build failed: {message}")
            print("CodeQL analysis may not work correctly.")

    # Create tools
    tools = _build_tools(tool_names)

    # Create agent
    agent_type = agent_config.get("agent_type", "ToolCallingAgent")
    # Prefer CLI arg for max_steps, then config, then default
    max_steps = args.max_steps if args.max_steps is not None else agent_config.get("max_steps", 20)
    verbosity_level = LogLevel(agent_config.get("verbosity_level", 1))
    
    # Get max_cost from config (0 or None means disabled)
    max_cost = agent_config.get("max_cost", 0.0)
    
    # Create step callbacks list for cost checking
    step_callbacks = None
    agent_ref = []  # Will hold reference to agent for callback
    if max_cost and max_cost > 0:
        print(f"Max cost limit enabled: ${max_cost:.4f} USD")
        step_callbacks = {
            ActionStep: [_create_cost_check_callback(max_cost, agent_ref)]
        }

    # Declare agent variable with union type to allow both agent types
    agent: Union[ToolCallingAgent, CodeAgent]
    if agent_type == "ToolCallingAgent":
        agent = ToolCallingAgent(
            tools=tools,
            model=model,
            max_steps=max_steps,
            verbosity_level=verbosity_level,
            stream_outputs=False,
            step_callbacks=step_callbacks,
        )
    elif agent_type == "CodeAgent":
        agent = CodeAgent(
            tools=tools,
            model=model,
            max_steps=max_steps,
            verbosity_level=verbosity_level,
            additional_authorized_imports=agent_config.get("additional_imports", []),
            stream_outputs=False,
            step_callbacks=step_callbacks,
        )
    else:
        raise ValueError(f"Unsupported agent type: {agent_type}")
    
    # Store agent reference for the callback
    agent_ref.append(agent)

    # Run agent
    try:
        result = agent.run(task, return_full_result=True)

        # Extract output
        if hasattr(result, "output"):
            output = result.output
        else:
            output = result

        # Save result
        # Prefer CLI arg for artifacts_dir, fallback to env var, then default
        artifacts_dir = args.artifacts_dir or os.environ.get("SMOLAGENTS_ARTIFACTS_DIR", "/app/artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)

        with open(os.path.join(artifacts_dir, "output.json"), "w") as f:
            json.dump(
                {
                    "output": str(output) if output is not None else "",
                    "steps": result.steps if hasattr(result, "steps") else [],
                },
                f,
            )

        # Save trajectory
        if hasattr(result, "steps"):
            with open(os.path.join(artifacts_dir, "trajectory.jsonl"), "w") as f:
                for step in result.steps:
                    f.write(json.dumps(step) + "\n")

        # Save metadata
        _write_meta_json(artifacts_dir, agent, result)

        sys.exit(0)
    except MaxCostExceededError as e:
        # Handle max cost exceeded gracefully
        import traceback
        
        artifacts_dir = args.artifacts_dir or os.environ.get("SMOLAGENTS_ARTIFACTS_DIR", "/app/artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)
        
        # Save partial result
        with open(os.path.join(artifacts_dir, "output.json"), "w") as f:
            json.dump(
                {
                    "output": f"TERMINATED: Max cost exceeded (${agent.monitor.total_cost:.4f})",
                    "steps": [],
                    "termination_reason": "max_cost_exceeded",
                    "total_cost": agent.monitor.total_cost,
                },
                f,
            )
        
        with open(os.path.join(artifacts_dir, "error.txt"), "w") as f:
            f.write(f"Max cost exceeded: {str(e)}\n")
            f.write(f"Total cost: ${agent.monitor.total_cost:.4f}\n")
            f.write(traceback.format_exc())
        
        # Write metadata with cost info
        _write_meta_json(artifacts_dir, agent, None)
        
        print(f"Agent terminated due to max cost limit. Total cost: ${agent.monitor.total_cost:.4f}")
        sys.exit(2)  # Exit code 2 for max cost exceeded
    except Exception as e:
        import traceback

        # Prefer CLI arg for artifacts_dir, fallback to env var, then default
        artifacts_dir = args.artifacts_dir or os.environ.get("SMOLAGENTS_ARTIFACTS_DIR", "/app/artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)
        with open(os.path.join(artifacts_dir, "error.txt"), "w") as f:
            f.write(f"Error: {str(e)}\n")
            f.write(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
