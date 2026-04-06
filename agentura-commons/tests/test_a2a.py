"""Tests for the A2A layer (a2a-sdk 1.0 protobuf types)."""

from __future__ import annotations

import pytest
from a2a.server.events import EventQueue
from a2a.types import (
    Message,
    Part,
    Role,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)

from agentura_commons.a2a_server import (
    _AgentExecutor,
    _extract_files,
    _extract_text,
    _extract_tool_call,
    create_agent_card,
)
from agentura_commons.base import (
    BaseAgentService,
    SkillDef,
    ToolDef,
)


class _MockService(BaseAgentService):
    """Minimal mock service for testing."""

    @property
    def agent_name(self) -> str:
        return "Test Agent"

    @property
    def agent_description(self) -> str:
        return "A test agent"

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="echo",
                description="Echo input",
                fn=self._echo,
            ),
        ]

    def get_skills(self) -> list[SkillDef]:
        return [
            SkillDef(
                id="test-skill",
                name="Test Skill",
                description="A test skill",
                tags=["test"],
            ),
        ]

    async def _echo(self, text: str = "") -> str:
        return f"echo: {text}"

    async def execute_skill(
        self, skill_id: str, message: str,
        *, task_id: str | None = None,
    ) -> str:
        return f"skill={skill_id} msg={message}"


@pytest.fixture
def service():
    svc = _MockService()
    svc.output_dir = None
    svc.base_url = "http://localhost:9999"
    return svc


@pytest.fixture
def executor(service):
    return _AgentExecutor(service)


# -- Helper function tests --

def test_extract_text_from_parts():
    msg = Message(
        role=Role.ROLE_USER,
        parts=[Part(text="hello"), Part(text="world")],
    )
    assert _extract_text(msg) == "hello\nworld"


def test_extract_text_empty():
    assert _extract_text(None) == ""
    assert _extract_text(Message(role=Role.ROLE_USER)) == ""


def test_extract_files_from_raw():
    msg = Message(
        role=Role.ROLE_USER,
        parts=[Part(
            raw=b"PDF content",
            filename="doc.pdf",
            media_type="application/pdf",
        )],
    )
    files = _extract_files(msg)
    assert len(files) == 1
    assert files[0]["name"] == "doc.pdf"
    assert files[0]["content"].startswith("data:application/pdf;base64,")


def test_extract_files_from_url():
    msg = Message(
        role=Role.ROLE_USER,
        parts=[Part(
            url="http://example.com/doc.pdf",
            filename="doc.pdf",
        )],
    )
    files = _extract_files(msg)
    assert len(files) == 1
    assert files[0]["name"] == "doc.pdf"
    assert files[0]["content"] == "http://example.com/doc.pdf"


def test_extract_tool_call():
    from google.protobuf.struct_pb2 import Value
    data = Value()
    data.struct_value.fields["tool"].string_value = "echo"
    args = data.struct_value.fields["arguments"]
    args.struct_value.fields["text"].string_value = "hi"

    msg = Message(
        role=Role.ROLE_USER,
        parts=[Part(data=data)],
    )
    result = _extract_tool_call(msg)
    assert result is not None
    assert result[0] == "echo"
    assert result[1]["text"] == "hi"


def test_extract_tool_call_none():
    msg = Message(
        role=Role.ROLE_USER,
        parts=[Part(text="just text")],
    )
    assert _extract_tool_call(msg) is None


# -- AgentCard tests --

def test_create_agent_card(service):
    card = create_agent_card(service, "http://localhost:8001")
    assert card.name == "Test Agent"
    assert card.description == "A test agent"
    assert len(card.skills) == 1
    assert card.skills[0].id == "test-skill"
    assert len(card.supported_interfaces) == 1
    assert "8001/a2a" in card.supported_interfaces[0].url


# -- Executor tests --

@pytest.mark.asyncio
async def test_executor_text_skill(executor):
    """Natural language message routes to default skill."""
    queue = EventQueue()
    ctx = _make_context("hello from user")
    await executor.execute(ctx, queue)
    events = await _drain_events(queue)

    assert len(events) >= 2
    assert _state_of(events[0]) == TaskState.TASK_STATE_WORKING
    assert _state_of(events[-1]) == TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
async def test_executor_explicit_tool_call(executor):
    """DataPart with tool name routes to explicit tool."""
    from google.protobuf.struct_pb2 import Value
    data = Value()
    data.struct_value.fields["tool"].string_value = "echo"
    args = data.struct_value.fields["arguments"]
    args.struct_value.fields["text"].string_value = "test123"

    ctx = _make_context_with_data(data)
    queue = EventQueue()
    await executor.execute(ctx, queue)
    events = await _drain_events(queue)

    last = events[-1]
    assert _state_of(last) == TaskState.TASK_STATE_COMPLETED
    text = last.status.message.parts[0].text
    assert "echo: test123" in text


@pytest.mark.asyncio
async def test_executor_with_file_output(service):
    """Tool returning download_url JSON emits artifact events."""
    import json as _json

    async def _file_tool() -> str:
        return _json.dumps({
            "download_url": "http://test/f.pdf",
            "filename": "f.pdf",
            "mime_type": "application/pdf",
        })

    service.get_tools = lambda: [
        ToolDef(name="file_tool", description="Returns a file", fn=_file_tool),
    ]
    executor = _AgentExecutor(service)

    from google.protobuf.struct_pb2 import Value
    data = Value()
    data.struct_value.fields["tool"].string_value = "file_tool"
    ctx = _make_context_with_data(data)
    queue = EventQueue()

    await executor.execute(ctx, queue)
    events = await _drain_events(queue)

    artifacts = [
        e for e in events
        if isinstance(e, TaskArtifactUpdateEvent)
    ]
    assert len(artifacts) == 1
    assert artifacts[0].artifact.name == "f.pdf"
    assert "test/f.pdf" in artifacts[0].artifact.parts[0].url


@pytest.mark.asyncio
async def test_executor_error_handling(service):
    """Failing tool emits FAILED status."""

    async def _failing_tool() -> str:
        raise RuntimeError("boom")

    service.get_tools = lambda: [
        ToolDef(name="fail", description="Always fails", fn=_failing_tool),
    ]
    executor = _AgentExecutor(service)

    from google.protobuf.struct_pb2 import Value
    data = Value()
    data.struct_value.fields["tool"].string_value = "fail"
    ctx = _make_context_with_data(data)
    queue = EventQueue()
    await executor.execute(ctx, queue)
    events = await _drain_events(queue)

    assert _state_of(events[-1]) == TaskState.TASK_STATE_FAILED
    assert "boom" in events[-1].status.message.parts[0].text


@pytest.mark.asyncio
async def test_executor_cancel(executor):
    """Cancel emits CANCELED status."""
    ctx = _make_context("cancel me")
    queue = EventQueue()
    await executor.cancel(ctx, queue)
    events = await _drain_events(queue)

    assert _state_of(events[0]) == TaskState.TASK_STATE_CANCELED


# -- Helpers --

class _FakeContext:
    """Minimal RequestContext stand-in."""

    def __init__(self, message, task_id="t1", context_id="c1"):
        self.message = message
        self.task_id = task_id
        self.context_id = context_id
        self.current_task = None
        self.related_tasks = []
        self.configuration = None
        self.call_context = None


def _make_context(text: str) -> _FakeContext:
    return _FakeContext(
        Message(
            role=Role.ROLE_USER,
            parts=[Part(text=text)],
        ),
    )


def _make_context_with_data(data) -> _FakeContext:
    return _FakeContext(
        Message(
            role=Role.ROLE_USER,
            parts=[Part(data=data)],
        ),
    )


async def _drain_events(queue: EventQueue) -> list:
    events = []
    try:
        while True:
            ev = await queue.dequeue_event(no_wait=True)
            events.append(ev)
    except Exception:
        pass
    return events


def _state_of(event) -> int:
    if isinstance(event, TaskStatusUpdateEvent):
        return event.status.state
    return -1
