"""Token-efficient generic form adaptation for long-tail ATS pages."""

from .adapter import GenericAIAdapter, apply_generic_ai
from .models import FormControl, FormIR, FormOption

__all__ = [
    "FormControl",
    "FormIR",
    "FormOption",
    "GenericAIAdapter",
    "apply_generic_ai",
]
