"""Tests for the add_root tool schema.

The published schema is sent to the Anthropic API as a tool input_schema,
which forbids oneOf/allOf/anyOf at the top level. The protocol discriminated
union lives under config.oneOf (nested = allowed), keyed on config.protocol.
"""

from __future__ import annotations

from filesystem_agent._schemas import _PROTOCOL_SCHEMAS, ADD_ROOT_SCHEMA


def test_no_top_level_combinators():
    """Anthropic API rejects oneOf/allOf/anyOf at the top level of input_schema."""
    forbidden = set(ADD_ROOT_SCHEMA) & {"oneOf", "allOf", "anyOf"}
    assert not forbidden, f"top-level combinators not allowed: {forbidden}"


def test_top_level_is_name_and_config():
    assert ADD_ROOT_SCHEMA["type"] == "object"
    props = ADD_ROOT_SCHEMA["properties"]
    assert set(props) == {"name", "config"}
    assert ADD_ROOT_SCHEMA["required"] == ["name", "config"]
    # base_path must NOT be a root-level sibling; it lives inside variants.
    assert "base_path" not in props


def test_config_is_protocol_discriminated_union():
    config = ADD_ROOT_SCHEMA["properties"]["config"]
    assert "oneOf" in config
    # every variant discriminates on a protocol const
    for variant in config["oneOf"]:
        assert variant["properties"]["protocol"]["const"] == variant["title"]
        # protocol is always required in a variant
        assert "protocol" in variant["required"]


def test_config_oneof_is_protocol_schemas():
    assert _PROTOCOL_SCHEMAS is ADD_ROOT_SCHEMA["properties"]["config"]["oneOf"]


def test_core_protocols_present():
    protocols = [v["title"] for v in _PROTOCOL_SCHEMAS]
    for p in ("local", "memory", "webdav", "sharepoint", "http", "ftp", "google_drive"):
        assert p in protocols, f"missing core protocol: {p}"


def test_base_path_inside_variants_that_use_it():
    variants = {v["title"]: v for v in _PROTOCOL_SCHEMAS}
    # local/http require base_path
    assert "base_path" in variants["local"]["required"] or "base_path" in variants["local"]["properties"]
    assert "base_path" in variants["local"]["required"]
    assert "base_path" in variants["http"]["required"]
    # sharepoint/google_drive must NOT use base_path
    assert "base_path" not in variants["sharepoint"]["properties"]
    assert "base_path" not in variants["google_drive"]["properties"]


def test_variant_required_fields():
    variants = {v["title"]: v for v in _PROTOCOL_SCHEMAS}
    assert variants["google_drive"]["required"] == ["protocol", "share_url"]
    assert variants["sharepoint"]["required"] == ["protocol", "site_url"]
    assert variants["google_drive"]["additionalProperties"] is False
