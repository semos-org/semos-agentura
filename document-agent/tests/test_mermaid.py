"""Tests for composition/_mermaid.py - block finding."""

from document_agent.composition._mermaid import find_mermaid_blocks


class TestFindMermaidBlocks:
    def test_single_block(self):
        md = "# Title\n\n```mermaid\ngraph TD\n  A-->B\n```\n\nText"
        blocks = find_mermaid_blocks(md)
        assert len(blocks) == 1
        assert "graph TD" in blocks[0][2]
        assert "A-->B" in blocks[0][2]

    def test_multiple_blocks(self):
        md = "```mermaid\ngraph LR\n  A-->B\n```\n\n```mermaid\nsequenceDiagram\n  A->>B: hi\n```"
        blocks = find_mermaid_blocks(md)
        assert len(blocks) == 2
        assert "graph LR" in blocks[0][2]
        assert "sequenceDiagram" in blocks[1][2]

    def test_no_blocks(self):
        assert find_mermaid_blocks("# Just markdown") == []

    def test_other_code_blocks_ignored(self):
        md = "```python\nprint('hi')\n```\n```mermaid\npie\n```"
        blocks = find_mermaid_blocks(md)
        assert len(blocks) == 1
        assert blocks[0][2] == "pie"

    def test_block_offsets(self):
        prefix = "Some text\n\n"
        md = prefix + "```mermaid\ngraph TD\n```\n\nMore text"
        blocks = find_mermaid_blocks(md)
        assert len(blocks) == 1
        start, end, _ = blocks[0]
        assert start == len(prefix)
        assert md[start:end] == "```mermaid\ngraph TD\n```"
