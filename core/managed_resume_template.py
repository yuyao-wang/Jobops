"""The single minimal managed LaTeX resume template used as a fallback base."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .resume_latex_markers import (
    JOBOPS_CONTENT_BEGIN,
    JOBOPS_CONTENT_END,
    MARKER_MACRO_DEFINITIONS,
)


MANAGED_RESUME_TEMPLATE_ID = "managed-resume-one-page-v1"

_PREAMBLE = (
    "\\documentclass[11pt,a4paper]{article}\n"
    "\\usepackage[T1]{fontenc}\n"
    "\\usepackage[utf8]{inputenc}\n"
    "\\usepackage[margin=0.75in]{geometry}\n"
    "\\usepackage{enumitem}\n"
    "\\setlist[itemize]{leftmargin=*,nosep,topsep=2pt}\n"
    "\\pagestyle{empty}\n"
    "\\setlength{\\parindent}{0pt}\n"
    f"{MARKER_MACRO_DEFINITIONS}"
    "\\begin{document}\n"
)
_POSTAMBLE = "\\end{document}\n"


@dataclass(frozen=True, slots=True)
class ManagedResumeTemplate:
    """A self-contained, versioned scaffold carrying the controlled region."""

    template_id: str
    template_sha256: str
    preamble: str
    postamble: str

    def __post_init__(self) -> None:
        if not isinstance(self.template_id, str) or not self.template_id:
            raise ValueError("template_id must be a non-empty string")
        if (
            not isinstance(self.preamble, str)
            or not isinstance(self.postamble, str)
            or not self.preamble
            or not self.postamble
        ):
            raise ValueError("template scaffold must be non-empty text")
        expected = hashlib.sha256(
            (self.preamble + self.postamble).encode("utf-8")
        ).hexdigest()
        if self.template_sha256 != expected:
            raise ValueError("template_sha256 does not match the scaffold")

    def wrap(self, region_body: str) -> str:
        """Place rendered markers inside the controlled region."""

        if not isinstance(region_body, str):
            raise TypeError("region_body must be a string")
        return (
            f"{self.preamble}"
            f"{JOBOPS_CONTENT_BEGIN}\n"
            f"{region_body}"
            f"{JOBOPS_CONTENT_END}\n"
            f"{self.postamble}"
        )


@runtime_checkable
class ManagedResumeTemplateProvider(Protocol):
    def get(self) -> ManagedResumeTemplate:
        """Return the single managed default template."""


class DefaultManagedResumeTemplateProvider:
    """Serve the one built-in template; this Slice has no template catalogue."""

    def get(self) -> ManagedResumeTemplate:
        return ManagedResumeTemplate(
            template_id=MANAGED_RESUME_TEMPLATE_ID,
            template_sha256=hashlib.sha256(
                (_PREAMBLE + _POSTAMBLE).encode("utf-8")
            ).hexdigest(),
            preamble=_PREAMBLE,
            postamble=_POSTAMBLE,
        )


__all__ = [
    "MANAGED_RESUME_TEMPLATE_ID",
    "DefaultManagedResumeTemplateProvider",
    "ManagedResumeTemplate",
    "ManagedResumeTemplateProvider",
]
