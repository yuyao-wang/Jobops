"""Token-efficient generic form adaptation for long-tail ATS pages."""

from .adapter import GenericAIAdapter, apply_generic_ai
from .models import FormControl, FormIR, FormOption
from .semantic_mapper import (
    FakeSemanticMapper,
    MappingRequest,
    MappingResponse,
    SemanticMapper,
)

__all__ = [
    "FakeSemanticMapper",
    "FormControl",
    "FormIR",
    "FormOption",
    "GenericAIAdapter",
    "MappingRequest",
    "MappingResponse",
    "SemanticMapper",
    "apply_generic_ai",
]
