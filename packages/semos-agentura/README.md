# semos-agentura

Umbrella package for the **Semos Agentura** multi-agent suite. Installing it pulls in the full set of
agents and the shared framework in one step:

```bash
uv add semos-agentura          # or: pip install semos-agentura
```

This installs:

| Package | Import | Role |
| ------- | ------ | ---- |
| `semos-agentura-core` | `semos.agentura.core` | Shared MCP + A2A base classes |
| `semos-agentura-email` | `semos.agentura.email` | Email client + `@mailgent` LLM agent |
| `semos-agentura-document` | `semos.agentura.document` | Document digestion / composition / diagrams |
| `semos-agentura-files` | `semos.agentura.files` | Virtual filesystem (local, WebDAV, archives) |
| `semos-agentura-ui` | `semos.agentura.ui` | Panel-based chat UI |

Each package is also installable on its own (for example `semos-agentura-email` for just the email
agent, which pulls in `semos-agentura-core`). The umbrella is a convenience meta-package and ships no
code of its own.
