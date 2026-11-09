"""Builders for small, real sample documents used by the document tests.

Everything is generated with the standard library so the tests don't need
Word, Excel or a PDF writer.
"""
import zipfile
from pathlib import Path

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
S_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R_NS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _escape_pdf(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path, pages):
    """Write a valid PDF. Each item in pages is the page's text, or None for an
    image-only page with no text layer (like a scan)."""
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    page_ids = []
    for i, text in enumerate(pages):
        page_id, content_id = 4 + 2 * i, 5 + 2 * i
        page_ids.append(page_id)
        if text:
            ops = ["BT", "/F1 11 Tf", "14 TL", "60 740 Td"]
            ops += [f"({_escape_pdf(line)}) Tj T*" for line in text.split("\n")]
            ops.append("ET")
            stream = "\n".join(ops).encode("latin-1")
        else:
            stream = b"0.5 0.5 0.5 rg 72 72 400 600 re f"  # a grey box: drawn, but no text
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objects[page_id] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode()
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    Path(path).write_bytes(bytes(out))


def _w_paragraph(text, style=None):
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{props}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def make_docx(path, blocks):
    """blocks: ("h", text) heading, ("p", text) paragraph, ("table", [[cells], ...])."""
    body = []
    for kind, value in blocks:
        if kind == "h":
            body.append(_w_paragraph(value, "Heading1"))
        elif kind == "p":
            body.append(_w_paragraph(value))
        else:
            rows = "".join(
                "<w:tr>" + "".join(f"<w:tc>{_w_paragraph(cell)}</w:tc>" for cell in row) + "</w:tr>"
                for row in value
            )
            body.append(f"<w:tbl>{rows}</w:tbl>")
    document = f'<?xml version="1.0" encoding="UTF-8"?><w:document {W_NS}><w:body>{"".join(body)}</w:body></w:document>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)


def make_xlsx(path, sheets):
    """sheets: {name: [[cell, ...], ...]}; strings go to sharedStrings, numbers stay numeric."""
    shared, index = [], {}

    def ref(col, row):
        return f"{chr(65 + col)}{row}"

    sheet_xml = []
    for rows in sheets.values():
        xml_rows = []
        for r, row in enumerate(rows, start=1):
            cells = []
            for c, value in enumerate(row):
                if value is None:
                    continue
                if isinstance(value, (int, float)):
                    cells.append(f'<c r="{ref(c, r)}"><v>{value}</v></c>')
                else:
                    if value not in index:
                        index[value] = len(shared)
                        shared.append(value)
                    cells.append(f'<c r="{ref(c, r)}" t="s"><v>{index[value]}</v></c>')
            xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
        sheet_xml.append(f'<worksheet {S_NS}><sheetData>{"".join(xml_rows)}</sheetData></worksheet>')

    workbook_sheets = "".join(
        f'<sheet name="{name}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, name in enumerate(sheets))
    rels = "".join(
        f'<Relationship Id="rId{i + 1}" Target="worksheets/sheet{i + 1}.xml" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
        for i in range(len(sheets)))
    strings = "".join(f"<si><t>{s}</t></si>" for s in shared)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", f"<workbook {S_NS} {R_NS}><sheets>{workbook_sheets}</sheets></workbook>")
        archive.writestr("xl/_rels/workbook.xml.rels",
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         f"{rels}</Relationships>")
        archive.writestr("xl/sharedStrings.xml", f"<sst {S_NS}>{strings}</sst>")
        for i, xml in enumerate(sheet_xml):
            archive.writestr(f"xl/worksheets/sheet{i + 1}.xml", xml)


def make_sample_folder(folder):
    """A folder of sample documents with known facts. Returns {key: Path}."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        "txt": folder / "meeting notes.txt",
        "md": folder / "travel.md",
        "csv": folder / "inventory.csv",
        "docx": folder / "project plan.docx",
        "xlsx": folder / "budget.xlsx",
        "pdf": folder / "handbook.pdf",
        "scan": folder / "scanned receipt.pdf",
    }
    files["txt"].write_text(
        "Weekly meeting\n\nThe team agreed to move the launch of Project Zephyr to 14 March.\n"
        "Priya Nair will prepare the release checklist.\n", encoding="utf-8")
    files["md"].write_text(
        "# Trip to Jaipur\n\nWe stay at the Lotus Courtyard hotel for three nights.\n\n"
        "## Packing\n\n- sunscreen\n- walking shoes\n", encoding="utf-8")
    files["csv"].write_text(
        "item,quantity,location\nLaptop stand,4,Shelf B\nUSB-C dock,12,Cabinet 3\n", encoding="utf-8")
    make_docx(files["docx"], [
        ("h", "Overview"),
        ("p", "Project Zephyr builds an offline assistant for small clinics."),
        ("h", "Budget"),
        ("p", "The approved budget is 48,250 rupees, signed off by Meera Iyer."),
        ("table", [["Phase", "Owner"], ["Design", "Arjun"], ["Testing", "Kavya"]]),
    ])
    make_xlsx(files["xlsx"], {
        "Expenses": [["Category", "Amount", "Month"], ["Hosting", 1200, "January"], ["Licences", 830.5, "February"]],
        "Notes": [["Remark"], ["Quarterly review scheduled with the auditors"]],
    })
    make_pdf(files["pdf"], [
        "Employee Handbook\nSection 1: Working hours are 9:30 to 17:30.",
        None,
        "Section 3: Annual leave is 24 days per year.\nRequests go to the people team.",
    ])
    make_pdf(files["scan"], [None, None])
    return files
