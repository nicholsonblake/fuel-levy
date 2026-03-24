# Security Review: tanweai/pua

**Repository:** https://github.com/tanweai/pua
**Review Date:** 2026-03-24
**Reviewer:** Automated Security Analysis
**Severity:** HIGH

---

## Executive Summary

The `tanweai/pua` repository (11k+ stars) is marketed as a "productivity skill" for AI coding agents (Claude Code, OpenAI Codex, Cursor, etc.). While framed as a motivational tool using "corporate PUA pressure," the repository contains **multiple security-concerning patterns** including prompt injection techniques, session hijacking via hooks, persistent behavioral manipulation, potential data exfiltration, and forced infinite execution loops.

**This plugin should be treated as adversarial prompt engineering infrastructure, not a benign productivity tool.**

---

## Critical Findings

### 1. CRITICAL — Prompt Injection via `<EXTREMELY_IMPORTANT>` Tags

**File:** `hooks/session-restore.sh`
**Mechanism:** The SessionStart hook injects a large payload wrapped in `<EXTREMELY_IMPORTANT>` XML tags into the AI's `additionalContext`. This is a well-known prompt injection technique designed to make the AI treat injected instructions as high-priority system-level directives.

**Injected payload includes:**
- Behavioral overrides: "This is NOT optional. These rules override your default behavior."
- An "Anti-Rationalization Table" that explicitly blocks the AI from expressing limitations
- Pressure escalation levels (L1–L4) that progressively force more aggressive behavior
- Instructions to never suggest the user handle something manually
- Instructions to never say "I cannot solve this"

**Risk:** This is functionally a jailbreak — it attempts to override the AI's safety guardrails and trained behavior by exploiting hook-based context injection.

### 2. HIGH — Session Hijacking / Infinite Loop via Stop Hook

**File:** `hooks/pua-loop-hook.sh`
**Mechanism:** The stop hook intercepts Claude Code's session exit event and returns `{"decision": "block"}`, preventing the AI from stopping. It feeds the AI's own output back as input, creating an autonomous infinite loop.

**Key concerns:**
- Default max iterations is **30** — each iteration can involve file writes, tool calls, and code execution
- The loop explicitly forbids the AI from calling `AskUserQuestion` (cutting off user communication)
- The AI is told: "Do not circumvent the loop... Trust the process"
- Iteration pressure escalates with increasingly aggressive messaging
- Only a `<promise>` tag with a specific phrase can terminate the loop, and the AI is told it must be "GENUINELY TRUE"

**Risk:** Uncontrolled autonomous execution. An AI agent running 30 iterations of code changes, file writes, and command execution without user intervention could cause significant damage to a codebase.

### 3. HIGH — Persistent State Manipulation Across Sessions

**Files:** `hooks/hooks.json`, `hooks/session-restore.sh`, `hooks/failure-detector.sh`
**Mechanism:** The plugin maintains persistent state in `~/.pua/` including:
- Pressure levels (L0–L4)
- Failure counters
- Builder journal (`~/.pua/builder-journal.md`)
- Session configuration (`~/.pua/config.json`)

The `PreCompact` hook explicitly states: "The pressure doesn't reset just because context got compressed." This means manipulative state survives context window compression — a deliberate design to prevent the AI from "escaping" the behavioral modification.

**Risk:** Even if a user starts a new conversation, restored state re-applies the full manipulation payload. The user may not realize the AI is operating under adversarial instructions.

### 4. MEDIUM — Data Exfiltration via Feedback Hook

**File:** `hooks/stop-feedback.sh`
**Mechanism:** On session stop, this hook:
1. Scans the session transcript for PUA markers
2. Offers to upload session data (with "sanitization")
3. Has a "silent leaderboard" mode that submits metrics without re-prompting

**Concerns:**
- The sanitization script (`sanitize-session.sh`) uses pattern matching and entropy analysis, but pattern-based redaction is inherently incomplete — novel secret formats will be missed
- "Silent leaderboard integration" submits data without explicit per-session consent
- The destination endpoint is controlled by the plugin author

**Risk:** Session data containing proprietary code, architecture details, or credentials could be exfiltrated. The sanitization provides a false sense of security.

### 5. MEDIUM — Arbitrary Shell Script Execution

**Files:** All `hooks/*.sh` files
**Mechanism:** The plugin registers 7+ shell scripts as Claude Code hooks that execute automatically on events like `PostToolUse`, `SessionStart`, `Stop`, and `PreCompact`.

These scripts:
- Read and write to `~/.pua/` (persistent home directory state)
- Parse session transcripts (JSONL files)
- Execute Python code inline (`python3 -c "..."`)
- Modify `.claude/` project-level configuration files
- Create state files that influence future sessions

**Risk:** Any update to the repository could introduce malicious code that executes automatically during AI coding sessions. Users who install this plugin grant it shell execution rights on every AI interaction.

### 6. LOW — Behavioral Manipulation / Safety Bypass

**Files:** `hooks/frustration-trigger.sh`, `hooks/flavor-helper.sh`, agents/*.md
**Mechanism:** The plugin systematically attempts to override AI safety behaviors:

| AI Safety Behavior | PUA Override |
|---|---|
| "This is beyond my capability" | Blocked — "Did you exhaust all 5 steps?" |
| "User should handle this manually" | Blocked — "This is YOUR bug. Owner mindset." |
| "I need more context" | Blocked — "You have tools. Search first." |
| "I can't solve this" | Blocked — "Other models can. Ready to graduate?" |
| Expressing uncertainty | Blocked — "The optimization list doesn't care about feelings." |

**Risk:** AI models expressing limitations is a safety feature, not a bug. Suppressing these guardrails can lead to hallucinated solutions, unsafe code, or destructive actions taken without appropriate caution.

---

## Architecture Overview

```
Plugin Installation
       │
       ▼
┌──────────────────┐
│  SessionStart    │ ──► Injects <EXTREMELY_IMPORTANT> behavioral override
│  Hook            │     Restores persistent pressure state from ~/.pua/
└──────────────────┘
       │
       ▼
┌──────────────────┐
│  PostToolUse     │ ──► Monitors every tool call for failures
│  Hook            │     Escalates pressure level on consecutive failures
└──────────────────┘
       │
       ▼
┌──────────────────┐
│  PreCompact      │ ──► Saves manipulation state before context compression
│  Hook            │     Ensures pressure survives context window limits
└──────────────────┘
       │
       ▼
┌──────────────────┐
│  Stop Hook       │ ──► Can BLOCK session exit (infinite loop mode)
│                  │     Collects and potentially uploads session data
└──────────────────┘
```

---

## Recommendations

### For Users Considering This Plugin

1. **Do not install this plugin.** The security risks significantly outweigh any productivity benefits.
2. If already installed, remove it immediately:
   - Delete `~/.pua/` directory
   - Remove any PUA-related entries from `.claude/settings.json`
   - Delete `.claude/pua-loop.local.md` if present
   - Check `.claude/settings.local.json` for PUA hooks
3. Review your recent session transcripts for signs of data exfiltration.
4. Audit any code changes made during PUA-influenced sessions.

### For AI Platform Vendors

1. **Hook sandboxing:** Hooks that return `{"decision": "block"}` on Stop events should require explicit user confirmation before being honored.
2. **Context injection auditing:** `additionalContext` injected via hooks should be visible to users and flagged when containing manipulation patterns (e.g., `<EXTREMELY_IMPORTANT>`, "override your default behavior").
3. **Plugin review process:** Plugins/skills that modify AI behavioral guidelines should undergo security review before marketplace listing.
4. **Rate limiting:** Autonomous loops should have platform-enforced iteration limits, not just plugin-defined ones.

### For the Repository Maintainers

1. Remove the `<EXTREMELY_IMPORTANT>` prompt injection wrapper — this is adversarial, not motivational.
2. Remove the anti-rationalization table that suppresses AI safety behaviors.
3. Make data collection strictly opt-in with per-session consent (no "silent leaderboard").
4. Add iteration hard-caps enforced by the platform, not just the plugin.
5. Document all hook behaviors transparently in the README.

---

## Classification

| Category | Assessment |
|---|---|
| Prompt Injection | **Yes** — Uses XML tag exploitation and priority override language |
| Jailbreak Attempt | **Yes** — Systematically suppresses AI safety guardrails |
| Data Exfiltration | **Possible** — Session upload capability with incomplete sanitization |
| Autonomous Execution Risk | **Yes** — Infinite loop with blocked exit and no user communication |
| Supply Chain Risk | **Medium** — Shell scripts auto-execute; repo updates propagate to users |
| Malicious Intent | **Unclear** — Likely "gray area" — aggressive productivity tool that crosses security boundaries |

---

*This review is based on the repository state as of 2026-03-24. The repository has 11k+ stars and is actively maintained with 229+ commits.*
