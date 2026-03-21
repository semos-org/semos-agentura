# File Handling Specification for MCP/A2A Chat Clients

> Status: Draft — March 2026
> Context: Semos Agentura agents need to exchange files with chat UIs.
> Neither MCP nor any chat client handles this well today.

## Problem

MCP tools that process or produce files (OCR, document composition, form filling, diagrams) need to:
1. **Receive** files from the user (input)
2. **Return** files to the user (output)

No MCP chat client (Claude Desktop, LibreChat, Cherry Studio, OpenCode) supports either direction reliably as of March 2026.

## Design Principle: The LLM Never Touches Binary Data

The LLM works with **file references** (names, IDs). A **client middleware layer** handles all binary serialization — injecting file content into tool calls before they reach the server, and extracting file content from tool results before they reach the LLM.

This is analogous to how Anthropic's API handles images: the client injects `image` blocks with base64 into the request; the LLM never generates base64 itself.

```
┌─────────────────────────────────────────────────────────────┐
│                        Chat Client                          │
│                                                             │
│  ┌──────────┐    ┌────────────────┐    ┌────────────────┐  │
│  │  User UI │───▶│  File Registry  │───▶│  LLM Context   │  │
│  │          │    │                │    │                │  │
│  │ attach   │    │ "invoice.pdf"  │    │ "User attached │  │
│  │ file     │    │  → stored blob │    │  invoice.pdf"  │  │
│  └──────────┘    └───────┬────────┘    └───────┬────────┘  │
│                          │                     │            │
│                          │    ┌────────────────┘            │
│                          │    │ LLM decides:                │
│                          │    │ call tool(source="invoice.pdf")
│                          ▼    ▼                             │
│                 ┌──────────────────────┐                    │
│                 │  Middleware (pre)     │                    │
│                 │                      │                    │
│                 │ tool schema says     │                    │
│                 │ source: file_ref     │                    │
│                 │ → replace with       │                    │
│                 │   signed URL or      │                    │
│                 │   base64 from        │                    │
│                 │   file registry      │                    │
│                 └──────────┬───────────┘                    │
│                            │                                │
└────────────────────────────┼────────────────────────────────┘
                             │  HTTP / MCP
                             ▼
                    ┌──────────────────┐
                    │   MCP Tool       │
                    │   (agent)        │
                    │                  │
                    │ receives: URL,   │
                    │ base64, or path  │
                    │ → processes file │
                    │ → returns result │
                    └────────┬─────────┘
                             │
                             ▼  tool result: {download_url, filename}
┌────────────────────────────┼────────────────────────────────┐
│                 ┌──────────┴───────────┐                    │
│                 │  Middleware (post)    │                    │
│                 │                      │                    │
│                 │ result has           │                    │
│                 │ download_url?        │                    │
│                 │ → fetch file         │                    │
│                 │ → register in        │                    │
│                 │   file registry      │                    │
│                 │ → replace URL with   │                    │
│                 │   "Tool produced     │                    │
│                 │    report.pptx"      │                    │
│                 └──────────┬───────────┘                    │
│                            │                                │
│                    ┌───────▼────────┐    ┌──────────┐       │
│                    │  LLM Context   │    │  User UI │       │
│                    │                │    │          │       │
│                    │ "Tool produced │───▶│ download │       │
│                    │  report.pptx"  │    │ button + │       │
│                    │                │    │ preview  │       │
│                    └────────────────┘    └──────────┘       │
│                        Chat Client                          │
└─────────────────────────────────────────────────────────────┘
```

## Specification

### 1. File Registry

The chat client MUST maintain a **file registry** — a mapping of file references to stored blobs.

```
Registry:
  "invoice.pdf"   → {blob: <bytes>, mime: "application/pdf", size: 245760, source: "upload"}
  "report.pptx"   → {blob: <bytes>, mime: "application/vnd...", size: 1048576, source: "tool:compose_document"}
```

Entries are added when:
- User attaches a file to a message
- A tool result contains a file (download_url, EmbeddedResource, or FilePart)

Entries are presented to the LLM as short text references, never as binary content.

### 2. Tool Schema Annotations

Tools declare which parameters accept files using a schema annotation:

```json
{
  "name": "digest_document",
  "parameters": {
    "type": "object",
    "properties": {
      "source": {
        "type": "string",
        "description": "Document to digest.",
        "x-file": true
      }
    }
  }
}
```

The `x-file: true` annotation (or equivalent, e.g., `format: "file-reference"`) tells the middleware: "when the LLM puts a filename here, resolve it from the file registry before sending to the tool."

Until clients support schema annotations, we use the description text as a hint:
`"Accepts an absolute file path or base64-encoded file content."`

### 3. Symmetric Middleware Design

Both directions — input and output — follow the same pattern. Each side has middleware that decides **independently** how to transport the file: inline base64 or URL. The decision is based on file size, network topology, and capabilities.

```
                   CLIENT MIDDLEWARE                    AGENT MIDDLEWARE
                   (pre/post-process)                  (pre/post-process)

INPUT:   LLM says "invoice.pdf"
           │
           ▼
         resolve from registry
           │
           ├─ small file? ──▶ inline base64  ─────────▶ detect base64 → write to temp
           │
           └─ large file? ──▶ signed URL     ─────────▶ detect URL → fetch → write to temp
                                                         │
                                                         ▼
                                                       _resolve_file() → local Path
                                                         │
                                                         ▼
                                                       tool processes file

OUTPUT:                                                tool produces file
                                                         │
                                                         ▼
                                                       decide transport:
                                                         │
         detect base64 → store in registry  ◀──────── ├─ small? → inline base64 in result
           │                                           │
         detect URL → fetch → store in registry ◀───── └─ large? → serve at /files/ → URL in result
           │
           ▼
         replace for LLM:
         "Tool produced report.pptx (1.0 MB)"
```

**Neither middleware is required.** The system degrades gracefully:
- No client middleware → LLM sees raw URL → user clicks link manually
- No agent middleware → tool returns URL only → still works
- Both present → seamless: LLM sees filenames, user sees previews/downloads

### 4. File Input (User → Tool)

#### 4.1 What the LLM sees

```
User attached: invoice.pdf (PDF, 240 KB)
User: "Please inspect the form fields in this document"
```

The LLM calls the tool with the **filename only**:

```json
{"name": "inspect_form", "arguments": {"file_path": "invoice.pdf"}}
```

#### 4.2 Client middleware decides transport

The client middleware looks up `"invoice.pdf"` in the file registry, then chooses:

| Condition | Transport | What the tool receives |
|-----------|-----------|----------------------|
| File ≤ threshold (e.g., 10 MB) | Inline base64 | `"data:application/pdf;base64,JVBERi0..."` |
| File > threshold | Signed URL | `"https://client/staging/abc123?token=xyz"` |
| Same machine (fallback) | Local path | `"/staging/user-42/invoice.pdf"` |

The threshold is a client configuration. Inline is simpler (no extra HTTP round-trip), URL scales better.

#### 4.3 Agent middleware resolves

The tool's `_resolve_file()` accepts any form and returns a local `Path`:
- base64/data URI → decode → write temp file
- URL → fetch → write temp file
- path → use directly

### 5. File Output (Tool → User)

#### 5.1 Agent middleware decides transport

The tool produces a file. The agent middleware chooses:

| Condition | Transport | What the client receives |
|-----------|-----------|------------------------|
| File ≤ threshold (e.g., 1 MB) | Inline base64 | `"data:application/pdf;base64,JVBERi0..."` in result |
| File > threshold | Download URL | `{"download_url": "http://agent:8002/files/abc.pptx"}` |
| Both (belt + suspenders) | URL + base64 | Both fields present; client picks |

Current implementation always uses URL (simplest, works everywhere).

#### 5.2 Client middleware registers the file

1. Detects file in tool result (`download_url`, base64, or `EmbeddedResource`)
2. If URL: fetches the file content
3. Registers in file registry: `"report.pptx" → {blob, mime, size, source: "tool"}`
4. Replaces the tool result for the LLM:

```
Tool produced: report.pptx (PowerPoint, 1.0 MB)
```

#### 5.3 What the user sees

The client renders the file from the registry:

| MIME type | Rendering |
|-----------|-----------|
| `image/*` | Inline image |
| `text/html` | Sandboxed iframe |
| `application/pdf` | PDF viewer or download button |
| `audio/*`, `video/*` | Media player |
| Other | Download button with icon + filename + size |

### 5. Inline Size Limits

| Context | Max inline (base64) | Larger files |
|---------|:------------------:|:------------:|
| MCP tool input (middleware → tool) | 10 MB | Signed URL |
| MCP tool output (tool → middleware) | No limit on download_url | Middleware fetches on demand |
| MCP EmbeddedResource (if used) | 1 MB | URI-only |
| A2A FilePart (agent-to-agent) | No limit | URI preferred for >10 MB |
| LLM context | **0 bytes** | LLM only sees filenames |

The key insight: **the LLM context window is not a constraint** because binary data never enters it. The middleware handles all serialization outside the LLM's token budget.

### 6. A2A File Transfer

For agent-to-agent communication (no user in the loop), use A2A's native `FilePart`:

```json
{
  "type": "file",
  "file": {
    "name": "report.pptx",
    "mimeType": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "bytes": "<base64>"
  }
}
```

Or by URI reference (preferred for large files):

```json
{
  "type": "file",
  "file": {
    "name": "report.pptx",
    "mimeType": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "uri": "http://document-agent:8002/files/a3f1c2d0_report.pptx"
  }
}
```

A2A `FilePart` is preferred over MCP `EmbeddedResource` for agent-to-agent because:
- First-class type (not bolted onto tool results)
- Supports both inline and URI natively
- Part of the task lifecycle (can stream chunks)
- No size constraints from LLM context

### 7. Security

#### Download URLs
- MUST use UUID/random tokens in the path (not guessable filenames)
- SHOULD be time-limited (signed URL or server-side TTL)
- MUST be scoped to the requesting user/session in multi-user deployments
- MUST use HTTPS in production

#### File Registry
- Entries MUST be scoped per user/session
- Entries SHOULD have a configurable TTL (default: 1 hour)
- The registry MUST NOT persist across sessions unless explicitly configured

#### Staging Area
- Uploaded files MUST be isolated per user
- Files MUST be deleted after the configured TTL
- Maximum upload size SHOULD be configurable (default: 50 MB)

### 8. Implementation Checklist

#### Agent side (our responsibility)

- [x] `_resolve_file()` accepts path, base64, and data URI
- [x] File-producing tools return `download_url` + `filename`
- [x] Output files use UUID-prefixed names
- [x] `/files/` static endpoint serves output directory
- [ ] Add `mime_type` and `size_bytes` to tool responses
- [ ] Add `x-file` schema annotation to file parameters
- [ ] Return `EmbeddedResource` for small files alongside download URL
- [ ] A2A `FilePart` responses for agent-to-agent
- [ ] Signed/expiring download URLs (production)
- [ ] Per-user file isolation (multi-user production)

#### Chat client middleware (upstream or our orchestrator)

- [ ] File registry (upload tracking + tool output tracking)
- [ ] Pre-processing: resolve file references → URL/base64 before tool call
- [ ] Post-processing: detect download_url → fetch → register → replace with text reference
- [ ] Schema-driven: use `x-file` annotation to identify file parameters
- [ ] Render files from registry as downloads/previews in UI

#### Chat client UI (upstream)

- [ ] File attachment UI with drag-and-drop
- [ ] Render tool-produced files as download buttons / inline previews
- [ ] Never display raw base64 or URLs to the user

### 9. Protocol Comparison

| Capability | MCP (today) | MCP + Middleware | A2A |
|-----------|:-----------:|:---------------:|:---:|
| File input to tool | Path only | File ref → URL/base64 | `FilePart` in message |
| File output from tool | Text with URL | URL → registry → preview | `FilePart` in artifact |
| LLM sees binary data | Yes (broken) | **Never** | N/A |
| Streaming large files | No | No | Yes (chunked artifacts) |
| File metadata | No standard | In registry | In `FilePart` |
| Client support needed | Major changes | Middleware only | New protocol support |

### 10. References

- [MCP EmbeddedResource spec](https://modelcontextprotocol.io/docs/concepts/resources)
- [A2A FilePart spec](https://a2a-protocol.org/latest/specification/)
- [LibreChat #8060 — Temporary file links for MCP](https://github.com/danny-avila/LibreChat/issues/8060)
- [LibreChat #10742 — File paths for MCP tools](https://github.com/danny-avila/LibreChat/discussions/10742)
- [Claude Desktop EmbeddedResource bug](https://github.com/modelcontextprotocol/csharp-sdk/issues/1261)
- [Goose #2917 — EmbeddedResource download](https://github.com/block/goose/issues/2917)
- [Claude Code #9152 — MCP image token limit](https://github.com/anthropics/claude-code/issues/9152)
- [MCP Apps extension (Jan 2026)](https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/)
