"""Playwright E2E tests for the Wunderbaum tree viewer app.

Wunderbaum renders inside Panel/Bokeh shadow DOM.
Playwright's ``css=`` selector prefix pierces shadow roots.
Context menu interaction is unreliable in shadow DOM (upstream known issue),
so mutation tests use Python API methods instead.

Requires: playwright browsers installed (npx playwright install).
"""

from __future__ import annotations

import time

import panel as pn
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def _port():
    """Sequential port counter to avoid collisions."""
    return {"value": 7100}


@pytest.fixture()
def port(_port):
    _port["value"] += 1
    return _port["value"]


@pytest.fixture(autouse=True)
def _server_cleanup():
    yield
    pn.state.reset()


# ---- Helpers ----


def _serve_app(port: int):
    """Import app_tree module fresh, serve it, return (server, tree, vfs)."""
    import importlib
    import os
    import sys
    from pathlib import Path

    # Disable SharePoint during tests
    os.environ["SKIP_SHAREPOINT"] = "1"

    project_root = str(Path(__file__).parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    import app_tree

    importlib.reload(app_tree)
    app = app_tree.template
    server = pn.serve(app, port=port, threaded=True, show=False)
    time.sleep(0.3)
    return server, app_tree.tree, app_tree.vfs


def _open_app(page: Page, port: int):
    """Navigate and wait for shadow DOM to render."""
    page.goto(f"http://localhost:{port}")
    time.sleep(5)  # shadow DOM layout time


def _get_row_texts(page: Page) -> list[str]:
    """Get all visible .wb-row title texts."""
    rows = page.locator("css=.wb-row .wb-title")
    count = rows.count()
    return [rows.nth(i).text_content().strip() for i in range(count)]


# ---- Tests: Tree rendering ----


def test_tree_loads(page: Page, port: int):
    server, tree, vfs = _serve_app(port)
    try:
        _open_app(page, port)
        wrapper = page.locator("css=.wunderbaum-wrapper")
        expect(wrapper.first).to_be_visible(timeout=15_000)
        rows = page.locator("css=.wb-row")
        assert rows.count() > 0, "No .wb-row elements — tree did not render"
    finally:
        server.stop()


def test_tree_has_roots(page: Page, port: int):
    server, tree, vfs = _serve_app(port)
    try:
        _open_app(page, port)
        texts = _get_row_texts(page)
        assert "local" in texts
        assert "webdav" in texts
    finally:
        server.stop()


def test_tree_has_children(page: Page, port: int):
    server, tree, vfs = _serve_app(port)
    try:
        _open_app(page, port)
        texts = _get_row_texts(page)
        assert "documents" in texts
        assert "images" in texts
        assert "shared" in texts
        assert "archive" in texts
    finally:
        server.stop()


def test_tree_has_files(page: Page, port: int):
    server, tree, vfs = _serve_app(port)
    try:
        _open_app(page, port)
        texts = _get_row_texts(page)
        assert "README.md" in texts
        assert "notes.txt" in texts
    finally:
        server.stop()


def test_tree_has_columns(page: Page, port: int):
    server, tree, vfs = _serve_app(port)
    try:
        _open_app(page, port)
        headers = page.locator("css=.wb-header")
        expect(headers.first).to_be_visible(timeout=15_000)
        header_text = headers.first.text_content()
        assert "Name" in header_text
        assert "Size" in header_text
    finally:
        server.stop()


# ---- Tests: Python API operations ----


def test_python_api_add_folder(page: Page, port: int):
    server, tree, vfs = _serve_app(port)
    try:
        _open_app(page, port)
        initial_count = page.locator("css=.wb-row").count()

        tree.add_folder("local://", "api_test_folder", key="local://api_test_folder")
        tree.expand_node("local://", True)
        time.sleep(1)

        new_count = page.locator("css=.wb-row").count()
        assert new_count > initial_count, "add_folder did not add a row"
        texts = _get_row_texts(page)
        assert "api_test_folder" in texts
    finally:
        server.stop()


def test_python_api_add_file(page: Page, port: int):
    server, tree, vfs = _serve_app(port)
    try:
        _open_app(page, port)
        initial_count = page.locator("css=.wb-row").count()

        tree.add_file(
            "local://documents",
            "api_test.txt",
            data={"size": "0 B"},
            key="local://documents/api_test.txt",
        )
        tree.expand_node("local://documents", True)
        time.sleep(1)

        new_count = page.locator("css=.wb-row").count()
        assert new_count > initial_count, "add_file did not add a row"
        texts = _get_row_texts(page)
        assert "api_test.txt" in texts
    finally:
        server.stop()


def test_python_api_remove_node(page: Page, port: int):
    server, tree, vfs = _serve_app(port)
    try:
        _open_app(page, port)
        initial_count = page.locator("css=.wb-row").count()

        tree.remove_node("local://README.md")
        time.sleep(1)

        new_count = page.locator("css=.wb-row").count()
        assert new_count < initial_count, "remove_node did not remove a row"
        texts = _get_row_texts(page)
        assert "README.md" not in texts
    finally:
        server.stop()


def test_python_api_set_source(page: Page, port: int):
    server, tree, vfs = _serve_app(port)
    try:
        _open_app(page, port)

        # Replace tree with minimal source
        tree.set_source([{"title": "test_root", "key": "test://", "icon": "bi bi-hdd", "children": []}])
        time.sleep(1)

        texts = _get_row_texts(page)
        assert "test_root" in texts
        assert "local" not in texts
    finally:
        server.stop()


# ---- Tests: activate (click) shows URI ----


def test_activate_shows_uri(page: Page, port: int):
    server, tree, vfs = _serve_app(port)
    try:
        _open_app(page, port)
        # Click on README.md
        readme_row = page.locator("css=.wb-row .wb-title", has_text="README.md").first
        readme_row.click()
        time.sleep(1)

        # Check the URI display input
        uri_input = page.locator("input[type='text']").first
        value = uri_input.input_value()
        assert "local://README.md" in value
    finally:
        server.stop()


# ---- Tests: DnD drop events (simulated via Python callback) ----


def test_drop_move(page: Page, port: int):
    """Default drop = move: source disappears, target gets file."""
    server, tree, vfs = _serve_app(port)
    try:
        _open_app(page, port)
        import app_tree

        # Simulate moving README.md into images/
        app_tree.on_tree_event(
            "drop",
            {
                "sourceKey": "local://README.md",
                "targetKey": "local://images",
                "region": "over",
            },
        )
        time.sleep(1)

        # VFS: README.md should be at images/README.md, not at root
        entries = [e["name"] for e in vfs.ls("local://images")]
        assert "README.md" in entries
        with pytest.raises(FileNotFoundError):
            vfs.info("local://README.md")

        # Tree: node key should be updated
        texts = _get_row_texts(page)
        assert "README.md" in texts  # still visible, now under images
    finally:
        server.stop()


def test_drop_copy(page: Page, port: int):
    """Ctrl+drop = copy: source stays, target gets a copy."""
    server, tree, vfs = _serve_app(port)
    try:
        _open_app(page, port)
        import app_tree

        initial_texts = _get_row_texts(page)
        readme_count_before = initial_texts.count("README.md")

        # Simulate Ctrl+drop of README.md into images/
        app_tree.on_tree_event(
            "drop",
            {
                "sourceKey": "local://README.md",
                "targetKey": "local://images",
                "region": "over",
                "copy": True,
            },
        )
        time.sleep(1)

        # VFS: original still exists AND copy exists
        assert vfs.info("local://README.md")["type"] == "file"
        entries = [e["name"] for e in vfs.ls("local://images")]
        assert "README.md" in entries

        # Tree: should have one more README.md than before
        texts = _get_row_texts(page)
        readme_count_after = texts.count("README.md")
        assert readme_count_after == readme_count_before + 1
    finally:
        server.stop()


def test_dnd_copy_browser(page: Page, port: int):
    """Actual browser Ctrl+drag from README.md to images/.

    Uses ``window.__wbForceCopy`` — a test hook in panelini's Wunderbaum Vue
    component that forces the DnD copy branch (since Playwright can't reliably
    deliver Ctrl keypresses into shadow DOM).
    """
    server, tree, vfs = _serve_app(port)
    try:
        _open_app(page, port)

        source = page.locator("css=.wb-row .wb-title", has_text="README.md").first
        target = page.locator("css=.wb-row .wb-title", has_text="images").first

        source_box = source.bounding_box()
        target_box = target.bounding_box()
        assert source_box and target_box, "Could not find source/target bounding boxes"

        sx = source_box["x"] + source_box["width"] / 2
        sy = source_box["y"] + source_box["height"] / 2
        tx = target_box["x"] + target_box["width"] / 2
        ty = target_box["y"] + target_box["height"] / 2

        # Enable copy mode via the test hook
        page.evaluate("window.__wbForceCopy = true")

        # Perform drag
        page.mouse.move(sx, sy)
        page.mouse.down()
        for i in range(5):
            frac = (i + 1) / 5
            page.mouse.move(sx + (tx - sx) * frac, sy + (ty - sy) * frac)
            time.sleep(0.05)
        page.mouse.up()
        time.sleep(2)

        # Disable copy mode
        page.evaluate("window.__wbForceCopy = false")

        # VFS: original should still exist AND copy should exist in images/
        assert vfs.info("local://README.md")["type"] == "file"
        entries = [e["name"] for e in vfs.ls("local://images")]
        assert "README.md" in entries, f"README.md not copied to images/. Contents: {entries}"
    finally:
        server.stop()
