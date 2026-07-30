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
    CoverLetterOverflowPreviewStatus.AVAILABLE: "当前 Cover Letter 预览已生成。",
    CoverLetterOverflowPreviewStatus.UNCHANGED: "当前 Cover Letter 预览未变化。",
    CoverLetterOverflowPreviewStatus.ITEM_NOT_CURRENT: "该待处理事项已不再是当前事项。",
    CoverLetterOverflowPreviewStatus.TARGET_STALE: "材料或溢出判定已变化，请刷新待处理事项。",
    CoverLetterOverflowPreviewStatus.SOURCE_UNAVAILABLE: "当前 Cover Letter 来源暂时不可用。",
    CoverLetterOverflowPreviewStatus.RENDERER_UNAVAILABLE: "预览服务暂时不可用。",
    CoverLetterOverflowPreviewStatus.PREVIEW_UNSAFE: "当前预览格式无法安全展示。",
    CoverLetterOverflowPreviewStatus.PREVIEW_INTEGRITY_FAILURE: "当前预览完整性校验失败。",
    CoverLetterOverflowPreviewStatus.UNSUPPORTED_TARGET: "该修正事项不支持 Cover Letter 预览。",
    CoverLetterOverflowPreviewStatus.FAILED: "暂时无法安全读取 Cover Letter 预览。",
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
