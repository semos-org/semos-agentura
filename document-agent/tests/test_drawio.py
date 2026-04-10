"""Tests for composition/_drawio.py - PNG embedding, extraction, block finding."""



from document_agent.composition._drawio import (
    _build_mxfile_for_png,
    _compress_diagram_content,
    _decompress_diagram_content,
    _embed_xml_in_png,
    _parse_mxfile_from_png,
    extract_xml_from_png,
    find_drawio_blocks,
)


class TestFindDrawioBlocks:
    def test_single_block(self):
        md = "# Title\n\n```drawio\n<mxfile>content</mxfile>\n```\n\nText"
        blocks = find_drawio_blocks(md)
        assert len(blocks) == 1
        start, end, code = blocks[0]
        assert "<mxfile>content</mxfile>" in code

    def test_multiple_blocks(self):
        md = "```drawio\nA\n```\n\n```drawio\nB\n```"
        blocks = find_drawio_blocks(md)
        assert len(blocks) == 2
        assert blocks[0][2] == "A"
        assert blocks[1][2] == "B"

    def test_no_blocks(self):
        assert find_drawio_blocks("# Just markdown") == []

    def test_other_fenced_blocks_ignored(self):
        md = "```python\nprint('hi')\n```\n```drawio\n<xml/>\n```"
        blocks = find_drawio_blocks(md)
        assert len(blocks) == 1
        assert "<xml/>" in blocks[0][2]


class TestCompressDecompress:
    def test_roundtrip(self):
        xml = "<mxGraphModel><root><mxCell id='0'/></root></mxGraphModel>"
        compressed = _compress_diagram_content(xml)
        assert isinstance(compressed, str)
        decompressed = _decompress_diagram_content(compressed)
        assert decompressed == xml

    def test_unicode_content(self):
        xml = '<mxCell value="Übersicht: Ärger"/>'
        result = _decompress_diagram_content(_compress_diagram_content(xml))
        assert result == xml


class TestBuildParseMxfile:
    def test_roundtrip(self, sample_drawio_xml):
        # Build: compress diagram content for PNG embedding
        built = _build_mxfile_for_png(sample_drawio_xml)
        assert "<diagram" in built
        # The diagram text should be compressed (no mxGraphModel children)
        assert "<mxGraphModel>" not in built

        # Parse: decompress back to inline children
        parsed = _parse_mxfile_from_png(built)
        assert "<mxGraphModel>" in parsed
        assert "Hello" in parsed


class TestPNGEmbedExtract:
    def test_embed_and_extract_roundtrip(self, sample_png, sample_drawio_xml):
        _embed_xml_in_png(sample_png, sample_drawio_xml)

        # Verify tEXt chunk was added
        data = sample_png.read_bytes()
        assert b"mxfile" in data

        # Extract and verify
        extracted = extract_xml_from_png(sample_png)
        assert extracted is not None
        assert "<mxGraphModel>" in extracted
        assert "Hello" in extracted

    def test_extract_from_plain_png(self, sample_png):
        assert extract_xml_from_png(sample_png) is None

    def test_embed_preserves_png_validity(self, sample_png, sample_drawio_xml):
        _embed_xml_in_png(sample_png, sample_drawio_xml)
        data = sample_png.read_bytes()
        # Valid PNG signature
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        # IEND at the end
        assert data[-12:-8] == b"\x00\x00\x00\x00"  # IEND length
        assert data[-8:-4] == b"IEND"
