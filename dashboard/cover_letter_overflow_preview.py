"""Authenticated UI adapter for read-only Cover Letter overflow previews."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.authenticated_subject import AuthenticatedSubjectContext
from core.cover_letter_overflow_preview import (
    CoverLetterOverflowCorrectionPreviewRef,
    CoverLetterOverflowPreviewProvider,
    CoverLetterOverflowPreviewResult,
    CoverLetterOverflowPreviewStatus,
)


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowPreviewUIResult:
    status: CoverLetterOverflowPreviewStatus
    preview: dict[str, Any] | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "preview": self.preview,
            "status": self.status.value,
        }


_MESSAGES = {
    CoverLetterOverflowPreviewStatus.AVAILABLE: "The current cover-letter preview is ready.",
    CoverLetterOverflowPreviewStatus.UNCHANGED: "The current cover-letter preview is unchanged.",
    CoverLetterOverflowPreviewStatus.ITEM_NOT_CURRENT: "This attention item is no longer current.",
    CoverLetterOverflowPreviewStatus.TARGET_STALE: "The material or overflow decision changed. Refresh the attention item.",
    CoverLetterOverflowPreviewStatus.SOURCE_UNAVAILABLE: "The current cover-letter source is unavailable.",
    CoverLetterOverflowPreviewStatus.RENDERER_UNAVAILABLE: "The preview renderer is unavailable.",
    CoverLetterOverflowPreviewStatus.PREVIEW_UNSAFE: "The preview format cannot be displayed safely.",
    CoverLetterOverflowPreviewStatus.PREVIEW_INTEGRITY_FAILURE: "The preview failed its integrity check.",
    CoverLetterOverflowPreviewStatus.UNSUPPORTED_TARGET: "This correction item does not support a cover-letter preview.",
    CoverLetterOverflowPreviewStatus.FAILED: "The cover-letter preview could not be read safely.",
}


class CoverLetterOverflowPreviewUIController:
    def __init__(
        self,
        *,
        target_repository: Any,
        preview_provider: CoverLetterOverflowPreviewProvider,
    ) -> None:
        if not hasattr(target_repository, "get"):
            raise TypeError("target_repository must be readable")
        if not isinstance(
            preview_provider, CoverLetterOverflowPreviewProvider
        ):
            raise TypeError("preview_provider must be typed")
        self._target_repository = target_repository
        self._preview_provider = preview_provider

    async def get_or_create(
        self,
        *,
        context: AuthenticatedSubjectContext,
        target_id: str,
    ) -> CoverLetterOverflowPreviewUIResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        read = self._target_repository.get(
            subject_id=context.subject_id, target_id=target_id
        )
        target = getattr(read, "target", None)
        if target is None:
            result = CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.TARGET_STALE
            )
        else:
            result = (
                self._preview_provider
                .get_or_create_cover_letter_overflow_preview(
                    subject_id=context.subject_id,
                    correction_target_ref=target.reference,
                )
            )
        preview = None
        if result.preview_ref is not None:
            preview = {
                "media_type": result.media_type,
                "page_count": result.page_count,
                "preview_reference": result.preview_ref.to_opaque(),
            }
        return CoverLetterOverflowPreviewUIResult(
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
        reference = CoverLetterOverflowCorrectionPreviewRef.from_opaque(
            opaque_preview_reference
        )
        return self._preview_provider.read_current_preview_page(
            subject_id=context.subject_id,
            preview_ref=reference,
            page_number=page_number,
        )


__all__ = [
    "CoverLetterOverflowPreviewUIController",
    "CoverLetterOverflowPreviewUIResult",
]
