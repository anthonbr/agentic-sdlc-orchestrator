"""Narrow deterministic PDF rendering boundary for SDLC document views."""

from __future__ import annotations

import html
from functools import partial
from pathlib import Path
from typing import Protocol

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    HRFlowable,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)

from agentic_sdlc.sdlc_document_models import (
    DocumentEntry,
    DocumentTable,
    SDLCDocument,
)


class PDFRenderError(RuntimeError):
    """Raised when a validated document view cannot become a complete PDF."""


class PDFRenderer(Protocol):
    """Renderer abstraction kept separate from governed document builders."""

    def render(self, document: SDLCDocument, output_path: Path) -> None: ...


class ReportLabPDFRenderer:
    """Render professional local PDFs with flowing text and repeating tables."""

    def __init__(self) -> None:
        self._regular_font, self._bold_font = _register_fonts()
        self._styles = _styles(self._regular_font, self._bold_font)

    def render(self, document: SDLCDocument, output_path: Path) -> None:
        """Render one validated view to exactly one caller-owned output path."""

        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        doc = SimpleDocTemplate(
            str(destination),
            pagesize=LETTER,
            leftMargin=0.68 * inch,
            rightMargin=0.68 * inch,
            topMargin=0.72 * inch,
            bottomMargin=0.68 * inch,
            title=document.title,
            author="Agentic SDLC Orchestrator",
            subject="Governed SDLC report",
            creator="Agentic SDLC Orchestrator / ReportLab",
            pageCompression=1,
            allowSplitting=1,
        )
        story = self._story(document, doc.width)
        page = partial(
            _draw_page,
            document=document,
            regular_font=self._regular_font,
            bold_font=self._bold_font,
        )
        try:
            doc.build(
                story,
                onFirstPage=page,
                onLaterPages=page,
                canvasmaker=partial(Canvas, invariant=1),
            )
        except Exception as error:
            destination.unlink(missing_ok=True)
            raise PDFRenderError(
                f"{document.title} rendering failed: {error}"
            ) from error
        try:
            contents = destination.read_bytes()
        except OSError as error:
            destination.unlink(missing_ok=True)
            raise PDFRenderError(
                f"{document.title} output could not be verified: {error}"
            ) from error
        if len(contents) < 512 or not contents.startswith(b"%PDF-"):
            destination.unlink(missing_ok=True)
            raise PDFRenderError(
                f"{document.title} renderer returned an invalid PDF file."
            )

    def _story(self, document: SDLCDocument, width: float) -> list[object]:
        styles = self._styles
        story: list[object] = [
            Spacer(1, 0.55 * inch),
            Paragraph(_markup(document.title), styles["DocumentTitle"]),
            Spacer(1, 0.16 * inch),
            Paragraph(_markup(document.project_name), styles["ProjectTitle"]),
            Spacer(1, 0.34 * inch),
            _metadata_table(document, width, styles),
            Spacer(1, 0.34 * inch),
            HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#2563A6")),
            Spacer(1, 0.22 * inch),
            Paragraph(_markup(document.authority_statement), styles["Provenance"]),
            Spacer(1, 0.34 * inch),
        ]
        for section in document.sections:
            story.extend(
                [
                    Paragraph(
                        _markup(f"{section.number}. {section.title}"),
                        styles["SectionHeading"],
                    ),
                    HRFlowable(
                        width="100%",
                        thickness=0.6,
                        color=colors.HexColor("#8AA8C3"),
                    ),
                    Spacer(1, 0.08 * inch),
                ]
            )
            for paragraph in section.introduction:
                story.extend(
                    (Paragraph(_markup(paragraph), styles["Body"]), Spacer(1, 5))
                )
            for entry in section.entries:
                story.extend(self._entry(entry, width))
            for table in section.tables:
                story.extend(self._table(table, width))
            story.append(Spacer(1, 0.12 * inch))
        if document.limitations:
            story.extend(
                (
                    Paragraph("Limitations", styles["SectionHeading"]),
                    HRFlowable(
                        width="100%",
                        thickness=0.6,
                        color=colors.HexColor("#8AA8C3"),
                    ),
                    Spacer(1, 0.08 * inch),
                )
            )
            for limitation in document.limitations:
                story.append(
                    Paragraph(f"&#8226;&nbsp; {_markup(limitation)}", styles["Body"])
                )
                story.append(Spacer(1, 3))
        return story

    def _entry(self, entry: DocumentEntry, width: float) -> list[object]:
        styles = self._styles
        story: list[object] = [
            Spacer(1, 0.08 * inch),
            Paragraph(_markup(entry.heading), styles["EntryHeading"]),
        ]
        for paragraph in entry.paragraphs:
            story.extend((Paragraph(_markup(paragraph), styles["Body"]), Spacer(1, 4)))
        if entry.fields:
            rows = [
                [
                    Paragraph(_markup(field.label), styles["FieldLabel"]),
                    Paragraph(_markup(field.value), styles["FieldValue"]),
                ]
                for field in entry.fields
            ]
            table = LongTable(
                rows,
                colWidths=(1.48 * inch, width - 1.48 * inch),
                hAlign="LEFT",
                splitByRow=1,
            )
            table.setStyle(_field_table_style())
            story.extend((table, Spacer(1, 0.06 * inch)))
        return story

    def _table(self, table: DocumentTable, width: float) -> list[object]:
        styles = self._styles
        header = [
            Paragraph(_markup(column), styles["TableHeader"])
            for column in table.columns
        ]
        rows = [
            [Paragraph(_markup(cell), styles["TableCell"]) for cell in row]
            for row in table.rows
        ]
        story: list[object] = [
            Spacer(1, 0.10 * inch),
            Paragraph(_markup(table.title), styles["EntryHeading"]),
        ]
        if not rows:
            story.append(Paragraph("No rows are available.", styles["Body"]))
            return story
        rendered = LongTable(
            [header, *rows],
            colWidths=_column_widths(len(table.columns), width),
            repeatRows=1,
            splitByRow=1,
            hAlign="LEFT",
        )
        rendered.setStyle(_data_table_style())
        story.extend((rendered, Spacer(1, 0.08 * inch)))
        return story


def _metadata_table(
    document: SDLCDocument,
    width: float,
    styles: dict[str, ParagraphStyle],
) -> LongTable:
    rows = [
        ("Run ID", document.run_id),
        ("Specification", document.requirement_spec_id),
        ("Specification version", str(document.requirement_spec_version)),
        ("Document kind", document.kind.value.replace("_", " ").title()),
        ("Source", "Governed SDLC evidence"),
    ]
    table = LongTable(
        [
            [
                Paragraph(_markup(label), styles["FieldLabel"]),
                Paragraph(_markup(value), styles["FieldValue"]),
            ]
            for label, value in rows
        ],
        colWidths=(1.48 * inch, width - 1.48 * inch),
        hAlign="LEFT",
    )
    table.setStyle(_field_table_style())
    return table


def _draw_page(
    canvas: Canvas,
    doc: SimpleDocTemplate,
    *,
    document: SDLCDocument,
    regular_font: str,
    bold_font: str,
) -> None:
    canvas.saveState()
    page_width, page_height = LETTER
    canvas.setStrokeColor(colors.HexColor("#CAD6E2"))
    canvas.setLineWidth(0.45)
    canvas.line(doc.leftMargin, page_height - 0.45 * inch, page_width - doc.rightMargin, page_height - 0.45 * inch)
    canvas.setFillColor(colors.HexColor("#33506A"))
    canvas.setFont(bold_font, 7.2)
    canvas.drawString(doc.leftMargin, page_height - 0.34 * inch, document.title)
    canvas.setFont(regular_font, 7.0)
    canvas.drawRightString(page_width - doc.rightMargin, page_height - 0.34 * inch, document.run_id)
    canvas.setStrokeColor(colors.HexColor("#CAD6E2"))
    canvas.line(doc.leftMargin, 0.43 * inch, page_width - doc.rightMargin, 0.43 * inch)
    canvas.setFillColor(colors.HexColor("#526B80"))
    canvas.setFont(regular_font, 7.0)
    canvas.drawString(doc.leftMargin, 0.28 * inch, "Governed SDLC evidence")
    canvas.drawRightString(
        page_width - doc.rightMargin,
        0.28 * inch,
        f"Page {canvas.getPageNumber()}",
    )
    canvas.restoreState()


def _styles(regular_font: str, bold_font: str) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "DocumentTitle": ParagraphStyle(
            "DocumentTitle",
            parent=base["Title"],
            fontName=bold_font,
            fontSize=24,
            leading=29,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#173B57"),
            spaceAfter=3,
        ),
        "ProjectTitle": ParagraphStyle(
            "ProjectTitle",
            parent=base["Heading2"],
            fontName=regular_font,
            fontSize=13,
            leading=17,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#41647F"),
        ),
        "Provenance": ParagraphStyle(
            "Provenance",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=10,
            leading=15,
            textColor=colors.HexColor("#243746"),
        ),
        "SectionHeading": ParagraphStyle(
            "SectionHeading",
            parent=base["Heading1"],
            fontName=bold_font,
            fontSize=15,
            leading=19,
            textColor=colors.HexColor("#173B57"),
            spaceBefore=7,
            spaceAfter=4,
            keepWithNext=True,
        ),
        "EntryHeading": ParagraphStyle(
            "EntryHeading",
            parent=base["Heading3"],
            fontName=bold_font,
            fontSize=10.3,
            leading=13.2,
            textColor=colors.HexColor("#254F70"),
            spaceBefore=4,
            spaceAfter=4,
            keepWithNext=True,
            splitLongWords=True,
        ),
        "Body": ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontName=regular_font,
            fontSize=9.2,
            leading=13.1,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#243746"),
            splitLongWords=True,
        ),
        "FieldLabel": ParagraphStyle(
            "FieldLabel",
            parent=base["BodyText"],
            fontName=bold_font,
            fontSize=7.6,
            leading=10,
            textColor=colors.HexColor("#294E68"),
            splitLongWords=True,
        ),
        "FieldValue": ParagraphStyle(
            "FieldValue",
            parent=base["BodyText"],
            fontName=regular_font,
            fontSize=7.6,
            leading=10,
            textColor=colors.HexColor("#243746"),
            splitLongWords=True,
        ),
        "TableHeader": ParagraphStyle(
            "TableHeader",
            parent=base["BodyText"],
            fontName=bold_font,
            fontSize=6.7,
            leading=8.3,
            textColor=colors.white,
            splitLongWords=True,
        ),
        "TableCell": ParagraphStyle(
            "TableCell",
            parent=base["BodyText"],
            fontName=regular_font,
            fontSize=6.35,
            leading=8.2,
            textColor=colors.HexColor("#243746"),
            splitLongWords=True,
        ),
    }


def _field_table_style() -> TableStyle:
    return TableStyle(
        (
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EAF1F7")),
            ("BACKGROUND", (1, 0), (1, -1), colors.HexColor("#F8FAFC")),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C8D5E0")),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        )
    )


def _data_table_style() -> TableStyle:
    return TableStyle(
        (
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#285D82")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), (colors.white, colors.HexColor("#F3F7FA"))),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#B8C8D5")),
            ("LEFTPADDING", (0, 0), (-1, -1), 3.2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3.2),
            ("TOPPADDING", (0, 0), (-1, -1), 3.2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2),
        )
    )


def _column_widths(count: int, width: float) -> tuple[float, ...]:
    weights = {
        2: (1.3, 4.7),
        3: (1.45, 0.8, 4.0),
        4: (0.7, 0.75, 0.9, 3.7),
        5: (0.65, 1.65, 0.75, 1.2, 0.7),
        6: (0.58, 1.35, 0.68, 0.82, 1.12, 1.0),
    }.get(count, tuple(1.0 for _ in range(count)))
    total = sum(weights)
    return tuple(width * weight / total for weight in weights)


def _markup(value: str) -> str:
    safe = "".join(
        character
        if character in "\n\t" or ord(character) >= 32
        else "\ufffd"
        for character in str(value)
    )
    return html.escape(safe, quote=False).replace("\n", "<br/>")


def _register_fonts() -> tuple[str, str]:
    candidates = (
        (
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ),
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ),
    )
    for regular, bold in candidates:
        if not regular.is_file() or not bold.is_file():
            continue
        names = ("AgenticSDLC-Regular", "AgenticSDLC-Bold")
        if names[0] not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(names[0], str(regular)))
        if names[1] not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(names[1], str(bold)))
        return names
    return "Helvetica", "Helvetica-Bold"
