"""Atomic successful-run publication of the governed SDLC PDF projection set."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentic_sdlc.pdf_renderer import PDFRenderer, ReportLabPDFRenderer
from agentic_sdlc.sdlc_document_builder import (
    SDLCDocumentBuildError,
    build_sdlc_documents,
)
from agentic_sdlc.sdlc_document_models import SDLC_PDF_FILENAMES


class SDLCPDFPublicationError(RuntimeError):
    """Raised when all four PDFs cannot be installed as one complete set."""


def write_sdlc_pdf_artifacts(
    state: Mapping[str, Any],
    output_dir: Path,
    *,
    renderer: PDFRenderer | None = None,
) -> tuple[Path, ...]:
    """Build, render, verify, and install exactly four successful-run PDFs."""

    destination = Path(output_dir)
    final_paths = tuple(destination / filename for filename in SDLC_PDF_FILENAMES)
    temporary_paths = tuple(
        destination / f".{filename}.tmp" for filename in SDLC_PDF_FILENAMES
    )
    active_renderer = renderer or ReportLabPDFRenderer()
    try:
        documents = build_sdlc_documents(state)
        filenames = tuple(document.filename for document in documents)
        if filenames != SDLC_PDF_FILENAMES:
            raise SDLCPDFPublicationError(
                "SDLC document builder returned a noncanonical PDF set."
            )
        destination.mkdir(parents=True, exist_ok=True)
        for document, temporary in zip(documents, temporary_paths, strict=True):
            temporary.unlink(missing_ok=True)
            active_renderer.render(document, temporary)
            _require_valid_pdf(temporary, document.title)
        for temporary, final in zip(temporary_paths, final_paths, strict=True):
            temporary.replace(final)
        for final in final_paths:
            _require_valid_pdf(final, final.name)
    except Exception as error:
        for path in (*temporary_paths, *final_paths):
            path.unlink(missing_ok=True)
        if isinstance(error, SDLCPDFPublicationError):
            raise
        if isinstance(error, SDLCDocumentBuildError):
            raise SDLCPDFPublicationError(
                f"Governed SDLC document views could not be built: {error}"
            ) from error
        raise SDLCPDFPublicationError(
            f"Governed SDLC PDF set could not be generated: {error}"
        ) from error
    return final_paths


def remove_sdlc_pdf_artifacts(output_dir: Path) -> None:
    """Remove application-owned final and temporary PDF-set members."""

    root = Path(output_dir)
    for filename in SDLC_PDF_FILENAMES:
        (root / filename).unlink(missing_ok=True)
        (root / f".{filename}.tmp").unlink(missing_ok=True)


def _require_valid_pdf(path: Path, label: str) -> None:
    try:
        contents = path.read_bytes()
    except OSError as error:
        raise SDLCPDFPublicationError(
            f"{label} could not be read after rendering: {error}"
        ) from error
    if len(contents) < 512 or not contents.startswith(b"%PDF-"):
        raise SDLCPDFPublicationError(
            f"{label} is empty or not a structurally recognizable PDF."
        )
