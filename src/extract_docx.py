from __future__ import annotations

from pathlib import Path
from typing import Iterator

from docx import Document
from docx.document import Document as _Document
from docx.oxml.text.paragraph import CT_P
from docx.oxml.table import CT_Tbl
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph


BlockParent = _Document | _Cell


def iter_block_items(parent: BlockParent) -> Iterator[Paragraph | Table]:
    parent_elm = parent.element.body if isinstance(parent, _Document) else parent._tc
    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)



def table_to_markdown(table: Table) -> str:
    rows: list[list[str]] = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            cell_text = "\n".join(p.text.strip() for p in cell.paragraphs if p.text.strip())
            cell_text = cell_text.replace("|", "\\|")
            cells.append(cell_text)
        if any(cell.strip() for cell in cells):
            rows.append(cells)

    if not rows:
        return ""

    width = max(len(r) for r in rows)
    normalized = [r + [""] * (width - len(r)) for r in rows]
    header = "| " + " | ".join(normalized[0]) + " |"
    divider = "| " + " | ".join(["---"] * width) + " |"
    body = ["| " + " | ".join(row) + " |" for row in normalized[1:]]
    return "\n".join([header, divider, *body]) if body else header



def extract_docx_text(docx_path: str | Path) -> str:
    path = Path(docx_path)
    document = Document(str(path))
    blocks: list[str] = []

    for block in iter_block_items(document):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if text:
                blocks.append(text)
        else:
            md_table = table_to_markdown(block)
            if md_table:
                blocks.append(md_table)

    return "\n\n".join(blocks)
