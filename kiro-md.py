#!/usr/bin/env python3
"""
Kiro Session Manager & Markdown Converter  (v1.0)
-------------------------------------------------
An interactive tool to browse, filter, and convert Kiro IDE chat
sessions into readable Markdown documents.

Kiro storage layout (as of this script being written):
  %APPDATA%/Kiro/User/globalStorage/kiro.kiroagent/
    workspace-sessions/<urlsafe-b64 workspace-path>/
        sessions.json                      ← per-workspace session index
        <sessionId>.json                   ← session shell (user msgs + stubs)
    <workspace-hash>/<bucket>/<execution-hash>   ← execution records (the gold)

The execution records hold the actual tool calls, assistant 'say'
messages, reasoning, summarizations (compaction), errors, etc.  This
script joins them with each session and renders rich Markdown.

Compaction handling:
  • Sessions whose first hidden user message starts with
    "# Conversation Summary" are tagged as "↻ from compaction".
  • Sessions containing a 'summarization' action are tagged "✂ compacts".
  • Title suffixes "(Continued)" / "(checkpoint)" are surfaced.
  • Live-Context mode replaces the pre-compaction history with the
    Conversation Summary block (the same trick Kiro itself uses).
"""

import os
import sys
import json
import glob
import re
import base64
import hashlib
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set, Iterable

# Platform-specific terminal handling
_IS_WINDOWS = sys.platform == 'win32'
if not _IS_WINDOWS:
    import tty
    import termios

# Force UTF-8 stdout on Windows (cp1252 cannot encode the emoji we use)
try:
    sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[attr-defined]
except Exception:
    pass

# ──────────────────────────────────────────────────────────────
# Terminal Styling
# ──────────────────────────────────────────────────────────────
class Style:
    HEADER  = '\033[95m'
    BLUE    = '\033[94m'
    CYAN    = '\033[96m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    RED     = '\033[91m'
    MAGENTA = '\033[35m'
    BOLD    = '\033[1m'
    UNDERLINE = '\033[4m'
    DIM     = '\033[2m'
    REVERSE = '\033[7m'
    RESET   = '\033[0m'
    NORMAL  = '\033[22m'      # cancels bold/dim, keeps background
    FG_RESET = '\033[39m'     # cancels foreground colour, keeps background
    BG_GRAY = '\033[48;5;236m'
    BG_SELECTED = '\033[48;5;238m'

    @staticmethod
    def title(msg): return f"{Style.BOLD}{Style.HEADER}{msg}{Style.RESET}"
    @staticmethod
    def info(msg): return f"{Style.BLUE}ℹ {msg}{Style.RESET}"
    @staticmethod
    def success(msg): return f"{Style.GREEN}✔ {msg}{Style.RESET}"
    @staticmethod
    def error(msg): return f"{Style.RED}✖ {msg}{Style.RESET}"
    @staticmethod
    def warn(msg): return f"{Style.YELLOW}⚠ {msg}{Style.RESET}"

# Enable ANSI escapes on Windows
if _IS_WINDOWS:
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────
def default_kiro_home() -> Path:
    """Locate Kiro's globalStorage directory."""
    env = os.environ.get("KIRO_HOME")
    if env:
        return Path(env)
    if _IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent"
    # macOS
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent"
    # Linux
    return Path.home() / ".config" / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent"

def default_kiro_cli_sessions_dir() -> Path:
    """Locate Kiro CLI's local session JSON directory."""
    env = os.environ.get("KIRO_CLI_SESSIONS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".kiro" / "sessions" / "cli"

KIRO_HOME = default_kiro_home()
WORKSPACE_SESSIONS_DIR = KIRO_HOME / "workspace-sessions"
KIRO_CLI_SESSIONS_DIR = default_kiro_cli_sessions_dir()

# ──────────────────────────────────────────────────────────────
# JSON fast path — use orjson if installed (3-5× faster than stdlib).
# Falls back to stdlib silently so the script remains zero-dependency.
# ──────────────────────────────────────────────────────────────
try:
    import orjson as _orjson  # type: ignore
    def _json_load_path(path: Path):
        with open(path, 'rb') as f:
            return _orjson.loads(f.read())
except ImportError:
    _orjson = None
    def _json_load_path(path: Path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)


# No persistent cache: every run reads live data from Kiro's storage.
# Speedups come from reading less per file (head-only previews,
# single-pass regex metadata) and from parallel IO/parsing.

# ──────────────────────────────────────────────────────────────
# Section Definitions  (key, display_name, emoji, default_on)
# ──────────────────────────────────────────────────────────────
SECTION_DEFS = [
    ('user_message',     'User Messages',       '👤', True ),
    ('agent_message',    'Agent Messages',      '🤖', True ),
    ('reasoning',        'Agent Reasoning',     '🧠', False),
    ('file_read',        'File Reads',          '📖', False),
    ('file_create',      'File Creates',        '🆕', True ),
    ('file_edit',        'File Edits',          '✏️ ', True ),
    ('file_delete',      'File Deletes',        '🗑️ ', True ),
    ('terminal_cmd',     'Terminal Commands',   '💻', True ),
    ('terminal_output',  'Terminal Outputs',    '📤', True ),
    ('process_ctrl',     'Process Control',     '⚙️ ', True ),
    ('code_search',      'Code Search',         '🔎', False),
    ('diagnostics',      'Diagnostics',         '🩺', False),
    ('web_search',       'Web Searches',        '🌐', False),
    ('web_fetch',        'Web Fetches',         '🔗', False),
    ('mcp_tool',         'MCP Calls',           '🔌', False),
    ('sub_agent',        'Sub-Agent Calls',     '🧩', True ),
    ('orchestration',    'Orchestrations',      '🎭', True ),
    ('summarization',    'Compaction Summary',  '✂️ ', True ),
    ('intent',           'Intent Classification','🎯', False),
    ('error',            'Errors',              '❗', False),
    ('user_input',       'Clarifying Q&A',      '❓', False),
    ('session_event',    'Session Events',      '🔔', True ),
    ('session_meta',     'Session Metadata',    '📝', True ),
]
ALL_SECTION_KEYS = {s[0] for s in SECTION_DEFS}

# Mapping from Kiro actionType → our section key
ACTION_TYPE_TO_SECTION: Dict[str, str] = {
    'say':                'agent_message',
    'reasoning':          'reasoning',
    'readFile':           'file_read',
    'readFiles':          'file_read',
    'readCode':           'file_read',
    'read_file':          'file_read',
    'create':             'file_create',
    'write':              'file_create',
    'replace':            'file_edit',
    'append':             'file_edit',
    'str_replace':        'file_edit',
    'delete':             'file_delete',
    'runCommand':         'terminal_cmd',
    'execute_pwsh':       'terminal_cmd',
    'controlProcess':     'process_ctrl',
    'getProcessOutput':   'process_ctrl',
    'search':             'code_search',
    'file_search':        'code_search',
    'getDiagnostics':     'diagnostics',
    'remote_web_search':  'web_search',
    'webFetch':           'web_fetch',
    'mcp':                'mcp_tool',
    'invokeSubAgent':     'sub_agent',
    'subagent_response':  'sub_agent',
    'subagentResponse':   'sub_agent',
    'specAgent':          'sub_agent',
    'summarization':      'summarization',
    'intentClassification':'intent',
    'displayError':       'error',
    'userInput':          'user_input',
    'analyzeRequirements':'sub_agent',
    'model':              None,   # internal book-keeping — drop
}

# ──────────────────────────────────────────────────────────────
# Filter Presets  (name, enabled_keys | None=defaults, clean)
# ──────────────────────────────────────────────────────────────
FILTER_PRESETS = [
    ('Defaults',        None, False),
    ('Chat Only',       {'user_message','agent_message','session_meta','session_event'}, False),
    ('Chat + Reasoning',{'user_message','agent_message','reasoning','session_meta','session_event'}, False),
    ('Chat + Terminal', {'user_message','agent_message','terminal_cmd','terminal_output',
                          'process_ctrl','session_meta','session_event'}, False),
    ('Code Activity',   {'user_message','agent_message','file_read','file_create','file_edit',
                          'file_delete','terminal_cmd','terminal_output','session_meta','session_event'}, False),
    ('Outputs Only',    {'terminal_output'}, False),
    ('Full Export',     ALL_SECTION_KEYS, False),
]

# ──────────────────────────────────────────────────────────────
# Utility helpers
# ──────────────────────────────────────────────────────────────
def clean_filename(text: str) -> str:
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[*_`]', '', text)
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    text = re.sub(r'[-\s]+', '-', text)
    return text[:60] if text else "untitled-session"

def normalize_title_candidate(text: str) -> str:
    text = re.sub(r'\s+', ' ', text.strip())
    return text[:80] + ("..." if len(text) > 80 else "")

def format_size(num_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(num_bytes)
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    if unit == "B":
        return f"{int(size)}{unit}"
    return f"{size:.1f}{unit}"

def parse_iso_datetime_ms(value: Optional[str]) -> int:
    """Parse Kiro CLI ISO timestamps, including nanosecond-style fractions."""
    if not value:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    try:
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        # datetime.fromisoformat accepts microseconds, not 9-digit fractions.
        text = re.sub(r'(\.\d{6})\d+([+-]\d\d:\d\d)$', r'\1\2', text)
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except Exception:
        return 0

def workspace_path_from_b64(name: str) -> str:
    """Decode the urlsafe b64 workspace folder name. Kiro pads with '_' instead of '='."""
    padded = name + ('=' * (-len(name) % 4))
    try:
        return base64.urlsafe_b64decode(padded.encode('ascii')).decode('latin-1', errors='replace')
    except Exception:
        return name

def short_workspace(p: Optional[str]) -> str:
    """Get a short, recognizable label for a workspace path."""
    if not p:
        return "?"
    parts = re.split(r'[\\/]+', p.rstrip('\\/'))
    if not parts:
        return p
    return parts[-1] or (parts[-2] if len(parts) > 1 else p)

def msg_text(content) -> str:
    """Extract plain text from Kiro's `message.content` (string or list of {type:text,text:...})."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for sub in content:
            if isinstance(sub, dict):
                if sub.get('type') == 'text' or sub.get('kind') == 'text':
                    t = sub.get('text', sub.get('data', ''))
                    if isinstance(t, str):
                        out.append(t)
                else:
                    # other types may appear; keep them with type marker
                    t = sub.get('text', '') if isinstance(sub.get('text'), str) else json.dumps(sub, ensure_ascii=False)
                    if t:
                        out.append(t)
            elif isinstance(sub, str):
                out.append(sub)
        return '\n'.join(out)
    return str(content) if content is not None else ''

CONVERSATION_SUMMARY_RE = re.compile(
    r'^\s*(?:#\s*Conversation\s*Summary|##\s*TASK\s*\d+\b)',
    re.IGNORECASE,
)


def _extract_user_text_from_chunk(chunk: bytes) -> str:
    """Pull the first text content out of a `"role":"user"` chunk without a
    full JSON parse. Handles both forms:
        "content": "some text"
        "content": [{"type":"text","text":"some text"}, ...]
    """
    # Form 1: "content": "..."  (string literal, with JSON escapes)
    m = re.search(rb'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk, re.DOTALL)
    if m:
        raw = m.group(1)
        try:
            # Use json to decode the escaped string properly
            return json.loads(b'"' + raw + b'"')
        except Exception:
            try:
                return raw.decode('utf-8', errors='replace') \
                    .replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
            except Exception:
                return ''
    # Form 2: "content": [...]  — collect every "text": "..." inside
    m = re.search(rb'"content"\s*:\s*\[(.*?)\]\s*,\s*"', chunk, re.DOTALL)
    if not m:
        m = re.search(rb'"content"\s*:\s*\[(.*?)\]\s*\}', chunk, re.DOTALL)
    if m:
        body = m.group(1)
        parts: List[str] = []
        for tm in re.finditer(rb'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', body, re.DOTALL):
            raw = tm.group(1)
            try:
                parts.append(json.loads(b'"' + raw + b'"'))
            except Exception:
                parts.append(raw.decode('utf-8', errors='replace'))
            if len(parts) >= 5:
                break
        return '\n'.join(parts)
    return ''

def is_conversation_summary(text: str) -> bool:
    return bool(text) and bool(CONVERSATION_SUMMARY_RE.match(text))

# ──────────────────────────────────────────────────────────────
# Execution-record index
#
# Execution records live under <KIRO_HOME>/<32-hex>/<32-hex>/<32-hex> .
# Index them every run: executionId → file_path  and
#                       chatSessionId → list of execution files.
# ──────────────────────────────────────────────────────────────
class ExecutionIndex:
    """Indexes every execution record under KIRO_HOME. Reads each file fresh
    on every run to keep the index accurate; speedups come from a single
    regex pass per file and parallel IO across workers."""

    def __init__(self, root: Path):
        self.root = root
        # Maps
        self.exec_by_id: Dict[str, Path] = {}
        self.exec_by_session: Dict[str, List[Path]] = {}
        # Per-execution lightweight metadata (kept in-memory for this run only)
        self.exec_meta: Dict[Path, Dict] = {}
        self._built = False

    # Single combined regex that captures every metadata field we need in
    # one linear pass over the file bytes — much faster than running each
    # pattern separately.
    _META_RE = re.compile(
        br'"(executionId|chatSessionId|workflowType|startTime|parentSessionIds)"'
        br'\s*:\s*(?:"([^"]*)"|(\d+)|\[([^\]]*)\])'
    )

    # The top-level chatSessionId is the value beside autonomyMode, after the
    # input. A continuation's input quotes an older chatSessionId first, and
    # that nested copy is the ancestor, not this session. parentSessionIds,
    # when present, is in the last kilobyte. The read stops at the real id
    # and still skips the action transcript after it.
    _META_HEAD_START = 4096
    _META_HEAD_CAP = 3 * 1024 * 1024
    _META_PARENT_CAP = 8 * 1024 * 1024
    _META_HEAD_STEP = 256 * 1024
    _META_TAIL = 16 * 1024
    _SESSION_PAIR_RE = re.compile(
        br'"autonomyMode"\s*:\s*"(?:[^"\\]|\\.)*"\s*,\s*"chatSessionId"\s*:\s*"([^"]*)"'
    )

    @staticmethod
    def _read_meta_bytes(fp: Path) -> Optional[bytes]:
        try:
            with open(fp, 'rb') as fh:
                head = fh.read(ExecutionIndex._META_HEAD_START)
                if b'"executionId"' not in head:
                    return None
                end = fh.seek(0, os.SEEK_END)
                tail = b''
                if end > len(head):
                    fh.seek(max(0, end - ExecutionIndex._META_TAIL))
                    tail = fh.read(ExecutionIndex._META_TAIL)
                    fh.seek(len(head))
                # A record that names parents has already quoted the ancestor
                # id inside the input. Keep reading until the real id.
                parented = b'"parentSessionIds"' in head or b'"parentSessionIds"' in tail
                if parented:
                    cap = ExecutionIndex._META_PARENT_CAP
                    while (ExecutionIndex._SESSION_PAIR_RE.search(head) is None
                           and len(head) < cap):
                        more = fh.read(ExecutionIndex._META_HEAD_STEP)
                        if not more:
                            break
                        head += more
                else:
                    while (b'"chatSessionId"' not in head
                           and len(head) < ExecutionIndex._META_HEAD_CAP):
                        more = fh.read(ExecutionIndex._META_HEAD_STEP)
                        if not more:
                            break
                        head += more
        except Exception:
            return None
        if end <= len(head):
            return head
        return head + b'\n' + tail

    @staticmethod
    def _scan_meta(fp: Path) -> Optional[Dict]:
        """Extract execution metadata without reading the whole transcript."""
        data = ExecutionIndex._read_meta_bytes(fp)
        if not data:
            return None
        out: Dict[str, Optional[object]] = {
            'executionId': None, 'chatSessionId': None,
            'workflowType': None, 'startTime': 0,
            'parentSessionIds': [],
        }
        for m in ExecutionIndex._META_RE.finditer(data):
            field = m.group(1).decode('ascii')
            sval, ival, aval = m.group(2), m.group(3), m.group(4)
            # The first occurrence is the top-level one (top of file). Once a
            # field is set, don't overwrite with a nested re-occurrence.
            if field in ('executionId', 'chatSessionId', 'workflowType'):
                if out[field] is None and sval is not None:
                    out[field] = sval.decode('utf-8', 'replace')
            elif field == 'startTime':
                if not out['startTime'] and ival is not None:
                    try: out['startTime'] = int(ival)
                    except Exception: pass
            elif field == 'parentSessionIds':
                # Only accept the first occurrence (top-level)
                if not out['parentSessionIds'] and aval is not None:
                    out['parentSessionIds'] = [
                        m2.decode('utf-8', 'replace')
                        for m2 in re.findall(br'"([^"]+)"', aval)
                    ]
        # The first chatSessionId in a continuation is the ancestor quoted
        # inside the input. The field beside autonomyMode is this session.
        pair = ExecutionIndex._SESSION_PAIR_RE.search(data)
        if pair:
            out['chatSessionId'] = pair.group(1).decode('utf-8', 'replace')
        return out

    def build(self, progress: bool = False):
        if self._built:
            return
        hex32 = re.compile(r'^[0-9a-f]{32}$')
        if not self.root.exists():
            self._built = True
            return
        shards = [d for d in self.root.iterdir() if d.is_dir() and hex32.match(d.name)]
        files: List[Path] = []
        for shard in shards:
            for bucket in shard.iterdir():
                if not bucket.is_dir():
                    continue
                for fp in bucket.iterdir():
                    if fp.is_file():
                        files.append(fp)
        if progress and files:
            sys.stdout.write(f"{Style.DIM}Reading {len(files)} execution records...{Style.RESET}\n")
            sys.stdout.flush()

        # Each worker reads only the metadata slices, not the transcript.
        from concurrent.futures import ThreadPoolExecutor
        workers = min(16, max(2, len(files)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            metas = list(ex.map(self._scan_meta, files))

        for fp, meta in zip(files, metas):
            if not meta:
                continue
            self.exec_meta[fp] = meta
            eid = meta.get('executionId')
            cid = meta.get('chatSessionId')
            if eid:
                self.exec_by_id[eid] = fp
            if cid:
                self.exec_by_session.setdefault(cid, []).append(fp)

        # Sort per-session execution lists by start time so the first entry is
        # always the earliest execution of that session.
        for cid, fps in self.exec_by_session.items():
            fps.sort(key=lambda p: self.exec_meta.get(p, {}).get('startTime', 0))
        self._built = True

    def first_exec_meta(self, chat_session_id: str) -> Optional[Dict]:
        """Return metadata for the earliest execution of a chat session."""
        fps = self.exec_by_session.get(chat_session_id) or []
        if not fps:
            return None
        return self.exec_meta.get(fps[0])

    def load_execution(self, fp: Path) -> Optional[Dict]:
        try:
            return _json_load_path(fp)
        except Exception:
            return None

EXEC_INDEX: Optional[ExecutionIndex] = None


# ──────────────────────────────────────────────────────────────
# Chain Graph — links sessions that flowed from each other via
# compaction. Combines:
#   • Authoritative `parentSessionIds` (set on every execution of newer
#     sessions; same value across all execs of one session — it's the
#     chain ancestry, oldest first, direct parent last, self excluded).
#   • Heuristic fallback for older sessions: same workspace + ↻
#     marker + chronological neighbour.
# ──────────────────────────────────────────────────────────────
class Chain:
    """One compaction lineage: a chronologically ordered list of
    sessions where each was seeded from the previous via compaction."""
    __slots__ = ('id', 'sessions', 'confidence')

    def __init__(self, chain_id: str):
        self.id = chain_id
        self.sessions: List['SessionEntry'] = []
        self.confidence: str = 'authoritative'  # or 'inferred' / 'mixed'

    @property
    def root(self) -> 'SessionEntry':
        return self.sessions[0]

    @property
    def tip(self) -> 'SessionEntry':
        return self.sessions[-1]

    @property
    def length(self) -> int:
        return len(self.sessions)

    @property
    def workspace(self) -> Optional[str]:
        return self.root.workspace_dir

    @property
    def last_activity(self) -> int:
        return max(s.date_created or 0 for s in self.sessions)

    @property
    def title(self) -> str:
        """A meaningful chain title — use the root's preview/title."""
        # Prefer the earliest session's preview text (real first user msg)
        for s in self.sessions:
            s.load_preview()
            if s.preview_text:
                return s.preview_text
        return self.root.display_title


class ChainGraph:
    """Computes compaction lineages across all sessions."""

    def __init__(self, sessions: List['SessionEntry'], exec_index: ExecutionIndex):
        self.sessions = sessions
        self.exec_index = exec_index
        # Outputs
        self.chain_of: Dict[str, str] = {}    # session_id → chain_id
        self.chains: Dict[str, Chain] = {}     # chain_id → Chain
        self.parents_of: Dict[str, List[str]] = {}  # session_id → list of ancestors (chronological)
        self.confidence_of: Dict[str, str] = {}     # session_id → 'authoritative' | 'inferred'
        self._build()

    def _build(self):
        by_id = {s.session_id: s for s in self.sessions}

        # --- Phase 1: authoritative parents from parentSessionIds ----------
        # Every execution carries the *full* ancestor list of its chat session.
        # Crucially, a session B in the middle of a chain may have *no own
        # executions* (Kiro doesn't always issue a new execution for a session;
        # tool calls can be attributed to a parent's executionId). But B will
        # still appear in some descendant's parentSessionIds. So we walk every
        # execution we have and reconstruct each ancestor's own ancestry.
        for fp, meta in self.exec_index.exec_meta.items():
            psids_raw = meta.get('parentSessionIds') or []
            cs = meta.get('chatSessionId')
            if not psids_raw or not cs:
                continue
            # The execution belongs to session `cs`; its chain (oldest → child)
            # is psids_raw + [cs].
            full_chain = list(psids_raw) + [cs]
            # For each session in the chain except the root, record its
            # immediate ancestry (everything to its left in the list).
            for i in range(1, len(full_chain)):
                child = full_chain[i]
                if child not in by_id:
                    continue
                ancestors = [a for a in full_chain[:i] if a in by_id and a != child]
                if not ancestors:
                    continue
                existing = self.parents_of.get(child)
                # Prefer the longest known chain (most informative)
                if not existing or len(ancestors) > len(existing):
                    self.parents_of[child] = ancestors
                    self.confidence_of[child] = 'authoritative'

        # --- Phase 2: heuristic parents for ↻ sessions without auth links --
        # Make sure compaction flags are loaded
        for s in self.sessions:
            s.load_preview()
        for s in self.sessions:
            if s.session_id in self.parents_of:
                continue
            if not s.from_compaction:
                continue
            ws = s.workspace_dir
            candidates = [
                other for other in self.sessions
                if other.session_id != s.session_id
                and other.workspace_dir == ws
                and (other.date_created or 0) < (s.date_created or 0)
            ]
            if not candidates:
                continue
            # Prefer a candidate with continuation_count one less than ours
            cc_self = s.continuation_count
            preferred = [c for c in candidates if c.continuation_count == max(0, cc_self - 1)]
            pool = preferred or candidates
            pool.sort(key=lambda c: -(c.date_created or 0))
            direct = pool[0]
            # Chain back through any authoritative ancestors of `direct`
            ancestor_chain = self.parents_of.get(direct.session_id, []) + [direct.session_id]
            self.parents_of[s.session_id] = ancestor_chain
            self.confidence_of[s.session_id] = 'inferred'

        # --- Phase 3: cluster sessions into chains via union-find ----------
        parent_uf: Dict[str, str] = {s.session_id: s.session_id for s in self.sessions}

        def find(x: str) -> str:
            while parent_uf[x] != x:
                parent_uf[x] = parent_uf[parent_uf[x]]
                x = parent_uf[x]
            return x

        def union(a: str, b: str):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent_uf[ra] = rb

        for sid, ancestors in self.parents_of.items():
            for a in ancestors:
                if a in parent_uf:
                    union(sid, a)

        # Build Chain objects
        clusters: Dict[str, List['SessionEntry']] = {}
        for s in self.sessions:
            clusters.setdefault(find(s.session_id), []).append(s)

        # Stable chain IDs based on root session ID — short alpha labels for
        # the UI: A, B, ..., Z, AA, AB, ...
        cluster_items = sorted(
            clusters.items(),
            key=lambda kv: -max(s.date_created or 0 for s in kv[1]),
        )
        for idx, (root, members) in enumerate(cluster_items):
            label = _alpha_label(idx)
            chain = Chain(label)
            # Sort sessions by date asc
            members.sort(key=lambda s: s.date_created or 0)
            chain.sessions = members
            # Confidence rollup
            confs = {self.confidence_of.get(s.session_id) for s in members}
            confs.discard(None)
            if confs == {'authoritative'}:
                chain.confidence = 'authoritative'
            elif 'inferred' in confs:
                chain.confidence = 'inferred' if len(confs) == 1 else 'mixed'
            else:
                chain.confidence = 'single'   # singleton chain (one session, no compaction)
            self.chains[label] = chain
            for s in members:
                self.chain_of[s.session_id] = label

    def chain_for(self, sid: str) -> Optional[Chain]:
        cid = self.chain_of.get(sid)
        return self.chains.get(cid) if cid else None

    def chain_position(self, sid: str) -> Optional[Tuple[int, int]]:
        ch = self.chain_for(sid)
        if not ch or ch.length <= 1:
            return None
        try:
            pos = next(i for i, s in enumerate(ch.sessions) if s.session_id == sid) + 1
            return (pos, ch.length)
        except StopIteration:
            return None


def _alpha_label(n: int) -> str:
    """0→A, 1→B, ..., 25→Z, 26→AA, 27→AB, ..."""
    label = ''
    n += 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        label = chr(ord('A') + r) + label
    return label


CHAIN_GRAPH: Optional[ChainGraph] = None


# ──────────────────────────────────────────────────────────────
# Session registry — scan all workspaces' sessions.json files
# ──────────────────────────────────────────────────────────────
class SessionEntry:
    __slots__ = ('session_id', 'title', 'date_created', 'workspace_dir', 'hidden',
                 'session_file', 'workspace_b64', 'continuation_count',
                 '_preview_first_user', '_preview_from_compaction',
                 '_preview_summary_heading', '_preview_loaded')

    def __init__(self, sid, title, date_ms, ws_dir, hidden, session_file, ws_b64):
        self.session_id = sid
        self.title = title or ""
        self.date_created: int = int(date_ms) if date_ms is not None else 0
        self.workspace_dir = ws_dir
        self.hidden = bool(hidden)
        self.session_file: Path = session_file
        self.workspace_b64 = ws_b64
        # Quick lexical hints from title — counts of "(Continued)" / "(checkpoint)"
        m = re.findall(r'\((Continued|checkpoint)\)', title or '', flags=re.IGNORECASE)
        self.continuation_count = len(m)
        self._preview_first_user: Optional[str] = None  # first non-hidden user msg snippet
        self._preview_summary_heading: Optional[str] = None  # extracted from intro summary
        self._preview_from_compaction: bool = False
        self._preview_loaded: bool = False

    def load_preview(self, head_bytes: int = 512 * 1024):
        """Cheaply derive preview info without a full json.load.

        Reads only the first `head_bytes` of the live session file and
        extracts:
          • whether the first user message is hidden (= ↻ from compaction)
          • a snippet of the first non-hidden user message (= preview text)
          • a heading hint from the intro Conversation Summary if present

        Always reads fresh data from disk — no cache.
        """
        # Re-read every call: caller controls memoization with _preview_loaded
        # in the same process, but we don't persist anything.
        if self._preview_loaded:
            return
        self._preview_loaded = True

        try:
            with open(self.session_file, 'rb') as f:
                head = f.read(head_bytes)
        except Exception:
            return

        # Slice the head into chunks per `"role": "..."` anchor so we can
        # inspect each message's hidden flag and content independently.
        anchors: List[Tuple[int, str]] = []
        for m in re.finditer(rb'"role"\s*:\s*"(user|assistant|tool|bot|human)"', head):
            anchors.append((m.start(), m.group(1).decode('ascii')))
        if not anchors:
            return
        # End each anchor's chunk at the next anchor (or end of head).
        anchors.append((len(head), ''))  # sentinel

        from_compaction = False
        preview_text: Optional[str] = None
        summary_heading: Optional[str] = None
        first_user_seen = False

        for i in range(len(anchors) - 1):
            start, role = anchors[i]
            end = anchors[i + 1][0]
            if role != 'user':
                continue
            chunk = head[start:end]
            hidden = bool(re.search(rb'"isHidden"\s*:\s*true', chunk))
            text = _extract_user_text_from_chunk(chunk)
            if not first_user_seen:
                first_user_seen = True
                if hidden:
                    from_compaction = True
                if hidden and text and is_conversation_summary(text):
                    hm = re.search(r'^##\s*(TASK\s*\d+[:.\s][^\n]*)',
                                   text, re.MULTILINE)
                    if hm:
                        summary_heading = normalize_title_candidate(hm.group(1))
            # First non-hidden, non-summary user message wins as preview
            if hidden or not text or is_conversation_summary(text):
                continue
            first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), '')
            if first_line and preview_text is None:
                preview_text = normalize_title_candidate(first_line)
                break

        self._preview_from_compaction = from_compaction
        self._preview_first_user      = preview_text
        self._preview_summary_heading = summary_heading

    # ------- title helpers -------
    @property
    def stripped_title(self) -> str:
        """Kiro's stored title with `(Continued)` / `(checkpoint)` suffixes
        removed (those are surfaced as separate ↪×N badges instead)."""
        t = (self.title or '').strip()
        t = re.sub(r'(\s*\((?:Continued|checkpoint)\))+\s*$', '', t,
                   flags=re.IGNORECASE).strip()
        return t

    @property
    def display_title(self) -> str:
        """Title shown in the list — Kiro's own title verbatim (minus the
        chain-suffixes). We never replace it with a user-message snippet here;
        the preview column shows that separately."""
        t = self.stripped_title
        return t or '(untitled)'

    @property
    def preview_text(self) -> str:
        """Short preview of the conversation content for the list view.
        Falls back to a summary-heading hint, else to first user msg."""
        if self._preview_first_user:
            return self._preview_first_user
        if self._preview_summary_heading:
            return f'(summary) {self._preview_summary_heading}'
        return ''

    @property
    def from_compaction(self) -> bool:
        return self._preview_from_compaction

    @property
    def date(self) -> datetime:
        if self.date_created:
            try:
                return datetime.fromtimestamp(self.date_created / 1000.0)
            except Exception:
                pass
        try:
            return datetime.fromtimestamp(self.session_file.stat().st_mtime)
        except Exception:
            return datetime.fromtimestamp(0)

    @property
    def size(self) -> int:
        try:
            return self.session_file.stat().st_size
        except Exception:
            return 0


def scan_all_sessions() -> List[SessionEntry]:
    """Read every workspace-sessions/<b64>/sessions.json and return all entries."""
    sessions: List[SessionEntry] = []
    if not WORKSPACE_SESSIONS_DIR.exists():
        return sessions
    for ws_dir in WORKSPACE_SESSIONS_DIR.iterdir():
        if not ws_dir.is_dir():
            continue
        index_fp = ws_dir / "sessions.json"
        if not index_fp.exists():
            continue
        try:
            with open(index_fp, 'r', encoding='utf-8') as f:
                rows = json.load(f)
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            sid = row.get('sessionId')
            if not sid:
                continue
            sf = ws_dir / f"{sid}.json"
            if not sf.exists():
                continue
            sessions.append(SessionEntry(
                sid=sid,
                title=row.get('title', ''),
                date_ms=row.get('dateCreated'),
                ws_dir=row.get('workspaceDirectory') or workspace_path_from_b64(ws_dir.name),
                hidden=row.get('hidden', False),
                session_file=sf,
                ws_b64=ws_dir.name,
            ))
    # newest first
    sessions.sort(key=lambda s: s.date_created, reverse=True)
    return sessions


# ──────────────────────────────────────────────────────────────
# Kiro CLI session registry — scan ~/.kiro/sessions/cli/*.json
# ──────────────────────────────────────────────────────────────
class CliSessionEntry:
    __slots__ = ('session_id', 'title', 'date_created', 'created_ms',
                 'updated_ms', 'created_at', 'updated_at', 'workspace_dir',
                 'hidden', 'session_file', 'workspace_b64',
                 'continuation_count', 'message_count', 'turn_count',
                 'session_created_reason', 'parent_session_id',
                 '_preview_first_user', '_preview_from_compaction',
                 '_preview_summary_heading', '_preview_loaded')

    def __init__(self, sid: str, title: str, created_ms: int, updated_ms: int,
                 created_at: str, updated_at: str, cwd: str, session_file: Path,
                 message_count: int, turn_count: int, reason: str):
        self.session_id = sid
        self.title = title or ""
        # For list views and workspace summaries, "date_created" means activity
        # time. The actual created timestamp is kept separately for export.
        self.date_created: int = int(updated_ms or created_ms or 0)
        self.created_ms = int(created_ms or 0)
        self.updated_ms = int(updated_ms or created_ms or 0)
        self.created_at = created_at or ""
        self.updated_at = updated_at or ""
        self.workspace_dir = cwd or ""
        self.hidden = False
        self.session_file: Path = session_file
        self.workspace_b64 = ""
        self.continuation_count = 0
        self.message_count = int(message_count or 0)
        self.turn_count = int(turn_count or 0)
        self.session_created_reason = reason or ""
        self.parent_session_id = ""
        self._preview_first_user: Optional[str] = None
        self._preview_summary_heading: Optional[str] = None
        self._preview_from_compaction = False
        self._preview_loaded = False

    def load_preview(self, head_bytes: int = 512 * 1024):
        if self._preview_loaded:
            return
        self._preview_loaded = True
        self._preview_first_user = normalize_title_candidate(self.title or '')
        try:
            data = _json_load_path(self.session_file)
            turns = (((data.get('session_state') or {})
                      .get('conversation_metadata') or {})
                     .get('user_turn_metadatas') or [])
            for turn in turns:
                ok = (turn.get('result') or {}).get('Ok')
                if not isinstance(ok, dict):
                    continue
                for block in ok.get('content') or []:
                    if isinstance(block, dict) and block.get('kind') == 'text':
                        text = block.get('data') or ''
                        if isinstance(text, str) and text.strip():
                            self._preview_first_user = normalize_title_candidate(text)
                            return
        except Exception:
            return

    @property
    def stripped_title(self) -> str:
        return (self.title or '').strip()

    @property
    def display_title(self) -> str:
        return self.stripped_title or '(untitled)'

    @property
    def preview_text(self) -> str:
        return self._preview_first_user or ''

    @property
    def from_compaction(self) -> bool:
        return False

    @property
    def is_empty_helper(self) -> bool:
        # A stage session always names the crew that launched it. Those are
        # not the user's chats, even after Kiro writes a turn into the index.
        if self.parent_session_id:
            return True
        if not (
            self.session_created_reason == 'subagent'
            and self.message_count == 0
            and self.turn_count == 0
        ):
            return False
        # A chat that is still open can have an empty index while the jsonl
        # already holds the prompt. A stage names its parent above, so a
        # parentless transcript is one of the user's own chats.
        try:
            if self.session_file.with_suffix('.jsonl').stat().st_size > 0:
                return False
        except OSError:
            pass
        return True

    @property
    def date(self) -> datetime:
        ms = self.updated_ms or self.date_created or self.created_ms
        if ms:
            try:
                return datetime.fromtimestamp(ms / 1000.0)
            except Exception:
                pass
        try:
            return datetime.fromtimestamp(self.session_file.stat().st_mtime)
        except Exception:
            return datetime.fromtimestamp(0)

    @property
    def size(self) -> int:
        total = 0
        try:
            total += self.session_file.stat().st_size
        except Exception:
            pass
        for extra in (self.session_file.with_suffix('.jsonl'), self.session_file.with_suffix('.history')):
            try:
                if extra.exists():
                    total += extra.stat().st_size
            except Exception:
                pass
        return total


def _cli_session_from_json(fp: Path, data: Dict) -> CliSessionEntry:
    turns = (((data.get('session_state') or {})
              .get('conversation_metadata') or {})
             .get('user_turn_metadatas') or [])
    if not isinstance(turns, list):
        turns = []
    message_count = 0
    for turn in turns:
        ids = turn.get('message_ids') if isinstance(turn, dict) else None
        if isinstance(ids, list):
            message_count += len(ids)
    if not message_count:
        message_count = len(turns)
    created_at = data.get('created_at') or ''
    updated_at = data.get('updated_at') or ''
    created_ms = parse_iso_datetime_ms(created_at)
    updated_ms = parse_iso_datetime_ms(updated_at)
    if not updated_ms:
        try:
            updated_ms = int(fp.stat().st_mtime * 1000)
        except Exception:
            updated_ms = created_ms
    entry = CliSessionEntry(
        sid=data.get('session_id') or fp.stem,
        title=data.get('title') or '',
        created_ms=created_ms,
        updated_ms=updated_ms,
        created_at=created_at,
        updated_at=updated_at,
        cwd=data.get('cwd') or '',
        session_file=fp,
        message_count=message_count,
        turn_count=len(turns),
        reason=data.get('session_created_reason') or '',
    )
    entry.parent_session_id = data.get('parent_session_id') or ''
    return entry


def _parse_cli_session_file(fp: Path) -> Optional[CliSessionEntry]:
    try:
        data = _json_load_path(fp)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return _cli_session_from_json(fp, data)


def _cli_json_paths(id_query: str = '') -> List[Path]:
    """JSON indexes in the CLI session directory.

    `id_query` limits the list to filenames that are that session id or,
    when it is at least 8 characters, start with it. The filename is the id,
    so a lookup does not have to parse every other session.
    """
    if not KIRO_CLI_SESSIONS_DIR.exists():
        return []
    query = (id_query or '').strip().lower()
    paths: List[Path] = []
    try:
        entries = list(os.scandir(KIRO_CLI_SESSIONS_DIR))
    except OSError:
        return []
    for entry in entries:
        name = entry.name
        if not name.endswith('.json'):
            continue
        stem = name[:-5]
        if query:
            stem_l = stem.lower()
            if stem_l != query and not (len(query) >= 8 and stem_l.startswith(query)):
                continue
        paths.append(Path(entry.path))
    return paths


def scan_cli_sessions(id_query: str = '') -> List[CliSessionEntry]:
    """Read Kiro CLI session indexes and return browser-ready entries."""
    paths = _cli_json_paths(id_query)
    if not paths:
        return []
    from concurrent.futures import ThreadPoolExecutor
    workers = min(32, max(1, len(paths)))
    sessions: List[CliSessionEntry] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for entry in pool.map(_parse_cli_session_file, paths, chunksize=32):
            if entry is not None:
                sessions.append(entry)
    sessions.sort(key=lambda s: s.date_created, reverse=True)
    return sessions

# ──────────────────────────────────────────────────────────────
# Session parser — joins shell-history with execution records
# ──────────────────────────────────────────────────────────────
class SessionParser:
    def __init__(self, entry: SessionEntry):
        self.entry = entry
        self.session_id = entry.session_id
        self.workspace_dir = entry.workspace_dir
        self.title = entry.title or "Untitled Session"
        self.date_created = entry.date_created
        self.hidden = entry.hidden

        self.metadata: Dict = {}
        self.data: List[Dict] = []        # chronological merged items

        # Compaction / lineage signals
        self.started_from_compaction: bool = False
        self.compaction_count: int = 0    # number of summarization actions inside
        self.parent_session_ids: List[str] = []
        self.continuation_count: int = entry.continuation_count
        self.summaries: List[str] = []    # raw conversation-summary texts found
        self.had_summary_user_intro: bool = False

        # Stats
        self.user_turn_count: int = 0
        self.credits_used: float = 0.0
        self.execution_count: int = 0
        self.context_usage_pct: float = 0.0
        self.model_titles: Set[str] = set()
        self.autonomy_mode: str = ""
        self.session_type: str = ""

        self._load()

    # ------------------------------------------------------------
    def _load(self):
        try:
            shell = _json_load_path(self.entry.session_file)
        except Exception:
            return
        if not isinstance(shell, dict):
            return

        self.metadata = {
            'sessionId': shell.get('sessionId', self.session_id),
            'title': shell.get('title', self.title),
            'workspacePath': shell.get('workspacePath') or shell.get('workspaceDirectory'),
            'defaultModelTitle': shell.get('defaultModelTitle'),
            'selectedModel': shell.get('selectedModel'),
            'autonomyMode': shell.get('autonomyMode'),
            'sessionType': shell.get('sessionType'),
            'hidden': shell.get('hidden', False),
            'contextUsagePercentage': shell.get('contextUsagePercentage'),
        }
        if isinstance(self.metadata.get('contextUsagePercentage'), (int, float)):
            self.context_usage_pct = float(self.metadata['contextUsagePercentage'])
        self.autonomy_mode = self.metadata.get('autonomyMode') or ''
        self.session_type = self.metadata.get('sessionType') or ''
        if self.metadata.get('selectedModel'):
            self.model_titles.add(str(self.metadata['selectedModel']))

        history = shell.get('history') or []
        if not isinstance(history, list):
            return

        # Walk history pair-by-pair; for each user→assistant pair, attach the
        # matching execution record's actions (if any).
        i = 0
        while i < len(history):
            entry = history[i]
            if not isinstance(entry, dict):
                i += 1; continue
            m = entry.get('message') or {}
            role = m.get('role')

            if role == 'user':
                content = msg_text(m.get('content'))
                hidden = bool(m.get('isHidden'))
                # detect intro Conversation Summary
                if i == 0 and hidden and is_conversation_summary(content):
                    self.started_from_compaction = True
                    self.had_summary_user_intro = True
                    self.summaries.append(content)
                    self.data.append({
                        'type': 'session_event',
                        'event': 'continued_from_compaction',
                        'content': '↻ Session continued from prior compaction summary',
                    })
                    self.data.append({
                        'type': 'summarization',
                        'content': content,
                        'where': 'intro',
                    })
                else:
                    if content.strip():
                        self.user_turn_count += 1
                        self.data.append({
                            'type': 'user_message',
                            'content': content,
                            'hidden': hidden,
                            'idx': i,
                        })

                # If the immediately next entry is an assistant with executionId, replay its actions
                if i + 1 < len(history):
                    nxt = history[i+1]
                    if isinstance(nxt, dict):
                        m2 = nxt.get('message') or {}
                        if m2.get('role') == 'assistant':
                            exec_id = nxt.get('executionId')
                            if exec_id and EXEC_INDEX is not None:
                                fp = EXEC_INDEX.exec_by_id.get(exec_id)
                                if fp is not None:
                                    self._absorb_execution(fp)
                            else:
                                # No executionId — render the assistant stub directly
                                a_text = msg_text(m2.get('content')).strip()
                                if a_text and a_text != 'On it.':
                                    self.data.append({
                                        'type': 'agent_message',
                                        'content': a_text,
                                    })
                            i += 2
                            continue
                i += 1
                continue

            elif role == 'assistant':
                # Orphan assistant message (no preceding user) — emit if non-stub
                a_text = msg_text(m.get('content')).strip()
                if a_text and a_text != 'On it.':
                    self.data.append({
                        'type': 'agent_message',
                        'content': a_text,
                    })
                exec_id = entry.get('executionId')
                if exec_id and EXEC_INDEX is not None:
                    fp = EXEC_INDEX.exec_by_id.get(exec_id)
                    if fp is not None:
                        self._absorb_execution(fp)
                i += 1
                continue
            else:
                i += 1
                continue

        # Some sessions store executions that don't appear in history (race
        # conditions, aborted turns). Pick those up too, sorted by startTime.
        if EXEC_INDEX is not None:
            covered_ids = set()
            for it in self.data:
                eid = it.get('execution_id')
                if eid:
                    covered_ids.add(eid)
            extras = []
            for fp in EXEC_INDEX.exec_by_session.get(self.session_id, []):
                ex = EXEC_INDEX.load_execution(fp)
                if not ex:
                    continue
                if ex.get('executionId') in covered_ids:
                    continue
                extras.append((ex.get('startTime') or 0, fp, ex))
            extras.sort(key=lambda t: t[0])
            for _t, fp, ex in extras:
                self._absorb_execution(fp, prefetched=ex, mark_extra=True)

        # Title resolution rules (per user feedback):
        #   1. Use Kiro's stored title verbatim, sans `(Continued)` /
        #      `(checkpoint)` chain-suffixes (those are surfaced as badges).
        #   2. Only fall back to a derived title when Kiro's title is
        #      *literally* empty or the placeholder "New Session" with no
        #      real content visible.
        raw = (self.title or '').strip()
        stripped = re.sub(r'(\s*\((?:Continued|checkpoint)\))+\s*$', '', raw,
                          flags=re.IGNORECASE).strip()
        # Things Kiro itself emits as a meaningful default — preserve them
        is_placeholder = (not stripped) or stripped.lower() == 'new session'
        if is_placeholder:
            # Look at the first non-hidden user message
            cand = ''
            for it in self.data:
                if it['type'] == 'user_message' and not it.get('hidden'):
                    body = it.get('content', '') or ''
                    first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), '')
                    cand = normalize_title_candidate(first_line)
                    if cand:
                        break
            if not cand:
                # Try the intro Conversation-Summary heading
                for it in self.data:
                    if it['type'] == 'summarization' and it.get('where') == 'intro':
                        body = it.get('content', '') or ''
                        hm = re.search(r'^##\s*(TASK\s*\d+[:.\s][^\n]*)',
                                       body, re.MULTILINE)
                        if hm:
                            cand = normalize_title_candidate(hm.group(1))
                            break
            if not cand:
                # Last resort
                cand = f"Session {self.session_id[:8]}"
            self.title = cand
        else:
            self.title = stripped

    # ------------------------------------------------------------
    def _absorb_execution(self, fp: Path, prefetched: Optional[Dict] = None, mark_extra: bool = False):
        ex = prefetched if prefetched is not None else (EXEC_INDEX.load_execution(fp) if EXEC_INDEX else None)
        if not isinstance(ex, dict):
            return
        self.execution_count += 1
        eid = ex.get('executionId')

        # Track lineage info
        psid = ex.get('parentSessionIds')
        if isinstance(psid, list):
            for p in psid:
                if isinstance(p, str) and p not in self.parent_session_ids and p != self.session_id:
                    self.parent_session_ids.append(p)
        cup = ex.get('contextUsagePercentage')
        if isinstance(cup, (int, float)) and cup > self.context_usage_pct:
            self.context_usage_pct = float(cup)
        usage = ex.get('usageSummary')
        if isinstance(usage, list):
            for u in usage:
                if not isinstance(u, dict):
                    continue
                # Kiro stores credits, not tokens
                if u.get('unit') == 'credit':
                    v = u.get('usage')
                    if isinstance(v, (int, float)):
                        self.credits_used += float(v)
                mt = u.get('modelId') or u.get('model') or u.get('modelTitle')
                if isinstance(mt, str):
                    self.model_titles.add(mt)

        if mark_extra:
            self.data.append({
                'type': 'session_event',
                'event': 'orphan_execution',
                'content': f'(orphan execution {eid[:8] if eid else "?"} not linked from history)',
                'execution_id': eid,
            })

        actions = ex.get('actions') or []
        if not isinstance(actions, list):
            return

        # Build a quick lookup for toolUse arg name → emoji classification later
        for action in actions:
            if not isinstance(action, dict):
                continue
            atype = action.get('actionType')
            section = ACTION_TYPE_TO_SECTION.get(atype, None)
            if section is None and atype != 'model':
                # unknown — categorize as session_event so it's not lost
                section = 'session_event'

            astate = action.get('actionState')
            ainput = action.get('input') or {}
            aoutput = action.get('output') or {}
            raw_input = action.get('rawInput') or {}
            err_msg = action.get('errorMessage')
            emitted_at = action.get('emittedAt')

            if atype == 'model':
                continue  # internal book-keeping

            if section == 'agent_message':
                txt = ''
                if isinstance(aoutput, dict):
                    txt = aoutput.get('message') or aoutput.get('text') or ''
                if not isinstance(txt, str):
                    txt = json.dumps(txt, ensure_ascii=False)
                txt = txt.strip()
                if not txt:
                    continue
                self.data.append({
                    'type': 'agent_message',
                    'content': txt,
                    'execution_id': eid,
                    'state': astate,
                    'ts': emitted_at,
                })
                continue

            if section == 'reasoning':
                txt = ''
                if isinstance(aoutput, dict):
                    txt = aoutput.get('message') or aoutput.get('text') or ''
                if not isinstance(txt, str):
                    txt = json.dumps(txt, ensure_ascii=False)
                if not txt.strip():
                    continue
                self.data.append({
                    'type': 'reasoning',
                    'content': txt.strip(),
                    'execution_id': eid,
                    'ts': emitted_at,
                })
                continue

            if section == 'summarization':
                txt = ''
                if isinstance(aoutput, dict):
                    txt = aoutput.get('content') or aoutput.get('message') or ''
                if isinstance(txt, str) and txt.strip():
                    self.compaction_count += 1
                    self.summaries.append(txt)
                    self.data.append({
                        'type': 'session_event',
                        'event': 'context_compacted',
                        'content': '✂ Context compacted into summary',
                    })
                    self.data.append({
                        'type': 'summarization',
                        'content': txt.strip(),
                        'execution_id': eid,
                        'ts': emitted_at,
                        'where': 'inline',
                    })
                continue

            if section == 'file_read':
                # Various shapes: input.path / input.files[*].path
                files = []
                if isinstance(ainput, dict):
                    if 'files' in ainput and isinstance(ainput['files'], list):
                        for f in ainput['files']:
                            if isinstance(f, dict):
                                rng = f.get('range') or {}
                                files.append({
                                    'path': f.get('path') or '',
                                    'start': rng.get('startLine') or f.get('start_line'),
                                    'end':   rng.get('endLine')   or f.get('end_line'),
                                })
                    elif 'path' in ainput:
                        files.append({
                            'path': ainput.get('path', ''),
                            'start': ainput.get('start_line') or ainput.get('startLine'),
                            'end':   ainput.get('end_line')   or ainput.get('endLine'),
                        })
                content = ''
                if isinstance(aoutput, dict):
                    content = aoutput.get('content') or aoutput.get('message') or ''
                self.data.append({
                    'type': 'file_read',
                    'tool': atype,
                    'state': astate,
                    'files': files,
                    'output': content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                    'execution_id': eid,
                    'error': err_msg,
                    'ts': emitted_at,
                })
                continue

            if section in ('file_create', 'file_edit', 'file_delete'):
                why = (raw_input or {}).get('explanation') or (ainput or {}).get('why') or ''
                self.data.append({
                    'type': section,
                    'tool': atype,
                    'state': astate,
                    'path': (ainput or {}).get('file') or (ainput or {}).get('path') or (raw_input or {}).get('targetFile') or '',
                    'original_content':  (ainput or {}).get('originalContent', ''),
                    'modified_content':  (ainput or {}).get('modifiedContent', ''),
                    'explanation': why,
                    'execution_id': eid,
                    'error': err_msg,
                    'ts': emitted_at,
                })
                continue

            if section == 'terminal_cmd':
                cmd = (ainput or {}).get('command') or (raw_input or {}).get('command') or ''
                cwd = (ainput or {}).get('cwd') or (raw_input or {}).get('cwd') or ''
                explanation = (raw_input or {}).get('explanation') or ''
                out = ''
                exit_code = None
                if isinstance(aoutput, dict):
                    out = aoutput.get('output') or aoutput.get('content') or ''
                    exit_code = aoutput.get('exitCode')
                self.data.append({
                    'type': 'terminal_cmd',
                    'tool': atype,
                    'state': astate,
                    'command': cmd,
                    'cwd': cwd,
                    'explanation': explanation,
                    'execution_id': eid,
                    'error': err_msg,
                    'ts': emitted_at,
                })
                if isinstance(out, str) and (out.strip() or exit_code is not None):
                    self.data.append({
                        'type': 'terminal_output',
                        'tool': atype,
                        'state': astate,
                        'output': out,
                        'exit_code': exit_code,
                        'execution_id': eid,
                        'ts': emitted_at,
                    })
                continue

            if section == 'process_ctrl':
                summary_bits = []
                if atype == 'controlProcess':
                    action_name = (ainput or {}).get('action', '?')
                    pid = (aoutput or {}).get('processId') or ''
                    summary_bits.append(f"{action_name} → pid={pid}")
                    cmd = (ainput or {}).get('command') or ''
                    cwd = (ainput or {}).get('cwd') or ''
                    self.data.append({
                        'type': 'process_ctrl',
                        'tool': atype,
                        'state': astate,
                        'command': cmd,
                        'cwd': cwd,
                        'summary': ' '.join(summary_bits),
                        'execution_id': eid,
                        'error': err_msg,
                        'ts': emitted_at,
                    })
                elif atype == 'getProcessOutput':
                    pid = (ainput or {}).get('processId') or (raw_input or {}).get('terminalId') or ''
                    out = ''
                    if isinstance(aoutput, dict):
                        out = aoutput.get('output') or ''
                    self.data.append({
                        'type': 'process_ctrl',
                        'tool': atype,
                        'state': astate,
                        'summary': f'pid={pid} output',
                        'output': out if isinstance(out, str) else json.dumps(out, ensure_ascii=False),
                        'execution_id': eid,
                        'error': err_msg,
                        'ts': emitted_at,
                    })
                continue

            if section == 'code_search':
                query = (ainput or {}).get('query') or (raw_input or {}).get('query') or ''
                why = (ainput or {}).get('why') or (raw_input or {}).get('explanation') or ''
                msg_out = ''
                if isinstance(aoutput, dict):
                    msg_out = aoutput.get('message') or aoutput.get('content') or ''
                self.data.append({
                    'type': 'code_search',
                    'tool': atype,
                    'state': astate,
                    'query': query,
                    'why': why,
                    'output': msg_out if isinstance(msg_out, str) else json.dumps(msg_out, ensure_ascii=False),
                    'execution_id': eid,
                    'error': err_msg,
                    'ts': emitted_at,
                })
                continue

            if section == 'diagnostics':
                paths = (ainput or {}).get('paths') or []
                summary = {}
                if isinstance(aoutput, dict):
                    summary = aoutput
                count_issues = 0
                if isinstance(summary, dict):
                    for v in summary.values():
                        if isinstance(v, list):
                            count_issues += len(v)
                self.data.append({
                    'type': 'diagnostics',
                    'paths': paths if isinstance(paths, list) else [],
                    'output': summary,
                    'issue_count': count_issues,
                    'state': astate,
                    'execution_id': eid,
                    'error': err_msg,
                    'ts': emitted_at,
                })
                continue

            if section == 'web_search':
                q = (ainput or {}).get('query') or (raw_input or {}).get('query') or ''
                results = []
                if isinstance(aoutput, dict):
                    rs = aoutput.get('results')
                    if isinstance(rs, list):
                        results = rs
                self.data.append({
                    'type': 'web_search',
                    'query': q,
                    'results': results,
                    'state': astate,
                    'execution_id': eid,
                    'error': err_msg,
                    'ts': emitted_at,
                })
                continue

            if section == 'web_fetch':
                url = (ainput or {}).get('url') or (raw_input or {}).get('url') or ''
                mode = (ainput or {}).get('mode') or ''
                content = ''
                status = ''
                if isinstance(aoutput, dict):
                    r = aoutput.get('result')
                    if isinstance(r, dict):
                        content = r.get('content', '')
                        status = r.get('statusCode', '')
                self.data.append({
                    'type': 'web_fetch',
                    'url': url,
                    'mode': mode,
                    'status': status,
                    'content': content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                    'state': astate,
                    'execution_id': eid,
                    'error': err_msg,
                    'ts': emitted_at,
                })
                continue

            if section == 'mcp_tool':
                server = (ainput or {}).get('serverName') or ''
                tool = (ainput or {}).get('toolName') or ''
                args = (ainput or {}).get('toolArgs') or {}
                resp = ''
                if isinstance(aoutput, dict):
                    resp = aoutput.get('response') or aoutput.get('content') or aoutput.get('message') or ''
                self.data.append({
                    'type': 'mcp_tool',
                    'server': server,
                    'tool_name': tool,
                    'arguments': args,
                    'response': resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False),
                    'state': astate,
                    'execution_id': eid,
                    'error': err_msg,
                    'ts': emitted_at,
                })
                continue

            if section == 'sub_agent':
                if atype == 'invokeSubAgent':
                    prompt = (ainput or {}).get('prompt') or ''
                    expl = (ainput or {}).get('explanation') or ''
                    name = (ainput or {}).get('subAgentName') or ''
                    self.data.append({
                        'type': 'sub_agent',
                        'kind': 'invoke',
                        'name': name,
                        'prompt': prompt,
                        'explanation': expl,
                        'state': astate,
                        'execution_id': eid,
                        'error': err_msg,
                        'ts': emitted_at,
                    })
                elif atype in ('subagent_response', 'subagentResponse'):
                    resp = (ainput or {}).get('response') or ''
                    self.data.append({
                        'type': 'sub_agent',
                        'kind': 'response',
                        'response': resp,
                        'state': astate,
                        'execution_id': eid,
                        'ts': emitted_at,
                    })
                elif atype == 'specAgent':
                    self.data.append({
                        'type': 'sub_agent',
                        'kind': 'spec',
                        'output': aoutput,
                        'state': astate,
                        'execution_id': eid,
                        'ts': emitted_at,
                    })
                else:  # analyzeRequirements etc.
                    self.data.append({
                        'type': 'sub_agent',
                        'kind': atype,
                        'input': ainput,
                        'output': aoutput,
                        'state': astate,
                        'execution_id': eid,
                        'error': err_msg,
                        'ts': emitted_at,
                    })
                continue

            if section == 'intent':
                ires = action.get('intentResult') or {}
                cls = ires.get('classification') or ''
                final = ires.get('finalIntent') or {}
                self.data.append({
                    'type': 'intent',
                    'classification': cls,
                    'final': final,
                    'execution_id': eid,
                    'ts': emitted_at,
                })
                continue

            if section == 'error':
                self.data.append({
                    'type': 'error',
                    'message': err_msg or (action.get('output') or {}).get('message', ''),
                    'kind': action.get('errorType') or atype,
                    'execution_id': eid,
                    'ts': emitted_at,
                })
                continue

            if section == 'user_input':
                qs = []
                if isinstance(aoutput, dict):
                    qs = aoutput.get('questions') or []
                self.data.append({
                    'type': 'user_input',
                    'questions': qs,
                    'execution_id': eid,
                    'ts': emitted_at,
                })
                continue

            # Catch-all
            self.data.append({
                'type': 'session_event',
                'event': atype or 'unknown',
                'content': f"({atype}) {astate or ''}".strip(),
                'execution_id': eid,
                'ts': emitted_at,
            })

    # ------------------------------------------------------------
    def get_turn_boundaries(self) -> List[int]:
        return [i for i, item in enumerate(self.data)
                if item['type'] == 'user_message' and not item.get('hidden')]

    def get_turn_count(self) -> int:
        return len(self.get_turn_boundaries())

    def trim_to_last_n_turns(self, n: int):
        """Keep only the last N user turns and everything that followed each.
        If the session started from a compaction summary (intro), prepend that
        summary block so the trimmed export retains its setup context."""
        if n <= 0:
            return
        b = self.get_turn_boundaries()
        if not b or n >= len(b):
            return
        cut = b[-n]
        # Preserve the intro session_event + summarization (if present) so
        # the reader understands the conversation's lineage.
        intro_prefix: List[Dict] = []
        for item in self.data[:cut]:
            if item['type'] == 'session_event' and item.get('event') == 'continued_from_compaction':
                intro_prefix.append(item)
            elif item['type'] == 'summarization' and item.get('where') == 'intro':
                intro_prefix.append(item)
            else:
                # Only the very first prefix matters; stop scanning past the
                # first non-intro item.
                if intro_prefix:
                    break
        self.data = intro_prefix + self.data[cut:]

    def trim_to_live_context(self):
        """Replicate Kiro's compaction logic:
           every `summarization` block CLEARS prior items and keeps only itself
           plus everything that came after it."""
        live: List[Dict] = []
        for item in self.data:
            if item['type'] == 'session_event' and item.get('event') == 'context_compacted':
                # Find the matching summarization right after — drop everything we accumulated.
                live = [item]
            elif item['type'] == 'summarization' and item.get('where') == 'inline':
                live.append(item)
            elif item['type'] == 'summarization' and item.get('where') == 'intro':
                # intro summaries already live at session start — keep them
                live.append(item)
            else:
                live.append(item)
        self.data = live

    # ------------------------------------------------------------
    def count_lines_by_section(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for it in self.data:
            t = it['type']
            counts[t] = counts.get(t, 0) + estimate_lines(it)
        if self.metadata:
            counts['session_meta'] = counts.get('session_meta', 0) + 7
        return counts

    # ------------------------------------------------------------
    def to_markdown(self, section_filter: Optional[Dict[str, bool]] = None,
                    clean_content: bool = False,
                    output_cap: int = 0,
                    user_cap: int = 0,
                    agent_cap: int = 0,
                    reasoning_cap: int = 0,
                    summary_cap: int = 0,
                    orch_filter: Optional[Dict] = None) -> str:
        if section_filter is None:
            section_filter = {s[0]: True for s in SECTION_DEFS}
        if orch_filter is None:
            orch_filter = default_orch_filter()

        def _cap_text(text: str, cap: int = 0) -> str:
            c = cap if cap > 0 else output_cap
            if c <= 0 or not text:
                return text
            lines = text.split('\n')
            if len(lines) <= c:
                return text
            kept = lines[-c:]
            return f'... ({len(lines) - c} lines trimmed) ...\n' + '\n'.join(kept)

        # Per-type "last N" caps (counted from the tail)
        keep_indices = set(range(len(self.data)))
        caps = {
            'user_message': user_cap,
            'agent_message': agent_cap,
            'reasoning': reasoning_cap,
            'summarization': summary_cap,
        }
        counters = {k: 0 for k in caps}
        for i in range(len(self.data) - 1, -1, -1):
            t = self.data[i]['type']
            if t in caps and caps[t] > 0:
                if counters[t] >= caps[t]:
                    keep_indices.discard(i)
                counters[t] += 1

        md: List[str] = []

        # Header — title and compaction badges
        badge_bits = []
        if self.started_from_compaction:
            badge_bits.append('↻ from compaction')
        if self.compaction_count:
            badge_bits.append(f'✂ compacted ×{self.compaction_count}')
        if self.continuation_count:
            badge_bits.append(f'↪ continued ×{self.continuation_count}')
        if self.hidden:
            badge_bits.append('hidden')
        badges = '  '.join(badge_bits)
        title_line = f"# {self.title}"
        if badges:
            title_line += f"   _({badges})_"
        md.append(title_line + '\n')

        if section_filter.get('session_meta', False):
            md.append('```yaml')
            md.append(f"Session ID:   {self.session_id}")
            md.append(f"Workspace:    {self.workspace_dir or '?'}")
            if self.date_created:
                try:
                    dt = datetime.fromtimestamp(self.date_created / 1000.0).strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    dt = str(self.date_created)
                md.append(f"Created:      {dt}")
            if self.model_titles:
                md.append(f"Model(s):     {', '.join(sorted(self.model_titles))}")
            if self.autonomy_mode:
                md.append(f"Autonomy:     {self.autonomy_mode}")
            if self.session_type:
                md.append(f"Session type: {self.session_type}")
            md.append(f"Executions:   {self.execution_count}")
            md.append(f"User turns:   {self.user_turn_count}")
            if self.context_usage_pct:
                md.append(f"Context:      {self.context_usage_pct:.1f}%")
            if self.credits_used:
                md.append(f"Credits:      {self.credits_used:.4f}")
            if self.parent_session_ids:
                md.append(f"Parents:      {len(self.parent_session_ids)} ancestor session(s)")
            md.append('```\n')

        last_rendered_msg = None
        for i, item in enumerate(self.data):
            if i not in keep_indices:
                continue
            t = item['type']
            if not section_filter.get(t, False):
                continue

            if t == 'user_message':
                content = item.get('content', '')
                if clean_content:
                    content = strip_ide_context(content)
                    if not content.strip():
                        continue
                    key = ('user', content)
                    if key == last_rendered_msg:
                        continue
                    last_rendered_msg = key
                md.append(f"## 👤 User\n\n{content}\n")

            elif t == 'agent_message':
                content = item.get('content', '').strip()
                if not content:
                    continue
                if clean_content:
                    key = ('agent', content)
                    if key == last_rendered_msg:
                        continue
                    last_rendered_msg = key
                md.append(f"## 🤖 Agent\n\n{content}\n")

            elif t == 'reasoning':
                content = item.get('content', '').strip()
                if not content:
                    continue
                md.append(f"> 🧠 **Reasoning**\n>\n> " + content.replace('\n', '\n> ') + "\n")

            elif t == 'summarization':
                content = item.get('content', '').strip()
                if not content:
                    continue
                where = item.get('where', 'inline')
                heading = '## ✂️ Compaction Summary (intro)' if where == 'intro' else '## ✂️ Compaction Summary'
                md.append(f"{heading}\n\n{content}\n")

            elif t == 'file_read':
                files = item.get('files') or []
                err = item.get('error')
                tool = item.get('tool', 'readFile')
                state = item.get('state', '')
                if files:
                    bullets = []
                    for f in files:
                        line = f.get('path', '?')
                        if f.get('start') is not None and f.get('end') is not None:
                            line += f"  [{f['start']}–{f['end']}]"
                        bullets.append(f"- `{line}`")
                    head = f"### 📖 Read ({tool})"
                    if err:
                        head += f"  *— failed: {err.splitlines()[0][:80]}*"
                    md.append(head + "\n\n" + "\n".join(bullets) + "\n")
                out = item.get('output', '')
                if isinstance(out, str) and out.strip():
                    out = _cap_text(out)
                    md.append(f"```text\n{out}\n```\n")

            elif t == 'file_create':
                path = item.get('path', '?')
                content = item.get('modified_content') or ''
                why = item.get('explanation') or ''
                state = item.get('state', '')
                err = item.get('error')
                head = f"### 🆕 Create `{path}`"
                if err:
                    head += f"  *— failed: {err.splitlines()[0][:80]}*"
                if state and state not in ('Accepted', 'Success'):
                    head += f"  *— {state}*"
                md.append(head)
                if why:
                    md.append(f"\n> {why}\n")
                if isinstance(content, str) and content:
                    lang = guess_lang(path)
                    md.append(f"\n```{lang}\n{_cap_text(content)}\n```\n")

            elif t == 'file_edit':
                path = item.get('path', '?')
                why = item.get('explanation') or ''
                state = item.get('state', '')
                tool = item.get('tool', 'edit')
                orig = item.get('original_content') or ''
                modf = item.get('modified_content') or ''
                err = item.get('error')
                head = f"### ✏️ Edit `{path}`  *(via {tool})*"
                if err:
                    head += f"  *— failed: {err.splitlines()[0][:80]}*"
                if state and state not in ('Accepted', 'Success'):
                    head += f"  *— {state}*"
                md.append(head)
                if why:
                    md.append(f"\n> {why}\n")
                # Render a compact diff when both sides exist
                if isinstance(modf, str) and modf:
                    lang = guess_lang(path)
                    md.append(f"\n**Modified:**\n\n```{lang}\n{_cap_text(modf)}\n```\n")
                elif isinstance(orig, str) and orig:
                    md.append(f"\n_(modified content omitted)_\n")

            elif t == 'file_delete':
                path = item.get('path', '?')
                why = item.get('explanation') or ''
                state = item.get('state', '')
                err = item.get('error')
                head = f"### 🗑️ Delete `{path}`"
                if err:
                    head += f"  *— failed: {err.splitlines()[0][:80]}*"
                elif state and state not in ('Accepted', 'Success'):
                    head += f"  *— {state}*"
                md.append(head + ("\n\n> " + why + "\n" if why else "\n"))

            elif t == 'terminal_cmd':
                cmd = item.get('command', '')
                cwd = item.get('cwd', '')
                why = item.get('explanation', '')
                tool = item.get('tool', 'runCommand')
                state = item.get('state', '')
                err = item.get('error')
                head = f"### 💻 {tool}"
                if state and state not in ('Accepted', 'Success'):
                    head += f"  *({state})*"
                if err:
                    head += f"  *— failed: {err.splitlines()[0][:80]}*"
                md.append(head)
                if why:
                    md.append(f"\n> {why}")
                if cwd:
                    md.append(f"\n> `cwd: {cwd}`")
                md.append(f"\n```powershell\n{cmd}\n```\n" if _IS_WINDOWS else f"\n```bash\n{cmd}\n```\n")

            elif t == 'terminal_output':
                out = item.get('output', '')
                ec = item.get('exit_code')
                if not isinstance(out, str):
                    out = json.dumps(out, ensure_ascii=False, indent=2)
                trimmed = _cap_text(out)
                head = "**Output:**"
                if ec is not None:
                    head += f"  *(exit {ec})*"
                if trimmed.strip():
                    md.append(f"{head}\n\n```text\n{trimmed}\n```\n")

            elif t == 'process_ctrl':
                tool = item.get('tool', '?')
                summary = item.get('summary', '')
                cmd = item.get('command', '')
                out = item.get('output', '')
                err = item.get('error')
                head = f"### ⚙️ Process — `{tool}` {summary}"
                if err:
                    head += f"  *— failed: {err.splitlines()[0][:80]}*"
                md.append(head)
                if cmd:
                    md.append(f"\n```text\n{cmd}\n```")
                if isinstance(out, str) and out.strip():
                    md.append(f"\n**Output:**\n\n```text\n{_cap_text(out)}\n```")
                md.append('')

            elif t == 'code_search':
                q = item.get('query', '')
                why = item.get('why', '')
                out = item.get('output', '')
                err = item.get('error')
                head = f"### 🔎 Search: `{q}`"
                if err:
                    head += f"  *— failed: {err.splitlines()[0][:80]}*"
                md.append(head)
                if why:
                    md.append(f"\n> {why}")
                if isinstance(out, str) and out.strip():
                    md.append(f"\n```text\n{_cap_text(out)}\n```")
                md.append('')

            elif t == 'diagnostics':
                paths = item.get('paths') or []
                ic = item.get('issue_count', 0)
                md.append(f"> 🩺 **Diagnostics:** {len(paths)} file(s), {ic} issue(s)\n")

            elif t == 'web_search':
                q = item.get('query', '')
                results = item.get('results') or []
                md.append(f"### 🌐 Web Search: `{q}`")
                bullets = []
                for r in results[:10]:
                    if isinstance(r, dict):
                        bullets.append(f"- [{r.get('title','(no title)')}]({r.get('url','')})")
                if bullets:
                    md.append('\n' + '\n'.join(bullets))
                md.append('')

            elif t == 'web_fetch':
                url = item.get('url', '')
                content = item.get('content', '')
                md.append(f"### 🔗 Web Fetch: <{url}>")
                if isinstance(content, str) and content.strip():
                    md.append(f"\n```text\n{_cap_text(content)}\n```")
                md.append('')

            elif t == 'mcp_tool':
                server = item.get('server', '?')
                tool = item.get('tool_name', '?')
                args = item.get('arguments') or {}
                resp = item.get('response', '')
                md.append(f"### 🔌 MCP: `{server}::{tool}`")
                if args:
                    md.append(f"\n```json\n{json.dumps(args, indent=2, ensure_ascii=False)}\n```")
                if isinstance(resp, str) and resp.strip():
                    md.append(f"\n**Response:**\n\n```text\n{_cap_text(resp)}\n```")
                md.append('')

            elif t == 'orchestration':
                heavy = None
                if any((orch_filter.get('layers') or {}).get(key) for key in ORCH_HEAVY_LAYERS):
                    heavy = load_orchestration_heavy(item, orch_filter, output_cap)
                block = render_orchestration(item, orch_filter, output_cap, heavy)
                if block.strip():
                    md.append(block)

            elif t == 'sub_agent':
                kind = item.get('kind', '')
                if kind == 'invoke':
                    name = item.get('name', '')
                    expl = item.get('explanation', '')
                    prompt = item.get('prompt', '')
                    md.append(f"### 🧩 Sub-Agent → `{name}`")
                    if expl:
                        md.append(f"\n> {expl}")
                    if prompt:
                        md.append(f"\n```text\n{_cap_text(prompt)}\n```")
                    md.append('')
                elif kind == 'response':
                    resp = item.get('response', '')
                    md.append(f"### 🧩 Sub-Agent Response\n\n{resp}\n")
                elif kind == 'spec':
                    md.append(f"> 🧩 **Spec Agent** invoked\n")
                else:
                    md.append(f"### 🧩 Sub-Agent: `{kind}`\n")

            elif t == 'intent':
                cls = item.get('classification', '?')
                md.append(f"> 🎯 **Intent:** {cls}\n")

            elif t == 'error':
                msg = item.get('message', '')
                k   = item.get('kind', '')
                md.append(f"> ❗ **Error** ({k}): {msg}\n")

            elif t == 'user_input':
                qs = item.get('questions') or []
                for q in qs:
                    if isinstance(q, dict):
                        question = q.get('question', '')
                        ans = (q.get('response') or {}).get('answer') if isinstance(q.get('response'), dict) else ''
                        md.append(f"> ❓ **Q:** {question}\n>\n> **A:** {ans}\n")

            elif t == 'session_event':
                md.append(f"> 🔔 **{item.get('content', item.get('event', ''))}**\n")

        return "\n".join(md)


class CliSessionParser:
    """Parser for Kiro CLI session files.

    `<session>.json` is only a compact session index. The detailed transcript
    lives beside it as `<session>.jsonl`, with Prompt / AssistantMessage /
    ToolResults / Compaction events. Prefer JSONL so file writes, reads,
    commands, MCP calls, web activity, and task/subagent records are visible.
    """

    def __init__(self, entry: CliSessionEntry):
        self.entry = entry
        self.session_id = entry.session_id
        self.workspace_dir = entry.workspace_dir
        self.title = entry.title or "Untitled CLI Session"
        self.date_created = entry.created_ms or entry.date_created
        self.hidden = False

        self.metadata: Dict = {}
        self.data: List[Dict] = []

        self.started_from_compaction = False
        self.compaction_count = 0
        self.continuation_count = 0
        self.parent_session_ids: List[str] = []

        self.model_titles: Set[str] = set()
        self.autonomy_mode: Optional[str] = None
        self.session_type: Optional[str] = "Kiro CLI"
        self.user_turn_count = entry.turn_count
        self.execution_count = 0
        self.context_usage_pct = 0.0
        self.credits_used = 0.0
        self._pending_tool_uses: Dict[str, Dict] = {}
        self._pending_item_idx: Dict[str, int] = {}
        self._orch_turn = 0

        self._load()

    @staticmethod
    def _json_text(value, indent: int = 2) -> str:
        if value is None:
            return ''
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, indent=indent)
        except Exception:
            return str(value)

    @staticmethod
    def _tool_args(tool_input) -> Tuple[Dict, str]:
        if isinstance(tool_input, dict):
            purpose = tool_input.get('__tool_use_purpose') or ''
            args = {k: v for k, v in tool_input.items() if k != '__tool_use_purpose'}
            return args, purpose
        return {'input': tool_input}, ''

    @staticmethod
    def _error_kind(err) -> str:
        if isinstance(err, dict):
            stream = err.get('Stream')
            if isinstance(stream, dict):
                kind = stream.get('kind')
                if isinstance(kind, dict) and kind.get('kind'):
                    return str(kind.get('kind'))
                if kind:
                    return str(kind)
            return next(iter(err.keys()), 'cli_error')
        return 'cli_error'

    @classmethod
    def _error_message(cls, err) -> str:
        if isinstance(err, dict):
            stream = err.get('Stream')
            if isinstance(stream, dict):
                kind = stream.get('kind')
                if isinstance(kind, dict) and kind.get('kind') == 'interrupted':
                    return 'CLI stream interrupted.'
        return cls._json_text(err)

    def _append_item(self, item: Dict, tool_use_id: Optional[str] = None):
        idx = len(self.data)
        self.data.append(item)
        if tool_use_id:
            self._pending_item_idx[tool_use_id] = idx

    def _jsonl_path(self) -> Path:
        return self.entry.session_file.with_suffix('.jsonl')

    @classmethod
    def _result_items_to_text(cls, items) -> str:
        parts: List[str] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if 'Text' in item:
                parts.append(str(item.get('Text') or ''))
            elif 'Json' in item:
                parts.append(cls._json_text(item.get('Json')))
            elif 'Error' in item:
                parts.append(cls._json_text(item.get('Error')))
            else:
                parts.append(cls._json_text(item))
        return '\n'.join(p for p in parts if p)

    @staticmethod
    def _first_json_item(items):
        for item in items or []:
            if isinstance(item, dict) and isinstance(item.get('Json'), dict):
                return item.get('Json')
        return None

    @staticmethod
    def _builtin_tool(result_obj) -> Tuple[str, Dict, str]:
        if not isinstance(result_obj, dict):
            return '', {}, ''
        tool = result_obj.get('tool') or {}
        if not isinstance(tool, dict):
            return '', {}, ''
        purpose = tool.get('tool_use_purpose') or ''
        kind = tool.get('kind') or {}
        if isinstance(kind, dict) and isinstance(kind.get('BuiltIn'), dict):
            built = kind.get('BuiltIn') or {}
            if built:
                name = next(iter(built.keys()))
                params = built.get(name) if isinstance(built.get(name), dict) else {}
                return name, params or {}, purpose
        return '', {}, purpose

    @staticmethod
    def _mcp_tool(result_obj) -> Tuple[str, str, Dict, str]:
        if not isinstance(result_obj, dict):
            return '', '', {}, ''
        tool = result_obj.get('tool') or {}
        if not isinstance(tool, dict):
            return '', '', {}, ''
        purpose = tool.get('tool_use_purpose') or ''
        kind = tool.get('kind') or {}
        if isinstance(kind, dict) and isinstance(kind.get('Mcp'), dict):
            mcp = kind.get('Mcp') or {}
            return (
                mcp.get('serverName') or 'mcp',
                mcp.get('toolName') or 'tool',
                mcp.get('params') or {},
                purpose,
            )
        return '', '', {}, purpose

    @classmethod
    def _result_payload(cls, result_obj) -> Tuple[str, List[Dict], str]:
        if isinstance(result_obj, str):
            result_obj = {'result': result_obj}
        elif not isinstance(result_obj, dict):
            return '', [], ''
        result = result_obj.get('result') or {}
        if isinstance(result, dict) and isinstance(result.get('Success'), dict):
            items = result.get('Success', {}).get('items') or []
            return 'Success', items, ''
        if isinstance(result, dict) and isinstance(result.get('Error'), dict):
            err = result.get('Error') or {}
            items = err.get('items') or []
            return 'Error', items, cls._json_text(err)
        return '', [], cls._json_text(result)

    @staticmethod
    def _read_files_from_ops(ops) -> List[Dict]:
        files = []
        for op in ops or []:
            if not isinstance(op, dict):
                continue
            files.append({
                'path': op.get('path') or '?',
                'start': op.get('offset'),
                'end': None,
            })
        return files

    def _append_file_write(self, args: Dict, purpose: str, execution_id: str,
                           state: str = 'Requested', error: str = ''):
        command = str(args.get('command') or '').lower()
        path = args.get('path') or args.get('file_path') or '?'
        if command in ('delete', 'remove', 'rm', 'unlink'):
            self._append_item({
                'type': 'file_delete',
                'path': path,
                'explanation': purpose,
                'state': state,
                'error': error,
                'execution_id': execution_id,
            }, execution_id)
            return
        if command in ('strreplace', 'replace', 'edit', 'insert', 'append', 'update'):
            self._append_item({
                'type': 'file_edit',
                'path': path,
                'tool': f"write:{command or 'edit'}",
                'original_content': args.get('oldStr') or args.get('old') or '',
                'modified_content': args.get('newStr') or args.get('new') or args.get('content') or '',
                'explanation': purpose,
                'state': state,
                'error': error,
                'execution_id': execution_id,
            }, execution_id)
            return
        self._append_item({
            'type': 'file_create',
            'path': path,
            'modified_content': args.get('content') or args.get('newStr') or '',
            'explanation': purpose,
            'state': state,
            'error': error,
            'execution_id': execution_id,
        }, execution_id)

    def _load(self):
        data = _json_load_path(self.entry.session_file)
        self.metadata = {
            'source': 'Kiro CLI',
            'session_id': self.session_id,
            'cwd': self.workspace_dir,
            'created_at': self.entry.created_at,
            'updated_at': self.entry.updated_at,
            'session_created_reason': self.entry.session_created_reason,
            'message_count': self.entry.message_count,
            'turn_count': self.entry.turn_count,
        }
        reason = self.entry.session_created_reason
        if reason:
            self.session_type = f"Kiro CLI ({reason})"

        jsonl_path = self._jsonl_path()
        if jsonl_path.exists():
            self.metadata['event_stream'] = str(jsonl_path)
            try:
                self.metadata['event_stream_size'] = format_size(jsonl_path.stat().st_size)
            except Exception:
                pass
            self._load_jsonl(jsonl_path)
            return

        title = (self.title or '').strip()
        if title:
            self.data.append({
                'type': 'user_message',
                'content': f"Kiro CLI session title / first prompt:\n\n{title}",
                'hidden': False,
            })

        turns = (((data.get('session_state') or {})
                  .get('conversation_metadata') or {})
                 .get('user_turn_metadatas') or [])
        if not isinstance(turns, list):
            turns = []
        self.user_turn_count = len(turns)

        for i, turn in enumerate(turns, start=1):
            if not isinstance(turn, dict):
                continue
            self.execution_count += int(turn.get('builtin_tool_uses') or 0)
            ctx = turn.get('context_usage_percentage')
            if isinstance(ctx, (int, float)):
                self.context_usage_pct = max(self.context_usage_pct, float(ctx))
            for usage in turn.get('metering_usage') or []:
                if isinstance(usage, dict) and isinstance(usage.get('value'), (int, float)):
                    self.credits_used += float(usage.get('value') or 0)

            result = turn.get('result') or {}
            if isinstance(result, dict) and 'Err' in result:
                err = result.get('Err')
                self.data.append({
                    'type': 'error',
                    'kind': self._error_kind(err),
                    'message': self._error_message(err),
                    'execution_id': f"cli-turn-{i}",
                })
                continue

            ok = result.get('Ok') if isinstance(result, dict) else None
            if not isinstance(ok, dict):
                continue
            role = ok.get('role') or 'assistant'
            for block in ok.get('content') or []:
                if not isinstance(block, dict):
                    continue
                kind = block.get('kind')
                block_data = block.get('data')
                if kind == 'text':
                    text = block_data if isinstance(block_data, str) else self._json_text(block_data)
                    if text and text.strip():
                        self.data.append({
                            'type': 'user_message' if role == 'user' else 'agent_message',
                            'content': text,
                            'hidden': False,
                            'execution_id': f"cli-turn-{i}",
                        })
                elif kind == 'thinking':
                    text = ''
                    if isinstance(block_data, dict):
                        text = block_data.get('text') or ''
                        model = block_data.get('modelId') or block_data.get('model_id')
                        if model:
                            self.model_titles.add(str(model))
                    else:
                        text = self._json_text(block_data)
                    if text and text.strip():
                        self.data.append({
                            'type': 'reasoning',
                            'content': text,
                            'execution_id': f"cli-turn-{i}",
                        })
                elif kind == 'toolUse':
                    self._append_tool_use(block_data, i)
                elif kind:
                    self.data.append({
                        'type': 'session_event',
                        'event': str(kind),
                        'content': f"CLI content block: {kind}",
                        'execution_id': f"cli-turn-{i}",
                    })

    def _load_jsonl(self, jsonl_path: Path):
        prompt_count = 0
        assistant_count = 0
        line_count = 0
        self._orch_turn = 0
        with open(jsonl_path, 'rb') as f:
            for line_count, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _orjson.loads(line) if _orjson is not None else json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                kind = obj.get('kind')
                data = obj.get('data') or {}
                if not isinstance(data, dict):
                    continue
                if kind == 'Prompt':
                    prompt_count += 1
                    self._orch_turn = prompt_count
                    raw_content = data.get('content')
                    content = msg_text(raw_content if isinstance(raw_content, (str, list)) else [])
                    if content.strip():
                        self._append_item({
                            'type': 'user_message',
                            'content': content,
                            'hidden': False,
                            'execution_id': data.get('message_id') or f"cli-prompt-{prompt_count}",
                        })
                elif kind == 'AssistantMessage':
                    assistant_count += 1
                    blocks = data.get('content')
                    if not isinstance(blocks, list):
                        blocks = []
                    self._append_cli_message_blocks(
                        blocks,
                        data.get('message_id') or f"cli-assistant-{assistant_count}",
                        role='assistant',
                    )
                elif kind == 'ToolResults':
                    results = data.get('results') or {}
                    if isinstance(results, dict):
                        for tool_use_id, result_obj in results.items():
                            self._append_tool_result(tool_use_id, result_obj)
                elif kind == 'Compaction':
                    summary = data.get('summary') or data.get('content') or self._json_text(data)
                    self.compaction_count += 1
                    self._append_item({
                        'type': 'session_event',
                        'event': 'context_compacted',
                        'content': 'Context compacted',
                        'execution_id': f"cli-compaction-{self.compaction_count}",
                    })
                    if summary and str(summary).strip():
                        self._append_item({
                            'type': 'summarization',
                            'where': 'inline',
                            'content': str(summary),
                            'execution_id': f"cli-compaction-{self.compaction_count}",
                        })
                else:
                    self._append_item({
                        'type': 'session_event',
                        'event': str(kind or 'unknown'),
                        'content': f"CLI event: {kind or 'unknown'}",
                    })
        self.user_turn_count = prompt_count or self.entry.turn_count
        self.metadata['jsonl_events'] = line_count
        crew_count = sum(1 for item in self.data if item.get('type') == 'orchestration')
        if crew_count:
            print(Style.info(f"Linking stages across {crew_count} orchestration(s)..."))
            enrich_orchestrations(self)
            linked = sum(
                1 for item in self.data if item.get('type') == 'orchestration'
                for stage in item.get('stages') or [] if stage.get('session_id')
            )
            print(Style.info(f"Linked {linked} stage session(s)."))
        if not self.data and self.title:
            self._append_item({
                'type': 'user_message',
                'content': f"Kiro CLI session title / first prompt:\n\n{self.title}",
                'hidden': False,
            })

    def _append_cli_message_blocks(self, blocks, execution_id: str, role: str = 'assistant'):
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get('kind')
            block_data = block.get('data')
            if kind == 'text':
                text = block_data if isinstance(block_data, str) else self._json_text(block_data)
                if text and text.strip():
                    self._append_item({
                        'type': 'user_message' if role == 'user' else 'agent_message',
                        'content': text,
                        'hidden': False,
                        'execution_id': execution_id,
                    })
            elif kind == 'thinking':
                text = ''
                if isinstance(block_data, dict):
                    text = block_data.get('text') or ''
                    model = block_data.get('modelId') or block_data.get('model_id')
                    if model:
                        self.model_titles.add(str(model))
                else:
                    text = self._json_text(block_data)
                if text and text.strip():
                    self._append_item({
                        'type': 'reasoning',
                        'content': text,
                        'execution_id': execution_id,
                    })
            elif kind == 'toolUse':
                self._append_tool_use(block_data, 0)
            elif kind:
                self._append_item({
                    'type': 'session_event',
                    'event': str(kind),
                    'content': f"CLI content block: {kind}",
                    'execution_id': execution_id,
                })

    def _append_orchestration(self, args: Dict, purpose: str, execution_id: str):
        """Record a crew at the moment the lead agent launches it."""
        specs = []
        for stage in args.get('stages') or []:
            if not isinstance(stage, dict):
                continue
            specs.append({
                'name': stage.get('name') or '(unnamed)',
                'role': stage.get('role') or '',
                'model': stage.get('model') or '',
                'brief': stage.get('prompt_template') or stage.get('prompt') or '',
            })
        self._append_item({
            'type': 'orchestration',
            'tool_use_id': execution_id,
            'purpose': purpose or '',
            'task': args.get('task') or args.get('prompt') or '',
            'mode': args.get('mode') or '',
            'crew_status': 'running',
            'status_detail': '',
            'failed_stage': '',
            'pipeline_text': '',
            'turn': int(self._orch_turn or 1),
            'stage_specs': specs,
            'stages': [],
            'execution_id': execution_id,
        }, execution_id)

    def _finalize_orchestration(self, item: Dict, result_obj):
        """Attach the crew outcome without moving the block in the transcript."""
        result = (result_obj or {}).get('result') if isinstance(result_obj, dict) else None
        item['pipeline_text'] = ''
        item['status_detail'] = ''
        item['failed_stage'] = ''
        if isinstance(result, str):
            label = result.strip().lower()
            item['crew_status'] = label if label in ('completed', 'cancelled', 'error', 'running') else 'error'
            if label == 'cancelled':
                item['crew_status'] = 'cancelled'
            item['status_detail'] = result.strip()
            return
        if isinstance(result, dict) and isinstance(result.get('Success'), dict):
            item['crew_status'] = 'completed'
            items = result['Success'].get('items') or []
            item['pipeline_text'] = self._result_items_to_text(items)
            return
        if isinstance(result, dict) and 'Error' in result:
            item['crew_status'] = 'error'
            err = result.get('Error')
            if isinstance(err, dict):
                custom = err.get('Custom')
                if isinstance(custom, str) and custom.strip():
                    detail = custom
                else:
                    detail = self._json_text(custom if custom is not None else err)
            else:
                detail = '' if err is None else str(err)
            if not isinstance(detail, str):
                detail = self._json_text(detail)
            item['status_detail'] = detail
            match = _FAILED_STAGE_RE.search(detail)
            if match:
                item['failed_stage'] = match.group(1)
            return
        item['crew_status'] = 'error'
        item['status_detail'] = self._json_text(result)[:500]

    def _append_tool_result(self, tool_use_id: str, result_obj):
        # A bare "Cancelled" is the whole result object, not a dict around it.
        if isinstance(result_obj, str):
            result_obj = {'result': result_obj}
        idx = self._pending_item_idx.get(tool_use_id)
        if idx is not None and 0 <= idx < len(self.data) and self.data[idx].get('type') == 'orchestration':
            self._finalize_orchestration(self.data[idx], result_obj)
            return
        if not isinstance(result_obj, dict):
            return
        status, items, error = self._result_payload(result_obj)
        built_name, params, purpose = self._builtin_tool(result_obj)
        server, mcp_name, mcp_params, mcp_purpose = self._mcp_tool(result_obj)
        text = self._result_items_to_text(items)
        json_item = self._first_json_item(items)
        pending = self._pending_tool_uses.get(tool_use_id) or {}
        idx = self._pending_item_idx.get(tool_use_id)

        if idx is not None and 0 <= idx < len(self.data):
            item = self.data[idx]
            if status == 'Error':
                item['state'] = 'Error'
                item['error'] = error or text
            elif status:
                item['state'] = status
            if item.get('type') == 'mcp_tool' and text:
                item['response'] = text
            elif item.get('type') == 'sub_agent' and text and item.get('kind') != 'response':
                self._append_item({
                    'type': 'sub_agent',
                    'kind': 'response',
                    'response': text,
                    'execution_id': tool_use_id,
                })

        if built_name == 'ExecuteCmd':
            if idx is None:
                self._append_item({
                    'type': 'terminal_cmd',
                    'tool': 'shell',
                    'command': params.get('command') or '',
                    'cwd': params.get('working_dir') or self.workspace_dir,
                    'explanation': purpose,
                    'state': status or 'Finished',
                    'error': error,
                    'execution_id': tool_use_id,
                }, tool_use_id)
            out = ''
            exit_code = None
            if isinstance(json_item, dict):
                out = json_item.get('stdout') or ''
                stderr = json_item.get('stderr') or ''
                if stderr:
                    out = (out + '\n' if out else '') + stderr
                exit_status = str(json_item.get('exit_status') or '')
                m = re.search(r'(-?\d+)', exit_status)
                if m:
                    try:
                        exit_code = int(m.group(1))
                    except Exception:
                        exit_code = None
            if not out:
                out = text
            self._append_item({
                'type': 'terminal_output',
                'output': out,
                'exit_code': exit_code,
                'execution_id': tool_use_id,
            })
            return

        if built_name == 'FileRead':
            self._append_item({
                'type': 'file_read',
                'tool': 'read',
                'files': self._read_files_from_ops(params.get('operations') or []),
                'output': text,
                'state': status or 'Finished',
                'error': error,
                'execution_id': tool_use_id,
            })
            return

        if built_name == 'FileWrite':
            if idx is None:
                self._append_file_write(params, purpose, tool_use_id, status or 'Finished', error)
            return

        if built_name in ('Grep', 'Glob', 'Code'):
            query = params.get('pattern') or params.get('query') or params.get('operation') or built_name
            if idx is not None and 0 <= idx < len(self.data) and self.data[idx].get('type') == 'code_search':
                self.data[idx]['output'] = text
                self.data[idx]['error'] = error
                if query:
                    self.data[idx]['query'] = str(query)
            else:
                self._append_item({
                    'type': 'code_search',
                    'query': str(query),
                    'why': purpose,
                    'output': text,
                    'error': error,
                    'execution_id': tool_use_id,
                })
            return

        if built_name == 'WebSearch':
            results = []
            if isinstance(json_item, dict):
                results = json_item.get('results') or []
            if idx is not None and 0 <= idx < len(self.data) and self.data[idx].get('type') == 'web_search':
                self.data[idx]['results'] = results
                self.data[idx]['error'] = error
                self.data[idx]['query'] = params.get('query') or self.data[idx].get('query') or ''
            else:
                self._append_item({
                    'type': 'web_search',
                    'query': params.get('query') or pending.get('args', {}).get('query') or '',
                    'results': results,
                    'error': error,
                    'execution_id': tool_use_id,
                })
            return

        if built_name == 'WebFetch':
            if idx is not None and 0 <= idx < len(self.data) and self.data[idx].get('type') == 'web_fetch':
                self.data[idx]['content'] = text
                self.data[idx]['error'] = error
                self.data[idx]['url'] = params.get('url') or self.data[idx].get('url') or ''
                self.data[idx]['mode'] = params.get('mode') or self.data[idx].get('mode') or ''
            else:
                self._append_item({
                    'type': 'web_fetch',
                    'url': params.get('url') or pending.get('args', {}).get('url') or '',
                    'mode': params.get('mode') or '',
                    'content': text,
                    'error': error,
                    'execution_id': tool_use_id,
                })
            return

        if built_name in ('Task', 'AgentCrew'):
            if idx is None:
                self._append_item({
                    'type': 'sub_agent',
                    'kind': 'response',
                    'response': text,
                    'execution_id': tool_use_id,
                })
            return

        if server or mcp_name:
            if idx is None:
                self._append_item({
                    'type': 'mcp_tool',
                    'server': server or 'mcp',
                    'tool_name': mcp_name or 'tool',
                    'arguments': mcp_params,
                    'response': text,
                    'state': status or 'Finished',
                    'error': error,
                    'execution_id': tool_use_id,
                }, tool_use_id)
            return

        if text and idx is None:
            self._append_item({
                'type': 'session_event',
                'event': built_name or pending.get('name') or 'toolResult',
                'content': text[:2000],
                'execution_id': tool_use_id,
            })

    def _append_tool_use(self, block_data, turn_index: int):
        if not isinstance(block_data, dict):
            self.data.append({
                'type': 'session_event',
                'event': 'toolUse',
                'content': self._json_text(block_data),
                'execution_id': f"cli-turn-{turn_index}",
            })
            return

        name = str(block_data.get('name') or 'tool')
        args, purpose = self._tool_args(block_data.get('input'))
        execution_id = block_data.get('toolUseId') or f"cli-turn-{turn_index}"
        self._pending_tool_uses[execution_id] = {
            'name': name,
            'args': args,
            'purpose': purpose,
        }

        if name == 'shell':
            command = args.get('command') or ''
            self._append_item({
                'type': 'terminal_cmd',
                'tool': 'shell',
                'command': self._json_text(command, indent=0),
                'cwd': args.get('cwd') or args.get('working_dir') or self.workspace_dir,
                'explanation': purpose,
                'state': 'Requested',
                'execution_id': execution_id,
            }, execution_id)
            return

        if name == 'write':
            self._append_file_write(args, purpose, execution_id)
            return

        if name == 'read':
            # The ToolResults event carries the actual file content. Keep this
            # pending so FileRead can render as one read block with output.
            return

        if name in ('grep', 'glob', 'code'):
            query = args.get('pattern') or args.get('query') or args.get('operation') or name
            self._append_item({
                'type': 'code_search',
                'query': str(query),
                'why': purpose,
                'output': '',
                'execution_id': execution_id,
            }, execution_id)
            return

        if name == 'web_search':
            self._append_item({
                'type': 'web_search',
                'query': args.get('query') or '',
                'results': [],
                'execution_id': execution_id,
            }, execution_id)
            return

        if name == 'web_fetch':
            self._append_item({
                'type': 'web_fetch',
                'url': args.get('url') or '',
                'mode': args.get('mode') or '',
                'content': '',
                'execution_id': execution_id,
            }, execution_id)
            return

        if name == 'subagent' and isinstance(args.get('stages'), list):
            self._append_orchestration(args, purpose, execution_id)
            return

        if name == 'subagent':
            self._append_item({
                'type': 'sub_agent',
                'kind': 'invoke',
                'name': args.get('agent') or args.get('mode') or 'subagent',
                'prompt': args.get('task') or args.get('prompt') or '',
                'explanation': purpose,
                'execution_id': execution_id,
            }, execution_id)
            return

        if name == 'todo_list':
            command = args.get('command') or 'update'
            update = args.get('context_update') or self._json_text(args)
            self._append_item({
                'type': 'session_event',
                'event': 'todo_list',
                'content': f"todo_list {command}: {update}",
                'execution_id': execution_id,
            }, execution_id)
            return

        server = 'obsidian' if name.startswith('vault_') else 'kiro-cli'
        self._append_item({
            'type': 'mcp_tool',
            'server': server,
            'tool_name': name,
            'arguments': args,
            'response': '',
            'state': 'Requested',
            'execution_id': execution_id,
        }, execution_id)

    get_turn_boundaries = SessionParser.get_turn_boundaries
    get_turn_count = SessionParser.get_turn_count
    trim_to_last_n_turns = SessionParser.trim_to_last_n_turns
    trim_to_live_context = SessionParser.trim_to_live_context
    count_lines_by_section = SessionParser.count_lines_by_section
    to_markdown = SessionParser.to_markdown


# ──────────────────────────────────────────────────────────────
# Heuristics
# ──────────────────────────────────────────────────────────────
EXT_LANG = {
    '.py': 'python', '.js': 'javascript', '.ts': 'typescript', '.tsx': 'tsx', '.jsx': 'jsx',
    '.go': 'go', '.rs': 'rust', '.java': 'java', '.kt': 'kotlin', '.swift': 'swift',
    '.c': 'c', '.h': 'c', '.cpp': 'cpp', '.hpp': 'cpp', '.cs': 'csharp',
    '.rb': 'ruby', '.php': 'php', '.sh': 'bash', '.ps1': 'powershell',
    '.json': 'json', '.yml': 'yaml', '.yaml': 'yaml', '.toml': 'toml',
    '.md': 'markdown', '.html': 'html', '.css': 'css', '.scss': 'scss',
    '.sql': 'sql', '.xml': 'xml',
}
def guess_lang(path: str) -> str:
    if not path:
        return 'text'
    p = path.lower()
    for ext, lang in EXT_LANG.items():
        if p.endswith(ext):
            return lang
    return 'text'

# ──────────────────────────────────────────────────────────────
# Orchestrations (Kiro CLI AgentCrew / subagent stages)
#
# A crew is one subagent tool call with a stages[] list. It sits in the
# parent transcript at the moment the lead agent launched it. Each stage
# is its own CLI session (parent_session_id). The default export keeps
# the roster and each stage's last useful report. Command output, file
# bodies, and reasoning stay behind filter rows that show their cost.
# ──────────────────────────────────────────────────────────────
ORCH_STATUSES = (
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled'),
    ('error', 'Error'),
    ('running', 'Still running'),
)
# (key, label, default_on). Light layers are stored as text. Heavy layers
# are counted during the scan and loaded only if the export turns them on.
ORCH_LAYERS = (
    ('last_state', 'Last state', True),
    ('roster', 'Stage roster', True),
    ('brief', 'Stage briefs', False),
    ('shared_task', 'Shared task', False),
    ('talk', 'Stage messages', False),
    ('commands', 'Stage commands', False),
    ('command_output', 'Stage command output', False),
    ('reasoning', 'Stage reasoning', False),
    ('file_read', 'Stage file reads', False),
    ('file_write', 'Stage file writes', False),
    ('search', 'Stage searches', False),
)
ORCH_LIGHT_LAYERS = ('last_state', 'roster', 'brief', 'shared_task', 'talk', 'commands')
ORCH_HEAVY_LAYERS = ('command_output', 'reasoning', 'file_read', 'file_write', 'search')
ORCH_HEAVY_TITLES = {
    'command_output': 'Command output',
    'reasoning': 'Reasoning',
    'file_read': 'File reads',
    'file_write': 'File writes',
    'search': 'Searches',
}
_STAGE_NAME_RE = re.compile(r'YOUR STAGE:\s*([A-Za-z0-9_-]+)')
_FAILED_STAGE_RE = re.compile(r"stage\s+'([^']+)'", re.IGNORECASE)


def default_orch_filter() -> Dict:
    return {
        'statuses': {key: True for key, _label in ORCH_STATUSES},
        'layers': {key: on for key, _label, on in ORCH_LAYERS},
        'latest_only': False,
    }


def cap_text(text: str, cap: int) -> str:
    """Keep the last `cap` lines. cap <= 0 keeps everything."""
    if not text or cap <= 0:
        return text or ''
    lines = text.split('\n')
    if len(lines) <= cap:
        return text
    kept = lines[-cap:]
    return f"... ({len(lines) - cap} lines trimmed) ...\n" + '\n'.join(kept)


def text_line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.split('\n'))


def estimate_tokens(chars: int) -> int:
    if chars <= 0:
        return 0
    return max(1, (int(chars) + 3) // 4)


def format_tokens(n: int) -> str:
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 10_000:
        tenths = n / 1000.0
        return f"{tenths:.1f}k"
    if n < 1_000_000:
        return f"{round(n / 1000)}k"
    if n < 10_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{round(n / 1_000_000)}M"


def _segment(text: str) -> Optional[Dict[str, int]]:
    """Size of one heavy block. Whitespace-only text is dropped, matching export.

    `lines` is how `cap_text` splits the text. `nl` is the newline count.
    `trimmed` stores the exact capped body length for each output-cap choice
    that actually shortens this text, so the filter can match the export
    without keeping the body.
    """
    if not text or not text.strip():
        return None
    nl = text.count('\n')
    parts = text.split('\n')
    splits = len(parts)
    trimmed: Dict[int, int] = {}
    for cap in CAP_STEPS:
        if not cap or splits <= cap:
            continue
        kept = parts[-cap:]
        notice = f"... ({splits - cap} lines trimmed) ...\n"
        trimmed[cap] = len(notice) + sum(len(part) for part in kept) + (cap - 1)
    return {'chars': len(text), 'nl': nl, 'lines': splits, 'trimmed': trimmed}


def _stage_names_in_prompt(data) -> List[str]:
    names: List[str] = []
    content = data.get('content') if isinstance(data, dict) else None
    if not isinstance(content, list):
        return names
    for block in content:
        if not isinstance(block, dict) or block.get('kind') != 'text':
            continue
        text = block.get('data') or ''
        if isinstance(text, str) and 'YOUR STAGE:' in text:
            names.extend(_STAGE_NAME_RE.findall(text))
    return names


def _stage_label_in_prompt(data) -> str:
    """Text after the last `YOUR STAGE:` marker, through the end of that line."""
    label = ''
    content = data.get('content') if isinstance(data, dict) else None
    if not isinstance(content, list):
        return ''
    marker = 'YOUR STAGE:'
    for block in content:
        if not isinstance(block, dict) or block.get('kind') != 'text':
            continue
        text = block.get('data') or ''
        if not isinstance(text, str) or marker not in text:
            continue
        for line in text.splitlines():
            idx = line.find(marker)
            if idx >= 0:
                label = line[idx + len(marker):].strip()
    return label


def _label_matches_spec(label: str, name: str) -> bool:
    if not label or not name:
        return False
    if label == name:
        return True
    # `log_slice_1 - analyze this` names `log_slice_1`. A hyphen with no
    # space stays inside the name, so `log_slice_1-extra` is not `log_slice_1`.
    if len(label) > len(name) and label.startswith(name):
        return label[len(name)] in ' \t:—'
    return False


def _resolve_stage_name(label: str, token: str, orchestrations: List[Dict]) -> str:
    """Longest spec name the YOUR STAGE line actually names.

    The token regex stops at a space, so `code review` would become `code`.
    The raw line is matched against the crew's own stage names first.
    """
    raw = (label or '').strip()
    best = ''
    if raw:
        for orch in orchestrations or []:
            for spec in orch.get('stage_specs') or []:
                name = spec.get('name') or ''
                if _label_matches_spec(raw, name) and len(name) > len(best):
                    best = name
    return best or token or ''


def _prompt_text(data) -> str:
    content = data.get('content') if isinstance(data, dict) else None
    if not isinstance(content, list):
        return ''
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get('kind') == 'text' and isinstance(block.get('data'), str):
            parts.append(block['data'])
    return '\n'.join(parts)


def _piece_metrics(text: str) -> Tuple[int, int]:
    """Newline count and length. Both add up exactly when the pieces are joined."""
    if not text:
        return 0, 0
    return text.count('\n'), len(text)


def _push_segment(bucket: Dict[str, List[Dict[str, int]]], layer: str, text: str):
    seg = _segment(text)
    if seg:
        bucket[layer].append(seg)


def _tool_call_input(block_data: Dict) -> Dict:
    raw = block_data.get('input') if isinstance(block_data, dict) else None
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if k != '__tool_use_purpose'}
    return {}


def _stringify_tool_value(value) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _cli_result_body(result_obj) -> Tuple[str, List]:
    """Return (status_key, items) from a Kiro CLI tool result object."""
    result = (result_obj or {}).get('result') if isinstance(result_obj, dict) else None
    if isinstance(result, str):
        return result, []
    if not isinstance(result, dict) or not result:
        return '', []
    key = next(iter(result))
    body = result.get(key)
    if isinstance(body, dict):
        items = body.get('items') or []
        return str(key), items if isinstance(items, list) else []
    return str(key), []


def _cli_output_text(result_obj) -> str:
    _status, items = _cli_result_body(result_obj)
    json_item = CliSessionParser._first_json_item(items)
    if isinstance(json_item, dict) and ('stdout' in json_item or 'stderr' in json_item):
        out = json_item.get('stdout') or ''
        err = json_item.get('stderr') or ''
        if not isinstance(out, str):
            out = _stringify_tool_value(out)
        if not isinstance(err, str):
            err = _stringify_tool_value(err)
        text = out
        if err:
            text = (text + '\n' if text else '') + err
        return text
    return CliSessionParser._result_items_to_text(items)


def _heavy_layer_for_tool(name: str) -> str:
    if name == 'shell':
        return 'command_output'
    if name == 'read':
        return 'file_read'
    if name == 'write':
        return 'file_write'
    if name in ('grep', 'glob', 'code', 'search', 'web_search', 'web_fetch', 'fetch', 'searchGitHub'):
        return 'search'
    return ''


def scan_stage_jsonl(path: Path, keep_heavy: Optional[Set[str]] = None) -> Dict:
    """Stream one stage session. Light text is kept. Heavy text is counted
    unless `keep_heavy` names layers whose bodies must be returned."""
    keep_heavy = set(keep_heavy or ())
    out = {
        'stage_names': [],
        'report': '',
        'utterance': '',
        'talk_parts': [],
        'commands': [],
        'tool_counts': {},
        'last_tool': '',
        'last_detail': '',
        'prompt': '',
        'stage_label': '',
        'segments': {key: [] for key in ORCH_HEAVY_LAYERS},
        'heavy_texts': {key: [] for key in keep_heavy},
        'events': 0,
    }
    if not path or not path.exists():
        return out
    pending: Dict[str, str] = {}
    counts: Dict[str, int] = {}
    loads = _orjson.loads if _orjson is not None else None
    try:
        fh = open(path, 'rb')
    except Exception:
        return out
    with fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or raw[0] != 123:  # '{'
                continue
            try:
                obj = loads(raw) if loads is not None else json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            out['events'] += 1
            kind = obj.get('kind')
            data = obj.get('data') or {}
            if not isinstance(data, dict):
                continue
            if kind == 'Prompt' and not out['stage_names'] and not out.get('stage_label'):
                names = _stage_names_in_prompt(data)
                label = _stage_label_in_prompt(data)
                if names or label:
                    if names:
                        out['stage_names'] = names
                    if label:
                        out['stage_label'] = label
                    out['prompt'] = _prompt_text(data) or out.get('prompt') or ''
                elif not out.get('prompt'):
                    out['prompt'] = _prompt_text(data)
            elif kind == 'AssistantMessage':
                blocks = data.get('content')
                if not isinstance(blocks, list):
                    blocks = []
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    bk = block.get('kind')
                    bd = block.get('data')
                    if bk == 'text':
                        text = bd if isinstance(bd, str) else ''
                        if text.strip():
                            if out['utterance']:
                                out['talk_parts'].append(out['utterance'])
                            out['utterance'] = text
                    elif bk == 'thinking':
                        text = bd.get('text') if isinstance(bd, dict) else ''
                        text = text or ''
                        _push_segment(out['segments'], 'reasoning', text)
                        if 'reasoning' in keep_heavy and text:
                            out['heavy_texts']['reasoning'].append(text)
                    elif bk == 'toolUse' and isinstance(bd, dict):
                        name = str(bd.get('name') or 'tool')
                        counts[name] = counts.get(name, 0) + 1
                        args = _tool_call_input(bd)
                        tool_id = str(bd.get('toolUseId') or '')
                        if tool_id:
                            pending[tool_id] = name
                        out['last_tool'] = name
                        if name == 'shell':
                            cmd = _stringify_tool_value(args.get('command') or '')
                            if cmd.strip():
                                out['commands'].append(cmd)
                                out['last_detail'] = cmd.strip().splitlines()[0][:180]
                        elif name == 'summary':
                            task_result = args.get('taskResult') or ''
                            if isinstance(task_result, str) and task_result.strip():
                                out['report'] = task_result
                            detail = (args.get('taskDescription') or '') if isinstance(args.get('taskDescription'), str) else ''
                            out['last_detail'] = (detail or 'summary').strip().splitlines()[0][:180]
                        elif name == 'write':
                            content = _stringify_tool_value(args.get('content') or args.get('newStr') or args.get('new') or '')
                            _push_segment(out['segments'], 'file_write', content)
                            if 'file_write' in keep_heavy and content:
                                out['heavy_texts']['file_write'].append(content)
                            path_s = _stringify_tool_value(args.get('path') or args.get('file_path') or 'write')
                            out['last_detail'] = path_s[:180]
                        elif name == 'read':
                            ops = args.get('operations') or []
                            path_s = ''
                            if isinstance(ops, list) and ops and isinstance(ops[0], dict):
                                path_s = str(ops[0].get('path') or '')
                            path_s = path_s or _stringify_tool_value(args.get('path') or 'read')
                            out['last_detail'] = path_s[:180]
                        elif name in ('grep', 'glob', 'code'):
                            query = _stringify_tool_value(args.get('pattern') or args.get('query') or args.get('operation') or name)
                            out['last_detail'] = query[:180]
                        else:
                            out['last_detail'] = name
            elif kind == 'ToolResults':
                results = data.get('results') or {}
                if not isinstance(results, dict):
                    continue
                for tool_id, payload in results.items():
                    name = pending.get(str(tool_id), '')
                    if name == 'summary':
                        if not out['report']:
                            text = _cli_output_text(payload)
                            if text.strip():
                                out['report'] = text
                        continue
                    layer = _heavy_layer_for_tool(name)
                    if not layer or layer == 'file_write':
                        continue
                    text = _cli_output_text(payload)
                    _push_segment(out['segments'], layer, text)
                    if layer in keep_heavy and text:
                        out['heavy_texts'][layer].append(text)
    out['tool_counts'] = counts
    return out


def split_pipeline_reports(text: str, stage_names: List[str]) -> Dict[str, str]:
    """Split a crew's pipeline text on exact `## <stage name>` lines.

    Headings inside a stage report stay inside that report.
    """
    names = [n for n in stage_names if n]
    name_set = set(names)
    buckets = {n: [] for n in names}
    current = None
    for line in (text or '').split('\n'):
        if line.startswith('## '):
            candidate = line[3:].strip()
            if candidate in name_set:
                current = candidate
                continue
        if current is not None:
            buckets[current].append(line)
    return {name: '\n'.join(lines).strip() for name, lines in buckets.items()}


def _norm_task_title(title: str) -> str:
    text = (title or '').strip()
    if text.endswith('...'):
        text = text[:-3].rstrip()
    return text


def _task_matches_title(task: str, title: str) -> bool:
    key = _norm_task_title(title)
    if not key:
        return False
    task = task or ''
    # A short title is only that exact task. `fix` must not match `fix the bug`.
    if len(key) < 24:
        return task == key or task.startswith(key + '\n')
    if task.startswith(key):
        return True
    # Kiro truncates the session title. A long stored title is still a prefix.
    probe = key[:80].rstrip()
    return len(probe) >= 40 and task.startswith(probe)


def _brief_match_score(task: str, brief: str, prompt: str) -> int:
    """How strongly this stage brief is the prompt the child actually received.

    Kiro stores the template with a `{task}` placeholder and sends the child
    the template with that placeholder filled. A later crew that reused a
    stage name still has its own brief, so the prompt identifies the crew.
    """
    prompt = prompt or ''
    brief = brief or ''
    if not prompt or not brief:
        return 0
    rendered = brief.replace('{task}', task or '')
    if rendered and prompt.strip() == rendered.strip():
        return max(len(rendered), 1)
    if len(rendered) >= 24 and rendered in prompt:
        return len(rendered)
    if '{task}' in brief:
        tail = brief.split('{task}', 1)[1].strip()
        if len(tail) >= 24 and tail in prompt:
            return len(tail)
    return 0


def _crew_stage_score(orch: Dict, stage_name: str, prompt: str, title: str) -> int:
    specs = orch.get('stage_specs') or []
    names = [spec.get('name') for spec in specs]
    if stage_name and stage_name not in names:
        return 0
    brief = ''
    for spec in specs:
        if spec.get('name') == stage_name:
            brief = spec.get('brief') or ''
            break
    score = _brief_match_score(orch.get('task') or '', brief, prompt)
    if score:
        return score + 1  # stay above the title-only fallback
    if _task_matches_title(orch.get('task') or '', title):
        return 1
    return 0


def _format_tool_counts(counts: Dict[str, int]) -> str:
    if not counts:
        return ''
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    parts = [f"{name} ×{n}" for name, n in ordered[:6]]
    extra = len(ordered) - len(parts)
    if extra > 0:
        parts.append(f"+{extra}")
    return ', '.join(parts)


def _status_sentence(status: str) -> str:
    return {
        'finished': 'Finished.',
        'partial': 'Stopped with a partial answer and no final report.',
        'interrupted': 'Stopped with no final answer.',
        'never_started': 'This stage never started.',
        'not_started_yet': 'This stage has not started yet.',
        'failed': 'This stage failed.',
        'running': 'Still running at export.',
    }.get(status, status)


def _source_sentence(source: str, crew_status: str) -> str:
    if source == 'pipeline':
        return 'Final report returned to the lead agent.'
    if source == 'summary':
        if crew_status == 'completed':
            return 'Final report recovered from the stage.'
        return 'Final report recovered from the stage. The crew itself did not return a pipeline.'
    if source == 'utterance':
        return 'No final report. This is the last thing the stage said.'
    if crew_status == 'running':
        return 'No answer recorded yet.'
    return 'No answer was recorded.'


def _parent_result_line(item: Dict) -> str:
    status = item.get('crew_status') or 'running'
    detail = (item.get('status_detail') or '').strip()
    launched = len(item.get('stages') or [])
    if status == 'completed':
        return f"Parent result: completed. The pipeline returned reports for the stages below ({launched} launched)."
    if status == 'cancelled':
        return "Parent result: cancelled. The crew returned no pipeline report, so each stage below is its own last recorded state."
    if status == 'error':
        extra = f" {detail}" if detail else ''
        return "Parent result: error." + extra
    return "Parent result: still running. Finished stages keep their reports. The others show the last recorded state."


def _stage_counts_phrase(stages: List[Dict]) -> str:
    launched = len(stages)
    started = sum(1 for s in stages if s.get('session_id'))
    with_report = sum(1 for s in stages if (s.get('report') or '').strip())
    not_started = sum(1 for s in stages if s.get('status') in ('never_started', 'not_started_yet'))
    return (f"{launched} launched · {started} started · {with_report} with a report · "
            f"{not_started} not started")


def _md_fence(text: str, cap: int = 0) -> str:
    shown = cap_text(text or '', cap)
    return f"```text\n{shown}\n```\n\n"


def assemble_crew_parts(item: Dict):
    """Build the markdown pieces the filter and the export both use."""
    status = item.get('crew_status') or 'running'
    label = {
        'completed': 'COMPLETED',
        'cancelled': 'CANCELLED',
        'error': 'ERROR',
        'running': 'RUNNING',
    }.get(status, status.upper())
    ordinal = int(item.get('ordinal_in_turn') or 1)
    total = int(item.get('turn_orch_count') or 1)
    purpose = (item.get('purpose') or 'Orchestration').strip() or 'Orchestration'
    header = (
        f"## 🎭 Orchestration {ordinal}/{total} in this turn — {label}\n\n"
        f"**{purpose}**\n\n"
        f"- {_parent_result_line(item)}\n"
        f"- Stages: {_stage_counts_phrase(item.get('stages') or [])}\n"
        f"- Id: `{item.get('tool_use_id') or '?'}`\n\n"
    )
    marker = (
        f"## 🎭 Orchestration {ordinal}/{total} in this turn — {label}\n\n"
        f"**{purpose}**\n\n"
        f"_Omitted ({label.lower()}). {_stage_counts_phrase(item.get('stages') or [])}. "
        f"Turn this status on in the filter to include the stage reports._\n\n"
    )
    task = (item.get('task') or '').strip()
    shared = ''
    if task:
        shared = f"**Shared task given to every stage:**\n\n{_md_fence(task)}"
    roster_rows = ['| Stage | Status | Report | Tools |', '|---|---|---|---|']
    stage_parts = []
    measure_stages = []
    for stage in item.get('stages') or []:
        name = stage.get('name') or '(unnamed)'
        status_label = (stage.get('status') or 'unknown').replace('_', ' ')
        if stage.get('superseded'):
            status_label += ', superseded'
        elif stage.get('latest_attempt'):
            status_label += ', latest'
        report = (stage.get('report') or '').strip()
        if report:
            report_cell = f"{len(report):,} chars"
        elif (stage.get('utterance') or '').strip():
            report_cell = 'partial'
        else:
            report_cell = '—'
        tools = _format_tool_counts(stage.get('tool_counts') or {}) or '—'
        safe_name = name.replace('|', '/')
        roster_rows.append(f"| `{safe_name}` | {status_label} | {report_cell} | {tools} |")

        source = stage.get('report_source') or 'none'
        body_bits = [f"### {name} — {status_label}", '', f"_{_source_sentence(source, status)}_"]
        if stage.get('failed') and item.get('status_detail'):
            body_bits.append(f"Crew error: {item.get('status_detail')}")
        model = stage.get('model') or ''
        if model:
            body_bits.append(f"Model: `{model}`")
        tools_line = _format_tool_counts(stage.get('tool_counts') or {})
        if tools_line:
            body_bits.append(f"Tools: {tools_line}")
        if not report:
            if stage.get('last_tool'):
                detail = stage.get('last_detail') or ''
                extra = f" — {detail}" if detail else ''
                body_bits.append(f"Last tool: `{stage.get('last_tool')}`{extra}")
            elif stage.get('status') in ('never_started', 'not_started_yet'):
                body_bits.append(_status_sentence(stage.get('status')))
        utterance = (stage.get('utterance') or '').strip()
        shown = report or (utterance if source == 'utterance' else '')
        if shown:
            body_bits.extend(['', shown])
        body_bits.append('')
        last_state = '\n'.join(body_bits) + '\n'
        note = (
            f"### {name} — {status_label}\n\n"
            f"_Later attempt kept. Body omitted by Latest attempt only._\n\n"
        )
        brief = (stage.get('brief') or '').strip()
        brief_md = f"**Stage brief:**\n\n{_md_fence(brief)}" if brief else ''
        talk = (stage.get('talk') or '').strip()
        talk_md = f"**What the stage said along the way:**\n\n{talk}\n\n" if talk else ''
        commands = [c for c in (stage.get('commands') or []) if isinstance(c, str) and c.strip()]
        if commands:
            joined = '\n\n'.join('$ ' + c.strip() for c in commands)
            commands_md = f"**Commands the stage ran:** {len(commands)}\n\n{_md_fence(joined)}"
        else:
            commands_md = ''
        segments = stage.get('segments') or {}
        stage_parts.append({
            'name': name,
            'session_id': stage.get('session_id') or '',
            'session_path': stage.get('session_path') or '',
            'superseded': bool(stage.get('superseded')),
            'last_state': last_state,
            'superseded_note': note,
            'brief': brief_md,
            'talk': talk_md,
            'commands': commands_md,
            'segments': segments,
        })
        measure_stages.append({
            'superseded': bool(stage.get('superseded')),
            'last_state': _piece_metrics(last_state),
            'note': _piece_metrics(note),
            'brief': _piece_metrics(brief_md),
            'talk': _piece_metrics(talk_md),
            'commands': _piece_metrics(commands_md),
            'segments': segments,
        })
    roster = '\n'.join(roster_rows) + '\n\n'
    item['parts'] = {
        'header': header,
        'marker': marker,
        'shared_task': shared,
        'roster': roster,
        'stages': stage_parts,
    }
    item['measure'] = {
        'header': _piece_metrics(header),
        'marker': _piece_metrics(marker),
        'shared_task': _piece_metrics(shared),
        'roster': _piece_metrics(roster),
        'stages': measure_stages,
    }


def _heavy_markdown(layer: str, texts: List[str], cap: int) -> str:
    blocks = []
    for text in texts or []:
        if not isinstance(text, str) or not text.strip():
            continue
        blocks.append(f"```text\n{cap_text(text, cap)}\n```")
    if not blocks:
        return ''
    title = ORCH_HEAVY_TITLES.get(layer, layer)
    return f"**{title}** ({len(blocks)})\n\n" + '\n\n'.join(blocks) + '\n\n'


def _notice_len(trimmed: int) -> int:
    digits = 1
    value = trimmed
    while value >= 10:
        digits += 1
        value //= 10
    return 24 + digits  # len('... (N lines trimmed) ...')


def _heavy_body_size(seg: Dict, cap: int) -> Tuple[int, int]:
    """Newline count and length of `cap_text` for one stored segment."""
    chars = int(seg.get('chars') or 0)
    if seg.get('nl') is None:
        nl = max(0, int(seg.get('lines') or 1) - 1)
        splits = nl + 1
    else:
        nl = int(seg.get('nl') or 0)
        splits = int(seg.get('lines') or (nl + 1))
    if cap > 0 and splits > cap:
        trimmed = seg.get('trimmed') or {}
        if cap in trimmed:
            return cap, int(trimmed[cap])
        # A cap this UI does not offer: lines are exact, characters are proportional.
        kept = int(round(chars * (cap / splits)))
        return cap, kept + _notice_len(splits - cap) + 1
    return nl, chars


def _heavy_estimate(segments: List[Dict], cap: int, layer: str,
                    cache: Optional[Dict] = None) -> Tuple[int, int]:
    """Newline count and length of `_heavy_markdown` for these segments.

    Counts match the fenced export for an unlimited cap and for every cap in
    the filter. Repeated filter redraws reuse `cache`.
    """
    if cache is not None:
        hit = cache.get((layer, cap))
        if hit is not None:
            return hit
    usable = [s for s in (segments or []) if int(s.get('chars') or 0) > 0 or int(s.get('lines') or 0) > 0]
    if not usable:
        if cache is not None:
            cache[(layer, cap)] = (0, 0)
        return 0, 0
    body_nl = 0
    body_chars = 0
    for seg in usable:
        nl, chars = _heavy_body_size(seg, cap)
        # ```text\n{body}\n```
        body_nl += nl + 2
        body_chars += chars + len('```text\n') + len('\n```')
    n = len(usable)
    heading = f"**{ORCH_HEAVY_TITLES.get(layer, layer)}** ({n})\n\n"
    # Heading, a blank line between blocks, and the trailing blank line.
    result = (heading.count('\n') + body_nl + 2 * (n - 1) + 2,
              len(heading) + body_chars + 2 * (n - 1) + 2)
    if cache is not None:
        cache[(layer, cap)] = result
    return result


def render_orchestration(item: Dict, orch_filter: Optional[Dict] = None,
                         output_cap: int = 0, heavy: Optional[Dict] = None) -> str:
    filt = orch_filter or default_orch_filter()
    parts = item.get('parts') or {}
    status = item.get('crew_status') or 'running'
    if not filt.get('statuses', {}).get(status, True):
        return parts.get('marker') or ''
    chunks: List[str] = [parts.get('header') or '']
    layers = filt.get('layers') or {}
    if layers.get('shared_task'):
        chunks.append(parts.get('shared_task') or '')
    if layers.get('roster'):
        chunks.append(parts.get('roster') or '')
    latest_only = bool(filt.get('latest_only'))
    heavy = heavy or {}
    for stage in parts.get('stages') or []:
        skip_body = latest_only and stage.get('superseded')
        if layers.get('last_state'):
            chunks.append(stage.get('superseded_note') if skip_body else (stage.get('last_state') or ''))
        if skip_body:
            continue
        for layer in ('brief', 'talk', 'commands'):
            if layers.get(layer):
                chunks.append(stage.get(layer) or '')
        session_id = stage.get('session_id') or ''
        for layer in ORCH_HEAVY_LAYERS:
            if not layers.get(layer):
                continue
            texts = heavy.get((session_id, layer))
            if texts:
                chunks.append(_heavy_markdown(layer, texts, output_cap))
    return ''.join(chunks)


def _string_metrics(text: str) -> Tuple[int, int]:
    if not text:
        return 0, 0
    return text_line_count(text), len(text)


def _crew_body_metrics(item: Dict, orch_filter: Dict, output_cap: int) -> Tuple[int, int]:
    """Size of one crew's export body. Integer adds only — the strings were measured once."""
    measure = item.get('measure') or {}
    lines, chars = measure.get('header') or (0, 0)
    layers = orch_filter.get('layers') or {}
    if layers.get('shared_task'):
        piece = measure.get('shared_task') or (0, 0)
        lines += piece[0]
        chars += piece[1]
    if layers.get('roster'):
        piece = measure.get('roster') or (0, 0)
        lines += piece[0]
        chars += piece[1]
    latest_only = bool(orch_filter.get('latest_only'))
    for stage in measure.get('stages') or []:
        skip_body = latest_only and stage.get('superseded')
        if layers.get('last_state'):
            piece = (stage.get('note') if skip_body else stage.get('last_state')) or (0, 0)
            lines += piece[0]
            chars += piece[1]
        if skip_body:
            continue
        for layer in ('brief', 'talk', 'commands'):
            if layers.get(layer):
                piece = stage.get(layer) or (0, 0)
                lines += piece[0]
                chars += piece[1]
        for layer in ORCH_HEAVY_LAYERS:
            if not layers.get(layer):
                continue
            heavy_lines, heavy_chars = _heavy_estimate(
                (stage.get('segments') or {}).get(layer) or [], output_cap, layer,
                stage.setdefault('_hcache', {}),
            )
            lines += heavy_lines
            chars += heavy_chars
    return lines, chars


def _layer_piece(stage: Dict, layer: str, latest_only: bool) -> Tuple[int, int]:
    skip_body = latest_only and stage.get('superseded')
    if layer == 'last_state':
        return (stage.get('note') if skip_body else stage.get('last_state')) or (0, 0)
    if skip_body:
        return 0, 0
    if layer in ORCH_HEAVY_LAYERS:
        return 0, 0
    return stage.get(layer) or (0, 0)


def orchestration_accounting(parsers, orch_filter: Optional[Dict] = None,
                             output_cap: int = 0) -> Dict:
    """Line and character totals for the filter. No markdown is rebuilt here."""
    filt = orch_filter or default_orch_filter()
    statuses_on = filt.get('statuses') or {}
    layers_on = filt.get('layers') or {}
    latest_only = bool(filt.get('latest_only'))
    selected_lines = selected_chars = 0
    by_status = {key: [0, 0] for key, _label in ORCH_STATUSES}
    by_layer = {key: [0, 0] for key, _label, _on in ORCH_LAYERS}
    by_status_crews = {key: 0 for key, _label in ORCH_STATUSES}
    crews = 0
    stages = 0
    for parser in parsers or []:
        for item in getattr(parser, 'data', []) or []:
            if item.get('type') != 'orchestration':
                continue
            crews += 1
            status = item.get('crew_status') or 'running'
            by_status_crews[status] = by_status_crews.get(status, 0) + 1
            measure = item.get('measure') or {}
            stages += len(measure.get('stages') or [])
            body_lines, body_chars = _crew_body_metrics(item, filt, output_cap)
            by_status.setdefault(status, [0, 0])
            by_status[status][0] += body_lines
            by_status[status][1] += body_chars
            if statuses_on.get(status, True):
                selected_lines += body_lines
                selected_chars += body_chars
            else:
                marker = measure.get('marker') or (0, 0)
                selected_lines += marker[0]
                selected_chars += marker[1]
            if not statuses_on.get(status, True):
                continue
            if measure.get('shared_task'):
                by_layer['shared_task'][0] += measure['shared_task'][0]
                by_layer['shared_task'][1] += measure['shared_task'][1]
            if measure.get('roster'):
                by_layer['roster'][0] += measure['roster'][0]
                by_layer['roster'][1] += measure['roster'][1]
            for stage in measure.get('stages') or []:
                for layer in ('last_state', 'brief', 'talk', 'commands'):
                    piece = _layer_piece(stage, layer, latest_only)
                    by_layer[layer][0] += piece[0]
                    by_layer[layer][1] += piece[1]
                if latest_only and stage.get('superseded'):
                    continue
                for layer in ORCH_HEAVY_LAYERS:
                    heavy_lines, heavy_chars = _heavy_estimate(
                        (stage.get('segments') or {}).get(layer) or [], output_cap, layer,
                        stage.setdefault('_hcache', {}),
                    )
                    by_layer[layer][0] += heavy_lines
                    by_layer[layer][1] += heavy_chars

    def pack(pair):
        return {'lines': pair[0], 'chars': pair[1], 'tokens': estimate_tokens(pair[1])}

    return {
        'selected_lines': selected_lines,
        'selected_chars': selected_chars,
        'selected_tokens': estimate_tokens(selected_chars),
        'crews': crews,
        'stages': stages,
        'by_status': {key: pack(value) for key, value in by_status.items()},
        'by_status_crews': by_status_crews,
        'by_layer': {key: pack(value) for key, value in by_layer.items()},
    }


def orchestration_metrics(item: Dict, orch_filter: Optional[Dict] = None,
                          output_cap: int = 0) -> Tuple[int, int]:
    """Lines and characters this one crew contributes to the current export."""
    class _P:
        def __init__(self, data):
            self.data = data
    stats = orchestration_accounting([_P([item])], orch_filter, output_cap)
    return stats['selected_lines'], stats['selected_chars']


def load_orchestration_heavy(item: Dict, orch_filter: Dict, output_cap: int = 0) -> Dict:
    """Re-read stage sessions for heavy layers the export actually turned on."""
    layers = orch_filter.get('layers') or {}
    wanted = {key for key in ORCH_HEAVY_LAYERS if layers.get(key)}
    loaded: Dict = {}
    if not wanted:
        return loaded
    latest_only = bool(orch_filter.get('latest_only'))
    for stage in (item.get('parts') or {}).get('stages') or []:
        if latest_only and stage.get('superseded'):
            continue
        path_s = stage.get('session_path') or ''
        session_id = stage.get('session_id') or ''
        if not path_s or not session_id:
            continue
        scanned = scan_stage_jsonl(Path(path_s), keep_heavy=wanted)
        for layer in wanted:
            texts = scanned.get('heavy_texts', {}).get(layer) or []
            if texts:
                loaded[(session_id, layer)] = texts
    return loaded


def _blank_stage(spec: Dict) -> Dict:
    return {
        'name': spec.get('name') or '(unnamed)',
        'role': spec.get('role') or '',
        'model': spec.get('model') or '',
        'brief': spec.get('brief') or '',
        'session_id': '',
        'session_path': '',
        'created_at': '',
        'status': 'never_started',
        'report': '',
        'report_source': 'none',
        'utterance': '',
        'talk': '',
        'commands': [],
        'tool_counts': {},
        'last_tool': '',
        'last_detail': '',
        'segments': {key: [] for key in ORCH_HEAVY_LAYERS},
        'superseded': False,
        'latest_attempt': False,
        'failed': False,
    }


def _apply_child_scan(stage: Dict, scan: Dict, pipeline_text: str, crew_status: str, failed: bool):
    pipeline_text = (pipeline_text or '').strip()
    summary = (scan.get('report') or '').strip()
    utterance = (scan.get('utterance') or '').strip()
    if pipeline_text:
        stage['report'] = pipeline_text
        stage['report_source'] = 'pipeline'
    elif summary:
        stage['report'] = summary
        stage['report_source'] = 'summary'
    elif utterance:
        stage['report'] = ''
        stage['report_source'] = 'utterance'
    else:
        stage['report'] = ''
        stage['report_source'] = 'none'
    stage['utterance'] = utterance
    talk_parts = [p for p in (scan.get('talk_parts') or []) if isinstance(p, str) and p.strip()]
    report_text = pipeline_text or summary
    # The final utterance is not part of the running transcript. When a real
    # report exists, the messages layer would otherwise drop it.
    if report_text and utterance and utterance not in talk_parts and utterance not in report_text:
        talk_parts.append(utterance)
    stage['talk'] = '\n\n'.join(talk_parts)
    stage['commands'] = list(scan.get('commands') or [])
    stage['tool_counts'] = dict(scan.get('tool_counts') or {})
    stage['last_tool'] = scan.get('last_tool') or ''
    stage['last_detail'] = scan.get('last_detail') or ''
    stage['segments'] = scan.get('segments') or {key: [] for key in ORCH_HEAVY_LAYERS}
    stage['failed'] = failed
    has_session = bool(stage.get('session_id'))
    has_report = bool(stage.get('report'))
    if failed:
        stage['status'] = 'failed'
    elif has_report:
        stage['status'] = 'finished'
    elif not has_session:
        stage['status'] = 'not_started_yet' if crew_status == 'running' else 'never_started'
    elif crew_status == 'running':
        stage['status'] = 'partial' if utterance else 'running'
    elif utterance or stage['report_source'] == 'utterance':
        stage['status'] = 'partial'
    else:
        stage['status'] = 'interrupted'


def _discover_stage_sessions(sessions_dir: Path, parent_id: str) -> List[Dict]:
    """Find child sessions by a byte search, then parse only the matches."""
    if not sessions_dir or not sessions_dir.exists():
        return []
    needle = f'"parent_session_id":"{parent_id}"'.encode('ascii')
    needle_spaced = f'"parent_session_id": "{parent_id}"'.encode('ascii')
    paths = []
    try:
        for entry in os.scandir(sessions_dir):
            name = entry.name
            if not name.endswith('.json') or name[:-5] == parent_id:
                continue
            try:
                if entry.stat().st_size > 262144:
                    continue
            except OSError:
                continue
            paths.append(entry.path)
    except OSError:
        return []

    def _one(path_s: str):
        try:
            with open(path_s, 'rb') as fh:
                raw = fh.read()
        except OSError:
            return None
        if needle not in raw and needle_spaced not in raw:
            return None
        try:
            data = _orjson.loads(raw) if _orjson is not None else json.loads(raw)
        except Exception:
            return None
        if not isinstance(data, dict) or data.get('parent_session_id') != parent_id:
            return None
        fp = Path(path_s)
        return {
            'id': fp.stem,
            'path': fp,
            'jsonl': fp.with_suffix('.jsonl'),
            'title': data.get('title') or '',
            'created_at': data.get('created_at') or '',
        }

    if not paths:
        return []
    from concurrent.futures import ThreadPoolExecutor
    workers = min(32, len(paths))
    found = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row in pool.map(_one, paths, chunksize=64):
            if row:
                found.append(row)
    found.sort(key=lambda row: (row.get('created_at') or '', row.get('id') or ''))
    return found


def _open_stage_slot(orch: Dict, filled_map: Dict, stage_name: str) -> bool:
    """A crew can take one child per time it lists this stage name."""
    needed = sum(
        1 for spec in (orch.get('stage_specs') or [])
        if (spec.get('name') or '') == stage_name
    )
    return len(filled_map.get(stage_name) or []) < needed


def _assign_stage_sessions(orchestrations: List[Dict], children: List[Dict]):
    """Attach each child session to the crew and stage it belongs to.

    The child prompt is the stage brief with `{task}` filled in, so a later
    crew wins when its own brief matches. An equal brief score keeps the crew
    whose task matches the child title, and otherwise the earlier crew.
    A crew that lists one name twice takes that many children, in creation order.
    """
    scans = []
    from concurrent.futures import ThreadPoolExecutor
    indexed = list(enumerate(children))

    def _scan_one(pair):
        idx, child = pair
        scan = scan_stage_jsonl(child['jsonl'])
        names = scan.get('stage_names') or []
        token = names[-1] if names else ''
        stage_name = _resolve_stage_name(scan.get('stage_label') or '', token, orchestrations)
        if not stage_name:
            stage_name = '(unnamed)'
        return idx, stage_name, scan

    if indexed:
        workers = min(8, len(indexed))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for idx, stage_name, scan in pool.map(_scan_one, indexed):
                scans.append((idx, stage_name, scan))
        scans.sort(key=lambda row: row[0])

    filled = []  # parallel to orchestrations: name -> [(child, scan), ...]
    for orch in orchestrations:
        filled.append({})
    unmatched = []
    for idx, stage_name, scan in scans:
        child = children[idx]
        prompt = scan.get('prompt') or ''
        title = child.get('title') or ''
        best_i = None
        best_rank = (0, 0)
        for orch_i, orch in enumerate(orchestrations):
            if not _open_stage_slot(orch, filled[orch_i], stage_name):
                continue
            score = _crew_stage_score(orch, stage_name, prompt, title)
            if score <= 0:
                continue
            title_hit = 1 if _task_matches_title(orch.get('task') or '', title) else 0
            # A higher brief wins. The same brief keeps the title match, then
            # the earlier crew.
            rank = (score, title_hit)
            if rank > best_rank:
                best_rank = rank
                best_i = orch_i
        if best_i is None:
            unmatched.append((child, stage_name, scan))
            continue
        filled[best_i].setdefault(stage_name, []).append((child, scan))
    return filled, unmatched


def _stage_strength(stage: Dict) -> int:
    """2 = final report, 1 = partial answer, 0 = nothing recorded."""
    if (stage.get('report') or '').strip():
        return 2
    if stage.get('report_source') == 'utterance' or (stage.get('utterance') or '').strip():
        return 1
    return 0


def _mark_latest_attempts(orchestrations: List[Dict]):
    """A later report wins. A later empty attempt does not erase an earlier report."""
    best: Dict[str, Tuple[int, int]] = {}
    for index, orch in enumerate(orchestrations):
        for stage in orch.get('stages') or []:
            name = stage.get('name') or ''
            if not name:
                continue
            strength = _stage_strength(stage)
            prev = best.get(name)
            if prev is None or strength > prev[1] or (strength == prev[1] and index > prev[0]):
                best[name] = (index, strength)
    counts: Dict[str, int] = {}
    for orch in orchestrations:
        for stage in orch.get('stages') or []:
            name = stage.get('name') or ''
            if name:
                counts[name] = counts.get(name, 0) + 1
    for index, orch in enumerate(orchestrations):
        for stage in orch.get('stages') or []:
            name = stage.get('name') or ''
            winner = best.get(name, (None, 0))[0]
            stage['superseded'] = bool(name) and winner is not None and winner != index
            stage['latest_attempt'] = bool(name) and winner == index and counts.get(name, 0) > 1


def enrich_orchestrations(parser) -> None:
    """Fill crew items from child sessions. Safe to call once after the jsonl load."""
    orchestrations = [item for item in parser.data if item.get('type') == 'orchestration']
    if not orchestrations:
        return
    by_turn: Dict[int, List[Dict]] = defaultdict(list)
    for item in orchestrations:
        by_turn[int(item.get('turn') or 1)].append(item)
    for items in by_turn.values():
        for index, item in enumerate(items, start=1):
            item['ordinal_in_turn'] = index
            item['turn_orch_count'] = len(items)

    sessions_dir = parser.entry.session_file.parent
    children = _discover_stage_sessions(sessions_dir, parser.session_id)
    filled, unmatched = _assign_stage_sessions(orchestrations, children)
    failed_names = {}
    for item in orchestrations:
        detail = item.get('status_detail') or ''
        match = _FAILED_STAGE_RE.search(detail)
        failed_names[id(item)] = match.group(1) if match else (item.get('failed_stage') or '')

    for orch, assigned in zip(orchestrations, filled):
        names = [s.get('name') for s in orch.get('stage_specs') or []]
        pipeline = split_pipeline_reports(orch.get('pipeline_text') or '', names)
        failed_stage = failed_names.get(id(orch), '')
        queues = {name: list(pairs) for name, pairs in assigned.items()}
        stages = []
        for spec in orch.get('stage_specs') or []:
            stage = _blank_stage(spec)
            name = spec.get('name') or '(unnamed)'
            queue = queues.get(name) or []
            report = pipeline.get(name, '')
            if queue:
                child, scan = queue.pop(0)
                stage['session_id'] = child.get('id') or ''
                stage['session_path'] = str(child.get('jsonl') or '')
                stage['created_at'] = child.get('created_at') or ''
                _apply_child_scan(stage, scan, report,
                                  orch.get('crew_status') or 'running',
                                  failed_stage == name)
            else:
                _apply_child_scan(stage, {}, report,
                                  orch.get('crew_status') or 'running',
                                  failed_stage == name)
            stages.append(stage)
        for name, queue in queues.items():
            while queue:
                child, scan = queue.pop(0)
                stage = _blank_stage({'name': name or '(unnamed)'})
                stage['session_id'] = child.get('id') or ''
                stage['session_path'] = str(child.get('jsonl') or '')
                _apply_child_scan(stage, scan, '', orch.get('crew_status') or 'running',
                                  failed_stage == name)
                stages.append(stage)
        orch['stages'] = stages
        orch.pop('pipeline_text', None)
    if unmatched:
        # A child we could not place still belongs to the nearest crew whose
        # task matches. Park it on that crew so its last state is not dropped.
        for child, stage_name, scan in unmatched:
            host = None
            for orch in orchestrations:
                if _task_matches_title(orch.get('task') or '', child.get('title') or ''):
                    host = orch
                    break
            if host is None:
                continue
            stage = _blank_stage({'name': stage_name or '(unassigned)'})
            stage['session_id'] = child.get('id') or ''
            stage['session_path'] = str(child.get('jsonl') or '')
            _apply_child_scan(stage, scan, '', host.get('crew_status') or 'running', False)
            host.setdefault('stages', []).append(stage)
    _mark_latest_attempts(orchestrations)
    for orch in orchestrations:
        assemble_crew_parts(orch)


def estimate_lines(item: Dict) -> int:
    t = item['type']
    if t == 'orchestration':
        lines, _chars = orchestration_metrics(item, default_orch_filter(), 0)
        return lines
    if t in ('user_message', 'agent_message', 'reasoning', 'summarization'):
        c = item.get('content', '')
        return (c.count('\n') + 4) if c else 0
    if t == 'file_read':
        n = len(item.get('files') or [])
        out = item.get('output', '') or ''
        return n + 3 + (out.count('\n') + 4 if out.strip() else 0)
    if t in ('file_create', 'file_edit'):
        c = item.get('modified_content', '') or ''
        return (c.count('\n') + 6) if c else 4
    if t == 'file_delete':
        return 2
    if t == 'terminal_cmd':
        return (item.get('command', '').count('\n') + 6)
    if t == 'terminal_output':
        o = item.get('output', '') or ''
        return (o.count('\n') + 4) if o.strip() else 0
    if t == 'process_ctrl':
        o = item.get('output', '') or ''
        return 3 + (o.count('\n') + 4 if isinstance(o, str) and o.strip() else 0)
    if t == 'code_search':
        o = item.get('output', '') or ''
        return 3 + (o.count('\n') + 4 if isinstance(o, str) and o.strip() else 0)
    if t == 'web_search':
        return 3 + len(item.get('results') or [])
    if t == 'web_fetch':
        c = item.get('content', '') or ''
        return 3 + (c.count('\n') + 4 if isinstance(c, str) and c.strip() else 0)
    if t == 'mcp_tool':
        r = item.get('response', '') or ''
        return 4 + (r.count('\n') + 2 if isinstance(r, str) and r.strip() else 0)
    if t == 'sub_agent':
        p = item.get('prompt', '') or item.get('response', '') or ''
        return 4 + (p.count('\n') + 2 if isinstance(p, str) else 0)
    if t in ('intent', 'error', 'session_event', 'diagnostics'):
        return 2
    if t == 'user_input':
        return 3 * max(1, len(item.get('questions') or []))
    return 1

def strip_ide_context(content: str) -> str:
    """Strip Kiro / IDE noise from user-message content."""
    if not content:
        return content
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    content = re.sub(r'(?is)<([A-Za-z0-9_-]*context[A-Za-z0-9_-]*)>.*?</\1>\s*', '\n', content)
    drop_block_prefixes = (
        "## active file:", "## active selection of the file:",
        "## open tabs:", "## files mentioned by the user:",
    )
    drop_line_prefixes = (
        "# context from my ide setup:", "## my request:",
    )
    lines = content.split('\n')
    cleaned = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip().lower()
        if any(stripped.startswith(p) for p in drop_line_prefixes):
            i += 1; continue
        if any(stripped.startswith(p) for p in drop_block_prefixes):
            i += 1
            while i < len(lines):
                nl = lines[i].strip().lower()
                if any(nl.startswith(p) for p in drop_line_prefixes):
                    break
                if re.match(r'#{1,6}\s', lines[i].strip()):
                    break
                i += 1
            continue
        cleaned.append(lines[i])
        i += 1
    out = "\n".join(cleaned)
    out = re.sub(r'\n{3,}', '\n\n', out)
    return out.strip()

# ──────────────────────────────────────────────────────────────
# Keyboard Input (raw terminal, single keypress)
# ──────────────────────────────────────────────────────────────
def _clear_screen():
    os.system('cls' if _IS_WINDOWS else 'clear')

def read_key() -> str:
    if _IS_WINDOWS:
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ('\r', '\n'):
            return 'ENTER'
        if ch == ' ':
            return 'SPACE'
        if ch == '\x03':
            raise KeyboardInterrupt
        if ch == '\x1b':
            return 'ESC'
        if ch == '\xe0' or ch == '\x00':
            ext = msvcrt.getwch()
            return {'H': 'UP', 'P': 'DOWN', 'M': 'RIGHT', 'K': 'LEFT'}.get(ext, '')
        return ch.upper()
    else:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                ch2 = sys.stdin.read(1)
                if ch2 == '[':
                    ch3 = sys.stdin.read(1)
                    return {'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT'}.get(ch3, '')
                return 'ESC'
            if ch in ('\r', '\n'):
                return 'ENTER'
            if ch == ' ':
                return 'SPACE'
            if ch == '\x03':
                raise KeyboardInterrupt
            return ch.upper()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ──────────────────────────────────────────────────────────────
# Interactive Section Filter
# ──────────────────────────────────────────────────────────────
OUTPUT_SECTIONS = {'terminal_output', 'process_ctrl', 'web_fetch', 'file_read'}
CAP_STEPS = [0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 50, 100, 200, 500]
MSG_CAP_STEPS = [0, 5] + list(range(10, 101, 10)) + [150, 200, 300, 500]

def _filter_row_specs() -> Tuple[List[Dict], int]:
    specs: List[Dict] = []

    def add(kind: str, key: str = '', name: str = '', emoji: str = ''):
        specs.append({'kind': kind, 'key': key, 'name': name, 'emoji': emoji})

    for key, name, emoji, _default in SECTION_DEFS:
        add('section', key, name, emoji)
        if key == 'orchestration':
            for status_key, status_label in ORCH_STATUSES:
                add('orch_status', status_key, status_label)
            for layer_key, layer_label, _on in ORCH_LAYERS:
                add('orch_layer', layer_key, layer_label)
            add('orch_flag', 'latest_only', 'Latest attempt only')
    add('sep')
    add('clean', 'clean', 'Clean Chat', '✂️ ')
    add('cap', 'output', 'Output cap', '📤')
    add('cap', 'user', 'User message cap', '👤')
    add('cap', 'agent', 'Agent message cap', '🤖')
    add('cap', 'reason', 'Reasoning cap', '🧠')
    add('cap', 'summary', 'Compaction summary cap', '✂️ ')
    next_id = 0
    for spec in specs:
        if spec['kind'] == 'sep':
            spec['id'] = None
        else:
            spec['id'] = next_id
            next_id += 1
    return specs, next_id


FILTER_ROW_SPECS, FILTER_ROW_COUNT = _filter_row_specs()


def _item_export_chars(item: Dict, output_cap: int) -> int:
    t = item.get('type')
    if t == 'terminal_output':
        output = item.get('output') or ''
        if not isinstance(output, str) or not output.strip():
            return 0
        total = text_line_count(output)
        if output_cap > 0 and total > output_cap:
            return int(len(output) * (output_cap / total)) + 48
        return len(output) + 48
    chars = 0
    for key in ('content', 'output', 'command', 'prompt', 'response', 'modified_content',
                'original_content', 'message', 'query', 'explanation', 'url'):
        value = item.get(key)
        if isinstance(value, str):
            chars += len(value)
    return chars + 24


def compute_filter_metrics(parsers, orch_filter: Dict, output_cap: int,
                           user_cap: int, agent_cap: int, reason_cap: int,
                           summary_cap: int) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, int], Dict]:
    """Line and character totals for the filter screen."""
    counts = {s[0]: 0 for s in SECTION_DEFS}
    char_counts = {s[0]: 0 for s in SECTION_DEFS}
    msg_counts: Dict[str, int] = {}
    caps_map = {
        'user_message': user_cap,
        'agent_message': agent_cap,
        'reasoning': reason_cap,
        'summarization': summary_cap,
    }
    for parser in parsers or []:
        keep = set(range(len(parser.data)))
        seen = {key: 0 for key in caps_map}
        for index in range(len(parser.data) - 1, -1, -1):
            kind = parser.data[index].get('type')
            if kind in caps_map and caps_map[kind] > 0:
                if seen[kind] >= caps_map[kind]:
                    keep.discard(index)
                seen[kind] += 1
        for index, item in enumerate(parser.data):
            if index not in keep:
                continue
            kind = item.get('type')
            if kind == 'orchestration':
                continue
            if kind in ('user_message', 'agent_message', 'reasoning', 'summarization'):
                msg_counts[kind] = msg_counts.get(kind, 0) + 1
            lines = estimate_lines(item)
            chars = _item_export_chars(item, output_cap)
            if kind == 'terminal_output' and output_cap > 0:
                output = item.get('output') or ''
                if isinstance(output, str):
                    total = output.count('\n') + 1 if output.strip() else 0
                    lines = min(total, output_cap) + 3 if total > 0 else 0
            counts[kind] = counts.get(kind, 0) + lines
            char_counts[kind] = char_counts.get(kind, 0) + chars
        if getattr(parser, 'metadata', None):
            counts['session_meta'] = counts.get('session_meta', 0) + 7
            char_counts['session_meta'] = char_counts.get('session_meta', 0) + 180
    account = orchestration_accounting(parsers, orch_filter, output_cap)
    counts['orchestration'] = account['selected_lines']
    char_counts['orchestration'] = account['selected_chars']
    return counts, char_counts, msg_counts, account


def _goto_col(col: int) -> str:
    return f'\033[{int(col)}G'


def _paint(text: str, *styles: str) -> str:
    if not text:
        return ''
    if not styles:
        return text
    return ''.join(styles) + text + Style.RESET


def _token_body(tokens: int) -> str:
    """Fixed shape `~###### tok` so every row's token figure is the same width."""
    num = format_tokens(tokens)
    if len(num) < 6:
        num = num.rjust(6)
    return f'~{num} tok'


def _filter_table_columns(width: int, lines_w: int, tokens_w: int) -> Dict:
    """Absolute 1-based columns for the section filter.

    Emoji are not one display width, and a label's length used to push the
    line and token figures to a different place on every row. Later fields
    are placed with cursor addressing so a wide glyph cannot move them.
    """
    width = max(48, int(width or 100))
    last = width - 1
    lines_w = max(8, int(lines_w))
    tokens_w = max(len('~ TOKENS'), int(tokens_w))
    name_col = 11
    span = last - name_col + 1
    gap_name, gap_tok, gap_note = 2, 1, 2
    tail = gap_name + lines_w + gap_tok + tokens_w
    name_w = 24
    note_w = span - name_w - tail - gap_note
    if note_w < 10:
        name_w = max(22, min(24, span - tail))
        note_w = span - name_w - tail - gap_note
    if note_w < 0:
        name_w = max(12, span - tail)
        note_w = 0
    if name_w + tail > span:
        name_w = max(8, span - tail)
        note_w = 0
    lines_col = name_col + name_w + gap_name
    tokens_col = lines_col + lines_w + gap_tok
    note_col = tokens_col + tokens_w + gap_note if note_w > 0 else 0
    return {
        'arrow': 3,
        'toggle': 5,
        'emoji': 8,
        'name': name_col,
        'name_w': name_w,
        'lines': lines_col,
        'lines_w': lines_w,
        'tokens': tokens_col,
        'tokens_w': tokens_w,
        'note': note_col,
        'note_w': max(0, note_w),
        'last': last,
        'width': width,
    }


def _filter_line(pieces: List[Tuple[int, str]]) -> str:
    parts = []
    for col, text in pieces:
        if col and text:
            parts.append(_goto_col(col) + text)
    return ''.join(parts)


# Status, layer, and latest-attempt rows belong to the Orchestrations section.
_ORCH_SUB_KINDS = ('orch_status', 'orch_layer', 'orch_flag')


def render_filter_column_head(cols: Dict) -> str:
    return _filter_line([
        (cols['lines'], _paint('LINES'.rjust(cols['lines_w']), Style.DIM)),
        (cols['tokens'], _paint('~ TOKENS'.ljust(cols['tokens_w']), Style.DIM)),
    ])


def render_filter_rows(
    specs,
    cursor: int,
    fstate: Dict[str, bool],
    orch_filter: Dict,
    agg: Dict[str, int],
    char_counts: Dict[str, int],
    msg_cnt: Dict[str, int],
    account: Dict,
    clean_content: bool,
    cap_display: Dict[str, int],
    width: int,
    sel_lines: int = 0,
    total_lines: int = 0,
    sel_tokens: int = 0,
    total_tokens: int = 0,
) -> Tuple[List[Tuple[Optional[int], str]], Dict]:
    """One filter row per spec. Line and token figures share fixed columns."""
    account = account or {}
    by_status = account.get('by_status') or {}
    by_layer = account.get('by_layer') or {}
    by_crews = account.get('by_status_crews') or {}
    orch_on = bool(fstate.get('orchestration', False))
    prepared = []
    line_widths = [len(f'{int(sel_lines):,}'), len(f'{int(total_lines):,}')]
    token_widths = [len(_token_body(sel_tokens)), len(_token_body(total_tokens))]

    for spec in specs:
        kind = spec['kind']
        if kind == 'sep':
            prepared.append({'sep': True})
            continue
        is_sub = kind in _ORCH_SUB_KINDS
        lines = 0
        tokens = 0
        note = ''
        metric = kind in ('section', 'orch_status', 'orch_layer')
        cap_text = ''
        if kind == 'section':
            key = spec['key']
            is_on = bool(fstate.get(key, False))
            lines = int(agg.get(key, 0) or 0)
            tokens = estimate_tokens(int(char_counts.get(key, 0) or 0))
            counted = int(msg_cnt.get(key) or 0)
            if key in ('user_message', 'agent_message', 'reasoning', 'summarization') and counted:
                note = '1 msg' if counted == 1 else f'{counted:,} msgs'
            elif key == 'orchestration' and account.get('crews'):
                crews = int(account.get('crews') or 0)
                stages = int(account.get('stages') or 0)
                note = '1 crew' if crews == 1 else f'{crews:,} crews'
                if stages:
                    note += ' · 1 stage' if stages == 1 else f' · {stages:,} stages'
        elif kind == 'orch_status':
            is_on = bool((orch_filter.get('statuses') or {}).get(spec['key']))
            info = by_status.get(spec['key']) or {}
            lines = int(info.get('lines') or 0)
            tokens = int(info.get('tokens') or 0)
            n_crews = int(by_crews.get(spec['key']) or 0)
            if n_crews:
                note = '1 crew' if n_crews == 1 else f'{n_crews:,} crews'
        elif kind == 'orch_layer':
            is_on = bool((orch_filter.get('layers') or {}).get(spec['key']))
            info = by_layer.get(spec['key']) or {}
            lines = int(info.get('lines') or 0)
            tokens = int(info.get('tokens') or 0)
        elif kind == 'orch_flag':
            is_on = bool(orch_filter.get('latest_only'))
            note = 'hides older attempts'
        elif kind == 'clean':
            is_on = bool(clean_content)
            note = 'strip IDE context'
        else:
            is_on = True
            value = int(cap_display.get(spec['key'], 0) or 0)
            if not value:
                cap_text = 'ALL'
            elif spec['key'] == 'output':
                cap_text = str(value)
            else:
                cap_text = f'Last {value}'
            if spec.get('id') == cursor:
                note = '◀▶'
        prepared.append({
            'spec': spec,
            'is_sub': is_sub,
            'is_on': is_on,
            'lines': lines,
            'tokens': tokens,
            'note': note,
            'metric': metric,
            'cap_text': cap_text,
        })
        if metric:
            line_widths.append(len(f'{lines:,}'))
            token_widths.append(len(_token_body(tokens)))
        elif cap_text:
            line_widths.append(len(cap_text))

    cols = _filter_table_columns(width, max(line_widths or [8]), max(token_widths or [11]))
    rows: List[Tuple[Optional[int], str]] = []
    rule = _paint('─' * max(8, cols['last'] - 2), Style.DIM)

    for item in prepared:
        if item.get('sep'):
            rows.append((None, _filter_line([(3, rule)])))
            continue
        spec = item['spec']
        kind = spec['kind']
        rid = spec.get('id')
        is_cursor = rid == cursor
        is_on = item['is_on']
        is_sub = item['is_sub']
        quiet = (is_sub and not orch_on) or not is_on
        pieces: List[Tuple[int, str]] = []
        if is_cursor:
            pieces.append((cols['arrow'], _paint('▸', Style.BOLD, Style.YELLOW)))
        if kind != 'cap':
            toggle = _paint('██', Style.GREEN) if is_on else _paint('░░', Style.DIM)
            pieces.append((cols['toggle'], toggle))
        glyph = (spec.get('emoji') or '').strip()
        if glyph and not is_sub:
            if quiet and not is_cursor and kind != 'cap':
                pieces.append((cols['emoji'], _paint(glyph, Style.DIM)))
            else:
                pieces.append((cols['emoji'], glyph))
        if kind == 'cap':
            name_styles = (Style.BOLD,) if is_cursor else (Style.DIM,)
        elif is_cursor and is_on:
            name_styles = (Style.BOLD, Style.GREEN)
        elif is_cursor:
            name_styles = (Style.BOLD, Style.RED)
        elif quiet:
            name_styles = (Style.DIM,)
        else:
            name_styles = ()
        label = spec.get('name') or ''
        if is_sub:
            # One column before the shared name, so the mark sits against the
            # sub-option and the words still start in the name column.
            mark_style = (Style.BOLD, Style.YELLOW) if is_cursor else (Style.DIM,)
            pieces.append((cols['name'] - 2, _paint('↳', *mark_style)))
        pieces.append((cols['name'], _paint(_clip(label, cols['name_w']), *name_styles)))
        if item['metric']:
            count_style = (Style.DIM,) if quiet else (Style.CYAN,)
            pieces.append((
                cols['lines'],
                _paint(f"{item['lines']:,}".rjust(cols['lines_w']), *count_style),
            ))
            pieces.append((
                cols['tokens'],
                _paint(_token_body(item['tokens']).ljust(cols['tokens_w']), *count_style),
            ))
        elif item['cap_text']:
            if is_cursor:
                value_style = (Style.BOLD, Style.YELLOW)
            elif item['cap_text'] == 'ALL':
                value_style = (Style.DIM,)
            else:
                value_style = (Style.YELLOW,)
            pieces.append((
                cols['lines'],
                _paint(item['cap_text'].rjust(cols['lines_w']), *value_style),
            ))
        if item['note'] and cols['note_w']:
            pieces.append((cols['note'], _paint(_clip(item['note'], cols['note_w']), Style.DIM)))
        rows.append((rid, _filter_line(pieces)))
    return rows, cols


def render_filter_footer(cols: Dict, sel_lines: int, total_lines: int,
                         sel_tokens: int, total_tokens: int, pct: float) -> List[str]:
    """Selected and overall figures in the same columns as the rows."""
    def figures(lines: int, tokens: int, *styles: str) -> List[Tuple[int, str]]:
        return [
            (cols['lines'], _paint(f'{int(lines):,}'.rjust(cols['lines_w']), *styles)),
            (cols['tokens'], _paint(_token_body(tokens).ljust(cols['tokens_w']), *styles)),
        ]

    sel_style = (Style.GREEN, Style.BOLD) if pct > 0 else (Style.RED, Style.BOLD)
    top: List[Tuple[int, str]] = []
    # Sit the bar in the blank name area, ending one column before the figures.
    bar_w = min(24, cols['lines'] - 4)
    if bar_w >= 8:
        filled = int(bar_w * max(0.0, min(100.0, pct)) / 100)
        bar = _paint('█' * filled, Style.GREEN) + _paint('░' * (bar_w - filled), Style.DIM)
        top.append((cols['lines'] - bar_w - 1, bar))
    top.extend(figures(sel_lines, sel_tokens, *sel_style))
    bottom = figures(total_lines, total_tokens, Style.DIM)
    if cols['note_w']:
        top.append((cols['note'], _paint(_clip(f'selected · {pct:.0f}%', cols['note_w']), Style.DIM)))
        bottom.append((cols['note'], _paint(_clip('all sections', cols['note_w']), Style.DIM)))
    rule = _filter_line([(3, _paint('━' * max(8, cols['last'] - 2), Style.DIM))])
    return [rule, _filter_line(top), _filter_line(bottom)]


def interactive_filter(parsers: List[SessionParser], scope_label: str = "") -> Tuple[Dict[str, bool], bool, int, int, int, int, int, Dict]:
    _line_cache: Dict = {}

    def get_lines(cap_out, cap_user, cap_agent, cap_reason, cap_sum, orch_key):
        key = (cap_out, cap_user, cap_agent, cap_reason, cap_sum, orch_key)
        if key in _line_cache:
            return _line_cache[key]
        counts, char_counts, msg_counts, account = compute_filter_metrics(
            parsers, orch_filter, cap_out, cap_user, cap_agent, cap_reason, cap_sum,
        )
        packed = (counts, char_counts, msg_counts, account)
        _line_cache[key] = packed
        return packed

    fstate: Dict[str, bool] = {s[0]: s[3] for s in SECTION_DEFS}
    orch_filter = default_orch_filter()
    clean_content = False
    output_cap = 8;  cap_idx = CAP_STEPS.index(8)
    user_cap = 0;    u_idx = 0
    agent_cap = 0;   a_idx = 0
    reason_cap = 0;  r_idx = 0
    summary_cap = 0; s_idx = 0

    cursor = 0
    specs = FILTER_ROW_SPECS
    num_items = FILTER_ROW_COUNT
    spec_by_id = {spec['id']: spec for spec in specs if spec.get('id') is not None}

    import shutil as _shutil

    # Alternate screen buffer (htop/vim/less style) — keeps the filter UI off
    # the terminal's scrollback so it can't accumulate frames there. We
    # combine that with an INTERNAL viewport: when the filter has more rows
    # than the terminal can show, we render only the slice around the cursor
    # and surface ▲/▼ markers so you always know there's more.
    sys.stdout.write('\033[?1049h\033[?25l\033[H\033[2J')
    sys.stdout.flush()

    scroll_offset = 0

    def _reset_orch():
        fresh = default_orch_filter()
        orch_filter['statuses'].clear()
        orch_filter['statuses'].update(fresh['statuses'])
        orch_filter['layers'].clear()
        orch_filter['layers'].update(fresh['layers'])
        orch_filter['latest_only'] = False

    def _orch_key():
        return (
            tuple(key for key, _label in ORCH_STATUSES if orch_filter['statuses'].get(key)),
            tuple(key for key, _label, _on in ORCH_LAYERS if orch_filter['layers'].get(key)),
            bool(orch_filter.get('latest_only')),
        )

    try:
        while True:
            agg, char_counts, msg_cnt, account = get_lines(
                output_cap, user_cap, agent_cap, reason_cap, summary_cap, _orch_key(),
            )
            total_lines = sum(agg.get(s[0], 0) for s in SECTION_DEFS)
            total_chars = sum(char_counts.get(s[0], 0) for s in SECTION_DEFS)
            sel_lines = sum(agg.get(s[0], 0) for s in SECTION_DEFS if fstate.get(s[0], False))
            sel_chars = sum(char_counts.get(s[0], 0) for s in SECTION_DEFS if fstate.get(s[0], False))
            pct = (sel_lines / total_lines * 100) if total_lines > 0 else 0

            cap_display = {
                'output': output_cap,
                'user': user_cap,
                'agent': agent_cap,
                'reason': reason_cap,
                'summary': summary_cap,
            }
            term_size = _shutil.get_terminal_size((100, 30))
            term_w = max(48, term_size.columns)
            term_h = max(12, term_size.lines)
            sel_tokens = estimate_tokens(sel_chars)
            total_tokens = estimate_tokens(total_chars)
            mid_rows, cols = render_filter_rows(
                specs, cursor, fstate, orch_filter, agg, char_counts, msg_cnt, account,
                clean_content, cap_display, term_w,
                sel_lines=sel_lines, total_lines=total_lines,
                sel_tokens=sel_tokens, total_tokens=total_tokens,
            )

            HEADER_LINES = 3
            FOOTER_LINES = 6
            INDICATOR_LINES = 2
            view_h = max(5, term_h - HEADER_LINES - FOOTER_LINES - INDICATOR_LINES)
            cur_pos = next((idx for idx, (row_id, _) in enumerate(mid_rows) if row_id == cursor), 0)
            if cur_pos < scroll_offset:
                scroll_offset = cur_pos
            elif cur_pos >= scroll_offset + view_h:
                scroll_offset = cur_pos - view_h + 1
            max_offset = max(0, len(mid_rows) - view_h)
            scroll_offset = max(0, min(scroll_offset, max_offset))

            out = ['\033[H\033[2J']
            label = f"{len(parsers)} session{'s' if len(parsers) > 1 else ''}"
            if scope_label:
                label += f" · {scope_label}"
            out.append(f"  {Style.BOLD}{Style.HEADER}KIRO SECTION FILTER{Style.RESET}  {Style.DIM}({label}){Style.RESET}")
            rule = _paint('━' * max(8, cols['last'] - 2), Style.DIM)
            out.append(_filter_line([(3, rule)]))
            out.append(render_filter_column_head(cols))
            if scroll_offset > 0:
                out.append(_filter_line([(3, _paint(f'▲ {scroll_offset} more above', Style.DIM))]))
            else:
                out.append('')
            for _rid, text in mid_rows[scroll_offset:scroll_offset + view_h]:
                out.append(text)
            below = max(0, len(mid_rows) - (scroll_offset + view_h))
            if below > 0:
                out.append(_filter_line([(3, _paint(f'▼ {below} more below', Style.DIM))]))
            else:
                out.append('')
            out.extend(render_filter_footer(
                cols, sel_lines, total_lines, sel_tokens, total_tokens, pct,
            ))
            out.append(_filter_line([(3, _paint(
                '↑↓ move   ⏎ toggle   ◀▶ cap   Q export   A all   N none   D defaults   1-7 presets',
                Style.DIM))]))
            sys.stdout.write('\n'.join(out))
            sys.stdout.flush()

            key = read_key()
            spec = spec_by_id.get(cursor)
            if key == 'UP':
                cursor = (cursor - 1) % num_items
            elif key == 'DOWN':
                cursor = (cursor + 1) % num_items
            elif key in ('ENTER', 'SPACE') and spec:
                if spec['kind'] == 'section':
                    fstate[spec['key']] = not fstate.get(spec['key'], False)
                elif spec['kind'] == 'orch_status':
                    orch_filter['statuses'][spec['key']] = not orch_filter['statuses'].get(spec['key'], False)
                elif spec['kind'] == 'orch_layer':
                    orch_filter['layers'][spec['key']] = not orch_filter['layers'].get(spec['key'], False)
                elif spec['kind'] == 'orch_flag':
                    orch_filter['latest_only'] = not bool(orch_filter.get('latest_only'))
                elif spec['kind'] == 'clean':
                    clean_content = not clean_content
            elif key in ('LEFT', 'RIGHT') and spec and spec['kind'] == 'cap':
                step = -1 if key == 'LEFT' else 1
                cap_key = spec['key']
                if cap_key == 'output':
                    cap_idx = min(len(CAP_STEPS) - 1, max(0, cap_idx + step))
                    output_cap = CAP_STEPS[cap_idx]
                elif cap_key == 'user':
                    u_idx = min(len(MSG_CAP_STEPS) - 1, max(0, u_idx + step))
                    user_cap = MSG_CAP_STEPS[u_idx]
                elif cap_key == 'agent':
                    a_idx = min(len(MSG_CAP_STEPS) - 1, max(0, a_idx + step))
                    agent_cap = MSG_CAP_STEPS[a_idx]
                elif cap_key == 'reason':
                    r_idx = min(len(MSG_CAP_STEPS) - 1, max(0, r_idx + step))
                    reason_cap = MSG_CAP_STEPS[r_idx]
                elif cap_key == 'summary':
                    s_idx = min(len(MSG_CAP_STEPS) - 1, max(0, s_idx + step))
                    summary_cap = MSG_CAP_STEPS[s_idx]
            elif key == 'A':
                for s in SECTION_DEFS:
                    fstate[s[0]] = True
            elif key == 'N':
                for s in SECTION_DEFS:
                    fstate[s[0]] = False
            elif key == 'I':
                for s in SECTION_DEFS:
                    fstate[s[0]] = not fstate[s[0]]
            elif key == 'D':
                for s in SECTION_DEFS:
                    fstate[s[0]] = s[3]
                _reset_orch()
                clean_content = False
                output_cap = 8
                cap_idx = CAP_STEPS.index(8)
                user_cap = 0
                u_idx = 0
                agent_cap = 0
                a_idx = 0
                reason_cap = 0
                r_idx = 0
                summary_cap = 0
                s_idx = 0
            elif key == 'Q' or key == 'ESC':
                break
            elif key.isdigit():
                pi = int(key) - 1
                if 0 <= pi < len(FILTER_PRESETS):
                    _pname, pkeys, pclean = FILTER_PRESETS[pi]
                    if pkeys is None:
                        for s in SECTION_DEFS:
                            fstate[s[0]] = s[3]
                        _reset_orch()
                    else:
                        for s in SECTION_DEFS:
                            fstate[s[0]] = s[0] in pkeys
                    clean_content = pclean
    finally:
        sys.stdout.write('\033[?25h\033[?1049l')
        sys.stdout.flush()
    return fstate, clean_content, output_cap, user_cap, agent_cap, reason_cap, summary_cap, orch_filter

# ──────────────────────────────────────────────────────────────
# Extraction scope
# ──────────────────────────────────────────────────────────────
def select_extraction_scope(parsers: List[SessionParser]) -> Tuple[str, int]:
    _clear_screen()
    print(f"\n  {Style.BOLD}{Style.HEADER}EXTRACTION SCOPE{Style.RESET}\n")
    print(f"  {Style.DIM}{'━' * 64}{Style.RESET}\n")
    for i, p in enumerate(parsers):
        tc = p.get_turn_count()
        label = f"Session {i+1}" if len(parsers) > 1 else "Session"
        title = p.title
        if len(title) > 48:
            title = title[:45] + '...'
        badges = []
        if p.started_from_compaction:
            badges.append('↻ from compaction')
        if p.compaction_count:
            badges.append(f'✂ compacts ×{p.compaction_count}')
        if p.continuation_count:
            badges.append(f'↪ ×{p.continuation_count}')
        bs = ' '.join(badges)
        print(f"  {Style.CYAN}{label}{Style.RESET}  {Style.DIM}{title}{Style.RESET}")
        ctx_str = ""
        if p.context_usage_pct:
            ctx_str = f"  {Style.DIM}context: {p.context_usage_pct:.1f}%{Style.RESET}"
        bs_str = f"  {Style.YELLOW}{bs}{Style.RESET}" if bs else ""
        print(f"           {Style.BOLD}{tc}{Style.RESET} turn{'s' if tc != 1 else ''}{ctx_str}{bs_str}\n")
    print(f"  {Style.DIM}{'━' * 64}{Style.RESET}")
    print(f"  {Style.DIM}A 'turn' = one user message + the agent work that followed.{Style.RESET}\n")
    print(f"    {Style.GREEN}[F]{Style.RESET} Full Session — export every turn  {Style.DIM}(Default){Style.RESET}")
    print(f"    {Style.YELLOW}[L]{Style.RESET} Last N Turns — only the most recent N turns")
    print(f"    {Style.CYAN}[C]{Style.RESET} Live Context — replace pre-compaction history with the summary\n")
    choice = input(f"  {Style.BOLD}Select > {Style.RESET}").strip().lower()
    if choice == 'c':
        return 'live', 0
    if choice == 'l':
        while True:
            n = input(f"  {Style.BOLD}How many recent turns? > {Style.RESET}").strip()
            if n.isdigit() and int(n) > 0:
                return 'last_n', int(n)
            print(f"  {Style.error('Enter a positive number.')}")
    return 'full', 0

# ──────────────────────────────────────────────────────────────
# Session listing UI
# ──────────────────────────────────────────────────────────────
def format_relative_time(mtime: float) -> str:
    now = datetime.now().timestamp()
    diff = max(0, int(now - mtime))
    if diff < 60: return "(just now)"
    mins = diff // 60
    hours = mins // 60
    days = hours // 24
    if days > 0:
        return f"({days}d {hours % 24}h ago)"
    elif hours > 0:
        return f"({hours}h {mins % 60}m ago)"
    return f"({mins}m ago)"

def copy_to_clipboard(text: str) -> bool:
    import subprocess
    try:
        if sys.platform == 'win32':
            subprocess.run(['clip'], input=text.encode('utf-16le'), check=True)
        elif sys.platform == 'darwin':
            subprocess.run(['pbcopy'], input=text.encode('utf-8'), check=True)
        else:
            try:
                subprocess.run(['xclip', '-selection', 'clipboard'], input=text.encode('utf-8'),
                               check=True, stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                subprocess.run(['xsel', '--clipboard', '--input'], input=text.encode('utf-8'),
                               check=True, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def open_in_file_explorer(path: Path) -> Tuple[bool, Optional[Path]]:
    """Open the OS file manager at the SAVED FILE'S directory.

    Returns (success, opened_directory) — the second value is the actual
    folder that was opened, so the caller can print it back to the user
    for verification. We deliberately do NOT use Windows Explorer's
    `/select,<path>` trick: it silently fails (often opening the user's
    home folder instead) when the path contains spaces, which is exactly
    the case here. The user asked for the directory where the file was
    saved — we open exactly that.
    """
    import subprocess
    try:
        p = Path(path).resolve()
        target = p if p.is_dir() else p.parent
        if not target.exists():
            return False, target
        if sys.platform == 'win32':
            os.startfile(str(target))  # type: ignore[attr-defined]
            return True, target
        if sys.platform == 'darwin':
            subprocess.Popen(['open', str(target)])
            return True, target
        # Linux / other Unix
        for opener in ('xdg-open', 'gio open', 'gnome-open', 'kde-open'):
            try:
                cmd = opener.split() + [str(target)]
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True, target
            except FileNotFoundError:
                continue
        return False, target
    except Exception:
        return False, None


def combine_parsers_to_markdown(parsers: List['SessionParser'],
                                section_filter: Dict[str, bool],
                                clean_content: bool,
                                output_cap: int,
                                user_cap: int, agent_cap: int,
                                reasoning_cap: int, summary_cap: int,
                                orch_filter: Optional[Dict] = None) -> Tuple[str, str]:
    """Build a single Markdown document containing every parser's output, with
    clear `## ▶ Session N of K` dividers between them. Returns (md, filename)."""
    parts: List[str] = []

    # ---- Header / banner ----
    try:
        dates = [p.date_created for p in parsers if p.date_created]
        first_dt = datetime.fromtimestamp(min(dates) / 1000.0) if dates else None
        last_dt  = datetime.fromtimestamp(max(dates) / 1000.0) if dates else None
        span = f"{first_dt.strftime('%Y-%m-%d')} → {last_dt.strftime('%Y-%m-%d')}" if first_dt else '?'
    except Exception:
        span = '?'
    workspaces = sorted({p.workspace_dir for p in parsers if p.workspace_dir})
    total_turns = sum(p.user_turn_count for p in parsers)
    total_credits = sum(p.credits_used for p in parsers)

    banner_title = (
        f"{len(parsers)} Sessions Combined"
        if len({p.workspace_dir for p in parsers}) > 1
        else f"{len(parsers)} Sessions — {short_workspace(workspaces[0]) if workspaces else '?'}"
    )
    parts.append(f"# {banner_title}\n")
    parts.append('```yaml')
    parts.append(f"Sessions:    {len(parsers)}")
    parts.append(f"Span:        {span}")
    if workspaces:
        if len(workspaces) == 1:
            parts.append(f"Workspace:   {workspaces[0]}")
        else:
            parts.append(f"Workspaces:  ({len(workspaces)})")
            for w in workspaces:
                parts.append(f"  - {w}")
    parts.append(f"User turns:  {total_turns}")
    if total_credits:
        parts.append(f"Credits:     {total_credits:.4f}")
    parts.append('Members:')
    for i, p in enumerate(parsers, start=1):
        try:
            when = datetime.fromtimestamp((p.date_created or 0) / 1000.0).strftime('%Y-%m-%d %H:%M')
        except Exception:
            when = '?'
        parts.append(f"  {i:>2}. {when}  [{p.session_id[:8]}]  {p.title[:60]}")
    parts.append('```\n')

    # ---- Per-session bodies ----
    for i, p in enumerate(parsers, start=1):
        parts.append('\n' + '─' * 80)
        parts.append(f"## ▶ Session {i} of {len(parsers)}  —  {p.title}")
        parts.append('')
        body = p.to_markdown(
            section_filter=section_filter,
            clean_content=clean_content,
            output_cap=output_cap,
            user_cap=user_cap,
            agent_cap=agent_cap,
            reasoning_cap=reasoning_cap,
            summary_cap=summary_cap,
            orch_filter=orch_filter,
        )
        # Drop the per-session H1 — we already have a chain title
        body_lines = body.splitlines()
        if body_lines and body_lines[0].startswith('# '):
            body_lines = body_lines[1:]
        parts.append('\n'.join(body_lines).lstrip())

    # ---- Filename ----
    safe_title = clean_filename(parsers[0].title)
    try:
        date_prefix = datetime.fromtimestamp(
            (max((p.date_created or 0) for p in parsers)) / 1000.0
        ).strftime("%Y%m%d")
    except Exception:
        date_prefix = datetime.now().strftime("%Y%m%d")
    filename = f"{date_prefix}_combined-{len(parsers)}_{safe_title}.md"
    return '\n'.join(parts), filename


def maybe_open_directory(written_paths: List[Path]):
    """Ask whether to open the folder where the files were saved. Always
    opens the saved-file's directory directly (no Explorer /select trick)
    so the user lands inside the exact folder they expect."""
    if not written_paths:
        return
    dirs: List[Path] = []
    seen: Set[str] = set()
    for p in written_paths:
        try:
            d = p.resolve().parent
        except Exception:
            d = p.parent
        key = _norm_ws_path(str(d))
        if key not in seen:
            seen.add(key)
            dirs.append(d)
    if not dirs:
        return
    target_label = str(dirs[0]) if len(dirs) == 1 else f"{len(dirs)} output directories"
    answer = input(
        f"\n  {Style.BOLD}Open output directory?{Style.RESET}  "
        f"{Style.DIM}{target_label}{Style.RESET}  "
        f"{Style.BOLD}[Y/n] {Style.RESET}"
    ).strip().lower()
    if answer and answer not in ('y', 'yes'):
        return
    for target_dir in dirs:
        success, opened = open_in_file_explorer(target_dir)
        if success:
            print(f"  {Style.GREEN}➜{Style.RESET} Opened {opened}")
        else:
            print(f"  {Style.warn('Could not open file explorer.')}  "
                  f"{Style.DIM}({opened or target_dir}){Style.RESET}")


def script_output_directory() -> Path:
    try:
        return Path(__file__).parent.resolve()
    except NameError:
        return Path.cwd().resolve()


def _project_dirs_from_parsers(parsers: List['SessionParser']) -> List[Path]:
    dirs: List[Path] = []
    seen: Set[str] = set()
    for p in parsers:
        ws = getattr(p, 'workspace_dir', '') or ''
        if not ws:
            continue
        try:
            d = Path(ws).expanduser().resolve()
        except Exception:
            continue
        if not d.exists() or not d.is_dir():
            continue
        key = _norm_ws_path(str(d))
        if key not in seen:
            seen.add(key)
            dirs.append(d)
    return dirs


def choose_output_location(parsers: List['SessionParser'], file_mode: str) -> Tuple[str, Path]:
    """Return ('single'|'per_project', directory).

    If selected session project dir and script dir are the same, no prompt.
    For multiple workspaces and separate files, the user may save each file
    next to its own project; combined exports use one chosen directory.
    """
    script_dir = script_output_directory()
    project_dirs = _project_dirs_from_parsers(parsers)
    if not project_dirs:
        return 'single', script_dir

    if len(project_dirs) == 1:
        project_dir = project_dirs[0]
        if _norm_ws_path(str(project_dir)) == _norm_ws_path(str(script_dir)):
            return 'single', script_dir
        while True:
            print(f"\n  {Style.BOLD}Save Markdown file(s) where?{Style.RESET}")
            print(f"    {Style.YELLOW}[P]{Style.RESET}roject directory  {Style.DIM}{project_dir}{Style.RESET}  {Style.DIM}[Default]{Style.RESET}")
            print(f"    {Style.YELLOW}[S]{Style.RESET}cript directory   {Style.DIM}{script_dir}{Style.RESET}")
            ans = input(f"\n  {Style.BOLD}Select > {Style.RESET}").strip().lower() or 'p'
            if ans in ('p', 'project', 'workspace'):
                return 'single', project_dir
            if ans in ('s', 'script'):
                return 'single', script_dir
            print(f"  {Style.warn(f'Unrecognised input {ans!r}. Please type P or S.')}")

    if file_mode == 'separate':
        while True:
            print(f"\n  {Style.BOLD}Save Markdown files where?{Style.RESET}")
            print(f"    {Style.YELLOW}[P]{Style.RESET}roject directories  {Style.DIM}(each session saved in its own workspace){Style.RESET}  {Style.DIM}[Default]{Style.RESET}")
            print(f"    {Style.YELLOW}[S]{Style.RESET}cript directory     {Style.DIM}{script_dir}{Style.RESET}")
            ans = input(f"\n  {Style.BOLD}Select > {Style.RESET}").strip().lower() or 'p'
            if ans in ('p', 'project', 'projects', 'workspace', 'workspaces'):
                return 'per_project', script_dir
            if ans in ('s', 'script'):
                return 'single', script_dir
            print(f"  {Style.warn(f'Unrecognised input {ans!r}. Please type P or S.')}")

    first_project = project_dirs[0]
    if _norm_ws_path(str(first_project)) == _norm_ws_path(str(script_dir)):
        return 'single', script_dir
    while True:
        print(f"\n  {Style.BOLD}Save combined Markdown file where?{Style.RESET}")
        print(f"    {Style.YELLOW}[P]{Style.RESET}roject directory  {Style.DIM}{first_project}{Style.RESET}  {Style.DIM}[Default]{Style.RESET}")
        print(f"    {Style.YELLOW}[S]{Style.RESET}cript directory   {Style.DIM}{script_dir}{Style.RESET}")
        print(f"    {Style.DIM}Multiple project directories were selected; project means the first selected session's workspace.{Style.RESET}")
        ans = input(f"\n  {Style.BOLD}Select > {Style.RESET}").strip().lower() or 'p'
        if ans in ('p', 'project', 'workspace'):
            return 'single', first_project
        if ans in ('s', 'script'):
            return 'single', script_dir
        print(f"  {Style.warn(f'Unrecognised input {ans!r}. Please type P or S.')}")


def output_path_for_parser(parser: 'SessionParser', filename: str,
                           output_mode: str, fallback_dir: Path) -> Path:
    if output_mode == 'per_project':
        ws = getattr(parser, 'workspace_dir', '') or ''
        try:
            d = Path(ws).expanduser().resolve()
            if d.exists() and d.is_dir():
                return d / filename
        except Exception:
            pass
    return fallback_dir / filename

def print_menu_header(workspace_filter: Optional[str], total_sessions: int,
                      total_workspaces: int, cwd_match: Optional[str] = None,
                      source_label: str = "Kiro IDE",
                      storage_path: Optional[Path] = None):
    _clear_screen()
    print(f"\n{Style.BOLD}KIRO SESSION MANAGER{Style.RESET}  {Style.DIM}v1.3.0 · {source_label}{Style.RESET}")
    print(f"{Style.DIM}Storage:   {storage_path or KIRO_HOME}{Style.RESET}")
    try:
        out = Path(__file__).parent.resolve()
    except NameError:
        out = Path.cwd()
    print(f"{Style.DIM}Output:    {out}{Style.RESET}\n")


def print_scope_panel(all_sessions: List[SessionEntry],
                      current: Optional[str], cwd_match: Optional[str],
                      hidden_helpers: int = 0, showing_helpers: bool = False,
                      limit: int = 5) -> None:
    """Explain, in labelled lines, exactly what is on screen and what is not.

    Three questions have to be answerable without thinking: where am I, why am
    I seeing this many sessions, and how do I get to the others. A terse status
    strip answers none of them, so each answer gets its own labelled line.
    """
    rows = workspace_summary(all_sessions)
    total = len(all_sessions)
    L = 11  # label column

    def label(text: str) -> str:
        return f"  {Style.BOLD}{text:<{L}}{Style.RESET}"

    if current:
        here = next((r for r in rows if r[0] == current), None)
        n_here = len(here[1]) if here else 0
        mark = f'{Style.CYAN}◆{Style.RESET} ' if current == cwd_match else ''
        print(f"{label('HERE')}{mark}{Style.BOLD}{short_workspace(current)}{Style.RESET}"
              f"{Style.DIM}  ·  {n_here} session{'s' if n_here != 1 else ''} "
              f"in this directory only{Style.RESET}")
        print(f"  {' ' * L}{Style.DIM}{current}{Style.RESET}")

        others = [r for r in rows if r[0] != current]
        other_sessions = sum(len(r[1]) for r in others)
        if others:
            print(f"{label('ELSEWHERE')}{Style.DIM}{len(others)} other director"
                  f"{'ies' if len(others) != 1 else 'y'} hold {other_sessions} "
                  f"more session{'s' if other_sessions != 1 else ''}{Style.RESET}")
            print(f"  {' ' * L}{Style.YELLOW}[w]{Style.RESET} list every directory and switch"
                  f"     {Style.YELLOW}[x]{Style.RESET} show sessions from all directories")
    else:
        print(f"{label('SHOWING')}{Style.BOLD}every directory{Style.RESET}"
              f"{Style.DIM}  ·  {total} session{'s' if total != 1 else ''} "
              f"across {len(rows)} director{'ies' if len(rows) != 1 else 'y'}{Style.RESET}")
        bits = []
        for i, (ws, lst, _last) in enumerate(rows[:limit], start=1):
            dot = f'{Style.CYAN}◆{Style.RESET}' if ws == cwd_match else ''
            bits.append(f"{Style.YELLOW}w{i}{Style.RESET} {short_workspace(ws)}"
                        f"{Style.DIM}·{len(lst)}{Style.RESET}{dot}")
        strip = f"{Style.DIM}  {Style.RESET}".join(bits)
        if len(rows) > limit:
            strip += f"{Style.DIM}  +{len(rows) - limit} more{Style.RESET}"
        print(f"{label('JUMP TO')}{strip}")
        print(f"  {' ' * L}{Style.YELLOW}[w]{Style.RESET} list every directory and switch"
              f"     {Style.YELLOW}[w1]{Style.RESET} jump straight to a numbered one")

    if hidden_helpers:
        if showing_helpers:
            print(f"{label('INCLUDING')}{Style.DIM}{hidden_helpers} background helper session"
                  f"{'s' if hidden_helpers != 1 else ''} — sub-agent runs, not your own chats"
                  f"     {Style.MAGENTA}[e]{Style.DIM} hide them{Style.RESET}")
        else:
            print(f"{label('NOT SHOWN')}{Style.DIM}{hidden_helpers} background helper session"
                  f"{'s' if hidden_helpers != 1 else ''} — sub-agent runs, not your own chats"
                  f"     {Style.MAGENTA}[e]{Style.DIM} show them{Style.RESET}")
    print()


def print_workspace_summary(all_sessions: List[SessionEntry],
                            current: Optional[str], cwd_match: Optional[str],
                            limit: int = 6):
    """Kept for callers that only need the directory recap."""
    print_scope_panel(all_sessions, current, cwd_match, limit=limit)

def term_width(default: int = 120) -> int:
    """Usable terminal width, clamped to something a table can live in."""
    import shutil as _sh
    try:
        return max(76, min(_sh.get_terminal_size((default, 30)).columns, 220))
    except Exception:
        return default


def _clip(text: Optional[str], w: int) -> str:
    """Trim to width with an ellipsis, never wider than `w`."""
    t = (text or '').replace('\n', ' ').replace('\r', ' ').strip()
    if w <= 0:
        return ''
    return t if len(t) <= w else t[:max(1, w - 1)] + '…'


def list_sessions_table(sessions: List[SessionEntry], show_workspace: bool = True,
                        chain_graph: Optional['ChainGraph'] = None,
                        show_preview: bool = False):
    """One session per line, laid out to the width of the terminal.

    Rows alternate a faint background instead of being separated by blank
    lines, so each session still reads as its own block while twice as many
    stay on screen. Identity and title sit on the left where the eye lands;
    counters, size, age and the session ID trail off to the right, dimmed.
    """
    if not sessions:
        return
    show_chain = chain_graph is not None
    is_cli = any(hasattr(s, 'message_count') for s in sessions)
    W = term_width()

    # (header, width, align, flex) — flex columns share the leftover width.
    spec: List[Tuple[str, int, str, bool]] = [('#', 3, '>', False)]
    if show_workspace:
        spec.append(('DIRECTORY', 16, '<', False))
    spec.append(('TITLE', 0, '<', True))
    if show_preview:
        spec.append(('FIRST MESSAGE', 0, '<', True))
    if show_chain:
        spec.append(('CHAIN', 9, '<', False))
    spec.append((('MSGS', 5, '>') if is_cli else ('FLAGS', 8, '<')) + (False,))
    spec.append(('SIZE', 9, '>', False))
    spec.append(('AGE', 12, '<', False))
    spec.append(('SESSION ID', 36, '<', False))
    # Everything from AGE onwards is metadata and gets dimmed as one block.
    tail_from = len(spec) - 2

    def _flex_width(sp):
        gaps = 2 * (len(sp) - 1)
        fixed = sum(w for _, w, _, flex in sp if not flex)
        n = sum(1 for *_, flex in sp if flex) or 1
        return (W - 2 - fixed - gaps) // n

    if _flex_width(spec) < 26:
        # Titles matter more than the full UUID; keep a searchable prefix.
        spec = [(h, 9 if h == 'SESSION ID' else w, a, f) for h, w, a, f in spec]
    flex_w = max(14, _flex_width(spec))
    widths = [flex_w if flex else w for _, w, _, flex in spec]
    aligns = [a for _, _, a, _ in spec]

    def compose(cells: List[str]) -> Tuple[str, str]:
        padded = [f'{c:{a}{w}}'[:w] for c, a, w in zip(cells, aligns, widths)]
        line = '  '.join(padded)
        line = line[:W - 2].ljust(W - 2)
        cut = sum(widths[:tail_from]) + 2 * tail_from
        return line[:cut], line[cut:]

    head, tail = compose([h for h, _, _, _ in spec])
    print(f"  {Style.BOLD}{head}{Style.NORMAL}{Style.DIM}{tail}{Style.RESET}")

    for idx, s in enumerate(sessions):
        if show_preview:
            # Building a preview means opening and parsing the session file,
            # so it is only paid for when the column is actually shown.
            s.load_preview()

        flags = []
        if s.from_compaction:
            flags.append('↻')
        if s.continuation_count:
            flags.append(f'↪×{s.continuation_count}')
        if s.hidden:
            flags.append('hide')
        msg_count = getattr(s, 'message_count', 0)
        if msg_count and not is_cli:
            flags.append(f'{msg_count}m')

        chain_col = ''
        if chain_graph is not None:
            ch = chain_graph.chain_for(s.session_id)
            if ch and ch.length > 1:
                pos = next((i + 1 for i, x in enumerate(ch.sessions)
                            if x.session_id == s.session_id), None)
                mark = '✓' if chain_graph.confidence_of.get(s.session_id) == 'authoritative' else '~'
                chain_col = f'{ch.id}·{pos}/{ch.length}{mark}'

        dt = s.date
        age = format_relative_time(dt.timestamp()).strip('()') if dt.year > 1970 else '--'

        cells = [str(idx + 1)]
        if show_workspace:
            cells.append(_clip(short_workspace(s.workspace_dir), widths[len(cells)]))
        cells.append(_clip(s.display_title, flex_w))
        if show_preview:
            cells.append(_clip(s.preview_text, flex_w))
        if show_chain:
            cells.append(_clip(chain_col, 9))
        cells.append(_clip(str(msg_count) if is_cli and msg_count else
                           (' '.join(flags) if flags else '·'), widths[len(cells)]))
        cells.append(format_size(s.size))
        cells.append(_clip(age, 12))
        cells.append(_clip(s.session_id or '?', widths[-1]))

        head, tail = compose(cells)
        bg = Style.BG_GRAY if idx % 2 else ''
        accent = f'{Style.BOLD}{Style.GREEN}' if idx == 0 else ''
        print(f"{bg}{accent}  {head}{Style.NORMAL}{Style.FG_RESET}{Style.DIM}{tail}{Style.RESET}")

def _norm_ws_path(p: str) -> str:
    """Normalize a workspace path for cross-comparison (lowercase on Windows,
    forward slashes, no trailing separator)."""
    if not p:
        return ''
    q = str(p).replace('\\', '/').rstrip('/')
    return q.lower() if _IS_WINDOWS else q


def current_dir() -> str:
    """The directory the tool was launched from, normalized for display."""
    try:
        return str(Path.cwd().resolve())
    except Exception:
        return str(Path.cwd())


def detect_workspace_from_cwd(all_sessions: List[SessionEntry]) -> Optional[str]:
    """Return the workspace whose path IS the current directory — nothing else.

    Deliberately strict. Matching a parent directory instead would silently
    show sessions belonging to some other project (every path under the home
    directory has an ancestor workspace), which is indistinguishable from a
    bug when you are sitting in a specific project and asked for its sessions.
    Use `nearest_parent_workspace()` to *offer* a parent, never to auto-apply.
    """
    cwd_norm = _norm_ws_path(current_dir())
    if not cwd_norm:
        return None
    for ws in {s.workspace_dir for s in all_sessions if s.workspace_dir}:
        if _norm_ws_path(ws) == cwd_norm:
            return ws
    return None


def nearest_parent_workspace(all_sessions: List[SessionEntry]) -> Optional[str]:
    """Deepest workspace that *contains* the current directory, for hints only.
    Never applied automatically — see detect_workspace_from_cwd()."""
    cwd_norm = _norm_ws_path(current_dir())
    if not cwd_norm:
        return None
    best: Optional[Tuple[str, int]] = None
    for ws in {s.workspace_dir for s in all_sessions if s.workspace_dir}:
        wn = _norm_ws_path(ws)
        if wn and wn != cwd_norm and cwd_norm.startswith(wn + '/'):
            depth = wn.count('/')
            if best is None or depth > best[1]:
                best = (ws, depth)
    return best[0] if best else None


def print_no_sessions_here(cwd: str, all_sessions: List[SessionEntry],
                           parent: Optional[str]) -> None:
    """Shown when the current directory has no recorded sessions. Explicit is
    better than silently listing another directory's sessions."""
    rows = workspace_summary(all_sessions)
    L = 11

    def label(text: str) -> str:
        return f"  {Style.BOLD}{text:<{L}}{Style.RESET}"

    print(f"{label('HERE')}{Style.BOLD}{short_workspace(cwd)}{Style.RESET}"
          f"{Style.DIM}  ·  no sessions were ever recorded in this directory{Style.RESET}")
    print(f"  {' ' * L}{Style.DIM}{cwd}{Style.RESET}")
    if parent:
        n = len([s for s in all_sessions if s.workspace_dir == parent])
        print(f"{label('NEARBY')}{Style.DIM}the parent directory {Style.RESET}"
              f"{short_workspace(parent)}{Style.DIM} has {n} "
              f"session{'s' if n != 1 else ''}{Style.RESET}")
        print(f"  {' ' * L}{Style.DIM}{parent}{Style.RESET}")
    total = len(all_sessions)
    print(f"{label('ELSEWHERE')}{Style.DIM}{len(rows)} director"
          f"{'ies' if len(rows) != 1 else 'y'} hold {total} "
          f"session{'s' if total != 1 else ''} in total{Style.RESET}")
    print(f"  {' ' * L}{Style.YELLOW}[w]{Style.RESET} list every directory and switch"
          f"     {Style.YELLOW}[x]{Style.RESET} show sessions from all directories")
    print()


def workspace_summary(all_sessions: List[SessionEntry]) -> List[Tuple[str, List[SessionEntry], int]]:
    """Return [(workspace_dir, sessions, last_session_ms)] sorted by recency."""
    by_ws: Dict[str, List[SessionEntry]] = {}
    for s in all_sessions:
        by_ws.setdefault(s.workspace_dir or '(unknown)', []).append(s)
    rows = [(ws, lst, max((x.date_created or 0) for x in lst)) for ws, lst in by_ws.items()]
    rows.sort(key=lambda r: -r[2])
    return rows


def print_chain_session_line(num: int, lead: str, s: SessionEntry,
                             show_workspace: bool = False,
                             zebra: bool = False) -> None:
    """One line for a session inside the chain view, sized to the terminal.

    Shares the shape of the main list — identity and title on the left, dimmed
    counters, age and full session ID trailing right — so switching views does
    not mean re-learning the layout.
    """
    W = term_width()
    flags = []
    if s.from_compaction:
        flags.append('↻')
    if s.continuation_count:
        flags.append(f'↪×{s.continuation_count}')
    if s.hidden:
        flags.append('hide')
    dt = s.date
    age = format_relative_time(dt.timestamp()).strip('()') if dt.year > 1970 else '--'
    sid = s.session_id or '?'

    num_s = f"{str(num) + ')':<5}"
    lead_s = f"{lead:<12}"
    ws_w = 0 if not show_workspace else (10 if W < 110 else 14)
    ws_s = f"{_clip(short_workspace(s.workspace_dir), ws_w):<{ws_w}}  " if ws_w else ''
    flags_s = f"{' '.join(flags):<5}"
    size_s = f"{format_size(s.size):>9}"
    age_s = f"{age:<12}"
    short_id = sid[:8] + '…' if len(sid) > 9 else sid

    # Drop the least useful column first until the title has room to breathe.
    variants = [
        f"{flags_s}  {size_s}  {age_s}  {sid}",
        f"{flags_s}  {size_s}  {age_s}  {short_id}",
        f"{size_s}  {age_s}  {short_id}",
        f"{size_s}  {age_s}",
        f"{size_s}",
    ]
    lead_len = 2 + len(num_s) + len(lead_s) + len(ws_s) + 2
    tail = variants[-1]
    title_w = 4
    for candidate in variants:
        room = W - lead_len - len(candidate)
        if room >= 14:
            tail, title_w = candidate, room
            break
    else:
        tail = variants[-1]
        title_w = max(0, W - lead_len - len(tail))

    title_s = f"{_clip(s.display_title, title_w):<{title_w}}" if title_w else ''
    bg = Style.BG_GRAY if zebra else ''
    ws_styled = f"{Style.DIM}{ws_s}{Style.NORMAL}" if ws_s else ''
    print(f"{bg}  {Style.YELLOW}{num_s}{Style.FG_RESET}"
          f"{Style.DIM}{lead_s}{Style.NORMAL}{ws_styled}{title_s}  "
          f"{Style.DIM}{tail}{Style.RESET}")


def list_chains_grouped(scoped_sessions: List[SessionEntry], all_sessions: List[SessionEntry],
                        chain_graph: 'ChainGraph', show_workspace: bool = True,
                        show_hidden: bool = False,
                        workspace_filter: Optional[str] = None) -> List[SessionEntry]:
    """Render every chain in scope, with all members shown (even hidden ones
    so chain integrity is preserved). Returns the ORDERED list of sessions so
    the caller can map row IDs back to entries."""
    by_id = {s.session_id: s for s in all_sessions}

    # Collect chain IDs in scope (workspace filter applies; hidden filter
    # applies to whether the chain has ANY visible member).
    in_scope_chains: Set[str] = set()
    for s in scoped_sessions:
        cid = chain_graph.chain_of.get(s.session_id)
        if cid is not None:
            in_scope_chains.add(cid)

    # Gather every chain (in scope) with all its members
    chain_members: Dict[str, List[SessionEntry]] = {}
    for cid in in_scope_chains:
        ch = chain_graph.chains.get(cid)
        if not ch:
            continue
        members = list(ch.sessions)
        if workspace_filter:
            members = [m for m in members if m.workspace_dir == workspace_filter]
        if not members:
            continue
        members.sort(key=lambda s: s.date_created or 0)
        chain_members[cid] = members

    # Sort chains: multi-session first (by last activity), then singletons.
    def chain_rank(cid: str) -> Tuple[int, int]:
        members = chain_members[cid]
        is_multi = 0 if len(members) > 1 else 1   # multi-session first
        last_activity = -max((m.date_created or 0) for m in members)
        return (is_multi, last_activity)
    chain_order = sorted(chain_members.keys(), key=chain_rank)

    flat_ordered: List[SessionEntry] = []
    next_id = 1

    # Multi-session chains first
    multi_ids = [c for c in chain_order if len(chain_members[c]) > 1]
    singleton_ids = [c for c in chain_order if len(chain_members[c]) == 1]

    for cid in multi_ids:
        members = chain_members[cid]
        ch = chain_graph.chains[cid]
        conf_mark = {
            'authoritative': f'{Style.GREEN}✓ authoritative{Style.RESET}',
            'inferred':      f'{Style.YELLOW}~ inferred{Style.RESET}',
            'mixed':         f'{Style.CYAN}± mixed{Style.RESET}',
        }.get(ch.confidence, ch.confidence)
        try:
            last_dt = datetime.fromtimestamp(ch.last_activity / 1000.0)
            when = format_relative_time(last_dt.timestamp())
        except Exception:
            when = ''
        ws_label = f'  {Style.DIM}· {short_workspace(ch.workspace)}{Style.RESET}' if show_workspace else ''
        print()
        print(f"  {Style.BOLD}◆ Chain {ch.id}{Style.RESET}  "
              f"{Style.DIM}{ch.length} sessions · last {when} · {Style.RESET}{conf_mark}{ws_label}")
        t = ch.title or '(untitled chain)'
        if len(t) > 100: t = t[:97] + '...'
        print(f"  {Style.DIM}└─ {t}{Style.RESET}")

        for row_i, s in enumerate(members):
            pos = next((i+1 for i, x in enumerate(ch.sessions) if x.session_id == s.session_id), 0)
            conf_local = chain_graph.confidence_of.get(s.session_id, '-')
            if pos == 1:
                tree = '┌─'
            elif pos == ch.length:
                tree = '└─'
            else:
                tree = '├─'
            conf_mark_local = '✓' if conf_local == 'authoritative' else '~'
            print_chain_session_line(
                next_id, f'{tree} {pos:>2}/{ch.length}{conf_mark_local}', s,
                show_workspace=show_workspace, zebra=bool(row_i % 2))
            flat_ordered.append(s)
            next_id += 1

    if singleton_ids:
        # Filter singletons by hidden flag (only meaningful for singletons)
        visible_singletons = []
        for cid in singleton_ids:
            members = chain_members[cid]
            if not show_hidden:
                members = [m for m in members if not m.hidden]
            if members:
                visible_singletons.append((cid, members))
        if visible_singletons:
            print()
            print(f"  {Style.BOLD}— Standalone sessions —{Style.RESET}  {Style.DIM}({len(visible_singletons)}){Style.RESET}")
            row_i = 0
            for cid, members in visible_singletons:
                for s in members:
                    print_chain_session_line(next_id, '·', s,
                                             show_workspace=show_workspace,
                                             zebra=bool(row_i % 2))
                    flat_ordered.append(s)
                    next_id += 1
                    row_i += 1

    return flat_ordered


def select_workspace(all_sessions: List[SessionEntry], current: Optional[str] = None) -> Optional[str]:
    """One line per directory: number, session count, age, name, then the path."""
    rows = workspace_summary(all_sessions)
    W = term_width()
    cwd_hit = detect_workspace_from_cwd(all_sessions)
    _clear_screen()
    print(f"\n  {Style.BOLD}CHOOSE A DIRECTORY{Style.RESET}  "
          f"{Style.DIM}{len(rows)} with sessions · most recent first{Style.RESET}\n")
    name_w = max(14, min(28, max((len(short_workspace(ws)) for ws, _, _ in rows), default=14)))
    path_w = max(20, W - (6 + 7 + 13 + name_w + 10))
    print(f"  {Style.YELLOW}[0]{Style.RESET}  {Style.BOLD}{'every directory':<{name_w}}{Style.RESET}"
          f"  {Style.DIM}{len(all_sessions)} sessions in total{Style.RESET}\n")
    for i, (ws, lst, last_ms) in enumerate(rows, start=1):
        try:
            age = format_relative_time(last_ms / 1000.0).strip('()')
        except Exception:
            age = '?'
        if current and ws == current:
            mark = f'{Style.GREEN}●{Style.RESET}'
        elif ws == cwd_hit:
            mark = f'{Style.CYAN}◆{Style.RESET}'
        else:
            mark = ' '
        bg = Style.BG_GRAY if i % 2 == 0 else ''
        print(f"  {bg}{Style.YELLOW}{f'[{i}]':<5}{Style.FG_RESET} {mark} "
              f"{Style.DIM}{len(lst):>3} sess  {age:<12}{Style.NORMAL} "
              f"{_clip(short_workspace(ws), name_w):<{name_w}}  "
              f"{Style.DIM}{_clip(ws, path_w):<{path_w}}{Style.RESET}")
    print(f"\n  {Style.DIM}● current   ◆ your directory   Enter or 0 = every directory{Style.RESET}")
    choice = input(f"\n  {Style.BOLD}Directory > {Style.RESET}").strip()
    if not choice or choice == '0':
        return None
    if choice.isdigit() and 1 <= int(choice) <= len(rows):
        return rows[int(choice) - 1][0]
    return None

# ──────────────────────────────────────────────────────────────
# Chain merge export
# ──────────────────────────────────────────────────────────────
def _parse_sessions_parallel(entries: List['SessionEntry'], max_workers: int = 6,
                             parser_cls=SessionParser) -> List['SessionParser']:
    """Build SessionParsers for many sessions concurrently.

    SessionParser._load() opens the session JSON and every referenced
    execution file. That's hundreds of MB of disk IO + JSON parsing across
    a multi-MB session list. Running it in a thread pool drives the SSD in
    parallel and lets multiple json.load calls overlap (json/orjson both
    release the GIL during heavy parsing).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if not entries:
        return []
    parsers: List[Optional[SessionParser]] = [None] * len(entries)
    workers = min(max_workers, max(2, len(entries)))

    def _parse_one(idx_entry):
        idx, entry = idx_entry
        try:
            return idx, parser_cls(entry)
        except Exception as e:
            print(Style.error(f"Failed to parse {entry.session_id[:8]}: {e}"))
            return idx, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for idx, parser in ex.map(_parse_one, list(enumerate(entries))):
            parsers[idx] = parser
    return [p for p in parsers if p is not None]


def merge_chain_to_markdown(chain: 'Chain',
                            section_filter: Dict[str, bool],
                            clean_content: bool = False,
                            output_cap: int = 0,
                            orch_filter: Optional[Dict] = None,
                            user_cap: int = 0,
                            agent_cap: int = 0,
                            reasoning_cap: int = 0,
                            summary_cap: int = 0) -> Tuple[str, str]:
    """Concatenate every session in a chain into one Markdown document.
    Returns (markdown, filename)."""
    parts: List[str] = []
    # ---- Banner ----
    try:
        first_dt = datetime.fromtimestamp((chain.root.date_created or 0) / 1000.0)
        last_dt  = datetime.fromtimestamp((chain.tip.date_created or 0) / 1000.0)
        span = f"{first_dt.strftime('%Y-%m-%d')} → {last_dt.strftime('%Y-%m-%d')}"
    except Exception:
        span = '?'
    title = chain.title or chain.root.display_title or '(untitled)'
    parts.append(f"# Chain {chain.id} — {title}\n")
    parts.append('```yaml')
    parts.append(f"Chain ID:       {chain.id}")
    parts.append(f"Confidence:     {chain.confidence}")
    parts.append(f"Workspace:      {chain.workspace or '?'}")
    parts.append(f"Span:           {span}")
    parts.append(f"Sessions:       {chain.length}")
    # Per-session table
    parts.append("Members:")
    for i, s in enumerate(chain.sessions, start=1):
        try:
            dt = datetime.fromtimestamp((s.date_created or 0) / 1000.0)
            when = dt.strftime('%Y-%m-%d %H:%M')
        except Exception:
            when = '?'
        marker = '↻' if s.from_compaction else '·'
        parts.append(f"  {i:>2}. {marker} {when}  [{s.session_id[:8]}]  {s.display_title[:60]}")
    parts.append('```\n')

    # ---- Per-session body ----
    total_turns = 0
    total_credits = 0.0
    for i, s in enumerate(chain.sessions, start=1):
        parser = SessionParser(s)
        total_turns += parser.user_turn_count
        total_credits += parser.credits_used
        # In chain context, skip the intro Conversation Summary — the prior
        # session's real content provides that. We KEEP inline summaries
        # (mid-session compactions) for accuracy.
        new_data: List[Dict] = []
        skip_intro = (i > 1)
        skipped_intro = False
        for item in parser.data:
            if skip_intro and not skipped_intro:
                if item['type'] == 'session_event' and item.get('event') == 'continued_from_compaction':
                    continue
                if item['type'] == 'summarization' and item.get('where') == 'intro':
                    skipped_intro = True
                    continue
            new_data.append(item)
        parser.data = new_data

        parts.append('\n' + '─' * 80)
        link_note = '(root)' if i == 1 else f'(continued from session {i-1}/{chain.length})'
        parts.append(f"## ▶ Session {i} of {chain.length}  —  {link_note}")
        if skipped_intro:
            parts.append(f"\n_(intro Conversation Summary omitted — the prior session's content above is what it summarized)_\n")
        parts.append('')
        # Render the session inline (skip title — we already have a chain title)
        body = parser.to_markdown(section_filter=section_filter,
                                  clean_content=clean_content,
                                  output_cap=output_cap,
                                  user_cap=user_cap,
                                  agent_cap=agent_cap,
                                  reasoning_cap=reasoning_cap,
                                  summary_cap=summary_cap,
                                  orch_filter=orch_filter)
        # Strip body's H1 since we used our own
        body_lines = body.splitlines()
        if body_lines and body_lines[0].startswith('# '):
            body_lines = body_lines[1:]
        parts.append('\n'.join(body_lines).lstrip())

    # Update the banner with totals (rendered earlier with placeholders we
    # didn't fill — just append a summary block at the end too).
    parts.append('\n' + '─' * 80)
    parts.append('```yaml')
    parts.append(f"Chain totals:")
    parts.append(f"  user turns:   {total_turns}")
    parts.append(f"  credits used: {total_credits:.4f}")
    parts.append('```')

    safe_title = clean_filename(title)
    try:
        date_prefix = datetime.fromtimestamp((chain.tip.date_created or 0) / 1000.0).strftime("%Y%m%d")
    except Exception:
        date_prefix = datetime.now().strftime("%Y%m%d")
    filename = f"{date_prefix}_chain-{chain.id}_{safe_title}.md"
    return '\n'.join(parts), filename


def process_chain_export(chain: 'Chain'):
    """Interactive flow to export an entire chain as one merged document."""
    if chain.length <= 1:
        print(Style.warn("This chain has only one session — use normal session export."))
        input(f"\n{Style.DIM}Press Enter to continue...{Style.RESET}")
        return
    if EXEC_INDEX is not None and not EXEC_INDEX._built:
        EXEC_INDEX.build(progress=True)
    print(f"\n{Style.info(f'Parsing chain {chain.id} ({chain.length} sessions)...')}")
    parsers = _parse_sessions_parallel(chain.sessions)
    if not parsers:
        return
    # Reuse the interactive filter UI so options match per-session export
    section_filter, clean_content, output_cap, user_cap, agent_cap, reason_cap, summary_cap, orch_filter = interactive_filter(
        parsers, scope_label=f"chain {chain.id}"
    )
    if not any(section_filter.values()):
        print(Style.warn("Nothing selected — skipping export."))
        input(f"\n{Style.DIM}Press Enter to continue...{Style.RESET}")
        return

    md, fname = merge_chain_to_markdown(
        chain, section_filter, clean_content, output_cap, orch_filter,
        user_cap=user_cap, agent_cap=agent_cap,
        reasoning_cap=reason_cap, summary_cap=summary_cap,
    )
    _mode, out_dir = choose_output_location(parsers, file_mode='combined')
    out_path = out_dir / fname
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"  {Style.GREEN}➜{Style.RESET} Saved: {fname}  "
          f"{Style.CYAN}({md.count(chr(10))+1:,} lines){Style.RESET}")
    maybe_open_directory([out_path])
    input(f"\n{Style.DIM}Press Enter to continue...{Style.RESET}")


# ──────────────────────────────────────────────────────────────
# Conversion pipeline
# ──────────────────────────────────────────────────────────────
def process_conversion(indices_str: str, sessions: List[SessionEntry],
                       parser_cls=SessionParser,
                       build_execution_index: bool = True,
                       allow_scope: bool = True):
    if not indices_str.strip():
        return
    try:
        parts = [x.strip() for x in indices_str.split(',')]
        idx = [int(x) - 1 for x in parts if x.isdigit()]
    except ValueError:
        print(Style.error("Invalid input format.")); return
    chosen = [sessions[i] for i in idx if 0 <= i < len(sessions)]
    if not chosen:
        return

    # Make sure execution index is built before parsing
    if build_execution_index and EXEC_INDEX is not None and not EXEC_INDEX._built:
        EXEC_INDEX.build(progress=True)

    print(f"\n{Style.info(f'Parsing {len(chosen)} session(s)...')}")
    parsers = _parse_sessions_parallel(chosen, parser_cls=parser_cls)
    if not parsers:
        return

    scope_type, turn_limit = ('full', 0)
    scope_label = ""
    if allow_scope:
        scope_type, turn_limit = select_extraction_scope(parsers)
        if scope_type == 'last_n':
            for p in parsers:
                p.trim_to_last_n_turns(turn_limit)
            scope_label = f"last {turn_limit} turn{'s' if turn_limit != 1 else ''}"
        elif scope_type == 'live':
            for p in parsers:
                p.trim_to_live_context()
            scope_label = "live context"

    section_filter, clean_content, output_cap, user_cap, agent_cap, reason_cap, summary_cap, orch_filter = \
        interactive_filter(parsers, scope_label=scope_label)

    if not any(section_filter.values()):
        print(Style.warn("Nothing selected — skipping export."))
        input(f"\n{Style.DIM}Press Enter to return to menu...{Style.RESET}")
        return

    _clear_screen()
    dest = ''
    while dest not in ('f', 'c', 'b'):
        print(f"\n  {Style.BOLD}Export Destination:{Style.RESET}")
        print(f"    {Style.YELLOW}[F]{Style.RESET}ile (save to disk)       {Style.DIM}[Default]{Style.RESET}")
        print(f"    {Style.YELLOW}[C]{Style.RESET}lipboard (copy directly)")
        print(f"    {Style.YELLOW}[B]{Style.RESET}oth")
        dest = input(f"\n  {Style.BOLD}Select > {Style.RESET}").strip().lower() or 'f'

    # If we'll write files AND more than one session was selected, ask whether
    # to merge them into one file or keep them separate.
    file_mode = 'separate'  # default for single-session
    if dest in ('f', 'b') and len(parsers) > 1:
        # Accept any reasonable phrasing of either option. The visual `[O]ne`
        # easily reads as `[0]ne`, so both `0` and `o` are valid for combined.
        COMBINED_INPUTS = {'o', '0', 'one', 'combined', 'combine', 'c', '1', 'merge', 'merged'}
        SEPARATE_INPUTS = {'s', 'sep', 'separate', '2'}
        while True:
            print(f"\n  {Style.BOLD}{len(parsers)} sessions selected — save as:{Style.RESET}")
            print(f"    {Style.YELLOW}[O]{Style.RESET}ne combined file  {Style.DIM}(merged with section dividers){Style.RESET}")
            print(f"    {Style.YELLOW}[S]{Style.RESET}eparate files     {Style.DIM}(one per session) [Default]{Style.RESET}")
            ans = input(f"\n  {Style.BOLD}Select > {Style.RESET}").strip().lower()
            if ans == '':
                file_mode = 'separate'
                break
            if ans in COMBINED_INPUTS:
                file_mode = 'combined'; break
            if ans in SEPARATE_INPUTS:
                file_mode = 'separate'; break
            print(f"  {Style.warn(f'Unrecognised input {ans!r}. Please type O or S.')}")
        # Echo back so we never silently do the opposite of what they meant
        if file_mode == 'combined':
            print(f"  {Style.GREEN}→ Combining {len(parsers)} sessions into ONE file.{Style.RESET}")
        else:
            print(f"  {Style.GREEN}→ Writing {len(parsers)} SEPARATE files (one per session).{Style.RESET}")

    output_mode, out_dir = ('single', script_output_directory())
    if dest in ('f', 'b'):
        output_mode, out_dir = choose_output_location(parsers, file_mode)

    print(f"\n{Style.info(f'Processing {len(parsers)} session(s)...')}")

    written_paths: List[Path] = []
    clipboard_md: List[str] = []

    if dest in ('f', 'b') and file_mode == 'combined' and len(parsers) > 1:
        # One merged document
        try:
            combined_md, combined_fname = combine_parsers_to_markdown(
                parsers, section_filter, clean_content, output_cap,
                user_cap, agent_cap, reason_cap, summary_cap, orch_filter,
            )
            out_path = out_dir / combined_fname
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(combined_md)
            print(f"  {Style.GREEN}➜{Style.RESET} Saved combined: {combined_fname}  "
                  f"{Style.CYAN}({combined_md.count(chr(10))+1:,} lines){Style.RESET}")
            written_paths.append(out_path)
            if dest in ('c', 'b'):
                clipboard_md.append(combined_md)
        except Exception as e:
            print(f"  {Style.error(f'Combined export failed: {e}')}")
    else:
        # Separate files (or single-session)
        for parser in parsers:
            try:
                md_content = parser.to_markdown(
                    section_filter=section_filter,
                    clean_content=clean_content,
                    output_cap=output_cap,
                    user_cap=user_cap,
                    agent_cap=agent_cap,
                    reasoning_cap=reason_cap,
                    summary_cap=summary_cap,
                    orch_filter=orch_filter,
                )
                try:
                    date_prefix = datetime.fromtimestamp((parser.date_created or 0) / 1000.0).strftime("%Y%m%d")
                except Exception:
                    date_prefix = datetime.now().strftime("%Y%m%d")
                safe_title = clean_filename(parser.title)
                out_filename = f"{date_prefix}_{safe_title}.md"
                line_count = md_content.count('\n') + 1

                if dest in ('c', 'b'):
                    if len(parsers) > 1:
                        clipboard_md.append(f"<!-- Session: {out_filename} -->\n" + md_content)
                    else:
                        clipboard_md.append(md_content)
                if dest in ('f', 'b'):
                    out_path = output_path_for_parser(parser, out_filename, output_mode, out_dir)
                    with open(out_path, 'w', encoding='utf-8') as f:
                        f.write(md_content)
                    print(f"  {Style.GREEN}➜{Style.RESET} Saved: {out_filename}  "
                          f"{Style.CYAN}({line_count:,} lines){Style.RESET}")
                    written_paths.append(out_path)
            except Exception as e:
                print(f"  {Style.error(f'Failed {parser.session_id[:8]}: {e}')}")

    if dest in ('c', 'b') and clipboard_md:
        text = "\n\n---\n\n".join(clipboard_md)
        if copy_to_clipboard(text):
            print(f"  {Style.GREEN}➜{Style.RESET} Copied to clipboard! "
                  f"{Style.CYAN}({text.count(chr(10)):,} total lines){Style.RESET}")
        else:
            print(f"  {Style.RED}➜{Style.RESET} Failed to copy to clipboard.")

    if written_paths:
        maybe_open_directory(written_paths)

    input(f"\n{Style.DIM}Press Enter to return to menu...{Style.RESET}")


def _find_entries_by_session_id(entries: List[SessionEntry], query: str) -> List[SessionEntry]:
    q = (query or '').strip().lower()
    if not q:
        return []
    exact = [e for e in entries if (e.session_id or '').lower() == q]
    if exact:
        return exact
    if len(q) >= 8:
        return [e for e in entries if (e.session_id or '').lower().startswith(q)]
    return []


def _session_match_line(source: str, entry: SessionEntry, index: Optional[int] = None) -> str:
    try:
        when = entry.date.strftime('%Y-%m-%d %H:%M')
    except Exception:
        when = '?'
    title = normalize_title_candidate(entry.display_title)
    workspace = entry.workspace_dir or '?'
    prefix = f"{Style.YELLOW}[{index}]{Style.RESET} " if index is not None else ""
    detail = f"{source:<8}  {entry.session_id}  {when}  {short_workspace(workspace)}"
    if hasattr(entry, 'message_count'):
        detail += f"  msgs={getattr(entry, 'message_count', 0)}"
    return f"  {prefix}{detail}\n      {Style.DIM}{workspace}{Style.RESET}\n      {title}"


def interactive_find_session_by_id():
    """Find one session ID across IDE and CLI storage, then export it."""
    global EXEC_INDEX, CHAIN_GRAPH
    _clear_screen()
    print(f"\n{Style.BOLD}FIND SESSION BY ID{Style.RESET}\n")
    sid = input(f"  {Style.BOLD}Session ID or prefix > {Style.RESET}").strip()
    if not sid:
        return

    print(f"\n{Style.DIM}Searching IDE and CLI session stores in parallel...{Style.RESET}")
    from concurrent.futures import ThreadPoolExecutor

    def _scan_ide():
        return scan_all_sessions() if WORKSPACE_SESSIONS_DIR.exists() else []

    def _scan_cli():
        if not KIRO_CLI_SESSIONS_DIR.exists():
            return []
        # The session id is the filename, so a search parses only the matches.
        return scan_cli_sessions(id_query=sid)

    with ThreadPoolExecutor(max_workers=2) as ex:
        ide_future = ex.submit(_scan_ide)
        cli_future = ex.submit(_scan_cli)
        try:
            ide_sessions = ide_future.result()
        except Exception as e:
            print(Style.warn(f"IDE scan failed: {e}"))
            ide_sessions = []
        try:
            cli_sessions = cli_future.result()
        except Exception as e:
            print(Style.warn(f"CLI scan failed: {e}"))
            cli_sessions = []

    matches: List[Tuple[str, SessionEntry]] = []
    for entry in _find_entries_by_session_id(ide_sessions, sid):
        matches.append(('IDE', entry))
    for entry in _find_entries_by_session_id(cli_sessions, sid):
        matches.append(('CLI', entry))

    if not matches:
        print(Style.error(f"No session found for: {sid}"))
        input(f"\n{Style.DIM}Press Enter to return to source menu...{Style.RESET}")
        return

    _clear_screen()
    print(f"\n{Style.BOLD}SESSION FOUND{Style.RESET}\n")
    if len(matches) == 1:
        source, entry = matches[0]
        print(Style.success(f"This session belongs to Kiro {source}."))
        print(_session_match_line(source, entry))
    else:
        sources = sorted({source for source, _ in matches})
        if 'IDE' in sources and 'CLI' in sources:
            print(Style.warn("This session ID exists in both Kiro IDE and Kiro CLI. Choose which one to use."))
        else:
            print(Style.warn("Multiple sessions matched that prefix. Choose which one to use."))
        for i, (source, entry) in enumerate(matches, start=1):
            print(_session_match_line(source, entry, i))
        choice = input(f"\n  {Style.BOLD}Use which match? > {Style.RESET}").strip().lower()
        if not choice or choice == 'q':
            return
        if not choice.isdigit() or not (1 <= int(choice) <= len(matches)):
            print(Style.error("Invalid selection."))
            input(f"\n{Style.DIM}Press Enter to return to source menu...{Style.RESET}")
            return
        source, entry = matches[int(choice) - 1]

    answer = input(f"\n  {Style.BOLD}Export this {source} session now?{Style.RESET}  {Style.BOLD}[Y/n] {Style.RESET}").strip().lower()
    if answer and answer not in ('y', 'yes'):
        return

    if source == 'CLI':
        EXEC_INDEX = None
        CHAIN_GRAPH = None
        process_conversion(
            "1", [entry],
            parser_cls=CliSessionParser,
            build_execution_index=False,
            allow_scope=False,
        )
    else:
        EXEC_INDEX = ExecutionIndex(KIRO_HOME)
        EXEC_INDEX.build(progress=True)
        CHAIN_GRAPH = ChainGraph(ide_sessions, EXEC_INDEX)
        process_conversion("1", [entry])

# ──────────────────────────────────────────────────────────────
# Interactive main loop
# ──────────────────────────────────────────────────────────────
def interactive_loop_ide():
    global EXEC_INDEX, CHAIN_GRAPH
    if not WORKSPACE_SESSIONS_DIR.exists():
        print(Style.error(f"Workspace-sessions folder not found: {WORKSPACE_SESSIONS_DIR}"))
        print(Style.info("Set KIRO_HOME env var if Kiro stores data elsewhere."))
        sys.exit(1)

    print(f"{Style.DIM}Scanning Kiro storage...{Style.RESET}")
    EXEC_INDEX = ExecutionIndex(KIRO_HOME)
    EXEC_INDEX.build(progress=True)

    all_sessions_full = scan_all_sessions()
    if not all_sessions_full:
        print(Style.error("No sessions found.")); sys.exit(1)

    CHAIN_GRAPH = ChainGraph(all_sessions_full, EXEC_INDEX)

    # Scope to the launch directory — that exact directory and nothing else.
    CWD = current_dir()
    cwd_match = detect_workspace_from_cwd(all_sessions_full)
    cwd_parent = nearest_parent_workspace(all_sessions_full)
    workspace_filter: Optional[str] = cwd_match or CWD
    show_hidden = False
    show_preview = False
    sort_key = 'date'
    view_limit = 15
    chain_view = False

    def current_sessions():
        pool = all_sessions_full
        if not show_hidden:
            pool = [s for s in pool if not s.hidden]
        if workspace_filter:
            pool = [s for s in pool if s.workspace_dir == workspace_filter]
        if sort_key == 'size':
            pool = sorted(pool, key=lambda s: -s.size)
        return pool

    total_workspaces = len({s.workspace_dir for s in all_sessions_full if s.workspace_dir})

    while True:
        sessions = current_sessions()
        print_menu_header(workspace_filter, len(all_sessions_full), total_workspaces, cwd_match)

        scoped_to_empty_cwd = (workspace_filter == CWD and cwd_match is None)
        multi_chains = [c for c in CHAIN_GRAPH.chains.values() if c.length > 1]
        if scoped_to_empty_cwd:
            print_no_sessions_here(CWD, all_sessions_full, cwd_parent)
            rendered_sessions: List = []
        else:
            print_workspace_summary(all_sessions_full, workspace_filter, cwd_match, limit=6)

            # Chain summary line
            if multi_chains:
                in_scope = [c for c in multi_chains if not workspace_filter or c.workspace == workspace_filter]
                print(f"  {Style.BOLD}CHAINS{Style.RESET}  "
                      f"{Style.DIM}({len(in_scope)} multi-session chain"
                      f"{'s' if len(in_scope) != 1 else ''} in scope · view: "
                      f"{'CHAIN-GROUPED' if chain_view else 'flat'} · toggle with C){Style.RESET}")

            # Render listing
            rendered_sessions = sessions[:view_limit]
            if chain_view:
                # In chain view, ignore view_limit per-chain — show entire chains
                rendered_sessions = list_chains_grouped(
                    sessions, all_sessions_full, CHAIN_GRAPH,
                    show_workspace=(workspace_filter is None),
                    show_hidden=show_hidden,
                    workspace_filter=workspace_filter,
                )
            else:
                list_sessions_table(
                    rendered_sessions,
                    show_workspace=(workspace_filter is None),
                    chain_graph=CHAIN_GRAPH,
                    show_preview=show_preview,
                )
                if len(sessions) > view_limit:
                    print(f"{Style.DIM}(Showing {view_limit} of {len(sessions)} sessions — press M for more){Style.RESET}")

        print(f"\n{Style.BOLD}OPTIONS:{Style.RESET}")
        # Workspace switching surfaced FIRST since it's how you find your sessions.
        print(f"  {Style.CYAN}[w]{Style.RESET}      : Switch workspace  "
              f"{Style.DIM}(or {Style.RESET}{Style.YELLOW}w1{Style.RESET}{Style.DIM}, {Style.RESET}{Style.YELLOW}w2{Style.RESET}{Style.DIM}, …  to jump directly){Style.RESET}")
        print(f"  {Style.CYAN}[x]{Style.RESET}      : Show ALL workspaces (clear filter)")
        print(f"  {Style.DIM}{'─' * 56}{Style.RESET}")
        print(f"  {Style.GREEN}[ID, ID]{Style.RESET}: Convert specific sessions (e.g. '1, 3')")
        print(f"  {Style.YELLOW}[a]{Style.RESET}      : Convert ALL listed sessions")
        if multi_chains:
            print(f"  {Style.YELLOW}[c]{Style.RESET}      : Toggle chain view  ({'ON' if chain_view else 'OFF'})")
            print(f"  {Style.YELLOW}[ce <id>]{Style.RESET}: Export entire chain as one merged doc (e.g. 'ce A')")
        show_h = 'ON' if show_hidden else 'OFF'
        print(f"  {Style.MAGENTA}[h]{Style.RESET}      : Toggle hidden sessions  ({show_h})")
        print(f"  {Style.MAGENTA}[m]{Style.RESET}      : Show more rows (current: {view_limit})")
        print(f"  {Style.MAGENTA}[v]{Style.RESET}      : Toggle message preview column  "
              f"({'ON' if show_preview else 'OFF'})")
        print(f"  {Style.MAGENTA}[s]{Style.RESET}      : Sort by size / date toggle  (current: {sort_key})")
        print(f"  {Style.MAGENTA}[r]{Style.RESET}      : Reload session index")
        print(f"  {Style.RED}[q]{Style.RESET}      : Quit")
        choice = input(f"\n{Style.BOLD}Select > {Style.RESET}").strip().lower()

        if choice == 'q':
            print('Bye.'); sys.exit(0)
        elif choice == 'w':
            ws = select_workspace(all_sessions_full, workspace_filter)
            workspace_filter = ws
        elif re.fullmatch(r'w\d+', choice):
            # `w<N>` jumps straight to the Nth workspace shown in the summary
            rows = workspace_summary(all_sessions_full)
            n = int(choice[1:])
            if 1 <= n <= len(rows):
                workspace_filter = rows[n - 1][0]
            else:
                print(Style.error(f"No workspace #{n} (have {len(rows)})."))
                input(f"\n{Style.DIM}Press Enter to continue...{Style.RESET}")
        elif choice == 'x':
            workspace_filter = None
        elif choice == 'r':
            EXEC_INDEX = ExecutionIndex(KIRO_HOME)
            EXEC_INDEX.build(progress=True)
            all_sessions_full = scan_all_sessions()
            total_workspaces = len({s.workspace_dir for s in all_sessions_full if s.workspace_dir})
            CHAIN_GRAPH = ChainGraph(all_sessions_full, EXEC_INDEX)
            cwd_match = detect_workspace_from_cwd(all_sessions_full)
            cwd_parent = nearest_parent_workspace(all_sessions_full)
        elif choice == 'h':
            show_hidden = not show_hidden
        elif choice == 'v':
            show_preview = not show_preview
        elif choice == 'm':
            view_limit = min(view_limit + 15, 200)
        elif choice == 's':
            sort_key = 'size' if sort_key == 'date' else 'date'
        elif choice == 'c':
            chain_view = not chain_view
        elif choice.startswith('ce '):
            label = choice[3:].strip().upper()
            ch = CHAIN_GRAPH.chains.get(label)
            if not ch:
                print(Style.error(f"No chain {label!r} found."))
                input(f"\n{Style.DIM}Press Enter to continue...{Style.RESET}")
            else:
                process_chain_export(ch)
        elif choice == 'a':
            confirm = input(f"{Style.warn('Convert ALL listed sessions? (y/n): ')}")
            if confirm.lower() == 'y':
                idx = ",".join([str(i+1) for i in range(len(rendered_sessions))])
                process_conversion(idx, rendered_sessions)
        elif choice:
            process_conversion(choice, rendered_sessions)


def interactive_loop_cli():
    global EXEC_INDEX, CHAIN_GRAPH
    EXEC_INDEX = None
    CHAIN_GRAPH = None

    if not KIRO_CLI_SESSIONS_DIR.exists():
        print(Style.error(f"Kiro CLI sessions folder not found: {KIRO_CLI_SESSIONS_DIR}"))
        print(Style.info("Set KIRO_CLI_SESSIONS_DIR env var if Kiro CLI stores sessions elsewhere."))
        sys.exit(1)

    print(f"{Style.DIM}Scanning Kiro CLI sessions...{Style.RESET}")
    all_sessions_full = scan_cli_sessions()
    if not all_sessions_full:
        print(Style.error("No Kiro CLI sessions found.")); sys.exit(1)

    # Scope to the directory we were launched from — that exact directory and
    # nothing else. A parent workspace is only ever offered, never applied.
    CWD = current_dir()
    cwd_match = detect_workspace_from_cwd(all_sessions_full)
    cwd_parent = nearest_parent_workspace(all_sessions_full)
    workspace_filter: Optional[str] = cwd_match or CWD
    show_empty_helpers = False
    show_preview = False
    sort_key = 'date'
    view_limit = 15

    def visible_base_sessions():
        if show_empty_helpers:
            return all_sessions_full
        return [s for s in all_sessions_full if not getattr(s, 'is_empty_helper', False)]

    def current_sessions():
        pool = visible_base_sessions()
        if workspace_filter:
            pool = [s for s in pool if s.workspace_dir == workspace_filter]
        if sort_key == 'size':
            pool = sorted(pool, key=lambda s: -s.size)
        elif sort_key == 'messages':
            pool = sorted(pool, key=lambda s: -getattr(s, 'message_count', 0))
        else:
            pool = sorted(pool, key=lambda s: -s.date_created)
        return pool

    while True:
        base_sessions = visible_base_sessions()
        total_workspaces = len({s.workspace_dir for s in base_sessions if s.workspace_dir})
        hidden_helper_count = sum(1 for s in all_sessions_full if getattr(s, 'is_empty_helper', False))
        sessions = current_sessions()
        print_menu_header(
            workspace_filter, len(base_sessions), total_workspaces,
            cwd_match, source_label="Kiro CLI", storage_path=KIRO_CLI_SESSIONS_DIR
        )
        # Scoped to the launch directory, and that directory has nothing.
        # Say so plainly instead of drifting to some other directory.
        scoped_to_empty_cwd = (workspace_filter == CWD and cwd_match is None)
        if scoped_to_empty_cwd:
            print_no_sessions_here(CWD, base_sessions, cwd_parent)
            rendered_sessions: List = []
        else:
            print_scope_panel(base_sessions, workspace_filter, cwd_match,
                              hidden_helpers=hidden_helper_count,
                              showing_helpers=show_empty_helpers, limit=5)

            rendered_sessions = sessions[:view_limit]
            list_sessions_table(
                rendered_sessions,
                show_workspace=(workspace_filter is None),
                chain_graph=None,
                show_preview=show_preview,
            )
            if len(sessions) > view_limit:
                print(f"{Style.DIM}(Showing {view_limit} of {len(sessions)} sessions — press M for more){Style.RESET}")

        print(f"\n{Style.BOLD}OPTIONS:{Style.RESET}")
        print(f"  {Style.CYAN}[w]{Style.RESET}      : Switch directory  "
              f"{Style.DIM}(or {Style.RESET}{Style.YELLOW}w1{Style.RESET}{Style.DIM}, {Style.RESET}{Style.YELLOW}w2{Style.RESET}{Style.DIM}, …  to jump directly){Style.RESET}")
        print(f"  {Style.CYAN}[x]{Style.RESET}      : Show ALL directories (clear filter)")
        print(f"  {Style.DIM}{'─' * 56}{Style.RESET}")
        print(f"  {Style.GREEN}[ID, ID]{Style.RESET}: Convert specific CLI sessions (e.g. '1, 3')")
        print(f"  {Style.YELLOW}[a]{Style.RESET}      : Convert ALL listed sessions")
        print(f"  {Style.MAGENTA}[e]{Style.RESET}      : Toggle CLI helper/subagent sessions  "
              f"({'SHOWING' if show_empty_helpers else 'HIDDEN'})")
        print(f"  {Style.MAGENTA}[m]{Style.RESET}      : Show more rows (current: {view_limit})")
        print(f"  {Style.MAGENTA}[v]{Style.RESET}      : Toggle message preview column  "
              f"({'ON' if show_preview else 'OFF'})")
        print(f"  {Style.MAGENTA}[s]{Style.RESET}      : Sort date / messages / size  (current: {sort_key})")
        print(f"  {Style.MAGENTA}[r]{Style.RESET}      : Reload CLI session index")
        print(f"  {Style.RED}[q]{Style.RESET}      : Quit")
        choice = input(f"\n{Style.BOLD}Select > {Style.RESET}").strip().lower()

        if choice == 'q':
            print('Bye.'); sys.exit(0)
        elif choice == 'w':
            ws = select_workspace(base_sessions, workspace_filter)
            workspace_filter = ws
        elif re.fullmatch(r'w\d+', choice):
            rows = workspace_summary(base_sessions)
            n = int(choice[1:])
            if 1 <= n <= len(rows):
                workspace_filter = rows[n - 1][0]
            else:
                print(Style.error(f"No directory #{n} (have {len(rows)})."))
                input(f"\n{Style.DIM}Press Enter to continue...{Style.RESET}")
        elif choice == 'x':
            workspace_filter = None
        elif choice == 'r':
            all_sessions_full = scan_cli_sessions()
            cwd_match = detect_workspace_from_cwd(all_sessions_full)
            cwd_parent = nearest_parent_workspace(all_sessions_full)
        elif choice == 'e':
            show_empty_helpers = not show_empty_helpers
        elif choice == 'v':
            show_preview = not show_preview
        elif choice == 'm':
            view_limit = min(view_limit + 15, 200)
        elif choice == 's':
            order = ['date', 'messages', 'size']
            sort_key = order[(order.index(sort_key) + 1) % len(order)]
        elif choice == 'a':
            confirm = input(f"{Style.warn('Convert ALL listed CLI sessions? (y/n): ')}")
            if confirm.lower() == 'y':
                idx = ",".join([str(i+1) for i in range(len(rendered_sessions))])
                process_conversion(
                    idx, rendered_sessions,
                    parser_cls=CliSessionParser,
                    build_execution_index=False,
                    allow_scope=False,
                )
        elif choice:
            process_conversion(
                choice, rendered_sessions,
                parser_cls=CliSessionParser,
                build_execution_index=False,
                allow_scope=False,
            )


def select_session_source() -> str:
    default_source = 'cli' if KIRO_CLI_SESSIONS_DIR.exists() else 'ide'
    while True:
        _clear_screen()
        print(f"\n{Style.BOLD}KIRO SESSION MANAGER{Style.RESET}  {Style.DIM}choose session source{Style.RESET}\n")
        ide_default = f" {Style.DIM}[Default]{Style.RESET}" if default_source == 'ide' else ''
        cli_default = f" {Style.DIM}[Default]{Style.RESET}" if default_source == 'cli' else ''
        print(f"  {Style.YELLOW}[1]{Style.RESET} Kiro IDE sessions{ide_default}")
        print(f"      {Style.DIM}{WORKSPACE_SESSIONS_DIR}{Style.RESET}")
        print(f"  {Style.YELLOW}[2]{Style.RESET} Kiro CLI sessions{cli_default}")
        print(f"      {Style.DIM}{KIRO_CLI_SESSIONS_DIR}{Style.RESET}")
        print(f"  {Style.YELLOW}[3]{Style.RESET} Find by session ID")
        print(f"      {Style.DIM}Search IDE and CLI stores in parallel{Style.RESET}")
        print(f"  {Style.RED}[q]{Style.RESET} Quit")
        choice = input(f"\n{Style.BOLD}Select source > {Style.RESET}").strip().lower()
        if not choice:
            return default_source
        if choice in ('1', 'i', 'ide', 'kiro ide'):
            return 'ide'
        if choice in ('2', 'c', 'cli', 'kiro cli'):
            return 'cli'
        if choice in ('3', 'f', 'find', 'id', 'session id'):
            return 'find'
        if choice == 'q':
            print('Bye.'); sys.exit(0)


def interactive_loop():
    while True:
        source = select_session_source()
        if source == 'cli':
            interactive_loop_cli()
        elif source == 'find':
            interactive_find_session_by_id()
        else:
            interactive_loop_ide()

if __name__ == "__main__":
    try:
        interactive_loop()
    except KeyboardInterrupt:
        print("\nCancelled."); sys.exit(0)
