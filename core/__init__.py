"""Stable, privacy-preserving primitives for Jobops.

The public imports in this module are intentionally small.  Browser adapters and
Codex skills should exchange these objects instead of depending on implementation
details from the legacy projects that Jobops reuses.
"""

from .outcomes import (
    ApplicationOutcome,
    EvidenceKind,
    EvidenceRef,
    ExitCode,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)

__all__ = [
    "ApplicationOutcome",
    "EvidenceKind",
    "EvidenceRef",
    "ExitCode",
    "OutcomePhase",
    "OutcomeStatus",
    "ReasonCode",
]
