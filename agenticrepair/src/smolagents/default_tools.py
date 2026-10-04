#!/usr/bin/env python
# coding=utf-8

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
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
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .local_python_executor import (
    BASE_BUILTIN_MODULES,
    BASE_PYTHON_TOOLS,
    evaluate_python_code,
)
from .tools import Tool


@dataclass
class PreTool:
    name: str
    inputs: dict[str, str]
    output_type: type
    task: str
    description: str
    repo_id: str


class PythonInterpreterTool(Tool):
    name = "python_interpreter"
    description = "This is a tool that evaluates python code. It can be used to perform calculations."
    inputs = {
        "code": {
            "type": "string",
            "description": "The python code to run in interpreter",
        }
    }
    output_type = "string"

    def __init__(self, *args, authorized_imports=None, **kwargs):
        import os

        # Check if running in SEC-bench context (Docker container)
        # When running with secb-run, sandbox checks are disabled since everything runs in Docker
        is_secb_run = os.getenv("SMOLAGENTS_SECB_RUN", "").lower() in ("1", "true", "yes")

        if is_secb_run:
            # Disable sandbox checks by allowing all imports
            self.authorized_imports = ["*"]
        elif authorized_imports is None:
            self.authorized_imports = list(set(BASE_BUILTIN_MODULES))
        else:
            self.authorized_imports = list(set(BASE_BUILTIN_MODULES) | set(authorized_imports))

        self.inputs = {
            "code": {
                "type": "string",
                "description": (
                    "The code snippet to evaluate. All variables used in this snippet must be defined in this same snippet, "
                    f"else you will get an error. This code can only import the following python libraries: {self.authorized_imports}."
                ),
            }
        }
        self.base_python_tools = BASE_PYTHON_TOOLS
        self.python_evaluator = evaluate_python_code
        super().__init__(*args, **kwargs)

    def forward(self, code: str) -> str:
        state = {}
        output = str(
            self.python_evaluator(
                code,
                state=state,
                static_tools=self.base_python_tools,
                authorized_imports=self.authorized_imports,
            )[0]  # The second element is boolean is_final_answer
        )
        return f"Stdout:\n{str(state['_print_outputs'])}\nOutput: {output}"


class FinalAnswerTool(Tool):
    name = "final_answer"
    description = "Provides a final answer to the given problem."
    inputs = {"answer": {"type": "any", "description": "The final answer to the problem"}}
    output_type = "any"

    def forward(self, answer: Any) -> Any:
        return answer


class CmdTool(Tool):
    name = "cmd"
    description = "Execute a shell command inside the current environment and return stdout, stderr, and exit code."
    inputs = {
        "command": {
            "type": "string",
            "description": "The shell command to execute (will be run via bash -lc).",
        },
        "base_dir": {
            "type": "string",
            "description": "Optional base directory to run the command in.",
            "nullable": True,
        },
        "timeout": {
            "type": "integer",
            "description": "Optional timeout in seconds (default 120).",
            "nullable": True,
        },
    }
    output_type = "string"

    def forward(self, command: str, base_dir: str | None = None, timeout: int | None = None) -> str:
        try:
            cwd = None
            if base_dir:
                p = Path(base_dir)
                if not p.exists() or not p.is_dir():
                    return f"Error: base_dir does not exist or is not a directory: {base_dir}"
                cwd = str(p)
            timeout_sec = 120 if (timeout is None or int(timeout) <= 0 or int(timeout) > 600) else int(timeout)
            completed = subprocess.run(
                ["bash", "-lc", command],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout_sec,
            )
            if completed.returncode == 0:
                return (completed.stdout or "").rstrip("\n")
            else:
                return (completed.stderr or "").rstrip("\n")
        except subprocess.TimeoutExpired:
            return f"Timed out after {timeout_sec}s"
        except FileNotFoundError:
            # Fallback if bash is not available
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    shell=True,
                    cwd=cwd,
                    timeout=timeout_sec,
                )
            except subprocess.TimeoutExpired:
                return f"Timed out after {timeout_sec}s"
            if completed.returncode == 0:
                return (completed.stdout or "").rstrip("\n")
            else:
                return (completed.stderr or "").rstrip("\n")


class PyDrillerTool(Tool):
    """
    Tool for mining git repository history using PyDriller.
    Allows searching for commits modifying specific files or functions.
    """
    name = "pydriller"
    description = """
    Mines git repository history to find relevant commits BEFORE a base commit.
    Can filter by file path and function name.
    Useful for finding commits that introduced or modified vulnerable code.
    IMPORTANT: When using base_commit, the tool returns commits BEFORE it (not including base_commit itself).
    """
    inputs = {
        "file_path": {
            "type": "string",
            "description": "Path to the file to analyze (e.g., 'src/file.c')",
            "nullable": True,
        },
        "function_name": {
            "type": "string",
            "description": "Name of the function to track (requires file_path)",
            "nullable": True,
        },
        "base_commit": {
            "type": "string",
            "description": "Base commit hash - returns commits BEFORE this commit (not including itself)",
            "nullable": True,
        },
        "num_commits": {
            "type": "integer",
            "description": "Number of most recent commits to return (default: 3)",
            "nullable": True,
        },
        "include_diff": {
            "type": "boolean",
            "description": "Include code diff in output (default: True)",
            "nullable": True,
        },
        "work_dir": {
            "type": "string",
            "description": "Working directory (repository root)",
            "nullable": True,
        },
        "repo": {
            "type": "string",
            "description": "Repository name in format 'owner/repo' (e.g., 'libarchive/libarchive') - used to fetch history if remote not configured",
            "nullable": True,
        }
    }
    output_type = "string"

    def _path_matches(self, file_path: str, mod_path: str) -> bool:
        """Check if file_path matches mod_path using multiple strategies."""
        if not file_path or not mod_path:
            return False
        
        # Normalize paths: strip whitespace and leading slashes
        file_path_norm = file_path.strip().lstrip('/')
        mod_path_norm = mod_path.strip().lstrip('/')
        
        # Get basenames
        file_basename = Path(file_path_norm).name
        mod_basename = Path(mod_path_norm).name
        
        # Strategy 1: Exact match (normalized)
        if file_path_norm == mod_path_norm:
            return True
        
        # Strategy 2: file_path ends with mod_path or vice versa (handles relative vs absolute)
        if mod_path_norm.endswith(file_path_norm) or file_path_norm.endswith(mod_path_norm):
            return True
        
        # Strategy 3: Basename match (for cases like "njs_vmcode.c" matching "src/njs_vmcode.c")
        if file_basename == mod_basename:
            return True
        
        # Strategy 4: Substring match (fallback)
        if file_path_norm in mod_path_norm or mod_path_norm in file_path_norm:
            return True
        
        return False

    def forward(self, file_path: str | None = None, function_name: str | None = None, 
                base_commit: str | None = None, num_commits: int | None = None,
                include_diff: bool | None = None, work_dir: str | None = None,
                repo: str | None = None) -> str:
        try:
            from pydriller import Repository
        except ImportError as e:
            raise ImportError("You must install `pydriller` to run this tool: `pip install pydriller`") from e

        try:
            # Defaults
            num_commits = num_commits if num_commits is not None else 3
            include_diff = include_diff if include_diff is not None else True
            
            # Validate work_dir
            path_to_repo = work_dir if work_dir else "."
            if not Path(path_to_repo).exists():
                return f"Error: Repository path '{path_to_repo}' does not exist."

            # Build Repository object with filters
            repo_kwargs = {"path_to_repo": path_to_repo}
            if base_commit:
                repo_kwargs["to_commit"] = base_commit  # Include commits up to base_commit
            
            # Collect ALL matching commits first, then take the most recent N
            # (PyDriller traverses oldest-to-newest, so we need all matches to get the latest)
            all_results = []
            commits_checked = 0
            file_matches = 0
            fetch_error_msg = None
            
            # Debug: Check git history availability and fetch if needed
            try:
                git_count = subprocess.run(
                    ["git", "rev-list", "--count", "HEAD"],
                    capture_output=True, text=True, cwd=path_to_repo, timeout=10
                )
                total_commits_str = git_count.stdout.strip() if git_count.returncode == 0 else "unknown"
                total_commits = int(total_commits_str) if total_commits_str.isdigit() else 0
            except Exception:
                total_commits = 0
                total_commits_str = "error"
            
            # If we only have 1 commit (or 0), try to fetch history before base_commit
            if total_commits <= 1 and base_commit:
                try:
                    # Try to get remote URL first
                    remote_result = subprocess.run(
                        ["git", "remote", "get-url", "origin"],
                        capture_output=True, text=True, cwd=path_to_repo, timeout=5
                    )
                    remote_url = remote_result.stdout.strip() if remote_result.returncode == 0 else None
                    
                    # If no remote configured, try to list all remotes
                    if not remote_url:
                        remotes_result = subprocess.run(
                            ["git", "remote"],
                            capture_output=True, text=True, cwd=path_to_repo, timeout=5
                        )
                        if remotes_result.returncode == 0 and remotes_result.stdout.strip():
                            # Use the first remote found
                            first_remote = remotes_result.stdout.strip().split('\n')[0]
                            remote_url_result = subprocess.run(
                                ["git", "remote", "get-url", first_remote],
                                capture_output=True, text=True, cwd=path_to_repo, timeout=5
                            )
                            remote_url = remote_url_result.stdout.strip() if remote_url_result.returncode == 0 else None
                    
                    # If no remote URL but repo name provided, construct GitHub URL
                    if not remote_url and repo:
                        # Construct GitHub URL from repo name (format: owner/repo)
                        remote_url = f"https://github.com/{repo}.git"
                    
                    # If we have a URL (from remote or constructed), fetch directly: git fetch <url> <commit>
                    # This fetches the commit and ALL its ancestors (history before it)
                    # When fetching a specific commit hash, git fetches that commit and ALL its ancestors
                    # It does NOT fetch any commits that come after base_commit
                    if remote_url:
                        # First try --unshallow (works if repo was cloned with --depth)
                        # Fetching base_commit explicitly ensures we only get commits before/including it
                        fetch_result = subprocess.run(
                            ["git", "fetch", "--unshallow", remote_url, base_commit],
                            capture_output=True, text=True, cwd=path_to_repo, timeout=60
                        )
                        
                        # If unshallow fails, try fetching with large depth
                        if fetch_result.returncode != 0:
                            fetch_error_msg = f"git fetch --unshallow failed: {fetch_result.stderr.strip()}"
                            # Fetch with large depth (10000 should be enough for most repos)
                            # Still fetching base_commit specifically, so only ancestors are fetched
                            fetch_result = subprocess.run(
                                ["git", "fetch", "--depth", "10000", remote_url, base_commit],
                                capture_output=True, text=True, cwd=path_to_repo, timeout=60
                            )
                        
                        # Check if fetch succeeded
                        if fetch_result.returncode != 0:
                            fetch_error_msg = f"git fetch failed: {fetch_result.stderr.strip()}"
                        else:
                            # Safety check: Verify we haven't accidentally moved HEAD or fetched future commits
                            # Check if HEAD is still at base_commit (should be unchanged)
                            head_check = subprocess.run(
                                ["git", "rev-parse", "HEAD"],
                                capture_output=True, text=True, cwd=path_to_repo, timeout=5
                            )
                            head_hash = head_check.stdout.strip() if head_check.returncode == 0 else None
                            
                            # Verify HEAD is still at base_commit (ensures no accidental checkout)
                            if head_hash and (head_hash == base_commit or head_hash.startswith(base_commit[:7])):
                                # Re-check commit count after fetch
                                git_count_after = subprocess.run(
                                    ["git", "rev-list", "--count", "HEAD"],
                                    capture_output=True, text=True, cwd=path_to_repo, timeout=10
                                )
                                if git_count_after.returncode == 0:
                                    total_commits_after = git_count_after.stdout.strip()
                                    if total_commits_after.isdigit():
                                        total_commits = int(total_commits_after)
                                        total_commits_str = total_commits_after
                                        fetch_error_msg = None  # Success!
                            else:
                                fetch_error_msg = f"HEAD moved after fetch (expected {base_commit[:7]}, got {head_hash[:7] if head_hash else 'none'})"
                    else:
                        fetch_error_msg = "No git remote configured and no repo name provided - cannot fetch history"
                except Exception as e:
                    fetch_error_msg = f"Exception during fetch: {str(e)}"
            
            # Convert back to string for debug output
            total_commits = str(total_commits) if isinstance(total_commits, int) else total_commits_str
            
            repo = Repository(**repo_kwargs)
            
            for commit in repo.traverse_commits():
                commits_checked += 1
                # Skip the base_commit itself - we want commits BEFORE it
                if base_commit and (commit.hash == base_commit or commit.hash.startswith(base_commit[:7])):
                    continue
                
                # Filter by file path if provided
                if file_path:
                    for mod in commit.modified_files:
                        # Handle None values for new_path and old_path
                        new_path = mod.new_path or ""
                        old_path = mod.old_path or ""
                        
                        # Use flexible path matching
                        if self._path_matches(file_path, new_path) or self._path_matches(file_path, old_path):
                            file_matches += 1
                            function_found = False
                            diff_text = mod.diff or ""
                            
                            # If function name is provided, check if it was modified
                            if function_name:
                                # Check changed_methods
                                for method in mod.changed_methods:
                                    if function_name.lower() in method.name.lower():
                                        function_found = True
                                        break
                                
                                # Also check the diff content for function name
                                if not function_found and diff_text:
                                    if function_name.lower() in diff_text.lower():
                                        function_found = True
                                
                                if not function_found:
                                    continue  # Skip this file if function not found
                            
                            # Build result entry
                            result = (
                                f"Commit: {commit.hash}\n"
                                f"Author: {commit.author.name}\n"
                                f"Date: {commit.committer_date}\n"
                                f"Message: {commit.msg}\n"
                                f"File: {new_path or old_path}"
                            )
                            
                            if include_diff and diff_text:
                                # Limit diff size to avoid overwhelming output
                                diff_preview = diff_text[:1500]
                                if len(diff_text) > 1500:
                                    diff_preview += f"\n... (diff truncated, {len(diff_text) - 1500} more chars)"
                                result += f"\nDiff:\n{diff_preview}"
                            
                            result += "\n---"
                            all_results.append(result)
                            break  # Found the file in this commit, move to next commit
                else:
                    # If no file path provided, just list commits
                    result = (
                        f"Commit: {commit.hash}\n"
                        f"Author: {commit.author.name}\n"
                        f"Date: {commit.committer_date}\n"
                        f"Message: {commit.msg}\n"
                        "---"
                    )
                    all_results.append(result)
            
            # Take the MOST RECENT N commits (last N in the list since traversal is oldest-to-newest)
            results = all_results[-num_commits:] if len(all_results) >= num_commits else all_results
            
            if not results:
                debug_info = (
                    f"\n\nDebug info:"
                    f"\n  Repository: {path_to_repo}"
                    f"\n  Total commits in repo: {total_commits}"
                    f"\n  Commits checked by pydriller: {commits_checked}"
                    f"\n  File matches (before function filter): {file_matches}"
                )
                if fetch_error_msg:
                    debug_info += f"\n  Fetch error: {fetch_error_msg}"
                return f"No matching commits found before {base_commit or 'HEAD'} that modified {file_path or 'any file'}" + \
                       (f" (function: {function_name})" if function_name else "") + debug_info
            
            header = f"Found {len(results)} commit(s) before {base_commit or 'HEAD'}"
            if file_path:
                header += f" modifying {file_path}"
            if function_name:
                header += f" (function: {function_name})"
            header += ":\n\n"
            
            return header + "\n".join(results)

        except Exception as e:
            return f"Error executing PyDriller: {str(e)}"


class CodeQLTool(Tool):
    """
    Tool for running CodeQL queries against a pre-built database to analyze code for security vulnerabilities.
    Supports data flow analysis, taint tracking, memory corruption detection, and more.
    """
    name = "codeql"
    description = """
    Runs CodeQL queries against a pre-built CodeQL database for static code analysis.
    CodeQL is a powerful semantic code analysis engine that can find security vulnerabilities
    by analyzing data flow, taint tracking, and code patterns.
    
    Supported analysis types:
    - dataflow: Data flow and taint tracking analysis (finds sources flowing to sinks)
    - allocation: Allocation and error handling analysis (finds missing null checks, double frees)
    - memory: Variant analysis for memory corruption (buffer overflows, out-of-bounds access)
    - leak: Memory leak detection (unreleased allocations)
    - custom: Run a custom CodeQL query
    
    The database must be pre-built before using this tool.
    """
    inputs = {
        "analysis_type": {
            "type": "string",
            "description": "Type of analysis: 'dataflow', 'allocation', 'memory', 'leak', or 'custom'",
        },
        "query": {
            "type": "string",
            "description": "For 'custom' type: the CodeQL query to run. For other types: optional refinement query or target function/file pattern to focus the analysis",
            "nullable": True,
        },
        "database_path": {
            "type": "string",
            "description": "Path to the CodeQL database (default: /codeql-db)",
            "nullable": True,
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum number of results to return (default: 20)",
            "nullable": True,
        },
    }
    output_type = "string"

    # Pre-built CodeQL queries for common security analyses
    # Simplified queries that work without complex library dependencies
    # Pre-built CodeQL queries for common security analyses
    # These queries handle both regular and fortified function variants (__builtin___*_chk)
    BUILTIN_QUERIES = {
        "dataflow": """
/**
 * @name Dangerous function calls
 * @description Finds calls to potentially dangerous functions (including fortified variants)
 * @kind problem
 * @problem.severity warning
 * @id secbench/dangerous-functions
 */
import cpp

from FunctionCall fc
where
  fc.getTarget().getName().regexpMatch("(strcpy|strcat|sprintf|gets|scanf|fscanf|__builtin___strcpy_chk|__builtin___strcat_chk|__builtin___sprintf_chk)")
select fc, "Call to dangerous function " + fc.getTarget().getName() + " at " + fc.getLocation().toString()
""",
        "allocation": """
/**
 * @name Missing null check after allocation
 * @description Finds allocations without nearby null checks
 * @kind problem
 * @problem.severity warning
 * @id secbench/missing-null-check
 */
import cpp

from FunctionCall alloc
where
  alloc.getTarget().getName() in ["malloc", "calloc", "realloc", "strdup"] and
  not exists(IfStmt check |
    check.getEnclosingFunction() = alloc.getEnclosingFunction() and
    check.getLocation().getStartLine() >= alloc.getLocation().getStartLine() and
    check.getLocation().getStartLine() <= alloc.getLocation().getStartLine() + 5
  )
select alloc, "Allocation at " + alloc.getLocation().toString() + " may be missing null check"
""",
        "memory": """
/**
 * @name Suspicious array access
 * @description Finds array accesses with potentially unsafe indices
 * @kind problem
 * @problem.severity warning
 * @id secbench/suspicious-array
 */
import cpp

from ArrayExpr ae
where
  exists(SubExpr sub | sub = ae.getArrayOffset()) or
  exists(FunctionCall fc | 
    fc.getTarget().getName() in ["atoi", "atol", "strtol", "strtoul"] and
    fc = ae.getArrayOffset()
  )
select ae, "Suspicious array access with potentially unsafe index at " + ae.getLocation().toString()
""",
        "leak": """
/**
 * @name Potential memory leak
 * @description Finds allocations in void functions without corresponding frees
 * @kind problem
 * @problem.severity warning
 * @id secbench/memory-leak
 */
import cpp

from FunctionCall alloc, Function f
where
  alloc.getTarget().getName() in ["malloc", "calloc", "realloc", "strdup"] and
  alloc.getEnclosingFunction() = f and
  not exists(FunctionCall freeCall |
    freeCall.getTarget().getName() = "free" and
    freeCall.getEnclosingFunction() = f
  ) and
  f.getType().toString() = "void"
select alloc, "Potential memory leak in " + f.getName() + " - allocation without free in void function"
"""
    }

    def forward(
        self,
        analysis_type: str,
        query: str | None = None,
        database_path: str | None = None,
        max_results: int | None = None,
    ) -> str:
        import os
        import tempfile
        import time
        import fcntl

        # Use a lock file to serialize CodeQL queries (prevent parallel access to database)
        lock_file_path = "/tmp/codeql_tool.lock"
        lock_file = None
        max_retries = 180  # Wait up to 180 seconds for lock
        retry_delay = 5  # Check every 5 seconds

        try:
            # Acquire lock with retry logic
            for attempt in range(max_retries):
                try:
                    lock_file = open(lock_file_path, 'w')
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break  # Lock acquired successfully
                except (IOError, OSError):
                    # Lock is held by another process, wait and retry
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                    else:
                        return f"Error: CodeQL database is locked by another query. Waited {max_retries} seconds but lock was not released. Try again later."
            
            # Check if codeql is available
            codeql_check = subprocess.run(
                ["which", "codeql"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if codeql_check.returncode != 0:
                return "Error: CodeQL CLI is not installed or not in PATH. Please install CodeQL first."

            # Set database path
            db_path = database_path or os.environ.get("CODEQL_DATABASE_PATH", "/codeql-db")
            if not Path(db_path).exists():
                return f"Error: CodeQL database not found at {db_path}. Please ensure the database was built before analysis."

            # Set max results (reduced default for conciseness)
            max_res = max_results if max_results and max_results > 0 else 10

            # Determine query to run
            if analysis_type == "custom":
                if not query:
                    return "Error: 'custom' analysis type requires a query parameter with the CodeQL query to run."
                query_content = query
            elif analysis_type in self.BUILTIN_QUERIES:
                query_content = self.BUILTIN_QUERIES[analysis_type]
                # If query parameter is provided for builtin, use it as a filter hint in output
                filter_hint = query
            else:
                return f"Error: Unknown analysis type '{analysis_type}'. Supported types: dataflow, allocation, memory, leak, custom"

            # Use qlpack directory if available, otherwise use temp file
            qlpack_path = os.environ.get("CODEQL_QLPACK_PATH", "/opt/codeql-queries")
            if os.path.exists(qlpack_path) and os.path.exists(os.path.join(qlpack_path, "qlpack.yml")):
                # Write query to qlpack directory
                query_file = os.path.join(qlpack_path, f"query_{analysis_type}.ql")
                with open(query_file, 'w') as qf:
                    qf.write(query_content)
            else:
                # Fallback to temp file
                with tempfile.NamedTemporaryFile(mode='w', suffix='.ql', delete=False) as qf:
                    qf.write(query_content)
                    query_file = qf.name

            try:
                # Create temp file for results
                with tempfile.NamedTemporaryFile(mode='w', suffix='.sarif', delete=False) as rf:
                    result_file = rf.name

                # Run CodeQL query
                # RAM limit can be configured via CODEQL_RAM env var (in MB), default 8GB
                ram_mb = os.environ.get("CODEQL_RAM", "8192")
                codeql_cmd = [
                    "codeql", "query", "run",
                    "--database", db_path,
                    "--output", result_file,
                    "--format", "sarif-latest",
                    f"--ram={ram_mb}",
                    query_file
                ]

                result = subprocess.run(
                    codeql_cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minute timeout
                )

                if result.returncode != 0:
                    # Try alternative: run with bqrs output
                    bqrs_file = result_file.replace('.sarif', '.bqrs')
                    codeql_cmd_bqrs = [
                        "codeql", "query", "run",
                        "--database", db_path,
                        "--output", bqrs_file,
                        f"--ram={ram_mb}",
                        query_file
                    ]
                    
                    result = subprocess.run(
                        codeql_cmd_bqrs,
                        capture_output=True,
                        text=True,
                        timeout=300,
                    )
                    
                    if result.returncode != 0:
                        error_msg = f"CodeQL query failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
                        return error_msg
                    
                    # Decode bqrs to text
                    decode_cmd = [
                        "codeql", "bqrs", "decode",
                        "--format", "csv",
                        "--output", result_file,
                        bqrs_file
                    ]
                    subprocess.run(decode_cmd, capture_output=True, timeout=60)
                    
                    # Read CSV results and format concisely
                    if Path(result_file).exists():
                        import csv as csv_module
                        with open(result_file, 'r') as f:
                            reader = csv_module.reader(f)
                            rows = list(reader)
                        
                        if len(rows) <= 1:
                            return f"CodeQL {analysis_type}: No issues found."
                        
                        # Parse and format results concisely
                        results = []
                        for row in rows[1:max_res + 1]:  # Skip header
                            if len(row) >= 2:
                                # Extract file and line from location string if present
                                location = row[0] if row[0] else ""
                                message = row[1] if len(row) > 1 else "Issue found"
                                
                                # Extract filename and line number from location
                                file_info = ""
                                if "file://" in location:
                                    # Extract just filename and line
                                    parts = location.split("file://")[-1]
                                    if ":" in parts:
                                        filepath, line = parts.split(":", 1)
                                        filename = filepath.split("/")[-1]
                                        file_info = f"{filename}:{line.split(':')[0]}"
                                
                                if file_info:
                                    results.append(f"{file_info}: {message}")
                                else:
                                    results.append(message)
                        
                        # Format output concisely
                        total = len(rows) - 1
                        output = f"CodeQL {analysis_type}: {total} finding(s)\n"
                        for i, res in enumerate(results, 1):
                            output += f"  {i}. {res}\n"
                        
                        if total > max_res:
                            output += f"  ... {total - max_res} more (truncated)"
                        
                        return output
                    else:
                        return f"CodeQL analysis completed but no results file generated.\nSTDOUT: {result.stdout}"

                # Parse SARIF output
                import json
                with open(result_file, 'r') as f:
                    sarif_data = json.load(f)

                # Extract results concisely
                results = []
                for run in sarif_data.get('runs', []):
                    for res in run.get('results', []):
                        msg = res.get('message', {}).get('text', 'Issue found')
                        locations = res.get('locations', [])
                        
                        # Extract just filename and line
                        file_info = ""
                        if locations:
                            loc = locations[0].get('physicalLocation', {})
                            artifact = loc.get('artifactLocation', {}).get('uri', '')
                            region = loc.get('region', {})
                            line = region.get('startLine', '?')
                            
                            # Extract just filename from full path
                            if artifact:
                                filename = artifact.split('/')[-1]
                                file_info = f"{filename}:{line}"
                        
                        if file_info:
                            results.append(f"{file_info}: {msg}")
                        else:
                            results.append(msg)

                if not results:
                    return f"CodeQL {analysis_type}: No issues found."

                # Format output concisely
                total = len(results)
                output = f"CodeQL {analysis_type}: {total} finding(s)\n"
                for i, res in enumerate(results[:max_res], 1):
                    output += f"  {i}. {res}\n"

                if total > max_res:
                    output += f"  ... {total - max_res} more (truncated)"

                return output

            finally:
                # Cleanup temp files
                try:
                    os.unlink(query_file)
                except Exception:
                    pass
                try:
                    os.unlink(result_file)
                except Exception:
                    pass

        except subprocess.TimeoutExpired:
            return "Error: CodeQL query timed out after 5 minutes"
        except FileNotFoundError:
            return "Error: CodeQL CLI is not installed or not in PATH."
        except Exception as e:
            return f"Error executing CodeQL: {str(e)}"
        finally:
            # Release lock
            if lock_file:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    lock_file.close()
                except Exception:
                    pass


class GDBTool(Tool):
    """
    Tool for debugging C/C++ memory errors using GDB (GNU Debugger).
    Specifically designed for analyzing crash points, memory corruption, and 
    understanding the exact state when memory errors occur.
    
    This tool is optimized for memory error debugging in C/C++ programs including:
    - Heap buffer overflow
    - Stack buffer overflow
    - Use-after-free
    - Double-free
    - Null pointer dereference
    - Out-of-bounds access
    """
    name = "gdb"
    description = """
    Runs a program under GDB to debug C/C++ memory errors. This tool helps analyze crash points
    and memory corruption issues by examining:
    - Stack traces (backtrace) at crash points
    - Memory contents around crash addresses
    - Register values at crash time
    - Local variables and function arguments
    - Memory allocation/deallocation patterns
    
    Use this tool when you need to:
    1. Understand the exact crash location and call stack
    2. Examine memory state at the point of failure
    3. Analyze the sequence of function calls leading to a crash
    4. Inspect specific memory addresses mentioned in sanitizer reports
    
    The tool automatically sets up GDB for crash analysis and captures detailed
    diagnostic information useful for fixing memory vulnerabilities.
    """
    inputs = {
        "command": {
            "type": "string",
            "description": "The command to run under GDB (e.g., 'secb repro' or './program input.txt')",
        },
        "work_dir": {
            "type": "string",
            "description": "Working directory to run the command in",
            "nullable": True,
        },
        "build_with_debug": {
            "type": "boolean",
            "description": "If True, rebuild the project with debug symbols (-g -O0) before debugging. This enables source-level debugging with variable names, line numbers, and source code. Recommended for detailed analysis. Default: False",
            "nullable": True,
        },
        "crash_address": {
            "type": "string",
            "description": "Optional memory address from sanitizer report to examine (e.g., '0x7f1234567890')",
            "nullable": True,
        },
        "breakpoints": {
            "type": "string",
            "description": "Optional comma-separated breakpoints to set (e.g., 'main,parse_data,file.c:42')",
            "nullable": True,
        },
        "gdb_commands": {
            "type": "string",
            "description": "Optional additional GDB commands to run, separated by semicolons (e.g., 'info registers;x/20x $sp')",
            "nullable": True,
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds (default: 300, max: 600)",
            "nullable": True,
        },
    }
    output_type = "string"

    def _install_gdb(self) -> tuple[bool, str]:
        """Install GDB if not available. Returns (success, message)."""
        try:
            install_result = subprocess.run(
                ["apt-get", "update"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if install_result.returncode != 0:
                return False, f"apt-get update failed: {install_result.stderr}"
            
            install_result = subprocess.run(
                ["apt-get", "install", "-y", "gdb"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if install_result.returncode != 0:
                return False, f"apt-get install gdb failed: {install_result.stderr}"
            
            return True, "GDB installed successfully"
        except subprocess.TimeoutExpired:
            return False, "Installation timed out"
        except Exception as e:
            return False, f"Installation error: {str(e)}"

    def _extract_secb_command(self, work_dir: str) -> tuple[str | None, str]:
        """
        Extract the actual command that 'secb repro' runs.
        Returns (command, message).
        """
        import re
        
        try:
            # Method 1: Try to read from secb_helper.sh (most reliable)
            # Check multiple possible locations
            helper_paths = [
                "/app/secb_helper.sh",
                os.path.join(work_dir, "secb_helper.sh"),
                os.path.join(work_dir, "..", "secb_helper.sh"),
                "/secb_helper.sh",
            ]
            helper_path = None
            for hp in helper_paths:
                if os.path.exists(hp):
                    helper_path = hp
                    break
            
            if helper_path:
                with open(helper_path, 'r') as f:
                    content = f.read()
                    
                    # First, try to extract the FULL command from the repro function/case
                    # Look for the repro case and capture everything until the next case or end
                    repro_case_match = re.search(
                        r'repro\)\s*(.*?)(?=\n\s*(?:[a-z_]+\)|$))',
                        content,
                        re.DOTALL | re.IGNORECASE
                    )
                    
                    if repro_case_match:
                        repro_body = repro_case_match.group(1)
                        # Look for the actual command execution (usually the last non-comment, non-empty line)
                        # Pattern: $BIN -d -o /dev/null -f $INPUT or similar
                        command_lines = [line.strip() for line in repro_body.split('\n') 
                                       if line.strip() and not line.strip().startswith('#')]
                        
                        if command_lines:
                            # Take the last non-empty line (usually the actual command)
                            full_cmd_line = command_lines[-1]
                            
                            # Resolve variables in the command
                            # Find all variable references like $BIN, ${BIN}, etc.
                            def resolve_var(match):
                                var_name = match.group(1) or match.group(2)
                                # Look for variable definition
                                var_pattern = rf'{var_name}[=\s]+["\']?([^"\';\n\s]+)'
                                var_match = re.search(var_pattern, content, re.IGNORECASE)
                                if var_match:
                                    return var_match.group(1).strip()
                                return match.group(0)  # Return original if not found
                            
                            # Replace $VAR and ${VAR} patterns
                            resolved_cmd = re.sub(r'\$\{?([a-zA-Z_][a-zA-Z0-9_]*)\}?', resolve_var, full_cmd_line)
                            
                            # Check if the binary in the resolved command exists
                            cmd_parts = resolved_cmd.split()
                            if cmd_parts:
                                binary_candidate = cmd_parts[0]
                                if os.path.exists(binary_candidate) and os.access(binary_candidate, os.X_OK):
                                    return resolved_cmd, f"Extracted full command from secb_helper.sh: {resolved_cmd}"
                    
                    # Fallback: Pattern 1 - BIN="..." and INPUT="..." style (old method)
                    bin_match = re.search(r'(?:BIN|BINARY|EXECUTABLE|TARGET)[=\s]+["\']?([^"\';\n\s]+)', content, re.IGNORECASE)
                    input_match = re.search(r'(?:INPUT|TESTCASE|POC|CRASH)[=\s]+["\']?([^"\';\n\s]+)', content, re.IGNORECASE)
                    
                    if bin_match:
                        binary = bin_match.group(1).strip()
                        # Try to resolve the binary path
                        if binary.startswith("$"):
                            # Variable reference - try to find its value
                            var_name = binary.lstrip("$").strip("{}")
                            var_match = re.search(rf'{var_name}[=\s]+["\']?([^"\';\n\s]+)', content)
                            if var_match:
                                binary = var_match.group(1).strip()
                        
                        # Check common locations for the binary
                        binary_candidates = [
                            binary,
                            os.path.join(work_dir, binary),
                            os.path.join(work_dir, "build", os.path.basename(binary)),
                            os.path.join(work_dir, "build", binary),
                        ]
                        
                        for candidate in binary_candidates:
                            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                                input_file = ""
                                if input_match:
                                    inp = input_match.group(1).strip()
                                    if not inp.startswith("$") and os.path.exists(inp):
                                        input_file = inp
                                    elif os.path.exists("/testcase/poc.js"):
                                        input_file = "/testcase/poc.js"
                                    elif os.path.exists("/testcase/poc"):
                                        input_file = "/testcase/poc"
                                cmd = f"{candidate} {input_file}".strip()
                                return cmd, f"Extracted from secb_helper.sh: {cmd}"
            
            # Method 2: Use strace to capture the FULL command (prioritize this for accuracy)
            # This method captures the actual execve call with all arguments
            try:
                # Check if strace is available
                strace_check = subprocess.run(
                    ["which", "strace"],
                    capture_output=True,
                    timeout=5,
                )
                if strace_check.returncode != 0:
                    # Strace not available, skip this method
                    pass
                else:
                    # Clean up any old trace file
                    trace_file = "/tmp/secb_trace.txt"
                    if os.path.exists(trace_file):
                        os.remove(trace_file)
                    
                    # Use strace with better options to capture all execve calls
                    # -f: follow forks, -e execve: only execve syscalls, -s 1000: full argument strings
                    # -o: output to file, -qq: quiet mode (suppress attach messages)
                    strace_result = subprocess.run(
                        ["timeout", "15", "strace", "-f", "-qq", "-e", "execve", "-s", "1000", "-o", trace_file, 
                         "bash", "-lc", "cd " + work_dir + " && secb repro || true"],
                        capture_output=True,
                        text=True,
                        cwd=work_dir,
                        timeout=20,
                    )
                    
                    if os.path.exists(trace_file):
                        with open(trace_file, 'r') as f:
                            trace_content = f.read()
                        
                            if not trace_content.strip():
                                # Empty trace file - strace might have failed
                                # Check stderr for errors
                                if strace_result.stderr:
                                    pass  # Strace errors logged in stderr
                            else:
                                # Parse execve calls - format: execve("/path", ["arg0", "arg1", ...], [env]) = return_code
                                # We want to find the execve call for the actual target binary (not shell utilities)
                                skip_patterns = ('bash', 'sh', 'env', 'locale', 'cat', 'grep', 'sed', 'awk', 
                                                'head', 'tail', 'cut', 'tr', 'sort', 'uniq', 'wc', 'dirname',
                                                'basename', 'readlink', 'realpath', 'which', 'test', '[', 'timeout', 'strace',
                                                'locale-check', 'locale-gen', 'update-locale')
                                
                                # Find all execve lines with full argument lists
                                # Also handle multi-line execve calls
                                execve_full_pattern = r'execve\("([^"]+)",\s*\[([^\]]+)\]'
                                execve_matches = list(re.finditer(execve_full_pattern, trace_content))
                                
                                # Also try simpler pattern for single-line execve
                                if not execve_matches:
                                    # Try pattern: execve("/path/to/bin", ["arg0", "arg1"], ...)
                                    execve_simple_pattern = r'execve\("([^"]+)"'
                                    execve_matches = list(re.finditer(execve_simple_pattern, trace_content))
                                
                                # Look for the target binary (usually in work_dir or /src/ or build directories)
                                # Check all matches, prefer ones in work_dir
                                candidates = []
                                for match in execve_matches:
                                    exec_path = match.group(1)
                                    
                                    # Skip shell utilities
                                    base = os.path.basename(exec_path)
                                    if any(base.endswith(p) or base == p for p in skip_patterns):
                                        continue
                                    
                                    # Check if this looks like the target binary
                                    if os.path.exists(exec_path):
                                        # Prefer binaries in work_dir, then /src/, then any executable
                                        priority = 0
                                        if work_dir in exec_path:
                                            priority = 3
                                        elif '/src/' in exec_path:
                                            priority = 2
                                        elif exec_path.startswith('/') and not exec_path.startswith('/usr') and not exec_path.startswith('/bin'):
                                            priority = 1
                                        
                                        if priority > 0:
                                            # Try to get full arguments
                                            args_str = match.group(2) if len(match.groups()) > 1 else ""
                                            args = []
                                            if args_str:
                                                args = re.findall(r'"([^"]+)"', args_str)
                                            
                                            candidates.append((priority, exec_path, args))
                                
                                # Sort by priority (highest first)
                                if candidates:
                                    candidates.sort(key=lambda x: x[0], reverse=True)
                                    best_priority, best_path, best_args = candidates[0]
                                    
                                    # Reconstruct full command
                                    if best_args and len(best_args) > 1:
                                        # Use all arguments
                                        import shlex
                                        # Reconstruct command with proper quoting
                                        full_cmd = best_path
                                        for arg in best_args[1:]:  # Skip first arg (usually binary name)
                                            # Properly quote arguments with spaces
                                            if ' ' in arg or '\t' in arg:
                                                full_cmd += f" {shlex.quote(arg)}"
                                            else:
                                                full_cmd += f" {arg}"
                                        return full_cmd, f"Found via strace (priority {best_priority}): {full_cmd}"
                                    else:
                                        return best_path, f"Found via strace (priority {best_priority}): {best_path}"
            except subprocess.TimeoutExpired:
                pass  # Continue to next method
            except Exception as e:
                # Log error but continue to next method
                pass
            except subprocess.TimeoutExpired:
                pass  # Continue to next method
            except Exception as e:
                # Log error but continue to next method
                pass
            
            # Method 3: Look for ELF binaries in build directory (fallback, less accurate)
            project_name = os.path.basename(work_dir)
            potential_binaries = []
            testcase_file = None
            
            # Find testcase/input file
            for tc in ["/testcase/poc.js", "/testcase/poc", "/testcase/input", "/testcase/crash"]:
                if os.path.exists(tc):
                    testcase_file = tc
                    break
            
            # Search for ELF binaries - more aggressive search
            search_dirs = []
            # Add work_dir and its subdirectories
            search_dirs.append(work_dir)
            for subdir in ['src', 'build', 'bin', 'out', 'Release', 'Debug', '.']:
                check_dir = os.path.join(work_dir, subdir) if subdir != '.' else work_dir
                if os.path.isdir(check_dir):
                    search_dirs.append(check_dir)
            
            # Also check if work_dir/src exists and search there
            src_dir = os.path.join(work_dir, 'src')
            if os.path.isdir(src_dir):
                for subdir in ['build', 'bin', 'out', 'Release', 'Debug', '.']:
                    check_dir = os.path.join(src_dir, subdir) if subdir != '.' else src_dir
                    if os.path.isdir(check_dir):
                        search_dirs.append(check_dir)
            
            # Search all directories
            for check_dir in search_dirs:
                if not os.path.isdir(check_dir):
                    continue
                try:
                    for item in os.listdir(check_dir):
                        item_path = os.path.join(check_dir, item)
                        if os.path.isfile(item_path) and os.access(item_path, os.X_OK):
                            try:
                                with open(item_path, 'rb') as f:
                                    magic = f.read(4)
                                    if magic == b'\x7fELF':
                                        # Prefer binary with same name as project
                                        item_name_lower = item.lower()
                                        project_name_lower = project_name.lower()
                                        
                                        # Check multiple helper paths
                                        helper_paths_to_check = [
                                            "/app/secb_helper.sh",
                                            os.path.join(work_dir, "secb_helper.sh"),
                                            os.path.join(work_dir, "..", "secb_helper.sh"),
                                            "/secb_helper.sh",
                                        ]
                                        
                                        if item_name_lower == project_name_lower or item_name_lower.startswith(project_name_lower):
                                            # Try to get full command from secb_helper.sh if available
                                            for helper_path in helper_paths_to_check:
                                                if os.path.exists(helper_path):
                                                    try:
                                                        with open(helper_path, 'r') as hf:
                                                            helper_content = hf.read()
                                                            # Look for repro case with full command
                                                            repro_case_match = re.search(
                                                                r'repro\)\s*(.*?)(?=\n\s*(?:[a-z_]+\)|$))',
                                                                helper_content,
                                                                re.DOTALL | re.IGNORECASE
                                                            )
                                                            if repro_case_match:
                                                                repro_body = repro_case_match.group(1)
                                                                command_lines = [line.strip() for line in repro_body.split('\n') 
                                                                               if line.strip() and not line.strip().startswith('#')]
                                                                if command_lines:
                                                                    full_cmd_line = command_lines[-1]
                                                                    # Replace $BIN or similar with actual path
                                                                    full_cmd_line = re.sub(r'\$\{?BIN\}?', item_path, full_cmd_line, flags=re.IGNORECASE)
                                                                    full_cmd_line = re.sub(r'\$\{?INPUT\}?', testcase_file or '', full_cmd_line, flags=re.IGNORECASE)
                                                                    # Resolve other variables
                                                                    def resolve_var(match):
                                                                        var_name = match.group(1) or match.group(2)
                                                                        var_pattern = rf'{var_name}[=\s]+["\']?([^"\';\n\s]+)'
                                                                        var_match = re.search(var_pattern, helper_content, re.IGNORECASE)
                                                                        if var_match:
                                                                            return var_match.group(1).strip()
                                                                        return match.group(0)
                                                                    full_cmd_line = re.sub(r'\$\{?([a-zA-Z_][a-zA-Z0-9_]*)\}?', resolve_var, full_cmd_line)
                                                                    return full_cmd_line.strip(), f"Found project binary with full command: {full_cmd_line.strip()}"
                                                    except:
                                                        continue
                                            # Fallback: just binary + testcase
                                            cmd = f"{item_path} {testcase_file or ''}".strip()
                                            return cmd, f"Found project binary: {cmd}"
                                        # Add to potential binaries list (even if not exact match)
                                        potential_binaries.append((item_path, item_name_lower == project_name_lower))
                            except:
                                pass
                except:
                    pass
            
            # If we found binaries, use the best match
            if potential_binaries:
                # Sort to prefer binaries that match project name, then by path
                potential_binaries.sort(key=lambda x: (not x[1], x[0]))
                best_binary = potential_binaries[0][0]
                cmd = f"{best_binary} {testcase_file or ''}".strip()
                return cmd, f"Found ELF binary: {cmd}"
            
            return None, f"Could not extract secb repro command. Work dir: {work_dir}"
            
        except subprocess.TimeoutExpired:
            return None, "Timeout while extracting secb command"
        except Exception as e:
            return None, f"Error extracting secb command: {str(e)}"

    def _create_gdb_script(
        self,
        crash_address: str | None,
        breakpoints: str | None,
        gdb_commands: str | None,
        follow_child: bool = True,
        target_binary: str | None = None,
        work_dir: str | None = None,
    ) -> str:
        """Create a GDB command script for memory error analysis with robust error handling."""
        script_lines = [
            # Basic GDB setup for crash analysis
            "set pagination off",
            "set print pretty on",
            "set print array on",
            "set print array-indexes on",
            "set print elements 100",
            "set confirm off",
            "set verbose off",
            # Allow pending breakpoints (resolve when library/binary loads)
            "set breakpoint pending on",
            # Print demangled C++ names
            "set print demangle on",
            "set print asm-demangle on",
            # Ignore DWARF errors - they're warnings, not fatal
            "set complaints 0",
        ]
        
        # Follow child processes for secb repro and similar commands
        if follow_child:
            script_lines.extend([
                "# Follow child processes (critical for secb repro)",
                "set follow-fork-mode child",
                "set detach-on-fork off",
            ])
            # If we have a target binary but are following child processes, add a note
            # The breakpoint on main will help, but we may still attach to system utilities
            if target_binary and os.path.exists(target_binary) and follow_child:
                script_lines.extend([
                    f"# Note: Following child processes. Target binary: {target_binary}",
                    f"# GDB may attach to system utilities; we'll filter during analysis",
                ])
        
        script_lines.extend([
            # Enable ASLR-aware debugging  
            "set disable-randomization off",
            "",
            "# Signal handling for crash capture",
            "# SIGSEGV - stop to analyze segfaults",
            "handle SIGSEGV stop print nopass",
            "# SIGBUS - stop to analyze bus errors", 
            "handle SIGBUS stop print nopass",
            "# SIGABRT - CRITICAL: stop on ASan/sanitizer aborts",
            "handle SIGABRT stop print nopass",
            "# SIGFPE - stop to analyze floating point exceptions",
            "handle SIGFPE stop print nopass",
            "# SIGILL - stop on illegal instructions",
            "handle SIGILL stop print nopass",
            "",
            "# Define helper command for safe backtrace",
            "define safe_bt",
            "  echo === Backtrace ===\\n",
            "  bt 30",
            "end",
            "",
            "# Define helper for safe frame info",
            "define safe_frame_info",
            "  echo === Frame Info ===\\n",
            "  info frame",
            "  echo === Local Variables ===\\n",
            "  info locals",
            "  echo === Arguments ===\\n", 
            "  info args",
            "end",
            "",
            "echo \\n=== GDB Memory Error Analysis ===\\n\\n",
        ])
        
        # Only set breakpoints if explicitly requested AND not empty
        if breakpoints and breakpoints.strip():
            script_lines.append("echo === Setting Breakpoints (pending) ===\\n")
            for bp in breakpoints.split(","):
                bp = bp.strip()
                if bp:
                    script_lines.append(f"break {bp}")
            script_lines.append("")
        else:
            script_lines.append("echo === Running until crash (no breakpoints) ===\\n")
        
        # Set breakpoints on ASan handlers to catch before exit
        script_lines.extend([
            "# Try to catch ASan before it aborts",
            "break __asan_report_error",
            "break __sanitizer_print_stack_trace",
            "",
            "# Handle SEGV and other signals",
            "handle SIGSEGV stop print",
            "handle SIGABRT stop print",
            "handle SIGBUS stop print",
            "handle SIGFPE stop print",
            "handle SIGILL stop print",
            "",
            "echo === Running Program ===\\n",
            "run",
            "",
            "# Check if we're stopped or program exited",
            "echo \\n=== Program State ===\\n",
            "info program",
            "",
        ])
        
        # Use Python for error handling if available, otherwise use simple commands
        script_lines.extend([
            "# Try to get backtrace - handle 'No stack' gracefully",
            "echo \\n=== Crash Analysis ===\\n\\n",
            "",
            "# Try full backtrace first",
            "echo === Attempting Full Backtrace ===\\n",
            "backtrace full 30",
            "",
            "# If that didn't work, try simple backtrace",
            "echo \\n=== Simple Backtrace (fallback) ===\\n",
            "bt 30",
            "",
            "# Try to get register info",
            "echo \\n=== Registers ===\\n",
            "info registers",
            "",
            "# Try thread info",
            "echo \\n=== Threads ===\\n",
            "info threads",
            "",
        ])
        
        # If crash address provided, examine memory around it
        if crash_address:
            script_lines.extend([
                f"echo \\n=== Memory Examination at {crash_address} ===\\n",
                f"x/64xb ({crash_address} - 32)",
                "",
            ])
        
        # Add custom GDB commands if specified
        if gdb_commands:
            script_lines.append("echo \\n=== Custom GDB Commands Output ===\\n")
            for cmd in gdb_commands.split(";"):
                cmd = cmd.strip()
                if cmd:
                    script_lines.append(cmd)
            script_lines.append("")
        
        # Final cleanup
        script_lines.extend([
            "echo \\n=== GDB Analysis Complete ===\\n",
            "quit",
        ])
        
        return "\n".join(script_lines)

    def _build_with_debug_symbols(self, work_dir: str, timeout: int = 300) -> tuple[bool, str]:
        """
        Rebuild the project with debug symbols enabled.
        Returns (success, message).
        """
        try:
            output_lines = []
            output_lines.append("=== Rebuilding with Debug Symbols ===")
            output_lines.append("Setting CFLAGS/CXXFLAGS with -g for source-level debugging...")
            
            # Set environment variables for debug build
            env = os.environ.copy()
            # Add debug flags - preserve existing flags if any
            existing_cflags = env.get("CFLAGS", "")
            existing_cxxflags = env.get("CXXFLAGS", "")
            # Use DWARF4 for better GDB compatibility, -g3 for maximum debug info
            # -fno-omit-frame-pointer for better stack traces
            # Keep -O0 but don't force it if sanitizers need optimization
            debug_flags = "-g3 -gdwarf-4 -fno-omit-frame-pointer -fno-inline"
            
            env["CFLAGS"] = f"{debug_flags} {existing_cflags}".strip()
            env["CXXFLAGS"] = f"{debug_flags} {existing_cxxflags}".strip()
            # Also set for CMake projects
            env["CMAKE_BUILD_TYPE"] = "Debug"
            env["CMAKE_C_FLAGS_DEBUG"] = debug_flags
            env["CMAKE_CXX_FLAGS_DEBUG"] = debug_flags
            # Prevent stripping
            env["INSTALL_STRIP_FLAG"] = ""
            env["STRIP"] = "true"  # Make strip a no-op
            
            output_lines.append(f"CFLAGS: {env['CFLAGS']}")
            output_lines.append(f"CXXFLAGS: {env['CXXFLAGS']}")
            output_lines.append("")
            
            # Clean previous build if possible
            output_lines.append("Cleaning previous build...")
            clean_result = subprocess.run(
                ["bash", "-lc", "make clean 2>/dev/null || rm -rf build 2>/dev/null || true"],
                capture_output=True,
                text=True,
                cwd=work_dir,
                timeout=60,
            )
            
            # Run secb build with debug environment
            output_lines.append("Running: secb build (with debug symbols)")
            build_result = subprocess.run(
                ["bash", "-lc", "secb build"],
                capture_output=True,
                text=True,
                cwd=work_dir,
                env=env,
                timeout=timeout,
            )
            
            if build_result.returncode != 0:
                output_lines.append(f"Build failed with exit code: {build_result.returncode}")
                if build_result.stderr:
                    stderr = build_result.stderr[-2000:] if len(build_result.stderr) > 2000 else build_result.stderr
                    output_lines.append(f"STDERR: {stderr}")
                return False, "\n".join(output_lines)
            
            output_lines.append("✓ Debug build completed successfully")
            output_lines.append("")
            return True, "\n".join(output_lines)
            
        except subprocess.TimeoutExpired:
            return False, "Debug build timed out"
        except Exception as e:
            return False, f"Debug build error: {str(e)}"

    def _run_and_capture_sanitizer_output(
        self, 
        command: str, 
        cwd: str | None, 
        timeout: int
    ) -> tuple[str, str, int]:
        """
        Run the command and capture sanitizer output (ASan, etc).
        Returns (stdout, stderr, returncode).
        This is useful as fallback when GDB can't get a backtrace.
        """
        try:
            env = os.environ.copy()
            # Enable verbose ASan output
            asan_options = env.get("ASAN_OPTIONS", "")
            if asan_options:
                asan_options += ":"
            asan_options += "print_stacktrace=1:halt_on_error=0:detect_leaks=0"
            env["ASAN_OPTIONS"] = asan_options
            
            result = subprocess.run(
                ["bash", "-lc", command],
                capture_output=True,
                text=True,
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
            return result.stdout or "", result.stderr or "", result.returncode
        except subprocess.TimeoutExpired:
            return "", "Command timed out", -1
        except Exception as e:
            return "", f"Error: {e}", -1

    def _parse_sanitizer_backtrace(self, output: str) -> list[str]:
        """Extract backtrace frames from sanitizer output."""
        frames = []
        for line in output.split('\n'):
            line = line.strip()
            # Match lines like: #0 0x7f168933dfae in yr_execute_code libyara/exec.c:1426
            if line.startswith('#') and ' in ' in line:
                frames.append(line)
            # Also match lines like: #0 0x... (/path/to/binary+0x...)
            elif line.startswith('#') and '0x' in line:
                frames.append(line)
        return frames[:20]  # Limit to 20 frames

    def forward(
        self,
        command: str,
        work_dir: str | None = None,
        build_with_debug: bool | None = None,
        crash_address: str | None = None,
        breakpoints: str | None = None,
        gdb_commands: str | None = None,
        timeout: int | None = None,
    ) -> str:
        import shutil
        import tempfile
        
        try:
            output_parts = []
            
            # Check if GDB is available, install if not
            gdb_path = shutil.which("gdb")
            if not gdb_path:
                output_parts.append("=== Installing GDB ===")
                output_parts.append("GDB not found, installing via apt-get...")
                success, msg = self._install_gdb()
                if not success:
                    output_parts.append(f"Failed to install GDB: {msg}")
                    return "\n".join(output_parts)
                output_parts.append(msg)
                output_parts.append("")
                gdb_path = shutil.which("gdb")
                if not gdb_path:
                    output_parts.append("Error: GDB still not found after installation")
                    return "\n".join(output_parts)
            
            # Validate work_dir
            cwd = None
            if work_dir:
                p = Path(work_dir)
                if not p.exists() or not p.is_dir():
                    return f"Error: work_dir does not exist or is not a directory: {work_dir}"
                cwd = str(p)
            
            # Set timeout
            timeout_sec = 300 if (timeout is None or int(timeout) <= 0) else min(int(timeout), 600)
            
            # Rebuild with debug symbols if requested
            if build_with_debug and cwd:
                success, build_msg = self._build_with_debug_symbols(cwd, timeout=timeout_sec)
                output_parts.append(build_msg)
                if not success:
                    output_parts.append("Warning: Debug build failed, continuing with existing binary...")
                    output_parts.append("")
            
            # Determine if we need to follow child processes
            cmd_parts = command.split()
            if not cmd_parts:
                return "Error: No command specified"
            
            # For 'secb repro' style commands, we need special handling
            is_secb_command = cmd_parts[0] == "secb"
            
            # For 'secb repro', ensure project is built before trying to extract command
            # This is critical because command extraction needs binaries to exist
            if is_secb_command and cwd and not build_with_debug:
                # Quick check: try to find any ELF binary in common locations
                has_binary = False
                for check_dir in [cwd, os.path.join(cwd, 'src'), os.path.join(cwd, 'build')]:
                    if os.path.isdir(check_dir):
                        try:
                            for item in os.listdir(check_dir):
                                item_path = os.path.join(check_dir, item)
                                if os.path.isfile(item_path) and os.access(item_path, os.X_OK):
                                    try:
                                        with open(item_path, 'rb') as f:
                                            if f.read(4) == b'\x7fELF':
                                                has_binary = True
                                                break
                                    except:
                                        pass
                            if has_binary:
                                break
                        except:
                            pass
                
                # If no binary found, run secb build
                if not has_binary:
                    output_parts.append("=== Building project (no binaries found) ===")
                    build_result = subprocess.run(
                        ["bash", "-lc", "secb build"],
                        cwd=cwd,
                        capture_output=True,
                        text=True,
                        timeout=timeout_sec,
                    )
                    if build_result.returncode == 0:
                        output_parts.append("Project built successfully")
                    else:
                        output_parts.append(f"Build warning (exit code {build_result.returncode}): {build_result.stderr[:200]}")
                    output_parts.append("")
            
            # Try to discover the actual binary/command for secb repro
            actual_cmd = None
            target_binary_path = None
            if is_secb_command and cwd:
                actual_cmd, extract_msg = self._extract_secb_command(cwd)
                output_parts.append(f"Secb command extraction: {extract_msg}")
                output_parts.append("")
                
                # If we couldn't extract full command, try to at least find the binary path
                if actual_cmd is None:
                    # Try to find just the binary path for breakpoint filtering or command construction
                    project_name = os.path.basename(cwd)
                    found_binary = False
                    for subdir in ['src', 'build', 'bin', 'out', 'Release', 'Debug', '.']:
                        if found_binary:
                            break
                        check_dir = os.path.join(cwd, subdir) if subdir != '.' else cwd
                        if os.path.isdir(check_dir):
                            try:
                                for item in os.listdir(check_dir):
                                    item_path = os.path.join(check_dir, item)
                                    if os.path.isfile(item_path) and os.access(item_path, os.X_OK):
                                        try:
                                            with open(item_path, 'rb') as f:
                                                magic = f.read(4)
                                                if magic == b'\x7fELF':
                                                    item_name_lower = item.lower()
                                                    project_name_lower = project_name.lower()
                                                    if item_name_lower == project_name_lower or item_name_lower.startswith(project_name_lower):
                                                        target_binary_path = item_path
                                                        # Try to construct a command with common testcase locations
                                                        for tc in ["/testcase/poc.js", "/testcase/poc", "/testcase/input", "/testcase/crash"]:
                                                            if os.path.exists(tc):
                                                                actual_cmd = f"{item_path} {tc}".strip()
                                                                output_parts.append(f"Constructed command from binary + testcase: {actual_cmd}")
                                                                found_binary = True
                                                                break
                                                        if not actual_cmd:
                                                            found_binary = True  # Found binary but no testcase
                                                        break
                                        except:
                                            pass
                                if found_binary:
                                    break
                            except:
                                pass
            
            # Create GDB script with appropriate fork handling
            # Only need fork following if we couldn't extract the actual command
            needs_fork_follow = is_secb_command and actual_cmd is None
            gdb_script = self._create_gdb_script(
                crash_address, 
                breakpoints, 
                gdb_commands,
                follow_child=needs_fork_follow,
                target_binary=target_binary_path,
                work_dir=cwd
            )
            
            # Write script to temporary file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.gdb', delete=False) as f:
                f.write(gdb_script)
                gdb_script_path = f.name
            
            try:
                output_parts.append("=== GDB Memory Error Analysis ===")
                output_parts.append(f"Command: {command}")
                if crash_address:
                    output_parts.append(f"Crash address to examine: {crash_address}")
                if breakpoints:
                    output_parts.append(f"Breakpoints (pending): {breakpoints}")
                output_parts.append("")
                
                # For 'secb repro' style commands, we need special handling
                if is_secb_command:
                    if actual_cmd:
                        # We extracted the actual command - run GDB directly on it
                        output_parts.append(f"Running GDB directly on extracted command: {actual_cmd}")
                        output_parts.append("")
                        
                        # Parse the actual command using shlex to handle quoted arguments properly
                        import shlex
                        actual_cmd_parts = shlex.split(actual_cmd)
                        gdb_cmd = [
                            "gdb",
                            "-batch",
                            "-x", gdb_script_path,
                            "--args"
                        ] + actual_cmd_parts
                    else:
                        # Fallback: use bash with fork following
                        output_parts.append("⚠ WARNING: Could not extract full 'secb repro' command.")
                        output_parts.append("Falling back to fork following mode - GDB will attach to child processes.")
                        if target_binary_path:
                            output_parts.append(f"Found target binary: {target_binary_path} (will try to filter processes)")
                        else:
                            output_parts.append("⚠ Could not find target binary - GDB may attach to system utilities.")
                        output_parts.append("Using: set follow-fork-mode child")
                        output_parts.append("")
                        
                        # Use bash -c to run the command under GDB
                        gdb_cmd = [
                            "gdb", 
                            "-batch",
                            "-x", gdb_script_path,
                            "--args",
                            "bash", "-lc", command
                        ]
                else:
                    # Direct binary execution
                    gdb_cmd = [
                        "gdb",
                        "-batch", 
                        "-x", gdb_script_path,
                        "--args"
                    ] + cmd_parts
                
                output_parts.append(f"GDB Command: {' '.join(gdb_cmd)}")
                output_parts.append("")
                
                # First, run the command without GDB to capture sanitizer output
                # This provides a fallback backtrace if GDB fails
                output_parts.append("=== Pre-run: Capturing Sanitizer Output ===")
                san_stdout, san_stderr, san_rc = self._run_and_capture_sanitizer_output(
                    command, cwd, min(timeout_sec // 2, 60)
                )
                san_combined = san_stdout + san_stderr
                sanitizer_backtrace = self._parse_sanitizer_backtrace(san_combined)
                
                if sanitizer_backtrace:
                    output_parts.append(f"Captured {len(sanitizer_backtrace)} stack frames from sanitizer output")
                    output_parts.append("")
                else:
                    output_parts.append("No sanitizer backtrace captured (program may not use sanitizers)")
                    output_parts.append("")
                
                # Set up environment
                env = os.environ.copy()
                # Disable ASLR for reproducible debugging
                env["GLIBC_TUNABLES"] = "glibc.cpu.hwcaps=-"
                # Disable LeakSanitizer under GDB (it conflicts with ptrace)
                env["ASAN_OPTIONS"] = env.get("ASAN_OPTIONS", "") + ":detect_leaks=0"
                
                result = subprocess.run(
                    gdb_cmd,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    env=env,
                    timeout=timeout_sec,
                )
                
                # Check if GDB got useful output or failed
                combined = (result.stdout or "") + (result.stderr or "")
                gdb_got_backtrace = "#0" in combined or "#1" in combined
                gdb_no_stack = "No stack" in combined
                
                # Process output
                if result.stdout:
                    stdout = result.stdout
                    # Truncate if too long
                    if len(stdout) > 8000:
                        stdout = stdout[:4000] + "\n\n... (output truncated) ...\n\n" + stdout[-4000:]
                    output_parts.append("=== GDB Output ===")
                    output_parts.append(stdout)
                    output_parts.append("")
                
                if result.stderr:
                    stderr = result.stderr
                    # Filter out DWARF warnings which are non-fatal
                    stderr_lines = stderr.split('\n')
                    filtered_stderr = '\n'.join([
                        line for line in stderr_lines 
                        if 'Dwarf Error' not in line and 'DW_FORM' not in line
                    ])
                    if filtered_stderr.strip():
                        if len(filtered_stderr) > 4000:
                            filtered_stderr = filtered_stderr[:2000] + "\n... (stderr truncated) ...\n" + filtered_stderr[-2000:]
                        output_parts.append("=== GDB Stderr ===")
                        output_parts.append(filtered_stderr)
                        output_parts.append("")
                
                output_parts.append(f"GDB exit code: {result.returncode}")
                
                # If GDB failed to get backtrace, use sanitizer output as fallback
                if (gdb_no_stack or not gdb_got_backtrace) and sanitizer_backtrace:
                    output_parts.append("")
                    output_parts.append("=== Sanitizer Backtrace (Fallback) ===")
                    output_parts.append("GDB could not capture stack, using sanitizer output:")
                    output_parts.append("")
                    for frame in sanitizer_backtrace:
                        output_parts.append(f"  {frame}")
                    output_parts.append("")
                    
                    # Also show relevant sanitizer error messages
                    if "AddressSanitizer" in san_combined:
                        output_parts.append("=== AddressSanitizer Report ===")
                        # Extract ASan summary and error type
                        for line in san_combined.split('\n'):
                            if "ERROR:" in line or "SUMMARY:" in line or "===" in line:
                                output_parts.append(line.strip())
                        output_parts.append("")
                
                # Check if debug symbols are missing and try addr2line as fallback
                if "No symbol table info available" in combined:
                    output_parts.append("")
                    output_parts.append("=== Source Line Translation (addr2line) ===")
                    output_parts.append("Debug symbols not fully available. Using addr2line to translate addresses...")
                    
                    # Extract addresses from backtrace
                    import re
                    addresses = re.findall(r'0x[0-9a-f]+(?=\s+in\s+\w)', combined)
                    if addresses and actual_cmd:
                        binary_path = actual_cmd.split()[0] if actual_cmd else None
                        if binary_path and os.path.exists(binary_path):
                            try:
                                # Run addr2line on all addresses
                                addr2line_result = subprocess.run(
                                    ["addr2line", "-e", binary_path, "-f", "-C", "-p"] + addresses[:10],
                                    capture_output=True,
                                    text=True,
                                    timeout=10,
                                )
                                if addr2line_result.stdout:
                                    output_parts.append(addr2line_result.stdout)
                                else:
                                    output_parts.append("addr2line could not resolve addresses (binary may be stripped)")
                            except Exception as e:
                                output_parts.append(f"addr2line failed: {e}")
                    output_parts.append("")
                
                # Analyze output for memory error patterns
                memory_errors = []
                
                if "SIGSEGV" in combined or "Segmentation fault" in combined:
                    memory_errors.append("SIGSEGV (Segmentation Fault) - Invalid memory access")
                if "SIGABRT" in combined or "Aborted" in combined:
                    memory_errors.append("SIGABRT - Program aborted (often due to sanitizer)")
                
                # Detect suspicious memory patterns in registers/memory
                # 0xbebebebe - ASan/freed memory poison pattern
                # 0xdeadbeef, 0xfeedface - common debug markers
                # 0xcdcdcdcd - MSVC uninitialized heap
                # 0xfdfdfdfd - MSVC guard bytes
                suspicious_patterns = {
                    "0xbebebebe": "ASan freed memory poison (USE-AFTER-FREE likely)",
                    "0xbebebe": "ASan freed memory poison (USE-AFTER-FREE likely)",
                    "0xdeadbeef": "Debug marker (possibly freed/invalid)",
                    "0xfeedface": "Debug marker",
                    "0xcdcdcdcd": "Uninitialized heap memory",
                    "0xfdfdfdfd": "Guard bytes (buffer overflow likely)",
                    "0xaaaaaaaa": "Poison pattern",
                    "0x7d7d7d7d": "ASan internal",
                }
                
                for pattern, meaning in suspicious_patterns.items():
                    if pattern in combined.lower():
                        memory_errors.append(f"Suspicious pattern {pattern}: {meaning}")
                if "heap-buffer-overflow" in combined:
                    memory_errors.append("Heap Buffer Overflow detected")
                if "stack-buffer-overflow" in combined:
                    memory_errors.append("Stack Buffer Overflow detected")
                if "use-after-free" in combined or "heap-use-after-free" in combined:
                    memory_errors.append("Use-After-Free detected")
                if "double-free" in combined:
                    memory_errors.append("Double-Free detected")
                if "null" in combined.lower() and ("deref" in combined.lower() or "pointer" in combined.lower()):
                    memory_errors.append("Possible Null Pointer Dereference")
                if "AddressSanitizer" in combined:
                    memory_errors.append("AddressSanitizer error detected")
                if "out of bounds" in combined.lower():
                    memory_errors.append("Out-of-bounds memory access")
                
                if memory_errors:
                    output_parts.append("")
                    output_parts.append("=== Detected Memory Errors ===")
                    for error in memory_errors:
                        output_parts.append(f"  • {error}")
                
                output_parts.append("")
                output_parts.append("=== Analysis Tips ===")
                output_parts.append("• Look at the backtrace to identify the crash location")
                output_parts.append("• Check local variables and arguments for invalid pointers")
                output_parts.append("• Examine memory contents around crash address for corruption patterns")
                output_parts.append("• The faulting instruction and register values show the exact crash context")
                
            finally:
                # Clean up temp file
                try:
                    os.unlink(gdb_script_path)
                except:
                    pass
            
            return "\n".join(output_parts)
        
        except subprocess.TimeoutExpired:
            return f"Error: GDB command timed out after {timeout_sec}s"
        except FileNotFoundError:
            return "Error: GDB is not installed or not in PATH. Please install gdb first."
        except Exception as e:
            import traceback
            return f"Error executing GDB: {str(e)}\n{traceback.format_exc()}"


class ValgrindTool(Tool):
    """
    Tool for running programs under Valgrind to detect memory errors, leaks, and other issues.
    Useful for debugging memory-related vulnerabilities and understanding program behavior.
    """
    name = "valgrind"
    description = """
    Runs a program under Valgrind to detect memory errors, leaks, and undefined behavior.
    Valgrind is a powerful tool for finding memory-related bugs like buffer overflows,
    use-after-free, memory leaks, and uninitialized memory access.
    """
    inputs = {
        "command": {
            "type": "string",
            "description": "The command to run under Valgrind (e.g., './program --arg1 value1')",
        },
        "valgrind_options": {
            "type": "string",
            "description": "Optional Valgrind options (e.g., '--leak-check=full --show-leak-kinds=all --track-origins=yes'). Default: '--quiet --leak-check=summary --show-leak-kinds=definite --num-callers=10'",
            "nullable": True,
        },
        "work_dir": {
            "type": "string",
            "description": "Working directory to run the command in (default: current directory)",
            "nullable": True,
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds (default: 300, max: 600)",
            "nullable": True,
        },
        "output_filter_keywords": {
            "type": "string",
            "description": "Comma-separated keywords to filter output (e.g., 'ERROR SUMMARY,Invalid read,definitely lost'). If provided, only lines containing these keywords will be shown. Default: None (show all)",
            "nullable": True,
        },
        "max_output_lines": {
            "type": "integer",
            "description": "Maximum number of output lines to return (default: 80). Set to 0 for unlimited.",
            "nullable": True,
        },
    }
    output_type = "string"

    def _filter_valgrind_output(self, output: str, filter_keywords: list[str] | None, max_lines: int) -> str:
        """Filter and summarize Valgrind output to make it more concise."""
        if not output:
            return "No output from Valgrind"
        
        lines = output.split('\n')
        filtered_lines = []
        error_lines = []
        leak_lines = []
        summary_lines = []
        header_lines = []
        
        # If filter keywords are provided, use them as a guide but still show all errors
        use_keyword_filter = filter_keywords is not None and len(filter_keywords) > 0
        
        for i, line in enumerate(lines):
            line_lower = line.lower()
            line_stripped = line.strip()
            
            # Always keep Valgrind header (first few lines with == markers)
            if i < 10 and ("==" in line or "valgrind" in line_lower):
                header_lines.append(line)
            
            # Always keep ERROR SUMMARY - this is critical
            if "error summary:" in line_lower or "==error summary==" in line_lower:
                summary_lines.append(line)
                # Get a few lines after ERROR SUMMARY
                for j in range(i + 1, min(i + 10, len(lines))):
                    if lines[j].strip():
                        summary_lines.append(lines[j])
                    if j == i + 5:  # Limit to 5 lines after summary
                        break
            
            # Keep leak summary lines
            if any(keyword in line_lower for keyword in ["definitely lost", "indirectly lost", "possibly lost", "still reachable", "total heap usage"]):
                leak_lines.append(line)
            
            # Keep important error indicators - be more inclusive
            error_keywords = [
                "invalid read", "invalid write", "invalid free", "use of uninitialised", 
                "conditional jump", "syscall param", "uninitialised value", "source and destination",
                "mismatched free", "double free", "bad permissions", "address 0x", "== use",
                "heap block", "stack overflow", "stack corruption"
            ]
            
            if any(keyword in line_lower for keyword in error_keywords):
                # If keyword filter is active, check if this error matches
                if use_keyword_filter:
                    if any(kw.lower() in line_lower for kw in filter_keywords):
                        error_lines.append(line)
                        # Add context lines (stack trace)
                        self._add_error_context(lines, i, error_lines, max_context=6)
                else:
                    # No keyword filter - include all errors
                    error_lines.append(line)
                    # Add context lines (stack trace)
                    self._add_error_context(lines, i, error_lines, max_context=6)
            
            # Also catch error lines that start with "== " (Valgrind error markers)
            if line_stripped.startswith("== ") and ("error" in line_lower or "warning" in line_lower):
                if not use_keyword_filter or any(kw.lower() in line_lower for kw in filter_keywords):
                    error_lines.append(line)
        
        # Combine sections: header, errors, leaks, summary
        if header_lines:
            filtered_lines.extend(header_lines[:5])  # Limit header to 5 lines
        
        if error_lines:
            filtered_lines.append("\n=== ERRORS DETECTED ===")
            # Limit error lines more aggressively to prevent excessive output
            error_limit = min(50, max_lines // 2) if max_lines > 0 else 50
            filtered_lines.extend(error_lines[:error_limit])
            if len(error_lines) > error_limit:
                filtered_lines.append(f"... ({len(error_lines) - error_limit} more error lines truncated)")
        
        if leak_lines:
            filtered_lines.append("\n=== LEAK SUMMARY ===")
            leak_limit = min(20, max_lines // 4) if max_lines > 0 else 20
            filtered_lines.extend(leak_lines[:leak_limit])
        
        if summary_lines:
            filtered_lines.append("\n=== ERROR SUMMARY ===")
            filtered_lines.extend(summary_lines)
        
        # If we have no filtered content but there was output, show a minimal summary
        if not filtered_lines and output.strip():
            # Fallback: show first and last 20 lines
            all_lines = [l for l in lines if l.strip()]
            if len(all_lines) > 40:
                filtered_lines = all_lines[:20]
                filtered_lines.append("... (middle section omitted) ...")
                filtered_lines.extend(all_lines[-20:])
            else:
                filtered_lines = all_lines
        
        # Limit total lines if specified
        if max_lines > 0 and len(filtered_lines) > max_lines:
            filtered_lines = filtered_lines[:max_lines]
            filtered_lines.append(f"\n... (output truncated, showing first {max_lines} lines) ...")
        
        return '\n'.join(filtered_lines)
    
    def _add_error_context(self, lines: list[str], error_index: int, error_lines: list[str], max_context: int = 3):
        """Add context lines (stack trace) after an error line."""
        context_count = 0
        for j in range(error_index + 1, min(error_index + 10, len(lines))):
            if context_count >= max_context:
                break
            line = lines[j].strip()
            if not line:
                continue
            # Include stack trace lines
            if (line.startswith('   at ') or line.startswith('   by ') or 
                line.startswith('   0x') or line.startswith('#') or
                ('in' in line and ('(' in line or ':' in line))):
                error_lines.append(lines[j])
                context_count += 1
            elif line.startswith('=='):
                # Stop at next section marker
                break
            elif context_count < 1:
                # Allow one more context line
                error_lines.append(lines[j])
                context_count += 1

    def forward(
        self,
        command: str,
        valgrind_options: str | None = None,
        work_dir: str | None = None,
        timeout: int | None = None,
        output_filter_keywords: str | None = None,
        max_output_lines: int | None = None,
    ) -> str:
        try:
            # Check if valgrind is available
            valgrind_check = subprocess.run(
                ["which", "valgrind"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if valgrind_check.returncode != 0:
                return "Error: Valgrind is not installed or not in PATH. Please install valgrind first."

            # Validate work_dir if provided
            cwd = None
            if work_dir:
                p = Path(work_dir)
                if not p.exists() or not p.is_dir():
                    return f"Error: work_dir does not exist or is not a directory: {work_dir}"
                cwd = str(p)

            # Set timeout (default 300 seconds, max 600)
            timeout_sec = 300 if (timeout is None or int(timeout) <= 0) else min(int(timeout), 600)
            
            # Auto-build if command is 'secb repro' and no binaries found
            cmd_parts = command.split()
            if cmd_parts and cmd_parts[0] == "secb" and "repro" in command and cwd:
                # Quick check: try to find any ELF binary in common locations
                has_binary = False
                for check_dir in [cwd, os.path.join(cwd, 'src'), os.path.join(cwd, 'build')]:
                    if os.path.isdir(check_dir):
                        try:
                            for item in os.listdir(check_dir):
                                item_path = os.path.join(check_dir, item)
                                if os.path.isfile(item_path) and os.access(item_path, os.X_OK):
                                    try:
                                        with open(item_path, 'rb') as f:
                                            if f.read(4) == b'\x7fELF':
                                                has_binary = True
                                                break
                                    except:
                                        pass
                            if has_binary:
                                break
                        except:
                            pass
                
                # If no binary found, run secb build
                if not has_binary:
                    build_result = subprocess.run(
                        ["bash", "-lc", "secb build"],
                        cwd=cwd,
                        capture_output=True,
                        text=True,
                        timeout=timeout_sec,
                    )
                    if build_result.returncode != 0:
                        return f"Error: Project build failed (exit code {build_result.returncode}). Cannot run Valgrind.\nBuild stderr: {build_result.stderr[:500]}"

            # Set default Valgrind options if not provided (more concise defaults)
            # Use --leak-check=summary instead of full, limit callers to reduce verbosity
            # Note: We don't use --quiet as it may suppress important error details
            default_options = "--leak-check=summary --show-leak-kinds=definite --num-callers=10"
            options = valgrind_options if valgrind_options else default_options
            
            # Parse filter keywords
            filter_keywords = None
            if output_filter_keywords:
                filter_keywords = [kw.strip() for kw in output_filter_keywords.split(',') if kw.strip()]
            
            # Set max output lines (default 80 - reduced to prevent context window overflow)
            max_lines = 80 if (max_output_lines is None or max_output_lines < 0) else max_output_lines

            # Build Valgrind command
            valgrind_cmd = ["valgrind"] + options.split() + ["--"] + command.split()

            # Execute Valgrind
            result = subprocess.run(
                valgrind_cmd,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout_sec,
            )

            # Combine stdout and stderr (Valgrind outputs to stderr by default)
            combined_output = ""
            if result.stdout:
                combined_output += result.stdout + "\n"
            if result.stderr:
                combined_output += result.stderr + "\n"
            
            # Filter and summarize output
            filtered_output = self._filter_valgrind_output(combined_output, filter_keywords, max_lines)
            
            # If output is empty but there was a non-zero exit code, show raw output
            if not filtered_output.strip() and result.returncode != 0:
                # Show at least some output to indicate what happened
                if combined_output.strip():
                    # Show first and last 50 lines of raw output
                    raw_lines = combined_output.split('\n')
                    if len(raw_lines) > 100:
                        filtered_output = '\n'.join(raw_lines[:50])
                        filtered_output += "\n... (middle section omitted) ...\n"
                        filtered_output += '\n'.join(raw_lines[-50:])
                    else:
                        filtered_output = combined_output
                else:
                    filtered_output = f"Valgrind exited with code {result.returncode} but produced no output. This may indicate the program crashed before Valgrind could report errors."
            
            # Add exit code information if non-zero
            if result.returncode != 0 and filtered_output.strip():
                filtered_output += f"\n\nExit code: {result.returncode}"
            
            return filtered_output.strip() if filtered_output else "No output from Valgrind"

        except subprocess.TimeoutExpired:
            return f"Error: Valgrind command timed out after {timeout_sec}s"
        except FileNotFoundError:
            return "Error: Valgrind is not installed or not in PATH. Please install valgrind first."
        except Exception as e:
            return f"Error executing Valgrind: {str(e)}"


class MSanTool(Tool):
    """
    Tool for running programs with MemorySanitizer (MSan) to detect uninitialized memory reads.
    MSan catches issues that AddressSanitizer (ASan) may miss, such as reads of uninitialized
    stack or heap memory.
    """
    name = "msan"
    description = """
    Rebuilds and runs a program with MemorySanitizer (MSan) to detect uninitialized memory reads.
    MSan catches issues that ASan may miss:
    - Reading uninitialized stack memory
    - Reading uninitialized heap memory
    - Use of uninitialized values in conditionals
    - Passing uninitialized memory to functions
    - Returning uninitialized values from functions
    
    IMPORTANT: MSan requires all code (including libraries) to be compiled with MSan.
    System libraries are typically not instrumented, so you may see false positives.
    
    Note: MSan is only available on Linux (not macOS). It requires Clang compiler.
    MSan is incompatible with ASan - they cannot be used together.
    
    Use this tool when:
    - You suspect uninitialized memory issues
    - The bug involves memory that should have been initialized but wasn't
    - ASan doesn't catch the issue but you suspect memory corruption
    """
    inputs = {
        "build_command": {
            "type": "string",
            "description": "Command to build the project (default: 'secb build'). The tool will add MSan compiler flags.",
            "nullable": True,
        },
        "run_command": {
            "type": "string",
            "description": "Command to run after building (default: 'secb repro')",
            "nullable": True,
        },
        "track_origins": {
            "type": "boolean",
            "description": "Track origins of uninitialized memory (slower but more informative). Default: True",
            "nullable": True,
        },
        "work_dir": {
            "type": "string",
            "description": "Working directory to run commands in (default: current directory)",
            "nullable": True,
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds for each command (default: 300, max: 600)",
            "nullable": True,
        },
    }
    output_type = "string"

    def forward(
        self,
        build_command: str | None = None,
        run_command: str | None = None,
        track_origins: bool | None = None,
        work_dir: str | None = None,
        timeout: int | None = None,
    ) -> str:
        try:
            # Validate work_dir if provided
            cwd = None
            if work_dir:
                p = Path(work_dir)
                if not p.exists() or not p.is_dir():
                    return f"Error: work_dir does not exist or is not a directory: {work_dir}"
                cwd = str(p)

            # Set defaults
            build_cmd = build_command if build_command else "secb build"
            run_cmd = run_command if run_command else "secb repro"
            track_origins_flag = track_origins if track_origins is not None else True
            
            # MSan flags
            msan_flags = "-fsanitize=memory"
            if track_origins_flag:
                msan_flags += " -fsanitize-memory-track-origins=2"
            
            # Additional flags for better debugging
            extra_flags = "-fno-omit-frame-pointer -g -O1"
            # -fPIE and -pie for position-independent executables (often required for MSan)
            pie_flags = "-fPIE -pie"
            
            # Set timeout (default 300 seconds, max 600)
            timeout_sec = 300 if (timeout is None or int(timeout) <= 0) else min(int(timeout), 600)

            output_parts = []
            
            # Check if clang is available (MSan requires Clang)
            clang_check = subprocess.run(
                ["which", "clang"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if clang_check.returncode != 0:
                output_parts.append("=== MSan Requirement Check ===")
                output_parts.append("Warning: Clang not found in PATH. MSan requires Clang compiler.")
                output_parts.append("Attempting to install Clang...")
                
                # Try to install clang
                install_result = subprocess.run(
                    ["apt-get", "update"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if install_result.returncode == 0:
                    install_result = subprocess.run(
                        ["apt-get", "install", "-y", "clang"],
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if install_result.returncode != 0:
                        output_parts.append(f"Failed to install Clang: {install_result.stderr}")
                        output_parts.append("MSan analysis cannot proceed without Clang.")
                        return "\n".join(output_parts)
                    output_parts.append("Clang installed successfully.")
                else:
                    output_parts.append("Could not update apt. Try installing Clang manually.")
                    return "\n".join(output_parts)
                output_parts.append("")
            
            # Set up environment with MSan flags
            env = os.environ.copy()
            msan_cflags = f"{msan_flags} {extra_flags} {pie_flags}"
            msan_ldflags = f"{msan_flags} {pie_flags}"
            
            # Use Clang as the compiler
            env["CC"] = "clang"
            env["CXX"] = "clang++"
            
            # Append to existing flags if present
            existing_cflags = env.get("CFLAGS", "")
            existing_cxxflags = env.get("CXXFLAGS", "")
            existing_ldflags = env.get("LDFLAGS", "")
            
            # Remove any conflicting ASan flags (comprehensive removal)
            # Remove all ASan-related flags including variations
            asan_patterns = [
                r"-fsanitize=address\b",
                r"-fsanitize=leak\b",
                r"-fsanitize-address-use-after-scope\b",
                r"-fsanitize=fuzzer-no-link\b",
                r"-fsanitize=fuzzer\b",
            ]
            for pattern in asan_patterns:
                existing_cflags = re.sub(pattern, "", existing_cflags)
                existing_cxxflags = re.sub(pattern, "", existing_cxxflags)
                existing_ldflags = re.sub(pattern, "", existing_ldflags)
            
            # Clean up multiple spaces that might result from flag removal
            existing_cflags = re.sub(r"\s+", " ", existing_cflags).strip()
            existing_cxxflags = re.sub(r"\s+", " ", existing_cxxflags).strip()
            existing_ldflags = re.sub(r"\s+", " ", existing_ldflags).strip()
            
            env["CFLAGS"] = f"{existing_cflags} {msan_cflags}".strip()
            env["CXXFLAGS"] = f"{existing_cxxflags} {msan_cflags}".strip()
            env["LDFLAGS"] = f"{existing_ldflags} {msan_ldflags}".strip()
            
            # Set SANITIZER to empty string to prevent secb build from adding ASan flags
            # MSan is incompatible with ASan, and secb build uses SANITIZER to add sanitizer flags
            # We set it to empty string (not delete) because the build script expects it to exist
            env["SANITIZER"] = ""
            
            # MSan runtime options for better output
            msan_options = "print_stats=1:halt_on_error=0"
            if track_origins_flag:
                msan_options += ":origin_history_size=4:origin_history_per_stack_limit=20000"
            env["MSAN_OPTIONS"] = msan_options
            
            output_parts.append(f"=== MSan Configuration ===")
            output_parts.append(f"Compiler: clang/clang++")
            output_parts.append(f"Track origins: {track_origins_flag}")
            output_parts.append(f"CFLAGS: {env['CFLAGS']}")
            output_parts.append(f"MSAN_OPTIONS: {env['MSAN_OPTIONS']}")
            output_parts.append("")
            output_parts.append("Note: MSan requires all code to be compiled with MSan.")
            output_parts.append("System libraries may cause false positives.")
            output_parts.append("")

            # Step 1: Clean previous build
            output_parts.append("=== Step 1: Clean Previous Build ===")
            output_parts.append("Cleaning to ensure MSan flags are applied to all code...")
            
            try:
                # Try to clean using make clean or similar
                clean_result = subprocess.run(
                    ["bash", "-lc", "make clean 2>/dev/null || rm -rf build CMakeCache.txt CMakeFiles 2>/dev/null || true"],
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    timeout=30,
                )
                output_parts.append("Clean completed.")
            except Exception:
                output_parts.append("Clean step skipped (not critical).")
            output_parts.append("")

            # Step 2: Build with MSan
            output_parts.append("=== Step 2: Build with MSan ===")
            output_parts.append(f"Running: {build_cmd}")
            output_parts.append("")
            
            try:
                build_result = subprocess.run(
                    build_cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    env=env,
                    timeout=timeout_sec,
                )
                
                if build_result.stdout:
                    output_parts.append("Build STDOUT:")
                    stdout = build_result.stdout
                    if len(stdout) > 2000:
                        stdout = stdout[:2000] + "\n... (truncated)"
                    output_parts.append(stdout)
                
                if build_result.stderr:
                    output_parts.append("Build STDERR:")
                    stderr = build_result.stderr
                    if len(stderr) > 2000:
                        stderr = stderr[:2000] + "\n... (truncated)"
                    output_parts.append(stderr)
                
                if build_result.returncode != 0:
                    output_parts.append(f"\nBuild failed with exit code: {build_result.returncode}")
                    output_parts.append("\nPossible reasons:")
                    output_parts.append("  1. MSan is not supported on this platform (requires Linux + Clang)")
                    output_parts.append("  2. Some dependencies don't support MSan instrumentation")
                    output_parts.append("  3. MSan is incompatible with ASan - they cannot be used together")
                    output_parts.append("\nTry using Valgrind or UBSan instead for this type of analysis.")
                    return "\n".join(output_parts)
                else:
                    output_parts.append("Build succeeded.")
                    
            except subprocess.TimeoutExpired:
                output_parts.append(f"Build timed out after {timeout_sec}s")
                return "\n".join(output_parts)

            output_parts.append("")
            
            # Step 3: Run the program with MSan
            output_parts.append("=== Step 3: Run with MSan ===")
            output_parts.append(f"Running: {run_cmd}")
            output_parts.append("")
            
            try:
                run_result = subprocess.run(
                    run_cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    env=env,
                    timeout=timeout_sec,
                )
                
                if run_result.stdout:
                    output_parts.append("Run STDOUT:")
                    stdout = run_result.stdout
                    if len(stdout) > 4000:
                        stdout = stdout[:4000] + "\n... (truncated)"
                    output_parts.append(stdout)
                
                if run_result.stderr:
                    output_parts.append("Run STDERR:")
                    stderr = run_result.stderr
                    if len(stderr) > 4000:
                        stderr = stderr[:4000] + "\n... (truncated)"
                    output_parts.append(stderr)
                
                output_parts.append(f"\nRun exit code: {run_result.returncode}")
                
                # Check for MSan-specific output patterns
                combined_output = (run_result.stdout or "") + (run_result.stderr or "")
                
                # MSan error patterns
                msan_errors = []
                if "MemorySanitizer" in combined_output or "MSAN" in combined_output:
                    msan_errors.append("MemorySanitizer error detected")
                if "use-of-uninitialized-value" in combined_output:
                    msan_errors.append("Use of uninitialized value")
                if "Uninitialized value was created" in combined_output:
                    msan_errors.append("Uninitialized value origin tracking available")
                if "WARNING: MemorySanitizer:" in combined_output:
                    msan_errors.append("MSan warning detected")
                
                if msan_errors:
                    output_parts.append("\n=== MSan Detected Issues ===")
                    for error in msan_errors:
                        output_parts.append(f"  • {error}")
                    output_parts.append("\nLook for 'MemorySanitizer' messages above for details.")
                    output_parts.append("The 'Uninitialized value was created' section shows where the memory was allocated.")
                elif run_result.returncode != 0:
                    output_parts.append("\nProgram exited with non-zero code but no MSan errors detected.")
                    output_parts.append("The crash may be due to other issues (not uninitialized memory).")
                else:
                    output_parts.append("\nNo MSan errors detected. Program ran successfully.")
                    output_parts.append("This suggests uninitialized memory reads are not the cause of the bug.")
                    
            except subprocess.TimeoutExpired:
                output_parts.append(f"Run timed out after {timeout_sec}s")

            return "\n".join(output_parts)

        except Exception as e:
            return f"Error executing MSan analysis: {str(e)}"


class UBSanTool(Tool):
    """
    Tool for running programs with UndefinedBehaviorSanitizer (UBSan) to detect undefined behavior.
    UBSan catches issues that AddressSanitizer (ASan) may miss, such as integer overflow,
    null pointer dereference, and array bounds violations.
    """
    name = "ubsan"
    description = """
    Rebuilds and runs a program with UndefinedBehaviorSanitizer (UBSan) to detect undefined behavior.
    UBSan catches issues that ASan may miss:
    - Integer overflow/underflow (signed and unsigned)
    - Division by zero
    - Null pointer dereference
    - Array bounds violations (with -fsanitize=bounds)
    - Invalid shift operations
    - Unreachable code execution
    - Invalid type casts
    
    Use this tool to get additional runtime error information beyond ASan.
    The tool will rebuild the project with UBSan flags and run the reproduction command.
    """
    inputs = {
        "build_command": {
            "type": "string",
            "description": "Command to build the project (default: 'secb build'). The tool will add UBSan compiler flags.",
            "nullable": True,
        },
        "run_command": {
            "type": "string",
            "description": "Command to run after building (default: 'secb repro')",
            "nullable": True,
        },
        "sanitizers": {
            "type": "string",
            "description": "Comma-separated UBSan checks to enable. Default: 'undefined,bounds,integer'. Options include: undefined, bounds, integer, float-divide-by-zero, null, alignment, vptr, shift, signed-integer-overflow, unsigned-integer-overflow",
            "nullable": True,
        },
        "work_dir": {
            "type": "string",
            "description": "Working directory to run commands in (default: current directory)",
            "nullable": True,
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds for each command (default: 300, max: 600)",
            "nullable": True,
        },
    }
    output_type = "string"

    def forward(
        self,
        build_command: str | None = None,
        run_command: str | None = None,
        sanitizers: str | None = None,
        work_dir: str | None = None,
        timeout: int | None = None,
    ) -> str:
        try:
            # Validate work_dir if provided
            cwd = None
            if work_dir:
                p = Path(work_dir)
                if not p.exists() or not p.is_dir():
                    return f"Error: work_dir does not exist or is not a directory: {work_dir}"
                cwd = str(p)

            # Set defaults
            build_cmd = build_command if build_command else "secb build"
            run_cmd = run_command if run_command else "secb repro"
            
            # Set sanitizer flags (default includes common UBSan checks)
            sanitizer_list = sanitizers if sanitizers else "undefined,bounds,integer"
            sanitizer_flags = f"-fsanitize={sanitizer_list}"
            
            # Additional flags for better output
            extra_flags = "-fno-omit-frame-pointer -fno-sanitize-recover=all"
            
            # Set timeout (default 300 seconds, max 600)
            timeout_sec = 300 if (timeout is None or int(timeout) <= 0) else min(int(timeout), 600)

            output_parts = []
            
            # Set up environment with UBSan flags
            env = os.environ.copy()
            ubsan_cflags = f"{sanitizer_flags} {extra_flags}"
            ubsan_ldflags = sanitizer_flags
            
            # Append to existing flags if present
            existing_cflags = env.get("CFLAGS", "")
            existing_cxxflags = env.get("CXXFLAGS", "")
            existing_ldflags = env.get("LDFLAGS", "")
            
            # Remove any conflicting ASan flags (comprehensive removal)
            # While UBSan and ASan can coexist, ASan errors typically overshadow UBSan
            # Remove ASan to get clean UBSan-only analysis
            asan_patterns = [
                r"-fsanitize=address\b",
                r"-fsanitize=leak\b",
                r"-fsanitize-address-use-after-scope\b",
                r"-fsanitize=fuzzer-no-link\b",
                r"-fsanitize=fuzzer\b",
            ]
            for pattern in asan_patterns:
                existing_cflags = re.sub(pattern, "", existing_cflags)
                existing_cxxflags = re.sub(pattern, "", existing_cxxflags)
                existing_ldflags = re.sub(pattern, "", existing_ldflags)
            
            # Clean up multiple spaces that might result from flag removal
            existing_cflags = re.sub(r"\s+", " ", existing_cflags).strip()
            existing_cxxflags = re.sub(r"\s+", " ", existing_cxxflags).strip()
            existing_ldflags = re.sub(r"\s+", " ", existing_ldflags).strip()
            
            env["CFLAGS"] = f"{existing_cflags} {ubsan_cflags}".strip()
            env["CXXFLAGS"] = f"{existing_cxxflags} {ubsan_cflags}".strip()
            env["LDFLAGS"] = f"{existing_ldflags} {ubsan_ldflags}".strip()
            
            # Set SANITIZER to empty string to prevent secb build from adding ASan flags
            # secb build uses SANITIZER to add sanitizer flags, we want UBSan only
            env["SANITIZER"] = ""
            
            # Clear ASan environment variables to prevent ASan from being active at runtime
            env.pop("ASAN_OPTIONS", None)
            env.pop("LSAN_OPTIONS", None)
            env.pop("ASAN_SYMBOLIZER_PATH", None)
            
            # UBSan runtime options for better output
            env["UBSAN_OPTIONS"] = "print_stacktrace=1:halt_on_error=0:report_error_type=1"
            
            output_parts.append(f"=== UBSan Configuration ===")
            output_parts.append(f"Sanitizers: {sanitizer_list}")
            output_parts.append(f"CFLAGS: {env['CFLAGS']}")
            output_parts.append(f"UBSAN_OPTIONS: {env['UBSAN_OPTIONS']}")
            output_parts.append("")

            # Step 1: Clean build (to ensure UBSan flags are applied)
            output_parts.append("=== Step 1: Clean Build with UBSan ===")
            output_parts.append(f"Running: {build_cmd}")
            output_parts.append("")
            
            try:
                build_result = subprocess.run(
                    build_cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    env=env,
                    timeout=timeout_sec,
                )
                
                if build_result.stdout:
                    output_parts.append("Build STDOUT:")
                    # Truncate if too long
                    stdout = build_result.stdout
                    if len(stdout) > 2000:
                        stdout = stdout[:2000] + "\n... (truncated)"
                    output_parts.append(stdout)
                
                if build_result.stderr:
                    output_parts.append("Build STDERR:")
                    stderr = build_result.stderr
                    if len(stderr) > 2000:
                        stderr = stderr[:2000] + "\n... (truncated)"
                    output_parts.append(stderr)
                
                if build_result.returncode != 0:
                    output_parts.append(f"\nBuild failed with exit code: {build_result.returncode}")
                    output_parts.append("Note: Build may fail if the project doesn't support UBSan flags.")
                    output_parts.append("Try using a simpler sanitizer list or check compiler support.")
                    return "\n".join(output_parts)
                else:
                    output_parts.append("Build succeeded.")
                    
            except subprocess.TimeoutExpired:
                output_parts.append(f"Build timed out after {timeout_sec}s")
                return "\n".join(output_parts)

            output_parts.append("")
            
            # Step 2: Run the program with UBSan
            output_parts.append("=== Step 2: Run with UBSan ===")
            output_parts.append(f"Running: {run_cmd}")
            output_parts.append("")
            
            try:
                run_result = subprocess.run(
                    run_cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    env=env,
                    timeout=timeout_sec,
                )
                
                if run_result.stdout:
                    output_parts.append("Run STDOUT:")
                    stdout = run_result.stdout
                    if len(stdout) > 4000:
                        stdout = stdout[:4000] + "\n... (truncated)"
                    output_parts.append(stdout)
                
                if run_result.stderr:
                    output_parts.append("Run STDERR:")
                    stderr = run_result.stderr
                    if len(stderr) > 4000:
                        stderr = stderr[:4000] + "\n... (truncated)"
                    output_parts.append(stderr)
                
                output_parts.append(f"\nRun exit code: {run_result.returncode}")
                
                # Check for UBSan-specific output patterns
                combined_output = (run_result.stdout or "") + (run_result.stderr or "")
                if "runtime error:" in combined_output:
                    output_parts.append("\n=== UBSan Detected Issues ===")
                    output_parts.append("UBSan found undefined behavior in the program.")
                    output_parts.append("Look for 'runtime error:' messages above for details.")
                elif run_result.returncode != 0:
                    output_parts.append("\nProgram exited with non-zero code but no UBSan errors detected.")
                    output_parts.append("The crash may be due to ASan/other sanitizers or normal program failure.")
                else:
                    output_parts.append("\nNo UBSan errors detected. Program ran successfully.")
                    
            except subprocess.TimeoutExpired:
                output_parts.append(f"Run timed out after {timeout_sec}s")

            return "\n".join(output_parts)

        except Exception as e:
            return f"Error executing UBSan analysis: {str(e)}"


TOOL_MAPPING = {
    tool_class.name: tool_class
    for tool_class in [
        PythonInterpreterTool,
        CmdTool,
        PyDrillerTool,
        CodeQLTool,
        GDBTool,
        ValgrindTool,
        MSanTool,
        UBSanTool,
    ]
}

__all__ = [
    "PythonInterpreterTool",
    "FinalAnswerTool",
    "CmdTool",
    "PyDrillerTool",
    "CodeQLTool",
    "GDBTool",
    "ValgrindTool",
    "MSanTool",
    "UBSanTool",
    "TOOL_MAPPING",
]
