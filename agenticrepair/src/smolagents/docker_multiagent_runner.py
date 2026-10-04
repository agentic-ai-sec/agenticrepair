"""Runner script for executing multi-agent scaffold inside Docker containers for SEC-bench evaluation.

Architecture: 3 sequential analyzers → fixer+verifier
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from smolagents.default_tools import TOOL_MAPPING
from smolagents.monitoring import AgentLogger, LogLevel
from smolagents.multiagent_scaffold import MultiAgentOrchestrator


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
        
        # Check if secb command exists (SEC-bench Docker environment)
        # Check both the helper script and the command itself
        secb_helper = "/app/secb_helper.sh"
        secb_check = subprocess.run(
            ["which", "secb"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if os.path.exists(secb_helper) or secb_check.returncode == 0:
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
            # Direct Makefile (last resort) - use 'make all' which works for most projects
            # If this fails, CodeQL will fall back to autobuild
            build_cmd = "make all -j$(nproc)"
        
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


def main() -> None:
    """Main entry point for the multi-agent Docker container runner."""
    parser = argparse.ArgumentParser(description="Run multi-agent scaffold inside container")
    parser.add_argument("--config", help="Path to config JSON inside container")
    parser.add_argument("--instance", help="Path to instance JSON inside container")
    parser.add_argument("--artifacts-dir", help="Directory to write outputs")
    args = parser.parse_args()

    # Load config
    config_path = args.config or os.environ.get("SMOLAGENTS_CONFIG_PATH", "/app/agent_config.json")
    with open(config_path, "r") as f:
        config = json.load(f)

    # Load instance
    instance_path = args.instance or os.environ.get("SMOLAGENTS_INSTANCE_PATH", "/app/instance.json")
    with open(instance_path, "r") as f:
        instance = json.load(f)

    # Get tool names - collect from per-agent configs or fallback to agent.tools
    agent_tools_config = config.get("agent_tools", {})
    agent_config = config.get("agent", {})
    
    # Collect all unique tool names from per-agent configs
    all_tool_names = set()
    for agent_name in ["static_analyzer", "dynamic_analyzer", "history_miner", "fixer_verifier"]:
        agent_tool_config = agent_tools_config.get(agent_name, {})
        agent_tool_names = agent_tool_config.get("tools", [])
        all_tool_names.update(agent_tool_names)
    
    # Fallback to agent.tools if no per-agent tools configured
    if not all_tool_names:
        all_tool_names = set(agent_config.get("tools", []))
    
    tool_names = list(all_tool_names)
    
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

    # Create logger
    verbosity_level = agent_config.get("verbosity_level", 1)
    logger = AgentLogger(level=LogLevel(verbosity_level))

    # Run orchestrator
    try:
        orchestrator = MultiAgentOrchestrator(config, tools, logger)
        result = orchestrator.run(instance)

        # Save results
        artifacts_dir = args.artifacts_dir or os.environ.get("SMOLAGENTS_ARTIFACTS_DIR", "/app/artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)

        with open(os.path.join(artifacts_dir, "multiagent_result.json"), "w") as f:
            json.dump(result.to_dict(), f, indent=2)

        summary = {
            "instance_id": result.instance_id,
            "final_status": result.final_status,
            "total_token_usage": result.total_token_usage,
            "timing": result.timing,
        }
        with open(os.path.join(artifacts_dir, "summary.json"), "w") as f:
            json.dump(summary, f, indent=2)

        # Save trajectories for each agent
        trajectories = result.get_all_trajectories()
        for agent_name, trajectory in trajectories.items():
            traj_file = os.path.join(artifacts_dir, f"{agent_name}_trajectory.jsonl")
            with open(traj_file, "w") as f:
                for line in trajectory.to_jsonl():
                    f.write(line + "\n")

        if result.final_status == "success":
            print(f"\n✓ Multi-agent completed successfully for {result.instance_id}")
            sys.exit(0)
        else:
            print(f"\n✗ Multi-agent failed for {result.instance_id}")
            sys.exit(1)

    except Exception as e:
        import traceback
        
        artifacts_dir = args.artifacts_dir or os.environ.get("SMOLAGENTS_ARTIFACTS_DIR", "/app/artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)
        
        with open(os.path.join(artifacts_dir, "error.txt"), "w") as f:
            f.write(f"Error: {str(e)}\n")
            f.write(traceback.format_exc())
        
        print(f"Multi-agent error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
