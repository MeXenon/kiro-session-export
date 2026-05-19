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
    BG_GRAY = '\033[48;5;236m'

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

KIRO_HOME = default_kiro_home()
WORKSPACE_SESSIONS_DIR = KIRO_HOME / "workspace-sessions"

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
                if sub.get('type') == 'text':
                    t = sub.get('text', '')
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

    @staticmethod
    def _scan_meta(fp: Path) -> Optional[Dict]:
        """Extract metadata fields with a single regex pass over the file
        bytes — avoids constructing the full Python tree for multi-MB
        execution records."""
        try:
            with open(fp, 'rb') as f:
                data = f.read()
        except Exception:
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

        # Scan files in parallel. Each worker reads its file bytes and runs
        # the combined-regex meta extraction — disk IO overlaps across
        # workers and json/orjson aren't even needed at this stage.
        from concurrent.futures import ThreadPoolExecutor
        workers = min(8, max(2, len(files)))
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
                    summary_cap: int = 0) -> str:
        if section_filter is None:
            section_filter = {s[0]: True for s in SECTION_DEFS}

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

def estimate_lines(item: Dict) -> int:
    t = item['type']
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

def interactive_filter(parsers: List[SessionParser], scope_label: str = "") -> Tuple[Dict[str, bool], bool, int, int, int, int, int]:
    _line_cache: Dict = {}

    def get_lines(cap_out, cap_user, cap_agent, cap_reason, cap_sum, cc):
        key = (cap_out, cap_user, cap_agent, cap_reason, cap_sum, cc)
        if key in _line_cache:
            return _line_cache[key]
        counts = {s[0]: 0 for s in SECTION_DEFS}
        msg_counts: Dict[str, int] = {}
        for p in parsers:
            keep_indices = set(range(len(p.data)))
            counters = {'u': 0, 'a': 0, 'r': 0, 's': 0}
            caps_map = {'user_message': cap_user, 'agent_message': cap_agent,
                        'reasoning': cap_reason, 'summarization': cap_sum}
            sym = {'user_message': 'u', 'agent_message': 'a', 'reasoning': 'r', 'summarization': 's'}
            for i in range(len(p.data) - 1, -1, -1):
                t = p.data[i]['type']
                if t in caps_map and caps_map[t] > 0:
                    s = sym[t]
                    if counters[s] >= caps_map[t]:
                        keep_indices.discard(i)
                    counters[s] += 1
            for i, it in enumerate(p.data):
                if i not in keep_indices:
                    continue
                t = it['type']
                if t in ('user_message', 'agent_message', 'reasoning', 'summarization'):
                    msg_counts[t] = msg_counts.get(t, 0) + 1
                lines = estimate_lines(it)
                if t == 'terminal_output' and cap_out > 0:
                    o = it.get('output', '') or ''
                    if isinstance(o, str):
                        total = o.count('\n') + 1 if o.strip() else 0
                        lines = min(total, cap_out) + 3 if total > 0 else 0
                counts[t] = counts.get(t, 0) + lines
            if p.metadata:
                counts['session_meta'] = counts.get('session_meta', 0) + 7
        _line_cache[key] = (counts, msg_counts)
        return counts, msg_counts

    fstate: Dict[str, bool] = {s[0]: s[3] for s in SECTION_DEFS}
    clean_content = False
    output_cap = 8;  cap_idx = CAP_STEPS.index(8)
    user_cap = 0;    u_idx = 0
    agent_cap = 0;   a_idx = 0
    reason_cap = 0;  r_idx = 0
    summary_cap = 0; s_idx = 0

    cursor = 0
    ROW_CLEAN   = len(SECTION_DEFS)
    ROW_CAP     = len(SECTION_DEFS) + 1
    ROW_USER    = len(SECTION_DEFS) + 2
    ROW_AGENT   = len(SECTION_DEFS) + 3
    ROW_REASON  = len(SECTION_DEFS) + 4
    ROW_SUMMARY = len(SECTION_DEFS) + 5
    num_items   = len(SECTION_DEFS) + 6

    import shutil as _shutil

    # Alternate screen buffer (htop/vim/less style) — keeps the filter UI off
    # the terminal's scrollback so it can't accumulate frames there. We
    # combine that with an INTERNAL viewport: when the filter has more rows
    # than the terminal can show, we render only the slice around the cursor
    # and surface ▲/▼ markers so you always know there's more.
    sys.stdout.write('\033[?1049h\033[?25l\033[H\033[2J')
    sys.stdout.flush()

    scroll_offset = 0

    try:
        while True:
            agg, msg_cnt = get_lines(output_cap, user_cap, agent_cap, reason_cap, summary_cap, clean_content)
            total_lines = sum(agg.get(s[0], 0) for s in SECTION_DEFS)
            sel_lines = sum(agg.get(s[0], 0) for s in SECTION_DEFS if fstate.get(s[0], False))
            pct = (sel_lines / total_lines * 100) if total_lines > 0 else 0

            # --- Build the LIST of navigable / decorative middle rows ----
            # Each entry is (cursor_id_or_None, rendered_text).
            mid_rows: List[Tuple[Optional[int], str]] = []

            for i, (key, name, emoji, _default) in enumerate(SECTION_DEFS):
                is_cursor = (i == cursor)
                is_on = fstate.get(key, False)
                lines = agg.get(key, 0)
                arrow = f'{Style.BOLD}{Style.YELLOW}▸{Style.RESET}' if is_cursor else ' '
                toggle = f'{Style.GREEN}██{Style.RESET}' if is_on else f'{Style.DIM}░░{Style.RESET}'
                if is_cursor and is_on:    nstyle = f'{Style.BOLD}{Style.GREEN}'
                elif is_cursor and not is_on: nstyle = f'{Style.BOLD}{Style.RED}'
                elif is_on:                nstyle = ''
                else:                      nstyle = Style.DIM
                msg_n = msg_cnt.get(key, 0)
                extra = (f' {Style.DIM}({msg_n:,} Msg){Style.RESET}'
                         if key in ('user_message','agent_message','reasoning','summarization') and msg_n > 0
                         else '')
                count_str = (f'{Style.CYAN}{lines:>6,}{Style.RESET}'
                             if is_on and lines > 0 else f'{Style.DIM}{lines:>6,}{Style.RESET}')
                if lines == 0:
                    count_str = f'{Style.DIM}     0{Style.RESET}'
                visible = f'{emoji} {name}'
                pad = max(1, 44 - len(visible))
                dots = f'{Style.DIM}{"·" * pad}{Style.RESET}'
                mid_rows.append((i,
                    f'  {arrow} {toggle} {nstyle}{visible}{Style.RESET} {dots} {count_str}{extra}'))

            # Visual separator between sections and caps
            mid_rows.append((None, f'  {Style.DIM}{"─" * 62}{Style.RESET}'))

            # Clean Chat
            cc_cur = (cursor == ROW_CLEAN)
            cc_arrow = f'{Style.BOLD}{Style.YELLOW}▸{Style.RESET}' if cc_cur else ' '
            cc_tog = f'{Style.GREEN}██{Style.RESET}' if clean_content else f'{Style.DIM}░░{Style.RESET}'
            cc_st = f'{Style.BOLD}' if cc_cur else Style.DIM
            cc_val = f'{Style.GREEN}ON {Style.RESET}' if clean_content else f'{Style.DIM}OFF{Style.RESET}'
            mid_rows.append((ROW_CLEAN,
                f'  {cc_arrow} {cc_tog} {cc_st}✂️  Clean Chat{Style.RESET} {Style.DIM}(strip IDE context from 👤🤖){Style.RESET}  {cc_val}'))

            def _caprow(row_id, label_emoji, label_text, value):
                cur = (cursor == row_id)
                arrow = f'{Style.BOLD}{Style.YELLOW}▸{Style.RESET}' if cur else ' '
                st = f'{Style.BOLD}' if cur else Style.DIM
                val = f'{Style.DIM}ALL{Style.RESET}' if value == 0 else f'{Style.YELLOW}{value}{Style.RESET}'
                hint = f' {Style.DIM}◀▶{Style.RESET}' if cur else ''
                return (row_id, f'  {arrow}    {st}{label_emoji} {label_text}{Style.RESET} {val}{hint}')

            mid_rows.append(_caprow(ROW_CAP,    '📤', 'Terminal Output Cap     ', output_cap))
            mid_rows.append(_caprow(ROW_USER,   '👤', 'User Message Cap        ', f'Last {user_cap}' if user_cap else 0))
            mid_rows.append(_caprow(ROW_AGENT,  '🤖', 'Agent Message Cap       ', f'Last {agent_cap}' if agent_cap else 0))
            mid_rows.append(_caprow(ROW_REASON, '🧠', 'Reasoning Cap           ', f'Last {reason_cap}' if reason_cap else 0))
            mid_rows.append(_caprow(ROW_SUMMARY,'✂️ ', 'Compaction Summary Cap  ', f'Last {summary_cap}' if summary_cap else 0))

            # --- Viewport math ----------------------------------------------
            term_size = _shutil.get_terminal_size((80, 30))
            term_h = max(10, term_size.lines)
            # Fixed-size header (3 lines: blank + title + separator) +
            # fixed-size footer (separator + progress bar + hint + 1 cushion).
            # Plus 2 lines reserved for ▲/▼ markers so the layout never jumps.
            HEADER_LINES = 3
            FOOTER_LINES = 5
            INDICATOR_LINES = 2
            view_h = max(5, term_h - HEADER_LINES - FOOTER_LINES - INDICATOR_LINES)

            # Find cursor's index in mid_rows and scroll viewport so it's visible
            cur_pos = next((idx for idx, (rid, _) in enumerate(mid_rows) if rid == cursor), 0)
            if cur_pos < scroll_offset:
                scroll_offset = cur_pos
            elif cur_pos >= scroll_offset + view_h:
                scroll_offset = cur_pos - view_h + 1
            max_offset = max(0, len(mid_rows) - view_h)
            scroll_offset = max(0, min(scroll_offset, max_offset))

            # --- Compose frame ----------------------------------------------
            out = ['\033[H\033[2J']  # home + clear (alt-screen surface)
            label = f"{len(parsers)} session{'s' if len(parsers) > 1 else ''}"
            if scope_label:
                label += f" · {scope_label}"
            out.append(f"  {Style.BOLD}{Style.HEADER}KIRO SECTION FILTER{Style.RESET}  {Style.DIM}({label}){Style.RESET}")
            out.append(f"  {Style.DIM}{'━' * 62}{Style.RESET}")
            out.append('')

            # ▲ indicator (always reserve the line — keeps row positions stable)
            if scroll_offset > 0:
                out.append(f'  {Style.DIM}▲ {scroll_offset} more above{Style.RESET}')
            else:
                out.append('')

            # Viewport slice
            for _rid, text in mid_rows[scroll_offset:scroll_offset + view_h]:
                out.append(text)

            # ▼ indicator
            below = max(0, len(mid_rows) - (scroll_offset + view_h))
            if below > 0:
                out.append(f'  {Style.DIM}▼ {below} more below{Style.RESET}')
            else:
                out.append('')

            # Footer: separator + progress + hint
            out.append(f'  {Style.DIM}{"━" * 62}{Style.RESET}')
            bar_w = 30
            filled = int(bar_w * pct / 100)
            bar = f'{Style.GREEN}{"█" * filled}{Style.DIM}{"░" * (bar_w - filled)}{Style.RESET}'
            sel_c = Style.GREEN if pct > 0 else Style.RED
            out.append(f'  {bar}  {sel_c}{Style.BOLD}{sel_lines:,}{Style.RESET}{Style.DIM}/{Style.RESET}{total_lines:,}  {Style.DIM}({pct:.0f}%){Style.RESET}')
            out.append(f'  {Style.DIM}↑↓ move  ⏎ toggle  ◀▶ cap  Q export  A all  N none  D defaults  1-7 presets{Style.RESET}')

            sys.stdout.write('\n'.join(out))
            sys.stdout.flush()

            key = read_key()
            if key == 'UP': cursor = (cursor - 1) % num_items
            elif key == 'DOWN': cursor = (cursor + 1) % num_items
            elif key in ('ENTER','SPACE'):
                if cursor < len(SECTION_DEFS):
                    fstate[SECTION_DEFS[cursor][0]] = not fstate[SECTION_DEFS[cursor][0]]
                elif cursor == ROW_CLEAN:
                    clean_content = not clean_content
            elif key == 'LEFT':
                if cursor == ROW_CAP: cap_idx = max(0, cap_idx - 1); output_cap = CAP_STEPS[cap_idx]
                elif cursor == ROW_USER: u_idx = max(0, u_idx - 1); user_cap = MSG_CAP_STEPS[u_idx]
                elif cursor == ROW_AGENT: a_idx = max(0, a_idx - 1); agent_cap = MSG_CAP_STEPS[a_idx]
                elif cursor == ROW_REASON: r_idx = max(0, r_idx - 1); reason_cap = MSG_CAP_STEPS[r_idx]
                elif cursor == ROW_SUMMARY: s_idx = max(0, s_idx - 1); summary_cap = MSG_CAP_STEPS[s_idx]
            elif key == 'RIGHT':
                if cursor == ROW_CAP: cap_idx = min(len(CAP_STEPS) - 1, cap_idx + 1); output_cap = CAP_STEPS[cap_idx]
                elif cursor == ROW_USER: u_idx = min(len(MSG_CAP_STEPS) - 1, u_idx + 1); user_cap = MSG_CAP_STEPS[u_idx]
                elif cursor == ROW_AGENT: a_idx = min(len(MSG_CAP_STEPS) - 1, a_idx + 1); agent_cap = MSG_CAP_STEPS[a_idx]
                elif cursor == ROW_REASON: r_idx = min(len(MSG_CAP_STEPS) - 1, r_idx + 1); reason_cap = MSG_CAP_STEPS[r_idx]
                elif cursor == ROW_SUMMARY: s_idx = min(len(MSG_CAP_STEPS) - 1, s_idx + 1); summary_cap = MSG_CAP_STEPS[s_idx]
            elif key == 'A':
                for s in SECTION_DEFS: fstate[s[0]] = True
            elif key == 'N':
                for s in SECTION_DEFS: fstate[s[0]] = False
            elif key == 'I':
                for s in SECTION_DEFS: fstate[s[0]] = not fstate[s[0]]
            elif key == 'D':
                for s in SECTION_DEFS: fstate[s[0]] = s[3]
                clean_content = False
                output_cap = 8; cap_idx = CAP_STEPS.index(8)
                user_cap = 0; u_idx = 0
                agent_cap = 0; a_idx = 0
                reason_cap = 0; r_idx = 0
                summary_cap = 0; s_idx = 0
            elif key == 'Q' or key == 'ESC':
                break
            elif key.isdigit():
                pi = int(key) - 1
                if 0 <= pi < len(FILTER_PRESETS):
                    _pname, pkeys, pclean = FILTER_PRESETS[pi]
                    if pkeys is None:
                        for s in SECTION_DEFS: fstate[s[0]] = s[3]
                    else:
                        for s in SECTION_DEFS: fstate[s[0]] = s[0] in pkeys
                    clean_content = pclean
    finally:
        # Restore cursor and leave the alt screen. The terminal pops back to
        # whatever was on the main screen before we entered.
        sys.stdout.write('\033[?25h\033[?1049l')
        sys.stdout.flush()
    return fstate, clean_content, output_cap, user_cap, agent_cap, reason_cap, summary_cap

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
                                reasoning_cap: int, summary_cap: int) -> Tuple[str, str]:
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
    # All files are saved to the same out_dir in this script, so picking
    # any one and using its parent is correct. Resolve it now and show it
    # to the user before opening so they can verify.
    target_dir = written_paths[0].resolve().parent
    answer = input(
        f"\n  {Style.BOLD}Open output directory?{Style.RESET}  "
        f"{Style.DIM}{target_dir}{Style.RESET}  "
        f"{Style.BOLD}[Y/n] {Style.RESET}"
    ).strip().lower()
    if answer and answer not in ('y', 'yes'):
        return
    success, opened = open_in_file_explorer(target_dir)
    if success:
        print(f"  {Style.GREEN}➜{Style.RESET} Opened {opened}")
    else:
        print(f"  {Style.warn('Could not open file explorer.')}  "
              f"{Style.DIM}({opened or target_dir}){Style.RESET}")

def print_menu_header(workspace_filter: Optional[str], total_sessions: int,
                      total_workspaces: int, cwd_match: Optional[str] = None):
    _clear_screen()
    print(f"\n{Style.BOLD}KIRO SESSION MANAGER{Style.RESET}  {Style.DIM}v1.1{Style.RESET}")
    print(f"{Style.DIM}Storage:   {KIRO_HOME}{Style.RESET}")
    try:
        out = Path(__file__).parent.resolve()
    except NameError:
        out = Path.cwd()
    print(f"{Style.DIM}Output:    {out}{Style.RESET}")
    if workspace_filter:
        scope = workspace_filter
        if cwd_match and cwd_match == workspace_filter:
            scope += f"  {Style.CYAN}◆ auto-selected from cwd{Style.RESET}"
    else:
        scope = f"ALL ({total_workspaces} workspaces, {total_sessions} sessions)"
    print(f"{Style.DIM}Scope:     {Style.RESET}{scope}{Style.DIM}{Style.RESET}\n")


def print_workspace_summary(all_sessions: List[SessionEntry],
                            current: Optional[str], cwd_match: Optional[str],
                            limit: int = 6):
    """Compact recap of workspaces at the top of the main screen.
    Each row is numbered — typing the number is the same as pressing `w`
    followed by that number, so workspace switching is one keypress away."""
    rows = workspace_summary(all_sessions)
    if not rows:
        return
    hint = (f"  {Style.YELLOW}[w]{Style.RESET} switch  ·  "
            f"{Style.YELLOW}[x]{Style.RESET} show ALL  ·  "
            f"{Style.YELLOW}[w<N>]{Style.RESET} jump to N")
    print(f"  {Style.BOLD}WORKSPACES{Style.RESET}  "
          f"{Style.DIM}({len(rows)} total){Style.RESET}  {hint}")
    for i, (ws, lst, last_ms) in enumerate(rows[:limit], start=1):
        try:
            rel = format_relative_time(last_ms / 1000.0)
        except Exception:
            rel = ''
        marks = []
        if ws == current: marks.append(f'{Style.GREEN}●{Style.RESET}')
        if ws == cwd_match: marks.append(f'{Style.CYAN}◆{Style.RESET}')
        marker = ' '.join(marks) if marks else ' '
        short = short_workspace(ws)
        count = f'{len(lst):>3} sess'
        num = f'{Style.YELLOW}w{i}{Style.RESET}'
        print(f"    {num}  {marker:<2} {count}  {Style.DIM}{rel:<14}{Style.RESET} {short}")
    if len(rows) > limit:
        print(f"          {Style.DIM}... and {len(rows) - limit} more — press {Style.YELLOW}w{Style.DIM} for full list{Style.RESET}")
    print()

def list_sessions_table(sessions: List[SessionEntry], show_workspace: bool = True,
                        chain_graph: Optional['ChainGraph'] = None):
    # Column widths
    if show_workspace:
        cols = ('ID', 4), ('DATE', 30), ('WORKSPACE', 18), ('TITLE', 24), ('CHAIN', 8), ('BADGE', 8), ('PREVIEW', 44), ('SIZE', 8)
    else:
        cols = ('ID', 4), ('DATE', 30), ('TITLE', 28), ('CHAIN', 8), ('BADGE', 8), ('PREVIEW', 56), ('SIZE', 8)
    header = ' '.join(f'{name:<{w}}' for name, w in cols)
    total_w = sum(w + 1 for _, w in cols) - 1
    print(f"{Style.BOLD}{header}{Style.RESET}")
    print(f"{Style.DIM}{'-' * total_w}{Style.RESET}")
    for idx, s in enumerate(sessions):
        s.load_preview()
        dt = s.date
        dt_str = dt.strftime("%Y-%m-%d %H:%M") if dt.year > 1970 else '--'
        rel_str = format_relative_time(dt.timestamp())
        size_label = format_size(s.size)
        title = s.display_title

        badges = []
        if s.from_compaction:
            badges.append('↻')
        if s.continuation_count:
            badges.append(f'↪×{s.continuation_count}')
        if s.hidden:
            badges.append('hide')
        badge_str = ' '.join(badges) if badges else ''

        tag = ""
        row_color = Style.RESET
        if idx == 0:
            tag = " [LATEST]"; row_color = Style.GREEN
        elif idx < 3:
            tag = " [NEW]"; row_color = Style.BLUE
        elif idx % 2 == 0:
            row_color = Style.CYAN

        def fit(text, w):
            text = (text or '')
            return text if len(text) <= w else text[:max(1, w - 1)] + '…'

        title_w = 24 if show_workspace else 28
        preview_w = 44 if show_workspace else 56
        display_title = fit(f"{title}{tag}", title_w)
        preview = fit(s.preview_text or '', preview_w)
        date_col = f"{dt_str} {rel_str}"
        date_col = fit(date_col, 30)

        # Chain column
        chain_col = ''
        if chain_graph is not None:
            ch = chain_graph.chain_for(s.session_id)
            if ch and ch.length > 1:
                pos = next((i+1 for i, x in enumerate(ch.sessions) if x.session_id == s.session_id), None)
                conf_mark = '✓' if (chain_graph.confidence_of.get(s.session_id) == 'authoritative') else '~'
                chain_col = f'{ch.id}·{pos}/{ch.length}{conf_mark}'

        if show_workspace:
            ws_name = fit(short_workspace(s.workspace_dir), 18)
            print(
                f"{row_color}"
                f"{(str(idx+1)+'  '):<4} {date_col:<30} {ws_name:<18} {display_title:<24} {chain_col:<8} {badge_str:<8} "
                f"{Style.DIM}{preview:<44}{Style.RESET}{row_color} {size_label:<8}{Style.RESET}"
            )
        else:
            print(
                f"{row_color}"
                f"{(str(idx+1)+'  '):<4} {date_col:<30} {display_title:<28} {chain_col:<8} {badge_str:<8} "
                f"{Style.DIM}{preview:<56}{Style.RESET}{row_color} {size_label:<8}{Style.RESET}"
            )
    print(f"{Style.DIM}{'-' * total_w}{Style.RESET}")

def _norm_ws_path(p: str) -> str:
    """Normalize a workspace path for cross-comparison (lowercase on Windows,
    forward slashes, no trailing separator)."""
    if not p:
        return ''
    q = str(p).replace('\\', '/').rstrip('/')
    return q.lower() if _IS_WINDOWS else q


def detect_workspace_from_cwd(all_sessions: List[SessionEntry]) -> Optional[str]:
    """If the current working directory is *inside* one of the known
    workspaces (or one of them is inside the cwd), pick that workspace."""
    try:
        cwd = Path.cwd().resolve()
    except Exception:
        return None
    cwd_norm = _norm_ws_path(str(cwd))
    if not cwd_norm:
        return None
    workspaces = {s.workspace_dir for s in all_sessions if s.workspace_dir}
    best: Optional[Tuple[str, int]] = None
    for ws in workspaces:
        wn = _norm_ws_path(ws)
        if not wn:
            continue
        # Exact match
        if wn == cwd_norm:
            return ws
        # cwd is inside the workspace
        if cwd_norm.startswith(wn + '/'):
            depth = wn.count('/')
            if best is None or depth > best[1]:
                best = (ws, depth)
        # workspace is inside cwd (less common — e.g. a parent monorepo dir)
        elif wn.startswith(cwd_norm + '/'):
            depth = cwd_norm.count('/')
            if best is None or depth > best[1]:
                best = (ws, depth)
    return best[0] if best else None


def workspace_summary(all_sessions: List[SessionEntry]) -> List[Tuple[str, List[SessionEntry], int]]:
    """Return [(workspace_dir, sessions, last_session_ms)] sorted by recency."""
    by_ws: Dict[str, List[SessionEntry]] = {}
    for s in all_sessions:
        by_ws.setdefault(s.workspace_dir or '(unknown)', []).append(s)
    rows = [(ws, lst, max((x.date_created or 0) for x in lst)) for ws, lst in by_ws.items()]
    rows.sort(key=lambda r: -r[2])
    return rows


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

        for s in members:
            s.load_preview()
            dt = s.date
            dt_str = dt.strftime("%Y-%m-%d %H:%M") if dt.year > 1970 else '--'
            rel_str = format_relative_time(dt.timestamp())
            size_label = format_size(s.size)
            title = s.display_title
            preview = (s.preview_text or '')[:42]
            badges = []
            if s.from_compaction: badges.append('↻')
            if s.continuation_count: badges.append(f'↪×{s.continuation_count}')
            if s.hidden: badges.append('hide')
            badge_str = ' '.join(badges)

            pos = next((i+1 for i, x in enumerate(ch.sessions) if x.session_id == s.session_id), 0)
            conf_local = chain_graph.confidence_of.get(s.session_id, '-')
            if pos == 1:
                tree = '┌─'
            elif pos == ch.length:
                tree = '└─'
            else:
                tree = '├─'
            conf_mark_local = '✓' if conf_local == 'authoritative' else '~'
            chain_pos = f'{tree} {pos:>2}/{ch.length}{conf_mark_local}'

            id_label = f'{next_id:<3}'
            print(
                f"    {Style.YELLOW}{id_label}{Style.RESET} "
                f"{Style.DIM}{chain_pos:<12}{Style.RESET} "
                f"{title[:30]:<30} "
                f"{Style.DIM}{badge_str:<10}{Style.RESET} "
                f"{Style.DIM}{dt_str} {rel_str[:13]:<13}{Style.RESET} "
                f"{Style.DIM}{preview:<42}{Style.RESET} "
                f"{size_label}"
            )
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
            for cid, members in visible_singletons:
                for s in members:
                    s.load_preview()
                    dt = s.date
                    dt_str = dt.strftime("%Y-%m-%d %H:%M") if dt.year > 1970 else '--'
                    rel_str = format_relative_time(dt.timestamp())
                    size_label = format_size(s.size)
                    title = s.display_title
                    preview = (s.preview_text or '')[:42]
                    badges = []
                    if s.from_compaction: badges.append('↻')
                    if s.continuation_count: badges.append(f'↪×{s.continuation_count}')
                    if s.hidden: badges.append('hide')
                    badge_str = ' '.join(badges)
                    id_label = f'{next_id:<3}'
                    ws_label = f' {Style.DIM}{short_workspace(s.workspace_dir):<16}{Style.RESET}' if show_workspace else ''
                    print(
                        f"    {Style.YELLOW}{id_label}{Style.RESET}             "
                        f"{title[:30]:<30} "
                        f"{Style.DIM}{badge_str:<10}{Style.RESET} "
                        f"{Style.DIM}{dt_str} {rel_str[:13]:<13}{Style.RESET} "
                        f"{Style.DIM}{preview:<42}{Style.RESET}{ws_label} "
                        f"{size_label}"
                    )
                    flat_ordered.append(s)
                    next_id += 1

    return flat_ordered


def select_workspace(all_sessions: List[SessionEntry], current: Optional[str] = None) -> Optional[str]:
    rows = workspace_summary(all_sessions)
    _clear_screen()
    print(f"\n  {Style.BOLD}{Style.HEADER}WORKSPACES{Style.RESET}  {Style.DIM}(sorted by most recent activity){Style.RESET}\n")
    print(f"  {Style.DIM}{'-' * 92}{Style.RESET}")
    print(f"  {Style.BOLD}{'ID':<4} {'LAST ACTIVITY':<32} {'SESS':<6} {'WORKSPACE'}{Style.RESET}")
    print(f"  {Style.DIM}{'-' * 92}{Style.RESET}")
    print(f"  {Style.YELLOW}[0]{Style.RESET}  {'all':<32} {len(all_sessions):<6} {Style.DIM}— all workspaces —{Style.RESET}")
    cwd_hit = detect_workspace_from_cwd(all_sessions)
    for i, (ws, lst, last_ms) in enumerate(rows):
        try:
            dt = datetime.fromtimestamp(last_ms / 1000.0)
            when = dt.strftime('%Y-%m-%d %H:%M') + '  ' + format_relative_time(dt.timestamp())
        except Exception:
            when = '?'
        marker = ''
        if current and ws == current:
            marker = f' {Style.GREEN}● current{Style.RESET}'
        if ws == cwd_hit and cwd_hit != current:
            marker = f' {Style.CYAN}◆ matches cwd{Style.RESET}'
        print(f"  {Style.YELLOW}[{i+1}]{Style.RESET}  {when:<32} {len(lst):<6} {ws}{marker}")
    print(f"  {Style.DIM}{'-' * 92}{Style.RESET}")
    choice = input(f"\n  {Style.BOLD}Select workspace > {Style.RESET}").strip()
    if not choice or choice == '0':
        return None
    if choice.isdigit() and 1 <= int(choice) <= len(rows):
        return rows[int(choice) - 1][0]
    return None

# ──────────────────────────────────────────────────────────────
# Chain merge export
# ──────────────────────────────────────────────────────────────
def _parse_sessions_parallel(entries: List['SessionEntry'], max_workers: int = 6) -> List['SessionParser']:
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
            return idx, SessionParser(entry)
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
                            output_cap: int = 0) -> Tuple[str, str]:
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
                                  output_cap=output_cap)
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
    section_filter, clean_content, output_cap, *_caps = interactive_filter(
        parsers, scope_label=f"chain {chain.id}"
    )
    if not any(section_filter.values()):
        print(Style.warn("Nothing selected — skipping export."))
        input(f"\n{Style.DIM}Press Enter to continue...{Style.RESET}")
        return

    try:
        out_dir = Path(__file__).parent.resolve()
    except NameError:
        out_dir = Path.cwd()
    md, fname = merge_chain_to_markdown(chain, section_filter, clean_content, output_cap)
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
def process_conversion(indices_str: str, sessions: List[SessionEntry]):
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
    if EXEC_INDEX is not None and not EXEC_INDEX._built:
        EXEC_INDEX.build(progress=True)

    print(f"\n{Style.info(f'Parsing {len(chosen)} session(s)...')}")
    parsers = _parse_sessions_parallel(chosen)
    if not parsers:
        return

    scope_type, turn_limit = select_extraction_scope(parsers)
    scope_label = ""
    if scope_type == 'last_n':
        for p in parsers:
            p.trim_to_last_n_turns(turn_limit)
        scope_label = f"last {turn_limit} turn{'s' if turn_limit != 1 else ''}"
    elif scope_type == 'live':
        for p in parsers:
            p.trim_to_live_context()
        scope_label = "live context"

    section_filter, clean_content, output_cap, user_cap, agent_cap, reason_cap, summary_cap = \
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

    print(f"\n{Style.info(f'Processing {len(parsers)} session(s)...')}")
    try:
        out_dir = Path(__file__).parent.resolve()
    except NameError:
        out_dir = Path.cwd()

    written_paths: List[Path] = []
    clipboard_md: List[str] = []

    if dest in ('f', 'b') and file_mode == 'combined' and len(parsers) > 1:
        # One merged document
        try:
            combined_md, combined_fname = combine_parsers_to_markdown(
                parsers, section_filter, clean_content, output_cap,
                user_cap, agent_cap, reason_cap, summary_cap,
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
                    out_path = out_dir / out_filename
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

# ──────────────────────────────────────────────────────────────
# Interactive main loop
# ──────────────────────────────────────────────────────────────
def interactive_loop():
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

    # Auto-detect workspace from cwd
    cwd_match = detect_workspace_from_cwd(all_sessions_full)
    workspace_filter: Optional[str] = cwd_match
    show_hidden = False
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
        print_workspace_summary(all_sessions_full, workspace_filter, cwd_match, limit=6)

        # Chain summary line
        multi_chains = [c for c in CHAIN_GRAPH.chains.values() if c.length > 1]
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
        elif choice == 'h':
            show_hidden = not show_hidden
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

if __name__ == "__main__":
    try:
        interactive_loop()
    except KeyboardInterrupt:
        print("\nCancelled."); sys.exit(0)
