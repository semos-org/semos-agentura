# API Reference

Auto-generated from source docstrings via [mkdocstrings](https://mkdocstrings.github.io/). The shared
framework lives in `agentura-commons`; each agent is a thin `BaseAgentService` subclass.

## Core framework (agentura-commons)

### BaseAgentService

Abstract base every agent implements. Declares the tools and skills an agent exposes and serves them
over both MCP and A2A.

::: agentura_commons.BaseAgentService

---

### LLMExecutor

The universal agentic loop: takes a message plus tool definitions, drives the model/tool cycle, and
emits an `ExecutorResult`.

::: agentura_commons.LLMExecutor

---

### Tools

`AgentTool` / the `agent_tool` decorator define callable tools; `ToolResult` is the normalized,
multi-modal return type (text + data + files).

::: agentura_commons.AgentTool

::: agentura_commons.agent_tool

::: agentura_commons.ToolResult

---

### SkillDef

A2A skill definition advertised on the agent card.

::: agentura_commons.SkillDef

---

### AgenturaClient

Reference client for both MCP tools and A2A task delegation across agents.

::: agentura_commons.AgenturaClient

---

### MCPHub

Connects to external MCP servers and exposes their tools to an agent.

::: agentura_commons.MCPHub

---

### create_app

Wires a `BaseAgentService` into a FastAPI app serving MCP and A2A endpoints.

::: agentura_commons.create_app

## Agents

### DocumentAgentService

Document digestion (OCR to Markdown) and composition (Markdown to documents).

::: document_agent.service.DocumentAgentService

---

### EmailAgentService

Unified email client over IMAP/SMTP and Outlook COM, with the `@mailgent` LLM agent.

::: email_agent.service.EmailAgentService

---

### FilesystemAgentService

Multi-root virtual filesystem (local, WebDAV, SharePoint, archives) exposed as MCP/A2A tools.

::: filesystem_agent.service.FilesystemAgentService
