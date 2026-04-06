"""Tests for format_for_llm() on ClientToolResult and ClientA2AResult."""

from agentura_commons.client import ClientA2AResult, ClientToolResult
from agentura_commons.file_middleware import FileEntry


def _entry(name: str) -> FileEntry:
    return FileEntry(
        filename=name,
        blob=b"x",
        mime="application/pdf",
        size=1,
        source="test",
    )


class TestClientToolResultFormat:
    def test_text_only(self):
        r = ClientToolResult(text="50 emails found")
        assert r.format_for_llm() == "50 emails found"

    def test_error(self):
        r = ClientToolResult(text="tool crashed", is_error=True)
        assert r.format_for_llm() == "[Error] tool crashed"

    def test_with_file(self):
        r = ClientToolResult(text="Composed.", files=[_entry("report.docx")])
        out = r.format_for_llm()
        assert "Composed." in out
        assert "[Produced: report.docx]" in out

    def test_multiple_files(self):
        r = ClientToolResult(
            text="Done",
            files=[_entry("a.pdf"), _entry("b.png")],
        )
        assert "a.pdf, b.png" in r.format_for_llm()

    def test_empty(self):
        r = ClientToolResult(text="")
        assert r.format_for_llm() == "Done."


class TestClientA2AResultFormat:
    def test_completed_text(self):
        r = ClientA2AResult(text="Summary: batteries are good.")
        assert r.format_for_llm() == "Summary: batteries are good."

    def test_completed_with_agent_name(self):
        r = ClientA2AResult(
            text="Summary ready.",
            agent_name="document",
        )
        assert r.format_for_llm() == "[document] Summary ready."

    def test_completed_with_file(self):
        r = ClientA2AResult(
            text="Report ready.",
            agent_name="document",
            files=[_entry("report.html")],
        )
        out = r.format_for_llm()
        assert "[document] Report ready." in out
        assert "[Produced: report.html]" in out

    def test_input_required(self):
        r = ClientA2AResult(
            text="Should it be PDF or PPTX?",
            agent_name="document",
            status="input_required",
            task_id="task-1",
            context_id="ctx-1",
        )
        out = r.format_for_llm()
        assert "[document] [NEEDS INPUT]" in out
        assert "Agent asks: Should it be PDF or PPTX?" in out
        assert 'context_id="ctx-1"' in out

    def test_rejected(self):
        r = ClientA2AResult(
            text="I only handle documents",
            agent_name="email",
            status="rejected",
        )
        out = r.format_for_llm()
        assert "[email] [Rejected]" in out
        assert "I only handle documents" in out

    def test_auth_required(self):
        r = ClientA2AResult(
            text="Need SAP credentials",
            agent_name="sap",
            status="auth_required",
        )
        assert "[sap] [Auth required]" in r.format_for_llm()

    def test_failed(self):
        r = ClientA2AResult(
            text="Agent crashed",
            agent_name="document",
            status="failed",
        )
        assert "[document] [Failed]" in r.format_for_llm()

    def test_empty_completed(self):
        r = ClientA2AResult(text="", agent_name="document")
        assert r.format_for_llm() == "[document] Done."

    def test_no_agent_name(self):
        r = ClientA2AResult(text="Result without agent name")
        assert r.format_for_llm() == "Result without agent name"
