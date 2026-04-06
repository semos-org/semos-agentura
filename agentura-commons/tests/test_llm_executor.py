"""Tests for LLMExecutor - multi-step tool-calling loop.

All tests mock the LLM API. No real API keys needed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agentura_commons.base import ToolDef
from agentura_commons.llm_executor import (
    TOOL_REJECT_TASK,
    TOOL_REPORT_PROGRESS,
    TOOL_REQUEST_AUTH,
    TOOL_REQUEST_INPUT,
    TOOL_RETURN_RESULT,
    LLMExecutor,
)


# Test tools

async def _echo(text: str = "") -> str:
    return f"echo: {text}"


async def _add(a: int = 0, b: int = 0) -> str:
    return str(int(a) + int(b))


async def _failing_tool() -> str:
    raise RuntimeError("tool crashed")


async def _file_tool() -> str:
    return json.dumps({
        "download_url": "http://test/file.pdf",
        "filename": "file.pdf",
        "mime_type": "application/pdf",
    })


TOOLS = [
    ToolDef(name="echo", description="Echo text", fn=_echo),
    ToolDef(name="add", description="Add numbers", fn=_add),
    ToolDef(name="file_tool", description="Produce file", fn=_file_tool),
]


def _make_executor(tools=None, **kw) -> LLMExecutor:
    return LLMExecutor(
        tools=tools or TOOLS,
        model="mock",
        api_key="mock-key",
        api_base="https://api.anthropic.com",
        **kw,
    )


def _anthropic_text(text: str) -> dict:
    """Mock Anthropic response with text only."""
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
    }


def _anthropic_tool_use(
    name: str,
    args: dict,
    tool_use_id: str = "tu_1",
    text: str = "",
) -> dict:
    """Mock Anthropic response with a tool call."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    content.append({
        "type": "tool_use",
        "id": tool_use_id,
        "name": name,
        "input": args,
    })
    return {"content": content, "stop_reason": "tool_use"}


# Single-step: LLM picks tool, executes, produces final answer

class TestSingleStep:
    @pytest.mark.asyncio
    async def test_tool_then_answer(self):
        """LLM picks echo tool, gets result, produces text answer."""
        responses = [
            _anthropic_tool_use("echo", {"text": "hello"}),
            _anthropic_text("The echo said: hello"),
        ]
        executor = _make_executor()
        with patch.object(
            executor, "_call_llm", new=AsyncMock(side_effect=responses),
        ):
            result = await executor.run("say hello")
        assert result.status == "completed"
        assert "echo said" in result.text.lower()
        assert len(result.history) >= 3  # user + assistant+tool + assistant

    @pytest.mark.asyncio
    async def test_text_only_response(self):
        """LLM produces text without any tool call."""
        executor = _make_executor()
        with patch.object(
            executor,
            "_call_llm",
            new=AsyncMock(return_value=_anthropic_text("Done!")),
        ):
            result = await executor.run("just respond")
        assert result.status == "completed"
        assert result.text == "Done!"


# Multi-step: LLM uses multiple tools

class TestMultiStep:
    @pytest.mark.asyncio
    async def test_two_tools_then_answer(self):
        """LLM calls add, then echo, then answers."""
        responses = [
            _anthropic_tool_use("add", {"a": 2, "b": 3}),
            _anthropic_tool_use("echo", {"text": "sum is 5"}),
            _anthropic_text("The sum is 5."),
        ]
        executor = _make_executor()
        with patch.object(
            executor, "_call_llm", new=AsyncMock(side_effect=responses),
        ):
            result = await executor.run("add 2+3 and echo it")
        assert result.status == "completed"
        assert "5" in result.text


# return_result: curate output

class TestReturnResult:
    @pytest.mark.asyncio
    async def test_curate_files(self):
        """LLM produces files, then uses return_result to select one."""
        responses = [
            _anthropic_tool_use("file_tool", {}),
            _anthropic_tool_use(
                TOOL_RETURN_RESULT,
                {"message": "Here's your file", "files": ["file.pdf"]},
            ),
        ]
        executor = _make_executor()
        with patch.object(
            executor, "_call_llm", new=AsyncMock(side_effect=responses),
        ):
            result = await executor.run("make a file")
        assert result.status == "completed"
        assert result.text == "Here's your file"
        assert len(result.files) == 1
        assert result.files[0]["filename"] == "file.pdf"

    @pytest.mark.asyncio
    async def test_return_result_no_files(self):
        """return_result with no files param returns all produced."""
        responses = [
            _anthropic_tool_use("file_tool", {}),
            _anthropic_tool_use(
                TOOL_RETURN_RESULT, {"message": "All files"},
            ),
        ]
        executor = _make_executor()
        with patch.object(
            executor, "_call_llm", new=AsyncMock(side_effect=responses),
        ):
            result = await executor.run("make a file")
        assert result.status == "completed"
        assert len(result.files) == 1  # all produced files


# request_input: INPUT_REQUIRED

class TestRequestInput:
    @pytest.mark.asyncio
    async def test_input_required(self):
        """LLM asks for clarification."""
        responses = [
            _anthropic_tool_use(
                TOOL_REQUEST_INPUT,
                {
                    "question": "PDF or DOCX?",
                    "options": ["pdf", "docx"],
                },
            ),
        ]
        executor = _make_executor()
        with patch.object(
            executor, "_call_llm", new=AsyncMock(side_effect=responses),
        ):
            result = await executor.run("compose a report")
        assert result.status == "input_required"
        assert result.question == "PDF or DOCX?"
        assert len(result.history) > 0  # history preserved

    @pytest.mark.asyncio
    async def test_continuation(self):
        """Continue after INPUT_REQUIRED with history."""
        # First run: LLM asks for input
        first_responses = [
            _anthropic_tool_use(
                TOOL_REQUEST_INPUT,
                {"question": "Which format?"},
            ),
        ]
        executor = _make_executor()
        with patch.object(
            executor,
            "_call_llm",
            new=AsyncMock(side_effect=first_responses),
        ):
            r1 = await executor.run("compose a report")
        assert r1.status == "input_required"

        # Second run: provide answer with history
        second_responses = [
            _anthropic_tool_use("echo", {"text": "composing PDF"}),
            _anthropic_text("Report composed as PDF."),
        ]
        with patch.object(
            executor,
            "_call_llm",
            new=AsyncMock(side_effect=second_responses),
        ):
            r2 = await executor.run("PDF please", history=r1.history)
        assert r2.status == "completed"
        assert "pdf" in r2.text.lower()


# reject_task: REJECTED

class TestRejectTask:
    @pytest.mark.asyncio
    async def test_reject(self):
        """LLM rejects the task."""
        responses = [
            _anthropic_tool_use(
                TOOL_REJECT_TASK,
                {"reason": "I only handle documents"},
            ),
        ]
        executor = _make_executor()
        with patch.object(
            executor, "_call_llm", new=AsyncMock(side_effect=responses),
        ):
            result = await executor.run("book a flight")
        assert result.status == "rejected"
        assert "documents" in result.text.lower()


# request_auth: AUTH_REQUIRED

class TestRequestAuth:
    @pytest.mark.asyncio
    async def test_auth_required(self):
        """LLM requests authentication."""
        responses = [
            _anthropic_tool_use(
                TOOL_REQUEST_AUTH,
                {
                    "scheme": "bearer",
                    "message": "Need API token for SAP",
                },
            ),
        ]
        executor = _make_executor()
        with patch.object(
            executor, "_call_llm", new=AsyncMock(side_effect=responses),
        ):
            result = await executor.run("check SAP errors")
        assert result.status == "auth_required"
        assert result.auth_scheme == "bearer"
        assert "SAP" in result.text


# report_progress: WORKING update

class TestReportProgress:
    @pytest.mark.asyncio
    async def test_progress_then_complete(self):
        """LLM reports progress, continues, then completes."""
        responses = [
            _anthropic_tool_use(
                TOOL_REPORT_PROGRESS,
                {"message": "Step 1/3: analyzing..."},
            ),
            _anthropic_tool_use("echo", {"text": "analyzed"}),
            _anthropic_text("Analysis complete."),
        ]
        executor = _make_executor()
        with patch.object(
            executor, "_call_llm", new=AsyncMock(side_effect=responses),
        ):
            result = await executor.run("analyze this")
        assert result.status == "completed"
        assert len(result.progress_updates) == 1
        assert "Step 1/3" in result.progress_updates[0]

    @pytest.mark.asyncio
    async def test_progress_callback(self):
        """on_progress callback is called."""
        updates = []
        responses = [
            _anthropic_tool_use(
                TOOL_REPORT_PROGRESS, {"message": "working..."},
            ),
            _anthropic_text("Done."),
        ]
        executor = _make_executor(on_progress=lambda m: updates.append(m))
        with patch.object(
            executor, "_call_llm", new=AsyncMock(side_effect=responses),
        ):
            await executor.run("do something")
        assert "working..." in updates


# Max steps

class TestMaxSteps:
    @pytest.mark.asyncio
    async def test_max_steps_reached(self):
        """Loop terminates after max_steps."""
        # LLM always calls a tool, never produces text
        responses = [
            _anthropic_tool_use("echo", {"text": str(i)})
            for i in range(20)
        ]
        executor = _make_executor(max_steps=3)
        with patch.object(
            executor, "_call_llm", new=AsyncMock(side_effect=responses),
        ):
            result = await executor.run("loop forever")
        assert result.status == "completed"
        assert "max steps" in result.text.lower()


# Error handling

class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_tool_error_reported_to_llm(self):
        """Failing tool error is fed back to LLM."""
        tools_with_fail = TOOLS + [
            ToolDef(
                name="fail", description="Always fails", fn=_failing_tool,
            ),
        ]
        responses = [
            _anthropic_tool_use("fail", {}),
            _anthropic_text("The tool failed: tool crashed"),
        ]
        executor = _make_executor(tools=tools_with_fail)
        with patch.object(
            executor, "_call_llm", new=AsyncMock(side_effect=responses),
        ):
            result = await executor.run("try the failing tool")
        assert result.status == "completed"
        assert "crashed" in result.text.lower()

    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        """Unknown tool name returns error string."""
        responses = [
            _anthropic_tool_use("nonexistent", {"x": 1}),
            _anthropic_text("That tool doesn't exist."),
        ]
        executor = _make_executor()
        with patch.object(
            executor, "_call_llm", new=AsyncMock(side_effect=responses),
        ):
            result = await executor.run("call nonexistent")
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_llm_api_failure(self):
        """LLM API error returns failed status."""
        executor = _make_executor()
        with patch.object(
            executor,
            "_call_llm",
            new=AsyncMock(side_effect=RuntimeError("API down")),
        ):
            result = await executor.run("anything")
        assert result.status == "failed"
        assert "API down" in result.text
