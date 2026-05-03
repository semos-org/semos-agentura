# Semos Agentura — Agent Architecture Reference

This document captures the architectural patterns implemented across semos-agentura, drawing on proven patterns from production coding agents (Claude Code, OpenCode) adapted for domain-specific use cases (document management, email, research).

## 1. The Universal Agent Loop

**Implementation**: `agentura-commons/src/agentura_commons/llm_executor.py` — `LLMExecutor`

The core of every agent is the same loop:

```
User message
  ↓
LLM call (with tool definitions)
  ↓
Parse response → text parts + tool calls
  ↓
If no tool calls → return text (done)
If tool calls → execute each, append results
  ↓
Loop back to LLM call (up to max_steps)
```

`LLMExecutor` supports both Anthropic (native API + Azure AI Foundry) and OpenAI-compatible endpoints. Provider detection is automatic based on `api_base` URL.

Every agent instantiation differs only in:
- **tools** — the `list[ToolDef]` specific to the domain
- **system_prompt** — what the agent knows about itself
- **max_steps** — guard against runaway loops (default: 10)

This pattern matches Claude Code's QueryEngine and OpenCode's streamText+processor loop.

## 2. Tool System

**Implementation**: `agentura-commons/src/agentura_commons/base.py` — `ToolDef`, `ToolResult`

### ToolDef

```python
@dataclass
class ToolDef:
    name: str           # Tool identifier
    description: str    # LLM-visible description
    fn: Any             # Callable (async or sync)
    parameters: dict    # Optional explicit JSON schema (auto-generated if omitted)
    file_params: list   # Parameters that accept FileAttachment
    read_only: bool     # MCP annotation: safe to retry
    destructive: bool   # MCP annotation: may cause data loss
    idempotent: bool    # MCP annotation: same input → same result
```

### Auto-schema generation

If `parameters` is not provided, `LLMExecutor` generates a JSON schema from the function signature via `inspect.signature()`. Type annotations map to JSON types: `str→string`, `int→integer`, `bool→boolean`, `list→array`.

For complex parameter types (nested objects, typed arrays), provide an explicit `parameters` dict on `ToolDef` instead of relying on auto-generation. Example: `batch_edit` uses `parameters={"type": "object", "properties": {"edits": {"type": "array", "items": {"type": "object", ...}}}}` so the LLM passes structured data directly — no JSON-in-a-string encoding needed.

### Tool result normalization

Tool functions can return any Python type. The MCP wrapper normalizes automatically:
- `str` → text content
- `dict` / `list` → JSON data
- `Path` → file reference (download URL)
- `NamedFile` → file with display name
- `ToolResult` → explicit multi-modal (text + data + files)

### Design principles (from coding agent research)

- Always include `max_results` on search/list tools to prevent runaway output
- Return structured JSON for machine-parseable results
- Provide explicit error messages with context (which edit failed, why)
- Use `read_only`, `destructive`, `idempotent` annotations for client-side safety hints

## 3. Synthetic Tools (Agent Interaction)

**Implementation**: `agentura-commons/src/agentura_commons/llm_executor.py` — synthetic tool definitions

The LLM executor injects 5 synthetic tools into every agent session. These are intercepted by the executor itself (never reach real tool functions) and control the task lifecycle:

| Tool | Purpose | A2A State |
|------|---------|-----------|
| `_request_input` | Pause and ask the requester for clarification | `INPUT_REQUIRED` |
| `_return_result` | Explicit final answer with optional file selection | `COMPLETED` |
| `_report_progress` | Streaming progress updates during long operations | stays `WORKING` |
| `_reject_task` | Decline out-of-scope or invalid requests | `REJECTED` |
| `_request_auth` | Request credentials when auth is needed | `AUTH_REQUIRED` |

These correspond to patterns seen in coding agents:
- Claude Code: `AskUserQuestion` → `_request_input`
- Claude Code: status line → `_report_progress`
- OpenCode: `question` tool → `_request_input`

The key difference: in coding agents these are built-in UI features. In semos-agentura they're protocol-level (A2A task states), making them work across any transport.

## 4. A2A Protocol Integration

**Implementation**: `agentura-commons/src/agentura_commons/a2a_server.py`

### AgentCard

Each agent advertises itself at `/.well-known/agent-card.json`:

```json
{
  "name": "Filesystem Agent",
  "description": "Browse, search, read, write files across local, WebDAV, SharePoint, and remote archives.",
  "version": "0.2.0",
  "skills": [{"id": "filesystem-operations", "name": "Filesystem Operations", ...}],
  "capabilities": {"streaming": false},
  "supported_interfaces": [
    {"url": "http://host:port/a2a", "protocol_binding": "HTTP+JSON"},
    {"url": "http://host:port/a2a/rpc", "protocol_binding": "JSONRPC"}
  ]
}
```

### Task lifecycle

```
Client sends message
  → _AgentExecutor.execute()
    → TaskStatusUpdateEvent(WORKING)
    → dispatch: explicit tool call OR natural language via LLMExecutor
    → TaskArtifactUpdateEvent (for each file produced)
    → TaskStatusUpdateEvent(COMPLETED | FAILED | INPUT_REQUIRED | AUTH_REQUIRED)
```

### Multi-turn conversations

- Task history persisted in `_task_histories` (keyed by task_id and context_id)
- File context accumulated across tasks in `_context_files` (keyed by context_id)
- On `INPUT_REQUIRED`: history saved → client answers → executor resumes with full history

### Request routing

1. **Explicit tool call**: `Part(data={"tool": "read_file", "arguments": {"uri": "..."}})` → direct dispatch
2. **Natural language**: `Part(text="list all PDFs")` → LLMExecutor with all agent tools
3. **Fallback**: `execute_skill()` with keyword-based routing

## 5. MCP Protocol Integration

**Implementation**: `agentura-commons/src/agentura_commons/mcp_server.py`

The same `BaseAgentService` serves both MCP and A2A. `create_app()` wires both transports into a single FastAPI application.

- `get_tools()` → MCP tool definitions (auto-schema from ToolDef)
- File middleware (`FileRegistry`, `FileAttachment`) handles binary exchange
- See [file-handling-spec.md](file-handling-spec.md) for the full file exchange specification

## 6. BaseAgentService Contract

**Implementation**: `agentura-commons/src/agentura_commons/base.py`

Every agent implements this abstract base class:

```python
class BaseAgentService(ABC):
    # Required
    agent_name: str               # "Filesystem Agent"
    agent_description: str        # Human-readable description
    agent_version: str            # Semver
    get_tools() -> list[ToolDef]  # All tools the agent exposes
    get_skills() -> list[SkillDef]  # A2A skill definitions
    execute_skill(skill_id, message) -> str  # Natural language fallback

    # Provided by framework
    output_dir: Path              # Set by create_app(), for file output
    router_llm_model: str         # From env: ROUTER_LLM_MODEL
    router_llm_api_key: str       # From env: ROUTER_LLM_API_KEY
    router_llm_api_base: str      # From env: ROUTER_LLM_API_BASE
```

`create_app(service, base_url)` produces a FastAPI app with:
- MCP endpoints (tool listing, tool execution)
- A2A endpoints (`/a2a`, `/a2a/rpc`, `/.well-known/agent-card.json`)
- Static file serving (for `output_dir`)

## 7. Planning & Exploration Patterns

Coding agents separate **planning** (read-only exploration) from **execution** (full tool access). This pattern applies beyond code:

### Planning mode
- Agent explores the VFS: `list_files`, `file_tree`, `grep`, `glob`, `read_file`
- Asks clarifying questions via `_request_input`
- Proposes a plan before making changes
- No write operations until plan is approved

### Execution mode
- Agent executes with full tool access: `write_file`, `edit_file`, `batch_edit`, `move_file`, `delete_file`
- Reports progress via `_report_progress`
- Returns results via `_return_result`

### Task decomposition
- Complex requests → break into subtasks with explicit progress tracking
- Each subtask is a tool call or sequence of calls
- Progress reported between subtasks via `_report_progress`

### Exploration strategy (from coding agent research)
- **Breadth-first**: `file_tree` with low depth to get overview, then `read_file` on specific targets
- **Parallel search**: `grep` with concurrent file reads (up to 10 parallel) for remote roots
- **Smart routing**: prefer server-side search (`search_sharepoint`) over client-side scan where available

## 8. Editing Strategies

Three strategies, matching coding agent patterns:

### Full rewrite (`write_file`)
- Best for: new files, small files, complete rewrites
- LLM generates entire file content
- Risk: lost-in-the-middle for large files (LLM may miss middle sections)

### Surgical edit (`edit_file`)
- Best for: targeted changes in existing files
- Pattern: `old_text` → `new_text` with ambiguity detection
- Same pattern as Claude Code's FileEditTool
- Validates: `old_text` must exist and be unique (or use `replace_all`)

### Batch edit (`batch_edit`)
- Best for: multiple coordinated changes in one file
- Pattern: structured `edits` array parameter (not a JSON string — uses explicit `parameters` schema on `ToolDef` with `type: "array"`, `items: {type: "object", properties: {old, new}}`)
- Same efficiency as OpenCode's `multiedit` — N changes in 1 round trip instead of N
- Each edit sees the result of prior edits (sequential application)

### When to use which
| Scenario | Strategy |
|----------|----------|
| New file | `write_file` |
| Small file (<100 lines) | `write_file` |
| Single change in large file | `edit_file` |
| Multiple changes in one file | `batch_edit` |
| Same replacement everywhere | `edit_file(replace_all=true)` |

## 9. Memory System

The VFS itself serves as the memory substrate — no separate memory infrastructure needed.

### Session memory (per-session)
- `.md` files in the `session://` root (in-memory filesystem)
- Agent writes observations, decisions, and learnings as markdown
- Automatically available within the session via standard VFS tools
- Ephemeral: disappears when session ends

### Global memory (cross-session)
- `.md` files in a persistent VFS root (e.g. `memory://` backed by local disk)
- Present in ALL agent sessions as a mounted root
- Shared knowledge base: conventions, patterns, recurring solutions
- Agent reads on startup, writes back discoveries
- Updated via standard `write_file` / `edit_file` — no special API

### Future: Knowledge graph
- Evolve from flat `.md` files to structured knowledge graph
- **Build phase**: extract entities and relations from session memories
- **Patch phase**: incrementally update graph with new observations
- **Query phase**: semantic search over accumulated knowledge
- Graph operations exposed as additional VFS tools

### Key insight
VFS IS the memory system. Files are the universal abstraction — whether the "memory" lives in a local directory, an in-memory filesystem, or a SharePoint document library, the same tools (`read_file`, `write_file`, `edit_file`, `grep`) work uniformly.

## 10. Context Management

### Current implementation
- `LLMExecutor.max_steps` — hard guard against infinite loops
- Task history persistence — `_task_histories[task_id]` for multi-turn
- File context accumulation — `_context_files[context_id]` across tasks

### Future: Compaction
For long-running sessions, context will need compression:
- **Pruning**: remove old tool results, keep recent ones
- **Summarization**: summarize older conversation turns
- **Sawtooth pattern**: context grows during exploration, collapses during consolidation

This matches the patterns used by Claude Code (automatic compaction) and OpenCode (pruning + compaction agent).

## 11. File Exchange Protocol

See [file-handling-spec.md](file-handling-spec.md) for the full specification.

Core principle: **the LLM never touches binary data**. A client middleware layer handles all binary serialization — injecting file content into tool calls before they reach the server, and extracting file content from tool results before they reach the LLM.

Key types:
- `FileAttachment` — `{name: str, content: str}` — content is path, base64, or data URI
- `NamedFile` — `Path` + display name (when on-disk name differs from user-facing name)
- `FileRegistry` — client-side registry mapping names to content

## 12. Agent Composition

### MCP client (`MCPHub`)
Connect to external MCP servers to extend an agent's tool set. The hub manages connections, tool discovery, and result routing.

### A2A client (`A2AAgentInfo`, `A2AResult`)
Delegate tasks to other agents via A2A protocol. Supports:
- Agent discovery via AgentCard
- Task submission and result retrieval
- Multi-turn conversations across agents

### Unified client (`AgenturaClient`)
Single interface for both MCP tools and A2A task delegation. An orchestrator agent can:
1. Discover available specialist agents
2. Delegate file operations to filesystem-agent
3. Delegate email operations to email-agent
4. Delegate document operations to document-agent
5. Synthesize results from multiple agents

### Pattern: Hub-and-spoke
```
Orchestrator Agent
  ├── filesystem-agent (A2A) — file access, search, editing
  ├── document-agent (A2A) — OCR, composition, form filling
  ├── email-agent (A2A) — Outlook, calendar, contacts
  └── external MCP servers — additional capabilities
```
