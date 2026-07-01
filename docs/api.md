# API Reference

Auto-generated from source docstrings via [mkdocstrings](https://mkdocstrings.github.io/). The shared
framework lives in `semos-agentura-core`; each agent is a thin `BaseAgentService` subclass.

## Core framework (semos-agentura-core)

### BaseAgentService

Abstract base every agent implements. Declares the tools and skills an agent exposes and serves them
over both MCP and A2A.

::: semos.agentura.core.BaseAgentService

---

### LLMExecutor

The universal agentic loop: takes a message plus tool definitions, drives the model/tool cycle, and
emits an `ExecutorResult`.

::: semos.agentura.core.LLMExecutor

---

### Tools

`AgentTool` / the `agent_tool` decorator define callable tools; `ToolResult` is the normalized,
multi-modal return type (text + data + files).

::: semos.agentura.core.AgentTool

::: semos.agentura.core.agent_tool

::: semos.agentura.core.ToolResult

---

### SkillDef

A2A skill definition advertised on the agent card.

::: semos.agentura.core.SkillDef

---

### AgenturaClient

Reference client for both MCP tools and A2A task delegation across agents.

::: semos.agentura.core.AgenturaClient

---

### MCPHub

Connects to external MCP servers and exposes their tools to an agent.

::: semos.agentura.core.MCPHub

---

### create_app

Wires a `BaseAgentService` into a FastAPI app serving MCP and A2A endpoints.

::: semos.agentura.core.create_app

## Agents

### DocumentAgentService

Document digestion (OCR to Markdown) and composition (Markdown to documents).

::: semos.agentura.document.service.DocumentAgentService

---

### EmailAgentService

Unified email client over IMAP/SMTP and Outlook COM, with the `@mailgent` LLM agent.

::: semos.agentura.email.service.EmailAgentService

---

### FilesystemAgentService

Multi-root virtual filesystem (local, WebDAV, SharePoint, archives) exposed as MCP/A2A tools.

::: semos.agentura.files.service.FilesystemAgentService
