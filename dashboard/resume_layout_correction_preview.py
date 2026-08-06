"""Authenticated UI adapter for read-only Resume layout previews."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.authenticated_subject import AuthenticatedSubjectContext
from core.resume_layout_correction_preview import (
    ResumeLayoutCorrectionPreviewProvider,
    ResumeLayoutCorrectionPreviewRef,
    ResumeLayoutCorrectionPreviewResult,
    ResumeLayoutCorrectionPreviewStatus,
)


@dataclass(frozen=True, slots=True)
class ResumeLayoutCorrectionPreviewUIResult:
    status: ResumeLayoutCorrectionPreviewStatus
    preview: dict[str, Any] | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "preview": self.preview,
            "status": self.status.value,
        }


_MESSAGES = {
    ResumeLayoutCorrectionPreviewStatus.AVAILABLE: "The current resume preview is ready.",
    ResumeLayoutCorrectionPreviewStatus.UNCHANGED: "The current resume preview is unchanged.",
    ResumeLayoutCorrectionPreviewStatus.ITEM_NOT_CURRENT: "This attention item is no longer current.",
    ResumeLayoutCorrectionPreviewStatus.TARGET_STALE: "The material changed. Refresh the attention item.",
    ResumeLayoutCorrectionPreviewStatus.SOURCE_ARTIFACT_MISSING: "The current resume file is unavailable.",
    ResumeLayoutCorrectionPreviewStatus.RENDERER_UNAVAILABLE: "The preview renderer is unavailable.",
    ResumeLayoutCorrectionPreviewStatus.PREVIEW_UNSAFE: "The preview format cannot be displayed safely.",
    ResumeLayoutCorrectionPreviewStatus.PREVIEW_INTEGRITY_FAILURE: "The preview failed its integrity check.",
    ResumeLayoutCorrectionPreviewStatus.UNSUPPORTED_TARGET: "This correction item does not support a resume preview.",
    ResumeLayoutCorrectionPreviewStatus.FAILED: "The resume preview could not be read safely.",
}


class ResumeLayoutCorrectionPreviewUIController:
    def __init__(
        self,
        *,
        target_repository: Any,
        preview_provider: ResumeLayoutCorrectionPreviewProvider,
    ) -> None:
        if not hasattr(target_repository, "get"):
            raise TypeError("target_repository must be readable")
        if not isinstance(
            preview_provider, ResumeLayoutCorrectionPreviewProvider
        ):
            raise TypeError("preview_provider must be typed")
        self._target_repository = target_repository
        self._preview_provider = preview_provider

    async def get_or_create(
        self,
        *,
        context: AuthenticatedSubjectContext,
        target_id: str,
    ) -> ResumeLayoutCorrectionPreviewUIResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        read = self._target_repository.get(
            subject_id=context.subject_id, target_id=target_id
        )
        target = getattr(read, "target", None)
        if target is None:
            result = ResumeLayoutCorrectionPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.TARGET_STALE
            )
        else:
            result = (
                self._preview_provider
                .get_or_create_resume_layout_correction_preview(
                    subject_id=context.subject_id,
                    correction_target_ref=target.reference,
                )
            )
        preview = None
        if result.preview_ref is not None:
            preview = {
                "media_type": result.media_type,
                "origin_kind": (
                    result.origin_kind.value if result.origin_kind else None
                ),
                "page_count": result.page_count,
                "preview_reference": result.preview_ref.to_opaque(),
            }
        return ResumeLayoutCorrectionPreviewUIResult(
            result.status, preview, _MESSAGES[result.status]
        )

    def read_page(
        self,
        *,
        context: AuthenticatedSubjectContext,
        opaque_preview_reference: str,
        page_number: int,
    ) -> bytes | None:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        reference = ResumeLayoutCorrectionPreviewRef.from_opaque(
            opaque_preview_reference
        )
        return self._preview_provider.read_current_preview_page(
            subject_id=context.subject_id,
            preview_ref=reference,
            page_number=page_number,
        )


__all__ = [
    "ResumeLayoutCorrectionPreviewUIController",
    "ResumeLayoutCorrectionPreviewUIResult",
]
