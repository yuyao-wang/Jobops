"""Local, bounded PDF page rendering for visual inspection only."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Protocol, runtime_checkable


PDF_RENDERER_CONTRACT_VERSION = "pdf-page-renderer-v1"
DEFAULT_RENDER_DPI = 150
RENDER_IMAGE_FORMAT = "PNG"
MAX_RENDERED_PAGES = 20
MAX_RENDERED_IMAGE_BYTES = 8 * 1024 * 1024
_PDF_BASE_DPI = 72


class PdfRendererUnavailableError(RuntimeError):
    """No local renderer is available, or it cannot describe itself."""


@dataclass(frozen=True, slots=True)
class PdfRendererDescription:
    renderer_name: str
    renderer_version: str
    dpi: int
    image_format: str

    def __post_init__(self) -> None:
        for name in ("renderer_name", "renderer_version", "image_format"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 120
            ):
                raise ValueError(f"{name} is invalid")
        if type(self.dpi) is not int or not 36 <= self.dpi <= 600:
            raise ValueError("dpi is outside the renderer contract")


@dataclass(frozen=True, slots=True)
class RenderedPage:
    page_number: int
    width_px: int
    height_px: int
    image_format: str
    image_bytes: bytes

    def __post_init__(self) -> None:
        if type(self.page_number) is not int or self.page_number < 1:
            raise ValueError("page_number must be a positive integer")
        for name in ("width_px", "height_px"):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= 20_000:
                raise ValueError(f"{name} is outside the renderer contract")
        if (
            not isinstance(self.image_format, str)
            or not self.image_format.strip()
        ):
            raise ValueError("image_format is invalid")
        if (
            not isinstance(self.image_bytes, bytes)
            or not self.image_bytes
            or len(self.image_bytes) > MAX_RENDERED_IMAGE_BYTES
        ):
            raise ValueError("image_bytes is outside the renderer contract")


@runtime_checkable
class PdfPageRendererPort(Protocol):
    def describe(self) -> PdfRendererDescription:
        """Return renderer identity without rendering anything."""

    def render(self, pdf_bytes: bytes) -> tuple[RenderedPage, ...]:
        """Render every page at a fixed DPI in stable page order."""


class PdfiumPageRenderer:
    """Render locally through the pypdfium2 dependency pdfplumber already uses."""

    def __init__(self, *, dpi: int = DEFAULT_RENDER_DPI) -> None:
        if type(dpi) is not int or not 36 <= dpi <= 600:
            raise ValueError("dpi is outside the renderer contract")
        self._dpi = dpi

    def describe(self) -> PdfRendererDescription:
        try:
            import pypdfium2
        except ImportError as exc:
            raise PdfRendererUnavailableError(
                "no local PDF renderer is available"
            ) from exc
        version = getattr(pypdfium2, "__version__", "") or "unknown"
        return PdfRendererDescription(
            renderer_name="pypdfium2",
            renderer_version=str(version),
            dpi=self._dpi,
            image_format=RENDER_IMAGE_FORMAT,
        )

    def render(self, pdf_bytes: bytes) -> tuple[RenderedPage, ...]:
        if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
            raise ValueError("pdf_bytes must be non-empty")
        try:
            import pypdfium2
        except ImportError as exc:
            raise PdfRendererUnavailableError(
                "no local PDF renderer is available"
            ) from exc
        scale = self._dpi / _PDF_BASE_DPI
        pages: list[RenderedPage] = []
        try:
            document = pypdfium2.PdfDocument(pdf_bytes)
        except Exception as exc:
            raise PdfRendererUnavailableError(
                "the PDF could not be opened for rendering"
            ) from exc
        try:
            total = len(document)
            if total > MAX_RENDERED_PAGES:
                raise PdfRendererUnavailableError(
                    "the PDF has more pages than the renderer contract allows"
                )
            for index in range(total):
                page = document[index]
                image = page.render(scale=scale).to_pil()
                buffer = BytesIO()
                image.save(buffer, format=RENDER_IMAGE_FORMAT)
                content = buffer.getvalue()
                if len(content) > MAX_RENDERED_IMAGE_BYTES:
                    raise PdfRendererUnavailableError(
                        "a rendered page exceeded the renderer size bound"
                    )
                pages.append(
                    RenderedPage(
                        page_number=index + 1,
                        width_px=image.width,
                        height_px=image.height,
                        image_format=RENDER_IMAGE_FORMAT,
                        image_bytes=content,
                    )
                )
        except PdfRendererUnavailableError:
            raise
        except Exception as exc:
            raise PdfRendererUnavailableError(
                "the PDF pages could not be rendered"
            ) from exc
        finally:
            close = getattr(document, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        return tuple(pages)


__all__ = [
    "DEFAULT_RENDER_DPI",
    "MAX_RENDERED_IMAGE_BYTES",
    "MAX_RENDERED_PAGES",
    "PDF_RENDERER_CONTRACT_VERSION",
    "PdfPageRendererPort",
    "PdfRendererDescription",
    "PdfRendererUnavailableError",
    "PdfiumPageRenderer",
    "RENDER_IMAGE_FORMAT",
    "RenderedPage",
]
