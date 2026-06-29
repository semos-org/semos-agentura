# Semos Agentura

A modular multi-agent system for professional and scientific workflows, built on open
protocols: [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) and
[A2A](https://a2a-protocol.org/) (Agent-to-Agent Protocol).

## Principles

- **Sovereign architecture.** Every agent is an independent service with its own deployment,
  lifecycle, and data. Agents communicate only through standardized protocols, never through shared
  memory or framework internals.
- **Federated by design.** Agents are discovered via A2A Agent Cards and connected via protocol, not
  configuration. A new agent joins by publishing its capabilities.
- **Modular composition.** Each agent encapsulates a single domain and exposes it as MCP tools and A2A
  skills. The shared `agentura-commons` library provides only protocol wiring, no business logic.
- **Open standards, no lock-in.** MCP and A2A are Linux Foundation standards; LLM providers are
  interchangeable via `litellm`; agents run as standard HTTP services.

## Documentation

- [Agent Architecture](agent-architecture.md): the patterns implemented across every agent.
- [File Handling Spec](file-handling-spec.md): how binary files move through MCP/A2A tools.
- Capabilities:
    - [Assistant capabilities](capabilities/assistant_capabilities_usecase.md)
    - [Image generation and diagram embeds](capabilities/image_generation_and_diagram_embeds.md)
