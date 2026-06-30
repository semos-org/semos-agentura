from ._editable_slides import (
    apply_template,
    compose_editable_slides,
)
from ._generate_diagram import generate_diagram
from ._generate_image import generate_image as generate_image_fn
from ._slide_merge import (
    MergeConfig,
    SlideRef,
    load_merge_config,
    merge_slides,
    parse_source_args,
)
from .compose import compose

__all__ = [
    "apply_template",
    "compose",
    "compose_editable_slides",
    "generate_diagram",
    "generate_image_fn",
    "load_merge_config",
    "merge_slides",
    "MergeConfig",
    "parse_source_args",
    "SlideRef",
]
