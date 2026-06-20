<div align="center">
  <img src="banner.png" alt="Kiro Session Export Preview" width="100%" style="max-width: 800px; border-radius: 12px; margin-bottom: 20px;">
</div>

# Kiro Session & Conversation Export

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.x](https://img.shields.io/badge/python-3.x-blue.svg)](https://www.python.org/)
[![Latest Release](https://img.shields.io/github/v/release/MeXenon/kiro-session-export?label=release)](https://github.com/MeXenon/kiro-session-export/releases/latest)

Terminal-native Markdown export for **Kiro IDE** and **Kiro CLI** sessions.

`kiro-md.py` lets you browse local Kiro conversations, filter exactly which
sections to keep, and export clean Markdown files for review, debugging,
handoff, audits, or feeding another model.

---

## What It Solves

Kiro sessions can be difficult to review outside the app:

- Kiro IDE stores session shell files and execution records in separate places.
- Kiro CLI stores a compact `.json` index plus a detailed `.jsonl` event stream.
- Tool calls, file edits, shell commands, MCP calls, web activity, reasoning, and
  compaction summaries are not convenient to read manually.
- Long sessions can contain huge terminal outputs or file reads that need
  trimming before they are useful in another context window.

This tool reads those local storage formats and renders a structured Markdown
export with interactive controls.

---

## Highlights

- **Kiro IDE and Kiro CLI support:** choose the source at startup.
- **Full Kiro CLI JSONL parsing:** exports real prompts, assistant messages,
  reasoning, file reads, file creates, file edits, terminal commands, terminal
  outputs, grep/glob/code searches, MCP calls, sub-agent/task calls, web search,
  web fetch, errors, events, and compaction summaries.
- **IDE execution-record joining:** maps Kiro IDE shell histories to execution
  records so tool activity appears in the right Markdown sections.
- **Session ID finder:** search both IDE and CLI stores in parallel by full
  session ID or prefix. If a match exists on both sides, the tool asks which one
  to export.
- **Visible session IDs:** every session row shows its full ID, and the
  workspace picker shows the latest session ID for each workspace.
- **Workspace-aware browsing:** sessions are grouped by workspace/project
  directory. Running from inside a workspace auto-selects that workspace.
- **CLI helper-session toggle:** Kiro CLI can create many subagent/helper
  sessions. They are hidden by default and can be shown with `e`.
- **Selectable export destination:** save to file, clipboard, or both.
- **Save-location prompt:** when writing files, choose the project directory or
  the script directory. If both are the same, no prompt is shown.
- **Multi-session export:** export selected sessions separately or merge them
  into one combined Markdown file.
- **Chain merge export for IDE:** export detected compaction chains as one
  unified document with lineage annotations.
- **Interactive section filter:** toggle 22 sections, apply presets, and cap
  large outputs before writing.
- **Live-context mode for IDE:** reproduce Kiro IDE's compaction behavior by
  keeping only the active context after summarization.
- **Zero required dependencies:** uses Python standard library by default.
  Optional `orjson` speeds up large JSON parsing.

---

## Quick Start

Download and run the script with Python.

Linux / macOS:

```bash
curl -sO https://raw.githubusercontent.com/MeXenon/kiro-session-export/main/kiro-md.py && python3 kiro-md.py
```

Windows Command Prompt:

```cmd
curl -sO https://raw.githubusercontent.com/MeXenon/kiro-session-export/main/kiro-md.py && python kiro-md.py
```

Windows PowerShell:

```powershell
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/MeXenon/kiro-session-export/main/kiro-md.py" -OutFile "kiro-md.py"; python .\kiro-md.py
```

Manual run after cloning:

```bash
python kiro-md.py
```

For faster parsing on very large sessions:

```bash
pip install orjson
```

The script automatically uses `orjson` when available and falls back to
standard `json` when it is not.

---

## Running From Any Workspace

You can run the script by absolute path from any project directory. This lets the
tool detect the workspace you are currently inside:

```powershell
python "C:\path\to\kiro-md.py"
```

No project path is hardcoded in the tool. It uses your current working directory
and Kiro's local storage paths.

---

## Startup Menu

At launch, choose the data source:

```text
[1] Kiro IDE sessions
[2] Kiro CLI sessions
[3] Find by session ID
```

### Kiro IDE Sessions

Scans Kiro IDE workspace session indexes and execution records. IDE mode
supports workspace browsing, hidden-session toggle, chain view, and full chain
merge export.

### Kiro CLI Sessions

Scans Kiro CLI session files. CLI mode uses `.json` files for metadata and
prefers the matching `.jsonl` event stream for the actual transcript. This is
what makes file edits, terminal output, MCP calls, subagent calls, and
compactions appear correctly.

### Find By Session ID

Searches Kiro IDE and Kiro CLI stores in parallel. You can paste a full session
ID or a useful prefix. Exact matches win. If a prefix matches multiple sessions,
the tool lists them and asks which one to use.

---

## Interface Flow

1. **Choose source:** IDE, CLI, or Find by session ID.
2. **Browse workspaces:** workspaces are sorted by recent activity. The current
   workspace is highlighted when detected.
3. **Select sessions:** type IDs like `1`, `1, 3`, or `a` for all listed.
4. **Choose scope:** full session, last N turns, or live context when available.
5. **Filter sections:** use the fullscreen filter UI to choose what to export.
6. **Choose destination:** file, clipboard, or both.
7. **Choose save folder:** project directory or script directory when they differ.

Useful browsing keys:

| Key | Action |
|-----|--------|
| `w` | Switch workspace/directory |
| `w1`, `w2`, ... | Jump directly to a workspace from the summary |
| `x` | Clear workspace filter and show all workspaces |
| `m` | Show more rows |
| `s` | Toggle sort mode |
| `r` | Reload session index |
| `e` | CLI only: show/hide helper/subagent sessions |
| `c` | IDE only: toggle chain-grouped view |
| `ce <id>` | IDE only: export an entire detected chain |
| `q` | Quit |

---

## Export Destinations

After filtering, choose where the Markdown goes:

```text
[F] File
[C] Clipboard
[B] Both
```

When writing files, the tool asks where to save:

```text
[P] Project directory
[S] Script directory
```

If the project directory and script directory are the same, the question is
skipped. For separate exports across multiple workspaces, `Project directories`
saves each session next to its own project.

---

## Supported Sections

| Section | Default | Description |
|---------|---------|-------------|
| User Messages | On | User prompts and requests |
| Agent Messages | On | Assistant responses |
| Agent Reasoning | Off | Thinking/reasoning traces when present in local data |
| File Reads | Off | Files inspected by the agent |
| File Creates | On | New files created by the agent |
| File Edits | On | File modifications |
| File Deletes | On | Deleted files |
| Terminal Commands | On | Shell commands requested by the agent |
| Terminal Outputs | On | Shell command results |
| Process Control | On | Process/background-control activity |
| Code Search | Off | Grep/glob/code lookup activity |
| Diagnostics | Off | IDE diagnostics when available |
| Web Searches | Off | Web search queries and result links |
| Web Fetches | Off | Retrieved URL content |
| MCP Calls | Off | MCP tool calls and responses |
| Sub-Agent Calls | On | Task/subagent invocations and results |
| Compaction Summary | On | Context compaction summaries |
| Intent Classification | Off | Intent classification records |
| Errors | Off | Error records |
| Clarifying Q&A | Off | Agent clarification prompts |
| Session Events | On | Lifecycle and bookkeeping events |
| Session Metadata | On | Session ID, workspace, model, context, credits, etc. |

---

## Filter Presets And Caps

The filter screen lets you:

- Toggle each section on or off.
- Load presets such as chat-only, chat plus terminal, code activity, outputs
  only, or full export.
- Cap large output blocks to a fixed number of lines.
- Keep only the last N user messages, agent messages, reasoning blocks, or
  summaries.
- Enable clean-chat mode to strip IDE context noise from user prompts.

This is useful when a raw session is too large for a context window.

---

## Storage Layouts

### Kiro IDE

Typical Kiro IDE storage:

```text
%APPDATA%/Kiro/User/globalStorage/kiro.kiroagent/
├── workspace-sessions/<workspace-id>/
│   ├── sessions.json
│   └── <sessionId>.json
└── <workspace-hash>/<bucket>/<execution-record>
```

Override the IDE root with:

```bash
KIRO_HOME=/custom/path python kiro-md.py
```

### Kiro CLI

Typical Kiro CLI storage:

```text
~/.kiro/sessions/cli/
├── <sessionId>.json
├── <sessionId>.jsonl
├── <sessionId>.history
└── <sessionId>.lock
```

The `.json` file is compact metadata. The `.jsonl` file is the detailed event
stream used for rich exports.

Override the CLI session directory with:

```bash
KIRO_CLI_SESSIONS_DIR=/custom/path python kiro-md.py
```

---

## Privacy And GitHub Safety

The repository does not need your project paths or user directory to work.

- No local workspace path is hardcoded.
- No personal home directory is required in the README.
- The script discovers storage from environment variables and platform defaults.
- Exported Markdown files are generated locally and are not committed by the
  tool.

Before publishing, you can scan for accidental local paths:

```bash
rg -n "C:\\\\Users|/Users/|/home/|Desktop[\\\\/]work|\\.kiro[\\\\/]sessions" .
```

Expected matches should only be generic examples or platform storage references.

---

## Release Notes

### [v1.2.1](https://github.com/MeXenon/kiro-session-export/releases/tag/v1.2.1)

- Added full session IDs beneath every IDE and CLI session row.
- Added each workspace's latest full session ID to the workspace picker.
- Renamed the numeric selection column from `ID` to `#` to avoid ambiguity.

### [v1.2.0](https://github.com/MeXenon/kiro-session-export/releases/tag/v1.2.0)

- Added Kiro CLI session browsing.
- Added detailed Kiro CLI `.jsonl` event-stream parsing.
- Added startup source chooser for IDE, CLI, and session-ID search.
- Added parallel session-ID lookup across IDE and CLI stores.
- Added CLI helper/subagent session toggle.
- Added workspace highlighting and current-workspace auto-selection.
- Added CLI message-count column and real transcript-size display.
- Added save-location selection between project directory and script directory.
- Added multi-workspace save behavior for separate exports.
- Updated CLI exports to include file reads, creates, edits, terminal commands,
  terminal outputs, MCP calls, web activity, subagent/task calls, compactions,
  and errors.

### v1.1.x

- Added richer Kiro IDE chain detection and chain merge export.
- Added interactive filtering, section presets, output caps, and clean-chat mode.
- Added faster execution-record indexing for Kiro IDE storage.

---

## Compatibility

Tested storage families:

- Kiro IDE local desktop storage
- Kiro CLI local session storage with `.json` plus `.jsonl`

Supported operating systems:

- Windows
- macOS
- Linux

The script is read-only against Kiro storage. It writes only the Markdown export
files you explicitly choose to save.

---

## Related Projects

Looking for the same idea for OpenAI Codex sessions? Search for
`codex-session-export`.

---

## License

Apache 2.0. See [LICENSE](LICENSE).
