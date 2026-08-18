"""Validated, non-authoritative view models for governed SDLC documents."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


REQUIREMENTS_SPECIFICATION_PDF = "requirements_specification.pdf"
FUNCTIONAL_SPECIFICATION_PDF = "functional_specification.pdf"
DESIGN_SPECIFICATION_PDF = "design_specification.pdf"
TEST_PLAN_VALIDATION_REPORT_PDF = "test_plan_validation_report.pdf"
SDLC_PDF_FILENAMES = (
    REQUIREMENTS_SPECIFICATION_PDF,
    FUNCTIONAL_SPECIFICATION_PDF,
    DESIGN_SPECIFICATION_PDF,
    TEST_PLAN_VALIDATION_REPORT_PDF,
)
SDLC_DOCUMENT_SCHEMA_VERSION = "sdlc-document-view-v1"


class SDLCDocumentKind(StrEnum):
    """Stable document kinds in final publication order."""

    REQUIREMENTS_SPECIFICATION = "REQUIREMENTS_SPECIFICATION"
    FUNCTIONAL_SPECIFICATION = "FUNCTIONAL_SPECIFICATION"
    DESIGN_SPECIFICATION = "DESIGN_SPECIFICATION"
    TEST_PLAN_VALIDATION_REPORT = "TEST_PLAN_VALIDATION_REPORT"


_FILENAME_BY_KIND = {
    SDLCDocumentKind.REQUIREMENTS_SPECIFICATION: REQUIREMENTS_SPECIFICATION_PDF,
    SDLCDocumentKind.FUNCTIONAL_SPECIFICATION: FUNCTIONAL_SPECIFICATION_PDF,
    SDLCDocumentKind.DESIGN_SPECIFICATION: DESIGN_SPECIFICATION_PDF,
    SDLCDocumentKind.TEST_PLAN_VALIDATION_REPORT: (
        TEST_PLAN_VALIDATION_REPORT_PDF
    ),
}


class DocumentField(BaseModel):
    """One deterministic label/value pair in a document entry."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    label: str = Field(min_length=1)
    value: str = Field(min_length=1)


class DocumentEntry(BaseModel):
    """One titled evidence-backed record within a section."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    heading: str = Field(min_length=1)
    paragraphs: tuple[str, ...] = ()
    fields: tuple[DocumentField, ...] = ()
    canonical_identifiers: tuple[str, ...] = ()


class DocumentTable(BaseModel):
    """One deterministic table with validated rectangular rows."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    title: str = Field(min_length=1)
    columns: tuple[str, ...] = Field(min_length=1)
    rows: tuple[tuple[str, ...], ...] = ()

    @model_validator(mode="after")
    def validate_rows(self) -> Self:
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("Document table rows must match the column count.")
        return self


class DocumentSection(BaseModel):
    """One numbered section in stable builder-defined order."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    number: int = Field(ge=1)
    title: str = Field(min_length=1)
    introduction: tuple[str, ...] = ()
    entries: tuple[DocumentEntry, ...] = ()
    tables: tuple[DocumentTable, ...] = ()


class SDLCDocument(BaseModel):
    """Complete renderer-neutral projection of governed SDLC evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["sdlc-document-view-v1"]
    kind: SDLCDocumentKind
    filename: str
    title: str = Field(min_length=1)
    project_name: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    requirement_spec_id: str = Field(min_length=1)
    requirement_spec_version: int = Field(ge=1)
    authority_statement: str = Field(min_length=1)
    authoritative_sources: tuple[str, ...] = Field(min_length=1)
    sections: tuple[DocumentSection, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_identity_and_order(self) -> Self:
        if self.filename != _FILENAME_BY_KIND[self.kind]:
            raise ValueError("SDLC document filename does not match its kind.")
        numbers = tuple(section.number for section in self.sections)
        if numbers != tuple(range(1, len(numbers) + 1)):
            raise ValueError("SDLC document section numbers must be sequential.")
        return self

    def searchable_text(self) -> str:
        """Return exact renderer input text for semantic tests and inspection."""

        values = [
            self.title,
            self.project_name,
            self.run_id,
            self.requirement_spec_id,
            self.authority_statement,
            *self.authoritative_sources,
        ]
        for section in self.sections:
            values.extend((str(section.number), section.title, *section.introduction))
            for entry in section.entries:
                values.extend(
                    (
                        entry.heading,
                        *entry.paragraphs,
                        *entry.canonical_identifiers,
                    )
                )
                for field in entry.fields:
                    values.extend((field.label, field.value))
            for table in section.tables:
                values.extend((table.title, *table.columns))
                values.extend(cell for row in table.rows for cell in row)
        values.extend(self.limitations)
        return "\n".join(values)
