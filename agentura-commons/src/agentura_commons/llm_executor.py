"""Multi-step LLM tool-calling loop (agentic execution).

The universal agent loop used by:
- Service agents receiving A2A delegation
- Chat LLM / orchestrator processing user requests
- Mailgent processing incoming emails

Each instantiation differs only in the tools list and system_prompt.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .base import ToolDef

logger = logging.getLogger(__name__)

# Synthetic tool names (intercepted by the executor, never reach real tools)
TOOL_REQUEST_INPUT = "_request_input"
TOOL_RETURN_RESULT = "_return_result"
TOOL_REPORT_PROGRESS = "_report_progress"
TOOL_REJECT_TASK = "_reject_task"
TOOL_REQUEST_AUTH = "_request_auth"

_SYNTHETIC_TOOLS = {
    TOOL_REQUEST_INPUT,
    TOOL_RETURN_RESULT,
    TOOL_REPORT_PROGRESS,
    TOOL_REJECT_TASK,
    TOOL_REQUEST_AUTH,
}


@dataclass
class ExecutorResult:
    """Result from an LLMExecutor run."""

    text: str = ""
    files: list[dict] = field(default_factory=list)
    status: str = "completed"  # completed, input_required, rejected, auth_required, failed
    question: str = ""  # if input_required, the question to ask
    auth_scheme: str = ""  # if auth_required, the scheme needed
    history: list[dict] = field(default_factory=list)  # for continuation
    progress_updates: list[str] = field(default_factory=list)


@dataclass
class _ToolCall:
    """Parsed tool call from LLM response."""

    id: str
    name: str
    arguments: dict[str, Any]


def _detect_provider(endpoint: str) -> str:
    e = endpoint.lower()
    if "services.ai.azure.com" in e and "/anthropic" in e:
        return "azure_anthropic"
    if "api.anthropic.com" in e:
        return "anthropic"
    return "openai"


def _tool_schema(t: ToolDef) -> dict[str, Any]:
    """Convert ToolDef to Anthropic tool schema."""
    if t.parameters:
        params = t.parameters
    else:
        sig = inspect.signature(t.fn)
        props = {}
        required = []
        for name, p in sig.parameters.items():
            if name == "self":
                continue
            prop: dict[str, Any] = {"type": "string"}
            ann = p.annotation
            if ann is int:
                prop["type"] = "integer"
            elif ann is float:
                prop["type"] = "number"
            elif ann is bool:
                prop["type"] = "boolean"
            elif ann is list or (hasattr(ann, "__origin__") and ann.__origin__ is list):
                prop["type"] = "array"
                prop["items"] = {"type": "string"}
            props[name] = prop
            if p.default is inspect.Parameter.empty:
                required.append(name)
        params = {
            "type": "object",
            "properties": props,
            "required": required,
        }
    return {
        "name": t.name,
        "description": t.description or "",
        "input_schema": params,
    }


def _tool_schema_openai(t: ToolDef) -> dict[str, Any]:
    schema = _tool_schema(t)
    return {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema["description"],
            "parameters": schema["input_schema"],
        },
    }


def _synthetic_tool_schemas_anthropic() -> list[dict]:
    """Anthropic-format schemas for synthetic tools."""
    return [
        {
            "name": TOOL_REQUEST_INPUT,
            "description": (
                "Ask the requester for clarification or additional information. "
                "Use when you cannot complete the task without more input."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Suggested choices (optional)",
                    },
                },
                "required": ["question"],
            },
        },
        {
            "name": TOOL_RETURN_RESULT,
            "description": (
                "Return the final result to the requester. "
                "Optionally specify which files to include. "
                "If not called, your last text response is used."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Final answer text",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filenames to include (default: all produced files)",
                    },
                },
                "required": ["message"],
            },
        },
        {
            "name": TOOL_REPORT_PROGRESS,
            "description": (
                "Report progress to the requester during long operations. "
                "Use between tool calls to keep the requester informed."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Progress update text",
                    },
                },
                "required": ["message"],
            },
        },
        {
            "name": TOOL_REJECT_TASK,
            "description": (
                "Reject the task if it is outside your scope or invalid. "
                "Use when you cannot and should not attempt the task."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the task is rejected",
                    },
                },
                "required": ["reason"],
            },
        },
        {
            "name": TOOL_REQUEST_AUTH,
            "description": ("Request authentication from the requester. Use when credentials are needed to proceed."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "scheme": {
                        "type": "string",
                        "description": "Auth scheme needed (e.g., 'bearer', 'api_key')",
                    },
                    "message": {
                        "type": "string",
                        "description": "What credentials are needed and why",
                    },
                },
                "required": ["scheme", "message"],
            },
        },
    ]


def _synthetic_tool_schemas_openai() -> list[dict]:
    """OpenAI-format schemas for synthetic tools."""
    anthropic = _synthetic_tool_schemas_anthropic()
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in anthropic
    ]


class LLMExecutor:
    """Multi-step LLM tool-calling loop.

    Supports Anthropic (native + Azure AI Foundry) and OpenAI-compatible APIs.
    Injects 5 synthetic tools for task lifecycle control.
    """

    def __init__(
        self,
        tools: list[ToolDef],
        model: str,
        api_key: str,
        api_base: str,
        system_prompt: str = "",
        max_steps: int = 10,
        on_progress: Any | None = None,
    ):
        self.tools = tools
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.system_prompt = system_prompt or self._default_system()
        self.max_steps = max_steps
        self.on_progress = on_progress  # async callback(message: str)
        self._provider = _detect_provider(api_base)
        self._tool_map = {t.name: t for t in tools}
        self._produced_files: list[dict] = []

    def _default_system(self) -> str:
        return (
            "You are an AI agent. You MUST use the provided tools to "
            "complete tasks. Do NOT describe what you would do - execute "
            "it by calling the appropriate tool. After getting a tool "
            "result, either call another tool or provide your final "
            "answer as plain text. Use _request_input if you need "
            "clarification. Use _reject_task if the task is outside "
            "your capabilities. Use _return_result to provide your "
            "final answer with specific files."
        )

    async def run(
        self,
        message: str,
        files: list[dict] | None = None,
        history: list[dict] | None = None,
    ) -> ExecutorResult:
        """Run the agentic loop.

        Args:
            message: User/requester message text.
            files: FileAttachment dicts [{name, content}] for inline files.
            history: Prior conversation messages for continuation.

        Returns:
            ExecutorResult with text, files, status, and history.
        """
        self._produced_files = []
        progress_updates: list[str] = []

        # Build conversation
        messages = list(history or [])
        user_content = message
        if files:
            file_names = [f.get("name", "file") for f in files]
            user_content += f"\n\n[Attached files: {', '.join(file_names)}]"
        messages.append({"role": "user", "content": user_content})

        for step in range(self.max_steps):
            try:
                force_tool = step == 0 and not history
                response = await self._call_llm(
                    messages,
                    force_tool=force_tool,
                )
            except Exception as e:
                logger.exception("LLM call failed at step %d", step)
                return ExecutorResult(
                    text=f"LLM error: {e}",
                    status="failed",
                    history=messages,
                )

            # Parse response
            text_parts, tool_calls = self._parse_response(response)

            if not tool_calls:
                # LLM produced text only - done
                final_text = "\n".join(text_parts)
                messages.append({"role": "assistant", "content": final_text})
                return ExecutorResult(
                    text=final_text,
                    files=self._produced_files,
                    status="completed",
                    progress_updates=progress_updates,
                    history=messages,
                )

            # Append assistant message with tool calls
            messages.append(self._assistant_message(text_parts, tool_calls))

            # Execute each tool call
            for tc in tool_calls:
                if tc.name in _SYNTHETIC_TOOLS:
                    result = self._handle_synthetic(
                        tc,
                        messages,
                        progress_updates,
                    )
                    if result is not None:
                        result.progress_updates = progress_updates
                        return result
                    # report_progress returns None - continue
                    messages.append(self._tool_result_message(tc.id, "Progress noted."))
                else:
                    # Execute real tool
                    tool_result = await self._execute_tool(
                        tc.name,
                        tc.arguments,
                        files,
                    )
                    messages.append(self._tool_result_message(tc.id, tool_result))

        # Max steps reached
        final_text = "Task partially completed (max steps reached)."
        return ExecutorResult(
            text=final_text,
            files=self._produced_files,
            status="completed",
            history=messages,
            progress_updates=progress_updates,
        )

    def _handle_synthetic(
        self,
        tc: _ToolCall,
        messages: list[dict],
        progress_updates: list[str],
    ) -> ExecutorResult | None:
        """Handle a synthetic tool call. Returns result or None to continue."""
        args = tc.arguments

        if tc.name == TOOL_RETURN_RESULT:
            text = args.get("message", "")
            requested_files = args.get("files")
            if requested_files:
                files = [f for f in self._produced_files if f.get("filename") in requested_files]
            else:
                files = self._produced_files
            return ExecutorResult(
                text=text,
                files=files,
                status="completed",
                history=messages,
            )

        if tc.name == TOOL_REQUEST_INPUT:
            question = args.get("question", "")
            # Append tool_result so history is valid for continuation
            messages.append(self._tool_result_message(tc.id, "Waiting for input."))
            return ExecutorResult(
                text=question,
                files=self._produced_files,
                status="input_required",
                question=question,
                history=messages,
            )

        if tc.name == TOOL_REJECT_TASK:
            reason = args.get("reason", "Task rejected")
            messages.append(self._tool_result_message(tc.id, "Task rejected."))
            return ExecutorResult(
                text=reason,
                status="rejected",
                history=messages,
            )

        if tc.name == TOOL_REQUEST_AUTH:
            messages.append(self._tool_result_message(tc.id, "Waiting for auth."))
            return ExecutorResult(
                text=args.get("message", "Authentication required"),
                status="auth_required",
                auth_scheme=args.get("scheme", ""),
                history=messages,
            )

        if tc.name == TOOL_REPORT_PROGRESS:
            msg = args.get("message", "")
            progress_updates.append(msg)
            if self.on_progress:
                import asyncio

                if asyncio.iscoroutinefunction(self.on_progress):
                    asyncio.ensure_future(self.on_progress(msg))
                else:
                    self.on_progress(msg)
            return None  # continue loop

        return None

    async def _execute_tool(
        self,
        name: str,
        arguments: dict,
        files: list[dict] | None,
    ) -> str:
        """Execute a real tool and return result as string."""
        td = self._tool_map.get(name)
        if not td:
            return f"Error: unknown tool '{name}'"

        # Inject file content into file params.
        # Check both input files and previously produced files.
        if td.file_params:
            by_name: dict[str, str] = {}
            for f in files or []:
                if f.get("name") and f.get("content"):
                    by_name[f["name"]] = f["content"]
            # Also make produced files available by download_url
            for pf in self._produced_files:
                fn = pf.get("filename", "")
                url = pf.get("download_url", "")
                if fn and url:
                    by_name[fn] = url
            for param in td.file_params:
                val = arguments.get(param)
                if isinstance(val, str):
                    if val in by_name:
                        fa = {"name": val, "content": by_name[val]}
                        # Check if the tool expects a list
                        sig = inspect.signature(td.fn)
                        p = sig.parameters.get(param)
                        if p and "list" in str(p.annotation).lower():
                            arguments[param] = [fa]
                        else:
                            arguments[param] = fa
                    else:
                        logger.warning(
                            "File param %s=%r not in files: %s",
                            param,
                            val,
                            list(by_name.keys()),
                        )

        try:
            result = await td.fn(**arguments)
        except Exception as e:
            logger.warning("Tool %s failed: %s", name, e)
            return f"Error executing {name}: {e}"

        # Track file outputs
        result_str = str(result) if result is not None else ""
        try:
            data = json.loads(result_str)
            if isinstance(data, dict) and "download_url" in data:
                self._produced_files.append(data)
        except (json.JSONDecodeError, TypeError):
            pass

        return result_str

    # LLM API calls

    async def _call_llm(
        self,
        messages: list[dict],
        *,
        force_tool: bool = False,
    ) -> dict:
        if self._provider in ("azure_anthropic", "anthropic"):
            return await self._call_anthropic(
                messages,
                force_tool=force_tool,
            )
        return await self._call_openai(
            messages,
            force_tool=force_tool,
        )

    async def _call_anthropic(
        self,
        messages: list[dict],
        *,
        force_tool: bool = False,
    ) -> dict:
        url = f"{self.api_base.rstrip('/')}/v1/messages"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self._provider == "azure_anthropic":
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            headers["x-api-key"] = self.api_key

        # Build tool list: real + synthetic
        tools = [_tool_schema(t) for t in self.tools]
        tools.extend(_synthetic_tool_schemas_anthropic())

        # Separate system from messages
        api_messages = [m for m in messages if m.get("role") != "system"]

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "system": self.system_prompt,
            "tools": tools,
            "messages": api_messages,
        }
        if force_tool:
            payload["tool_choice"] = {"type": "any"}

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=300.0,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"Anthropic API error {resp.status_code}: {resp.text[:300]}")
            return resp.json()

    async def _call_openai(
        self,
        messages: list[dict],
        *,
        force_tool: bool = False,
    ) -> dict:
        url = f"{self.api_base.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        tools = [_tool_schema_openai(t) for t in self.tools]
        tools.extend(_synthetic_tool_schemas_openai())

        api_messages = [
            {"role": "system", "content": self.system_prompt},
        ]
        api_messages.extend(messages)

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "tools": tools,
            "messages": api_messages,
        }
        if force_tool:
            payload["tool_choice"] = "required"

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=300.0,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text[:300]}")
            return resp.json()

    # Response parsing

    def _parse_response(
        self,
        response: dict,
    ) -> tuple[list[str], list[_ToolCall]]:
        """Parse LLM response into text parts and tool calls."""
        if self._provider in ("azure_anthropic", "anthropic"):
            return self._parse_anthropic(response)
        return self._parse_openai(response)

    def _parse_anthropic(
        self,
        response: dict,
    ) -> tuple[list[str], list[_ToolCall]]:
        texts = []
        calls = []
        for block in response.get("content", []):
            if block.get("type") == "text":
                texts.append(block["text"])
            elif block.get("type") == "tool_use":
                calls.append(
                    _ToolCall(
                        id=block["id"],
                        name=block["name"],
                        arguments=block.get("input", {}),
                    )
                )
        return texts, calls

    def _parse_openai(
        self,
        response: dict,
    ) -> tuple[list[str], list[_ToolCall]]:
        texts = []
        calls = []
        choices = response.get("choices", [])
        if not choices:
            return texts, calls
        msg = choices[0].get("message", {})
        if msg.get("content"):
            texts.append(msg["content"])
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            calls.append(
                _ToolCall(
                    id=tc.get("id", ""),
                    name=fn.get("name", ""),
                    arguments=args,
                )
            )
        return texts, calls

    # Message construction

    def _assistant_message(
        self,
        text_parts: list[str],
        tool_calls: list[_ToolCall],
    ) -> dict:
        """Build assistant message with tool calls."""
        if self._provider in ("azure_anthropic", "anthropic"):
            content = []
            for t in text_parts:
                content.append({"type": "text", "text": t})
            for tc in tool_calls:
                content.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    }
                )
            return {"role": "assistant", "content": content}
        # OpenAI format
        msg: dict[str, Any] = {"role": "assistant"}
        if text_parts:
            msg["content"] = "\n".join(text_parts)
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in tool_calls
            ]
        return msg

    def _tool_result_message(self, tool_call_id: str, result: str) -> dict:
        """Build tool result message."""
        if self._provider in ("azure_anthropic", "anthropic"):
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": result,
                    }
                ],
            }
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
        }
