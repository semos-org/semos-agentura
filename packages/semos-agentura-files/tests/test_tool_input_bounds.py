"""Integer tool-input fields must be bounded (ge/le).

Unbounded depth/limit lets a negative or huge value through validation,
which can wedge a traversal or blow up result sets. Mirrors email-agent,
which bounds its ints.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from semos.agentura.files.tools import (
    FileTreeInput,
    GlobInput,
    GrepInput,
    SearchSharepointInput,
)


@pytest.mark.parametrize(
    "model, payload, field",
    [
        (FileTreeInput, {"depth": 0}, "depth"),
        (FileTreeInput, {"depth": -5}, "depth"),
        (FileTreeInput, {"depth": 9999}, "depth"),
        (SearchSharepointInput, {"limit": 0}, "limit"),
        (SearchSharepointInput, {"limit": 100000}, "limit"),
        (GrepInput, {"pattern": "x", "depth": -1}, "depth"),
        (GrepInput, {"pattern": "x", "max_results": 0}, "max_results"),
        (GlobInput, {"pattern": "*", "depth": 0}, "depth"),
        (GlobInput, {"pattern": "*", "max_results": -1}, "max_results"),
    ],
)
def test_out_of_range_int_rejected(model, payload, field):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "model, payload",
    [
        (FileTreeInput, {"depth": 3}),
        (SearchSharepointInput, {"limit": 20}),
        (GrepInput, {"pattern": "x", "depth": 3, "max_results": 100}),
        (GlobInput, {"pattern": "*", "depth": 5, "max_results": 500}),
    ],
)
def test_in_range_int_accepted(model, payload):
    assert model.model_validate(payload) is not None
