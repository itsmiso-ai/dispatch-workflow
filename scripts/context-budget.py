#!/usr/bin/env python3
"""
OpenClaw Context Budget Audit

Audits token overhead across OpenClaw components and surfaces actionable optimizations.
Run manually or as part of healthcheck.

Usage:
    python3 scripts/context-budget.py [--verbose]
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


WORKSPACE = Path.home() / ".openclaw" / "workspace-saffron"
SKILLS_DIR = Path("/app/skills")
OPENCLAW_EXTENSIONS = Path.home() / ".openclaw" / "extensions"
OPENCLAW_CONFIG = Path.home() / ".openclaw"


def count_words(text: str) -> int:
    words = re.findall(r'\b\w+\b', text)
    return len(words)


def count_code_tokens(text: str) -> int:
    return len(text) // 4


def estimate_skills_tokens(skills_dir: Path, verbose: bool) -> tuple[int, list[dict]]:
    total = 0
    findings = []
    
    if not skills_dir.exists():
        return 0, findings
    
    skill_files = list(skills_dir.glob("*/SKILL.md"))
    count = len(skill_files)
    
    for sf in skill_files:
        try:
            content = sf.read_text()
            lines = content.split("\n")
            line_count = len(lines)
            word_count = count_words(content)
            tokens = word_count * 13 // 10  # words × 1.3
            
            if line_count > 400:
                findings.append({
                    "path": str(sf.relative_to(skills_dir.parent)),
                    "issue": f"heavy skill ({line_count} lines > 400)",
                    "tokens": tokens,
                })
            
            total += tokens
        except Exception:
            continue
    
    return total, findings


def estimate_workspace_tokens(workspace: Path, verbose: bool) -> tuple[int, list[dict]]:
    total = 0
    findings = []
    combined_lines = 0
    
    ws_files = [
        workspace / "AGENTS.md",
        workspace / "SOUL.md",
        workspace / "USER.md",
        workspace / "IDENTITY.md",
        workspace / "TOOLS.md",
        workspace / "MEMORY.md",
        workspace / "HEARTBEAT.md",
    ]
    
    for wf in ws_files:
        if wf.exists():
            content = wf.read_text()
            lines = content.split("\n")
            line_count = len(lines)
            combined_lines += line_count
            word_count = count_words(content)
            tokens = word_count * 13 // 10
            
            if verbose:
                print(f"  {wf.name}: {line_count} lines, ~{tokens} tokens")
            
            total += tokens
    
    if combined_lines > 300:
        findings.append({
            "path": "workspace files (combined)",
            "issue": f"bloat ({combined_lines} lines > 300 threshold)",
            "tokens": 0,
        })
    
    return total, findings


def estimate_hooks_tokens(openclaw_ext: Path, verbose: bool) -> tuple[int, list[dict]]:
    total = 0
    findings = []
    
    if not openclaw_ext.exists():
        return 0, findings
    
    hook_files = list(openclaw_ext.glob("**/hooks/**/*.js")) + \
                 list(openclaw_ext.glob("**/hooks/**/*.sh"))
    
    for hf in hook_files:
        try:
            content = hf.read_text()
            tokens = count_code_tokens(content)
            lines = len(content.split("\n"))
            
            if lines > 100:
                findings.append({
                    "path": str(hf.relative_to(openclaw_ext)),
                    "issue": f"heavy hook ({lines} lines > 100)",
                    "tokens": tokens,
                })
            
            total += tokens
        except Exception:
            continue
    
    return total, findings


def estimate_mcp_tokens(verbose: bool) -> tuple[int, list[dict]]:
    """Estimate MCP tool token overhead from configured MCP servers."""
    total = 0
    findings = []
    
    mcp_configs = [
        Path.home() / ".openclaw" / ".mcp.json",
        Path.home() / ".mcp.json",
        Path("/app/.mcp.json"),
    ]
    
    servers = 0
    total_tools = 0
    
    for mcp in mcp_configs:
        if mcp.exists():
            try:
                data = json.loads(mcp.read_text())
                if isinstance(data, dict):
                    for server_name, server_config in data.items():
                        if isinstance(server_config, dict) and "tools" in server_config:
                            tool_count = len(server_config.get("tools", []))
                        elif isinstance(server_config, dict) and "command" in server_config:
                            tool_count = 5  # estimate CLI-wrapper servers at 5 tools
                        else:
                            tool_count = 10  # default estimate
                        
                        servers += 1
                        total_tools += tool_count
            except Exception:
                continue
    
    # MCP schema overhead: ~500 tokens per tool
    total = total_tools * 500
    
    if verbose:
        print(f"  MCP: {servers} servers, ~{total_tools} tools, ~{total} tokens (est)")
    
    if servers > 10:
        findings.append({
            "path": ".mcp.json",
            "issue": f"over-provisioned ({servers} servers > 10 threshold)",
            "tokens": total,
        })
    
    return total, findings


def estimate_repo_tokens(verbose: bool) -> tuple[int, list[dict]]:
    """Check active git repos for CLAUDE.md overhead."""
    total = 0
    findings = []
    
    git_dirs = [
        Path("/data/git"),
        Path.home() / "GitHub",
        Path.home() / "github",
    ]
    
    for git_dir in git_dirs:
        if not git_dir.exists():
            continue
        
        for repo in git_dir.iterdir():
            if not repo.is_dir():
                continue
            claude_md = repo / "CLAUDE.md"
            if claude_md.exists():
                try:
                    content = claude_md.read_text()
                    lines = len(content.split("\n"))
                    word_count = count_words(content)
                    tokens = word_count * 13 // 10
                    total += tokens
                    
                    if verbose:
                        print(f"  {repo.name}/CLAUDE.md: {lines} lines, ~{tokens} tokens")
                    
                    if lines > 200:
                        findings.append({
                            "path": f"{repo.name}/CLAUDE.md",
                            "issue": f"heavy CLAUDE.md ({lines} lines > 200)",
                            "tokens": tokens,
                        })
                except Exception:
                    continue
    
    return total, findings


def build_report(verbose: bool = False) -> dict[str, Any]:
    skills_tokens, skills_findings = estimate_skills_tokens(SKILLS_DIR, verbose)
    ws_tokens, ws_findings = estimate_workspace_tokens(WORKSPACE, verbose)
    hooks_tokens, hooks_findings = estimate_hooks_tokens(OPENCLAW_EXTENSIONS, verbose)
    mcp_tokens, mcp_findings = estimate_mcp_tokens(verbose)
    repo_tokens, repo_findings = estimate_repo_tokens(verbose)
    
    total = skills_tokens + ws_tokens + hooks_tokens + mcp_tokens + repo_tokens
    
    # Context model: MiniMax-M2.7 has ~200K context
    context_window = 180_000
    available = max(0, context_window - total)
    pct_used = (total / context_window) * 100
    
    all_findings = sorted(
        skills_findings + ws_findings + hooks_findings + mcp_findings + repo_findings,
        key=lambda f: f["tokens"],
        reverse=True,
    )
    
    # Estimate savings from findings
    savings_by_path: dict[str, int] = {}
    for f in all_findings:
        savings_by_path[f["path"]] = savings_by_path.get(f["path"], 0) + f["tokens"]
    
    # Top 3 by token impact
    top_actions = []
    for path, tokens in sorted(savings_by_path.items(), key=lambda x: x[1], reverse=True)[:3]:
        finding = next((f for f in all_findings if f["path"] == path), None)
        if finding:
            top_actions.append({
                "action": f"review {path}",
                "issue": finding["issue"],
                "tokens": tokens,
            })
    
    return {
        "total_tokens": total,
        "context_window": context_window,
        "available_tokens": available,
        "pct_used": round(pct_used, 1),
        "components": {
            "workspace_files": ws_tokens,
            "skills": skills_tokens,
            "hooks": hooks_tokens,
            "mcp_tools": mcp_tokens,
            "repo_claude_md": repo_tokens,
        },
        "findings_count": len(all_findings),
        "top_actions": top_actions,
        "savings_estimate": sum(t for t in savings_by_path.values()),
    }


def print_report(r: dict[str, Any], verbose: bool = False) -> None:
    print("")
    print("OpenClaw Context Budget Report")
    print("═══════════════════════════════════════")
    print("")
    print(f"Total estimated overhead: ~{r['total_tokens']:,} tokens")
    print(f"Context model: MiniMax-M2.7 (~200K window)")
    print(f"Effective available context: ~{r['available_tokens']:,} tokens ({r['pct_used']}% used)")
    print("")
    print("Component Breakdown:")
    print("┌─────────────────┬───────────┐")
    print("│ Component       │ Est Tokens │")
    print("├─────────────────┼───────────┤")
    for comp, tokens in r["components"].items():
        print(f"│ {comp:<15} │ {tokens:>10,} │")
    print("└─────────────────┴───────────┘")
    print("")
    
    if r["findings_count"] > 0:
        print(f"WARNING: Issues Found ({r['findings_count']}):")
        for i, action in enumerate(r["top_actions"], 1):
            print(f"  {i}. [{action['issue']}] {action['action']} → ~{action['tokens']:,} tokens")
        print("")
        print(f"Potential savings: ~{r['savings_estimate']:,} tokens")
    else:
        print("No issues found. Context budget is healthy.")


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenClaw Context Budget Audit")
    parser.add_argument("--verbose", action="store_true", help="Show per-file breakdown")
    args = parser.parse_args()
    
    try:
        report = build_report(verbose=args.verbose)
        print_report(report, verbose=args.verbose)
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())