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
    AgentTool,
    BaseAgentService,
    SkillDef,
)
from pydantic import BaseModel, Field


class EchoInput(BaseModel):
    text: str = Field(default="", description="Text to echo")


class EchoTool(AgentTool):
    name: str = "echo"
    description: str = "Echo input"
    args_schema: type[BaseModel] = EchoInput

    async def _arun(self, **kwargs) -> str:
        return f"echo: {kwargs.get('text', '')}"


class _MockService(BaseAgentService):
    """Minimal mock service for testing."""

    @property
    def agent_name(self) -> str:
        return "Test Agent"

    @property
    def agent_description(self) -> str:
        return "A test agent"

    def get_tools(self) -> list:
        return [EchoTool().bind_service(self)]

    def get_skills(self) -> list[SkillDef]:
        return [
            SkillDef(
                id="test-skill",
                name="Test Skill",
                description="A test skill",
                tags=["test"],
            ),
        ]

    async def execute_skill(
        self,
        skill_id: str,
        message: str,
        *,
        task_id: str | None = None,
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
        parts=[
            Part(
                raw=b"PDF content",
                filename="doc.pdf",
                media_type="application/pdf",
            )
        ],
    )
    files = _extract_files(msg)
    assert len(files) == 1
    assert files[0]["name"] == "doc.pdf"
    assert files[0]["content"].startswith("data:application/pdf;base64,")


def test_extract_files_from_url():
    msg = Message(
        role=Role.ROLE_USER,
        parts=[
            Part(
                url="http://example.com/doc.pdf",
                filename="doc.pdf",
            )
        ],
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
    assert len(card.supported_interfaces) == 2
    bindings = {i.protocol_binding: i.url for i in card.supported_interfaces}
    assert "HTTP+JSON" in bindings
    assert "JSONRPC" in bindings
    assert "8001/a2a" in bindings["HTTP+JSON"]
    assert "8001/a2a/rpc" in bindings["JSONRPC"]


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
async def test_executor_with_file_output(service, tmp_path):
    """Tool returning NamedFile emits artifact events."""
    service.output_dir = tmp_path
    service.base_url = "http://test"

    async def _file_tool():
        from agentura_commons.base import NamedFile

        tmp = service.output_dir / "f.pdf"
        tmp.write_bytes(b"PDF-CONTENT")
        return NamedFile(path=tmp, name="f.pdf")

    class _FT(AgentTool):
        name: str = "file_tool"
        description: str = "Returns a file"

        async def _arun(self, **kw):
            return await _file_tool()

    service.get_tools = lambda: [_FT()]
    executor = _AgentExecutor(service)

    from google.protobuf.struct_pb2 import Value

    data = Value()
    data.struct_value.fields["tool"].string_value = "file_tool"
    ctx = _make_context_with_data(data)
    queue = EventQueue()

    await executor.execute(ctx, queue)
    events = await _drain_events(queue)

    artifacts = [e for e in events if isinstance(e, TaskArtifactUpdateEvent)]
    assert len(artifacts) == 1
    assert artifacts[0].artifact.name == "f.pdf"
    assert "f.pdf" in artifacts[0].artifact.parts[0].url


@pytest.mark.asyncio
async def test_executor_with_multiple_file_outputs(service, tmp_path):
    """Tool returning ToolResult with multiple files emits all artifacts.

    Regression test for read_email with PDF + XML attachments where
    only one artifact was emitted.
    """
    from agentura_commons.base import NamedFile, ToolResult

    service.output_dir = tmp_path
    service.base_url = "http://test"

    async def _multi_file_tool():
        pdf = tmp_path / "invoice.pdf"
        pdf.write_bytes(b"PDF-CONTENT")
        xml = tmp_path / "e-invoice.xml"
        xml.write_bytes(b"<xml>INVOICE</xml>")
        return ToolResult(
            data={
                "subject": "Invoice",
                "attachments": [
                    {"file": "invoice.pdf"},
                    {"file": "e-invoice.xml"},
                ],
            },
            files=[
                NamedFile(path=pdf, name="invoice.pdf"),
                NamedFile(path=xml, name="e-invoice.xml"),
            ],
        )

    class _MFT(AgentTool):
        name: str = "read_email"
        description: str = "Returns multiple files"

        async def _arun(self, **kw):
            return await _multi_file_tool()

    service.get_tools = lambda: [_MFT()]
    executor = _AgentExecutor(service)

    from google.protobuf.struct_pb2 import Value

    data = Value()
    data.struct_value.fields["tool"].string_value = "read_email"
    ctx = _make_context_with_data(data)
    queue = EventQueue()

    await executor.execute(ctx, queue)
    events = await _drain_events(queue)

    artifacts = [e for e in events if isinstance(e, TaskArtifactUpdateEvent)]
    assert len(artifacts) == 2, f"Expected 2 artifacts, got {len(artifacts)}: {[a.artifact.name for a in artifacts]}"
    names = {a.artifact.name for a in artifacts}
    assert "invoice.pdf" in names
    assert "e-invoice.xml" in names
    # All URLs should be absolute
    for a in artifacts:
        url = a.artifact.parts[0].url
        assert url.startswith("http://"), f"Relative URL: {url}"


@pytest.mark.asyncio
async def test_llm_executor_multi_file_via_nl(service, tmp_path):
    """Multiple files from LLM executor path all appear as artifacts.

    Same as above but via NL -> LLM executor -> tool, not explicit DataPart.
    """
    from unittest.mock import AsyncMock, patch

    from agentura_commons.llm_executor import ExecutorResult

    service.output_dir = tmp_path
    service.base_url = "http://localhost:8001"
    type(service).router_llm_model = property(lambda s: "mock")

    class _FT(AgentTool):
        name: str = "read_email"
        description: str = "Read email"

        async def _arun(self, **kw):
            return "ok"

    service.get_tools = lambda: [_FT()]
    executor = _AgentExecutor(service)

    mock_result = ExecutorResult(
        text="Email with 2 attachments.",
        files=[
            {
                "filename": "invoice.pdf",
                "download_url": "files/abc_invoice.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 5000,
            },
            {
                "filename": "e-invoice.xml",
                "download_url": "files/def_e-invoice.xml",
                "mime_type": "application/xml",
                "size_bytes": 1200,
            },
        ],
        status="completed",
    )
    with patch.object(
        executor,
        "_run_llm_executor",
        new=AsyncMock(return_value=mock_result),
    ):
        ctx = _make_context("read the latest invoice email with attachments")
        queue = EventQueue()
        await executor.execute(ctx, queue)
        events = await _drain_events(queue)

    artifacts = [e for e in events if isinstance(e, TaskArtifactUpdateEvent)]
    assert len(artifacts) == 2, f"Expected 2 artifacts, got {len(artifacts)}: {[a.artifact.name for a in artifacts]}"
    names = {a.artifact.name for a in artifacts}
    assert "invoice.pdf" in names
    assert "e-invoice.xml" in names
    for a in artifacts:
        assert a.artifact.parts[0].url.startswith("http://")


@pytest.mark.asyncio
async def test_llm_executor_file_artifact_has_absolute_url(service, tmp_path):
    """Files from LLM executor path have absolute URLs in A2A artifacts.

    The LLM executor builds relative URLs (files/xxx.png). The A2A server
    must resolve them to absolute URLs before emitting artifacts.
    Regression test for: httpx.UnsupportedProtocol on relative URL.
    """
    from unittest.mock import AsyncMock, patch

    from agentura_commons.llm_executor import ExecutorResult

    service.output_dir = tmp_path
    service.base_url = "http://localhost:8002"
    # Enable NL routing so _run_llm_executor is called
    type(service).router_llm_model = property(lambda s: "mock-model")

    class _FT(AgentTool):
        name: str = "gen_image"
        description: str = "Generate image"

        async def _arun(self, **kw):
            return "ok"

    service.get_tools = lambda: [_FT()]
    executor = _AgentExecutor(service)

    # Mock _run_llm_executor to return result with relative file URL
    # (this is what the real executor produces via _track_file)
    mock_result = ExecutorResult(
        text="Image generated.",
        files=[
            {
                "filename": "fox.png",
                "download_url": "files/abc_fox.png",
                "mime_type": "image/png",
                "size_bytes": 1234,
            }
        ],
        status="completed",
    )
    with patch.object(
        executor,
        "_run_llm_executor",
        new=AsyncMock(return_value=mock_result),
    ):
        # Send NL message (no DataPart -> goes to LLM executor)
        ctx = _make_context("generate a fox image")
        queue = EventQueue()
        await executor.execute(ctx, queue)
        events = await _drain_events(queue)

    artifacts = [e for e in events if isinstance(e, TaskArtifactUpdateEvent)]
    assert len(artifacts) == 1
    url = artifacts[0].artifact.parts[0].url
    assert url.startswith("http://"), f"Expected absolute URL, got: {url}"
    assert "fox.png" in url


@pytest.mark.asyncio
async def test_executor_error_handling(service):
    """Failing tool emits FAILED status."""

    async def _failing_tool() -> str:
        raise RuntimeError("boom")

    class _FailT(AgentTool):
        name: str = "fail"
        description: str = "Always fails"

        async def _arun(self, **kw):
            return await _failing_tool()

    service.get_tools = lambda: [_FailT()]
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


# File injection in _call_tool


@pytest.mark.asyncio
async def test_call_tool_injects_file_into_param(service):
    """When files are provided, matching file_params get
    resolved to FileAttachment dicts."""
    received_args = {}

    async def _capture(**kwargs):
        received_args.update(kwargs)
        return "ok"

    class _DigestT(AgentTool):
        name: str = "digest"
        description: str = "Digest a doc"
        file_params: list[str] | None = ["source"]

        async def _arun(self_inner, **kwargs):
            received_args.update(kwargs)
            return "ok"

    service.get_tools = lambda: [_DigestT()]
    executor = _AgentExecutor(service)

    files = [{"name": "doc.pdf", "content": "data:app/pdf;base64,abc"}]
    text, file_list = await executor._call_tool(
        "digest",
        {"source": "doc.pdf"},
        files=files,
    )
    assert text == "ok"
    # source should be replaced with FileAttachment dict
    src = received_args["source"]
    assert isinstance(src, dict), f"Expected dict, got {type(src)}"
    assert src["name"] == "doc.pdf"
    assert src["content"] == "data:app/pdf;base64,abc"


@pytest.mark.asyncio
async def test_call_tool_no_match_passes_through(service):
    """File param value that doesn't match any file is unchanged."""
    received_args = {}

    class _DigestT(AgentTool):
        name: str = "digest"
        description: str = "d"
        file_params: list[str] | None = ["source"]

        async def _arun(self_inner, **kwargs):
            received_args.update(kwargs)
            return "ok"

    service.get_tools = lambda: [_DigestT()]
    executor = _AgentExecutor(service)

    files = [{"name": "other.pdf", "content": "data:app/pdf;base64,xyz"}]
    await executor._call_tool(
        "digest",
        {"source": "doc.pdf"},
        files=files,
    )
    # No match - source stays as plain string
    assert received_args["source"] == "doc.pdf"


@pytest.mark.asyncio
async def test_call_tool_no_files_no_change(service):
    """Without files, file_params are not modified."""
    received_args = {}

    class _DigestT(AgentTool):
        name: str = "digest"
        description: str = "d"
        file_params: list[str] | None = ["source"]

        async def _arun(self_inner, **kwargs):
            received_args.update(kwargs)
            return "ok"

    service.get_tools = lambda: [_DigestT()]
    executor = _AgentExecutor(service)

    await executor._call_tool(
        "digest",
        {"source": "doc.pdf"},
        files=None,
    )
    assert received_args["source"] == "doc.pdf"
