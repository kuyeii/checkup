from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

SUPPORTED_UPLOAD_EXTENSIONS = {".docx", ".doc", ".pdf"}


class DocumentIngestError(RuntimeError):
    """Raised when an uploaded contract cannot be normalized to DOCX."""

    def __init__(self, code: str, title: str, user_message: str, detail: str = "") -> None:
        super().__init__(detail or user_message)
        self.code = code
        self.title = title
        self.user_message = user_message
        self.detail = detail or user_message

    def to_http_detail(self) -> dict[str, str]:
        return {
            "code": self.code,
            "title": self.title,
            "user_message": self.user_message,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class IngestResult:
    original_path: Path
    working_docx_path: Path
    original_ext: str
    source_format: str
    converted: bool
    warnings: list[str] = field(default_factory=list)


def normalize_upload_to_docx(upload_path: str | Path, run_dir: str | Path) -> IngestResult:
    """Normalize .docx/.doc/.pdf uploads to run_dir/source.docx.

    The rest of the review pipeline only understands DOCX. This module keeps
    conversion concerns at the upload boundary so app.py and the DOCX comment
    export flow can continue to operate on a single working document.
    """

    original_path = Path(upload_path)
    target_dir = Path(run_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = target_dir / "source.docx"

    if not original_path.exists():
        raise DocumentIngestError(
            code="UPLOAD_FILE_NOT_FOUND",
            title="上传文件不存在",
            user_message="未找到上传的合同文件，请重新上传后再试。",
            detail=f"Upload file not found: {original_path}",
        )

    suffix = original_path.suffix.lower()
    if suffix not in SUPPORTED_UPLOAD_EXTENSIONS:
        raise DocumentIngestError(
            code="UNSUPPORTED_FILE_TYPE",
            title="文件格式不支持",
            user_message="请上传 PDF 或 Word（.doc/.docx）格式的合同文件后再试。",
            detail=f"目前支持 .pdf / .doc / .docx，收到：{suffix or 'unknown'}",
        )

    if suffix == ".docx":
        _copy_working_file(original_path, output_path)
        return IngestResult(
            original_path=original_path,
            working_docx_path=output_path,
            original_ext=suffix,
            source_format="docx",
            converted=False,
        )

    if suffix == ".doc":
        _convert_doc_to_docx(original_path, output_path, target_dir)
        return IngestResult(
            original_path=original_path,
            working_docx_path=output_path,
            original_ext=suffix,
            source_format="doc",
            converted=True,
        )

    _convert_pdf_to_docx(original_path, output_path)
    return IngestResult(
        original_path=original_path,
        working_docx_path=output_path,
        original_ext=suffix,
        source_format="pdf",
        converted=True,
    )


def _copy_working_file(src: Path, dst: Path) -> None:
    if src.resolve() == dst.resolve():
        return
    shutil.copy2(src, dst)
    _ensure_docx_created(dst, "DOCX 复制失败")


def _convert_doc_to_docx(input_path: Path, output_path: Path, work_dir: Path) -> None:
    soffice = _find_libreoffice_binary()
    if not soffice:
        raise DocumentIngestError(
            code="CONVERTER_NOT_AVAILABLE",
            title="文件转换组件未安装",
            user_message="当前服务暂不支持 .doc 文件转换，请联系管理员安装 LibreOffice 后再试，或上传 .docx 文件。",
            detail=(
                "LibreOffice/soffice executable was not found. "
                "Set LIBREOFFICE_PATH or SOFFICE_PATH to the soffice executable "
                "(macOS example: /Applications/LibreOffice.app/Contents/MacOS/soffice), "
                "or add soffice/libreoffice to PATH. "
                f"diagnostics={get_libreoffice_diagnostics()}"
            ),
        )

    working_input = _prepare_libreoffice_input(input_path, work_dir)
    produced, diagnostics = _run_libreoffice_docx_conversion(soffice, working_input, work_dir)
    if not produced:
        timeout_attempts = [item for item in diagnostics if item.get("timed_out")]
        if timeout_attempts and len(timeout_attempts) == len(diagnostics):
            raise DocumentIngestError(
                code="DOC_CONVERSION_TIMEOUT",
                title="DOC 转换超时",
                user_message=".doc 文件转换耗时过长，请另存为 .docx 后重试。",
                detail=_format_libreoffice_diagnostics(soffice, diagnostics),
            )
        raise DocumentIngestError(
            code="DOC_CONVERSION_FAILED",
            title="DOC 转换失败",
            user_message="无法将 .doc 合同转换为可审查的 Word 文档，请另存为 .docx 后重试。",
            detail=_format_libreoffice_diagnostics(soffice, diagnostics),
        )

    shutil.copy2(produced, output_path)
    _ensure_docx_created(output_path, "DOC 转换失败")


# Keep the unqualified conversion first. In subprocess mode quotes are not
# stripped by a shell, so passing docx:"Office Open XML Text" literally can make
# LibreOffice exit successfully without producing a file on some macOS/WPS DOCs.
_LIBREOFFICE_DOCX_CONVERT_TO_SPECS = ("docx", "docx:Office Open XML Text")


def _prepare_libreoffice_input(input_path: Path, work_dir: Path) -> Path:
    """Copy the DOC to an ASCII-only temp filename before LibreOffice runs.

    LibreOffice handles Unicode paths in most cases, but old WPS/MS-DOC files
    with spaces, parentheses and CJK characters in the source name are a common
    source of silent conversion failures. A stable temp name also makes output
    discovery deterministic: input.doc -> input.docx.
    """

    temp_input_dir = work_dir / "lo_input"
    temp_input_dir.mkdir(parents=True, exist_ok=True)
    temp_input = temp_input_dir / "input.doc"
    shutil.copy2(input_path, temp_input)
    return temp_input


def _run_libreoffice_docx_conversion(soffice: str, input_path: Path, work_dir: Path) -> tuple[Path | None, list[dict[str, object]]]:
    diagnostics: list[dict[str, object]] = []

    for index, convert_to in enumerate(_LIBREOFFICE_DOCX_CONVERT_TO_SPECS, start=1):
        attempt_dir = work_dir / "converted" / f"attempt_{index}"
        if attempt_dir.exists():
            shutil.rmtree(attempt_dir, ignore_errors=True)
        attempt_dir.mkdir(parents=True, exist_ok=True)

        # LibreOffice can share a locked user profile with a running desktop app.
        # A per-run temporary profile makes headless conversion deterministic on
        # macOS/Linux servers and avoids failures such as "source file could not be loaded".
        profile_dir = Path(tempfile.mkdtemp(prefix=f"lo-profile-{index}-", dir=str(work_dir)))
        profile_uri = profile_dir.resolve().as_uri()
        cmd = [
            soffice,
            f"-env:UserInstallation={profile_uri}",
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            "--norestore",
            "--invisible",
            "--nodefault",
            "--nolockcheck",
            "--convert-to",
            convert_to,
            "--outdir",
            str(attempt_dir),
            str(input_path),
        ]
        attempt_info: dict[str, object] = {
            "attempt": index,
            "convert_to": convert_to,
            "outdir": str(attempt_dir),
        }
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            attempt_info.update(
                {
                    "returncode": proc.returncode,
                    "stdout": _clip_process_output(proc.stdout),
                    "stderr": _clip_process_output(proc.stderr),
                }
            )
        except subprocess.TimeoutExpired as exc:
            attempt_info.update(
                {
                    "timed_out": True,
                    "timeout_seconds": exc.timeout,
                    "stdout": _clip_process_output(_decode_timeout_output(exc.stdout)),
                    "stderr": _clip_process_output(_decode_timeout_output(exc.stderr)),
                }
            )
            diagnostics.append(attempt_info)
            continue
        except OSError as exc:
            raise DocumentIngestError(
                code="CONVERTER_NOT_AVAILABLE",
                title="文件转换组件不可用",
                user_message="当前服务无法调用 LibreOffice，请检查 LibreOffice 安装路径或执行权限后再试。",
                detail=f"Failed to execute LibreOffice binary {soffice!r}: {exc}",
            ) from exc
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)

        candidates = _collect_docx_candidates(attempt_dir)
        attempt_info["candidates"] = [str(candidate) for candidate in candidates]
        for candidate in candidates:
            try:
                _validate_docx_package(candidate, title="DOC 转换失败")
                attempt_info["selected"] = str(candidate)
                diagnostics.append(attempt_info)
                return candidate, diagnostics
            except DocumentIngestError as exc:
                attempt_info.setdefault("invalid_candidates", [])
                invalid_candidates = attempt_info["invalid_candidates"]
                if isinstance(invalid_candidates, list):
                    invalid_candidates.append({"path": str(candidate), "detail": exc.detail})

        diagnostics.append(attempt_info)

    return None, diagnostics


def _collect_docx_candidates(directory: Path) -> list[Path]:
    candidates = [candidate for candidate in directory.glob("*.docx") if candidate.is_file()]
    return sorted(candidates, key=lambda candidate: candidate.stat().st_mtime, reverse=True)


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _clip_process_output(value: str | None, *, limit: int = 2000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


def _format_libreoffice_diagnostics(soffice: str, diagnostics: list[dict[str, object]]) -> str:
    return (
        "LibreOffice .doc -> .docx conversion did not produce a valid DOCX. "
        f"soffice={soffice!r}; attempts={diagnostics}. "
        "Try opening the .doc in LibreOffice/WPS and saving it as .docx manually, "
        "or verify that the backend runtime can execute the same LibreOffice binary. "
        "If running in Docker or on a remote server, LibreOffice must be installed inside that runtime, not only on the host machine."
    )


def get_libreoffice_diagnostics() -> dict[str, object]:
    """Return non-secret diagnostics for support and health checks."""

    env_candidates: dict[str, list[str]] = {}
    for env_name in ("LIBREOFFICE_PATH", "SOFFICE_PATH", "LIBREOFFICE_BINARY", "LIBREOFFICE_HOME"):
        env_candidates[env_name] = _expand_libreoffice_candidate(os.getenv(env_name))

    path_candidates = {name: shutil.which(name) for name in ("soffice", "libreoffice")}
    selected = _find_libreoffice_binary()
    return {
        "available": bool(selected),
        "selected": selected or "",
        "env_candidates": env_candidates,
        "path_candidates": path_candidates,
        "platform_hint": "macOS app bundle path is /Applications/LibreOffice.app/Contents/MacOS/soffice; Linux/Docker usually needs apt/apk/yum install libreoffice.",
    }


def _convert_pdf_to_docx(input_path: Path, output_path: Path) -> None:
    if _looks_like_scanned_pdf(input_path):
        raise DocumentIngestError(
            code="SCANNED_PDF_NOT_SUPPORTED",
            title="扫描版 PDF 暂不支持",
            user_message="当前第一版仅支持可复制文字的 PDF。请上传文字型 PDF，或先将扫描件 OCR/另存为 Word 后再试。",
            detail="No extractable text was found in the first pages of the PDF.",
        )

    try:
        from pdf2docx import Converter
    except Exception as exc:  # pragma: no cover - depends on runtime environment
        raise DocumentIngestError(
            code="CONVERTER_NOT_AVAILABLE",
            title="文件转换组件未安装",
            user_message="当前服务暂不支持 PDF 转 Word，请联系管理员安装 pdf2docx 后再试，或上传 .docx 文件。",
            detail=f"pdf2docx import failed: {exc}",
        ) from exc

    try:
        converter = Converter(str(input_path))
        try:
            converter.convert(str(output_path))
        finally:
            converter.close()
        _postprocess_pdf2docx_output(input_path, output_path)
    except DocumentIngestError:
        raise
    except Exception as exc:
        raise DocumentIngestError(
            code="PDF_CONVERSION_FAILED",
            title="PDF 转换失败",
            user_message="无法将该 PDF 合同转换为可审查的 Word 文档，请上传文字型 PDF 或 .docx 文件后重试。",
            detail=str(exc),
        ) from exc

    _ensure_docx_created(output_path, "PDF 转换失败")




def _postprocess_pdf2docx_output(input_path: Path, output_path: Path) -> None:
    """Best-effort cleanup for pdf2docx layout artifacts.

    The converter is good at preserving visual appearance, but Chinese contract
    PDFs often encode form blanks as vector underlines and use absolute
    positions for list indents.  That can turn ordinary lines into borderless
    tables, lose underline blanks, or push paragraphs to the wrong indentation.
    These cleanups are intentionally conservative and PDF-driven, so they work
    on other contract templates instead of matching one fixed document.
    """

    _sanitize_pdf2docx_layout_tables(output_path)
    _reflow_pdf2docx_paragraphs_from_source(input_path, output_path)
    _split_merged_contract_marker_paragraphs(output_path)
    _merge_soft_wrapped_contract_paragraphs(output_path)
    _repair_pdf2docx_contract_artifacts(input_path, output_path)
    _normalize_pdf2docx_font_styles(output_path)

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_NS = {"w": _W_NS}

# pdf2docx often preserves embedded PDF font names such as HYShuSongErKW in
# individual runs.  Those names are not guaranteed to exist on the machine that
# opens the downloaded DOCX, so Word/WPS falls back differently for copied runs
# and for runs rebuilt by our post-processor.  Normalize every generated run and
# document default to a stable CJK font while keeping existing size/bold/underline
# properties.  Use one family for latin/CJK slots so digits in underlined form
# blanks do not render with a different weight from surrounding Chinese text.
_DOCX_DEFAULT_FONT = "SimSun"
_DOCX_FONT_PART_RE = re.compile(
    r"^word/(?:document|styles|numbering|footnotes|endnotes|comments|header\d+|footer\d+)\.xml$"
)
_DOCX_FONT_ATTRS = ("ascii", "hAnsi", "eastAsia", "cs")
_DOCX_FONT_THEME_ATTRS = ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme")

_FORM_LINE_LABELS = (
    "签订日期",
    "签署日期",
    "日期",
    "授权代表",
    "法定代表人",
    "联系人",
    "联系电话",
)
_SIGNATURE_KEYWORDS = ("甲方", "乙方", "盖章", "授权代表")
_LAYOUT_FORM_KEYWORDS = (
    "签订日期",
    "签署日期",
    "甲方",
    "乙方",
    "盖章",
    "授权代表",
    "年",
    "月",
    "日",
    "户名",
    "开户行",
    "账号",
)


def _w(tag: str) -> str:
    return f"{{{_W_NS}}}{tag}"


def _normalize_pdf2docx_font_styles(docx_path: Path) -> int:
    """Normalize PDF-derived font names in generated DOCX packages.

    PDF converters commonly emit embedded font names into ``w:rFonts``.  If the
    end-user machine does not have those exact fonts, Word/WPS applies fallback
    font selection per run.  Our layout repair also rebuilds some runs, which can
    leave a mixture of copied PDF fonts and implicit defaults.  This pass is the
    final styling gate: it pins document defaults, styles, numbering run
    properties and every actual run to a stable CJK font, while leaving size,
    bold, underline and paragraph layout untouched.
    """

    docx_path = Path(docx_path)
    if not docx_path.exists():
        return 0

    try:
        with zipfile.ZipFile(docx_path, "r") as zin:
            names = zin.namelist()
            original_parts = {
                name: zin.read(name)
                for name in names
                if _DOCX_FONT_PART_RE.match(name)
            }
    except Exception:
        return 0

    if not original_parts:
        return 0

    updated_parts: dict[str, bytes] = {}
    changed = 0
    for name, data in original_parts.items():
        try:
            root = etree.fromstring(data)
        except etree.XMLSyntaxError:
            continue
        before = etree.tostring(root)
        _normalize_fonts_in_ooxml_root(root, ensure_doc_defaults=name == "word/styles.xml")
        after = etree.tostring(root)
        if after == before:
            continue
        updated_parts[name] = etree.tostring(
            root,
            xml_declaration=True,
            encoding="UTF-8",
            standalone=True,
        )
        changed += 1

    if not updated_parts:
        return 0

    temp_path = docx_path.with_suffix(f"{docx_path.suffix}.tmp")
    try:
        with zipfile.ZipFile(docx_path, "r") as zin, zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                data = updated_parts.get(name)
                if data is None:
                    data = zin.read(name)
                zout.writestr(name, data)
        _validate_docx_package(temp_path, title="PDF 转换后处理失败")
        temp_path.replace(docx_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        return 0

    return changed


def _normalize_fonts_in_ooxml_root(root: etree._Element, *, ensure_doc_defaults: bool = False) -> None:
    if ensure_doc_defaults:
        _ensure_styles_default_run_font(root)

    # Ensure every actual run has explicit font properties.  Runs created by the
    # repair code and runs copied from pdf2docx then resolve through the same
    # font family on Word/WPS, preventing visible font switching inside a page.
    for run in root.findall(".//w:r", namespaces=_NS):
        _ensure_run_font(run)

    # Also normalize style-level and numbering-level rPr nodes.  This covers
    # list labels and any new text that Word may synthesize from styles.
    for rpr in root.findall(".//w:rPr", namespaces=_NS):
        _ensure_rpr_font(rpr)


def _ensure_styles_default_run_font(styles_root: etree._Element) -> None:
    doc_defaults = styles_root.find("w:docDefaults", namespaces=_NS)
    if doc_defaults is None:
        doc_defaults = etree.Element(_w("docDefaults"))
        # Keep docDefaults near the top of styles.xml, after optional metadata.
        insert_at = 0
        for index, child in enumerate(list(styles_root)):
            if child.tag in {_w("docDefaults"), _w("latentStyles"), _w("style")} :
                insert_at = index
                break
            insert_at = index + 1
        styles_root.insert(insert_at, doc_defaults)

    rpr_default = doc_defaults.find("w:rPrDefault", namespaces=_NS)
    if rpr_default is None:
        rpr_default = etree.SubElement(doc_defaults, _w("rPrDefault"))
    rpr = rpr_default.find("w:rPr", namespaces=_NS)
    if rpr is None:
        rpr = etree.SubElement(rpr_default, _w("rPr"))
    _ensure_rpr_font(rpr)


def _ensure_run_font(run: etree._Element | None) -> None:
    if run is None:
        return
    rpr = run.find("w:rPr", namespaces=_NS)
    if rpr is None:
        rpr = etree.Element(_w("rPr"))
        run.insert(0, rpr)
    _ensure_rpr_font(rpr)


def _ensure_rpr_font(rpr: etree._Element | None) -> None:
    if rpr is None:
        return
    rfonts = rpr.find("w:rFonts", namespaces=_NS)
    if rfonts is None:
        rfonts = etree.Element(_w("rFonts"))
        rpr.insert(0, rfonts)
    for attr in _DOCX_FONT_ATTRS:
        rfonts.set(_w(attr), _DOCX_DEFAULT_FONT)
    for attr in _DOCX_FONT_THEME_ATTRS:
        rfonts.attrib.pop(_w(attr), None)
    rfonts.set(_w("hint"), "eastAsia")


def _sanitize_pdf2docx_layout_tables(docx_path: Path) -> int:
    """Flatten pdf2docx false-positive layout tables in generated DOCX files.

    pdf2docx is intentionally layout-oriented. In Chinese contracts, underline
    rules and signature/date blocks are often represented in the PDF as vector
    lines plus absolutely positioned text. pdf2docx can group those lines into
    borderless Word tables, which then show up as editable table/gridline blocks
    in the downloaded DOCX even though the source PDF did not contain a real
    table. This post-process only touches small, borderless tables that match
    that layout pattern and leaves real bordered tables intact.
    """

    docx_path = Path(docx_path)
    if not docx_path.exists():
        return 0

    try:
        with zipfile.ZipFile(docx_path, "r") as zin:
            document_xml = zin.read("word/document.xml")
            names = zin.namelist()
    except Exception:
        return 0

    try:
        root = etree.fromstring(document_xml)
    except etree.XMLSyntaxError:
        return 0

    body = root.find("w:body", namespaces=_NS)
    if body is None:
        return 0

    changed = 0
    for tbl in list(body.findall("w:tbl", namespaces=_NS)):
        if not _is_pdf2docx_layout_table(tbl):
            continue
        replacement_paragraphs = _layout_table_to_paragraphs(tbl)
        if not replacement_paragraphs:
            continue
        index = body.index(tbl)
        body.remove(tbl)
        for offset, paragraph in enumerate(replacement_paragraphs):
            body.insert(index + offset, paragraph)
        changed += 1

    if not changed:
        return 0

    updated_xml = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )
    temp_path = docx_path.with_suffix(f"{docx_path.suffix}.tmp")
    try:
        with zipfile.ZipFile(docx_path, "r") as zin, zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                data = updated_xml if name == "word/document.xml" else zin.read(name)
                zout.writestr(name, data)
        _validate_docx_package(temp_path, title="PDF 转换后处理失败")
        temp_path.replace(docx_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        return 0

    return changed


def _is_pdf2docx_layout_table(tbl: etree._Element) -> bool:
    if _has_visible_table_borders(tbl):
        return False
    if tbl.xpath(".//w:drawing | .//w:pict | .//w:object", namespaces=_NS):
        return False

    rows = tbl.findall("w:tr", namespaces=_NS)
    if not rows:
        return False

    row_count = len(rows)
    max_cols = max((len(row.findall("w:tc", namespaces=_NS)) for row in rows), default=0)
    text = _compact_text(_element_text(tbl))
    if not text:
        return False

    if _looks_like_signature_block(text):
        return True

    # pdf2docx frequently uses small borderless tables to emulate underlined
    # blanks or two-column signature/date rows.  Do not flatten a borderless
    # structure merely because it is small: real PDFs also contain small
    # borderless text tables.  Keep the rule tied to short, form-like rows and
    # avoid cells that already contain long/multiple paragraphs.
    if row_count <= 4 and max_cols <= 10 and len(text) <= 120:
        if _table_has_complex_cell_content(tbl):
            return False
        if any(keyword in text for keyword in _LAYOUT_FORM_KEYWORDS):
            return True
        if len(text) <= 80:
            return True

    return False


def _table_has_complex_cell_content(tbl: etree._Element) -> bool:
    for cell in tbl.findall(".//w:tc", namespaces=_NS):
        non_empty_paragraphs = [
            paragraph
            for paragraph in cell.findall("w:p", namespaces=_NS)
            if _compact_text(_element_text(paragraph))
        ]
        if len(non_empty_paragraphs) > 1:
            return True
        if len(_compact_text(_cell_text(cell))) > 90:
            return True
    return False


def _has_visible_table_borders(tbl: etree._Element) -> bool:
    border_nodes = tbl.xpath(".//w:tblBorders/* | .//w:tcBorders/*", namespaces=_NS)
    for border in border_nodes:
        value = border.get(_w("val"))
        if value not in {None, "nil", "none"}:
            return True
    return False


def _layout_table_to_paragraphs(tbl: etree._Element) -> list[etree._Element]:
    text = _compact_text(_element_text(tbl))
    if _looks_like_signature_block(text):
        return _signature_block_to_paragraphs(tbl)

    paragraphs: list[etree._Element] = []
    rows = tbl.findall("w:tr", namespaces=_NS)
    for row in rows:
        cells = row.findall("w:tc", namespaces=_NS)
        visible_cells = [_cell_text(cell) for cell in cells]
        if not any(_compact_text(value) for value in visible_cells):
            paragraphs.append(_new_paragraph(_first_cell_paragraph(cells)))
            continue

        underline_from_cell_index = _underline_value_cell_index(cells) if len(rows) == 1 else None
        paragraph = _row_to_paragraph(cells, underline_from_cell_index=underline_from_cell_index)
        if paragraph is not None:
            paragraphs.append(paragraph)

    return paragraphs


def _looks_like_signature_block(text: str) -> bool:
    return all(keyword in text for keyword in _SIGNATURE_KEYWORDS)


def _signature_block_to_paragraphs(tbl: etree._Element) -> list[etree._Element]:
    template = _first_cell_paragraph(tbl.findall(".//w:tc", namespaces=_NS))
    paragraphs = [
        _text_paragraph("甲方（盖章）：\t\t乙方（盖章）：", template=template),
        _text_paragraph("授权代表：\t\t授权代表：", template=template),
        _text_paragraph("年    月    日\t\t年    月    日", template=template),
        _text_paragraph("签订日期：        年    月    日", template=template),
    ]
    for paragraph in paragraphs:
        _remove_paragraph_alignment(paragraph)
    return paragraphs



def _remove_paragraph_alignment(paragraph: etree._Element) -> None:
    ppr = paragraph.find("w:pPr", namespaces=_NS)
    if ppr is None:
        return
    for jc in list(ppr.findall("w:jc", namespaces=_NS)):
        ppr.remove(jc)


def _row_to_paragraph(cells: list[etree._Element], *, underline_from_cell_index: int | None = None) -> etree._Element | None:
    paragraph = _new_paragraph(_first_cell_paragraph(cells))
    row_texts = [_cell_text(cell) for cell in cells]
    alignment_row = _row_needs_alignment(row_texts)
    appended = False

    for index, cell in enumerate(cells):
        cell_text = _cell_text(cell)
        if index > 0:
            separator = _cell_separator(row_texts[index - 1], cell_text, alignment_row=alignment_row)
            if separator:
                paragraph.append(_make_text_run(separator))
                appended = True

        cell_runs = _runs_from_cell(cell)
        underline_cell = underline_from_cell_index is not None and index >= underline_from_cell_index
        if cell_runs:
            for run in cell_runs:
                copied = copy.deepcopy(run)
                if underline_cell and _compact_text(_element_text(copied)):
                    _ensure_run_underline(copied)
                paragraph.append(copied)
                appended = True
        elif cell_text:
            paragraph.append(_make_text_run(cell_text, underline=underline_cell))
            appended = True

    return paragraph if appended else None


def _row_needs_alignment(row_texts: list[str]) -> bool:
    compact = _compact_text("".join(row_texts))
    if any(keyword in compact for keyword in _LAYOUT_FORM_KEYWORDS):
        return True
    non_empty = [value for value in row_texts if _compact_text(value)]
    return len(non_empty) >= 3


def _cell_separator(previous: str, current: str, *, alignment_row: bool) -> str:
    if not _compact_text(previous) or not _compact_text(current):
        return ""
    if alignment_row:
        return "\t"
    return ""


def _underline_value_cell_index(cells: list[etree._Element]) -> int | None:
    if len(cells) != 2:
        return None
    first = _compact_text(_cell_text(cells[0]))
    second = _compact_text(_cell_text(cells[1]))
    if not first or not second:
        return None
    normalized = first.rstrip(":：")
    if first.endswith((":", "：")) and normalized in _FORM_LINE_LABELS:
        return 1
    return None


def _first_cell_paragraph(cells: list[etree._Element]) -> etree._Element | None:
    for cell in cells:
        for paragraph in cell.findall(".//w:p", namespaces=_NS):
            if _compact_text(_element_text(paragraph)):
                return paragraph
    for cell in cells:
        paragraph = cell.find(".//w:p", namespaces=_NS)
        if paragraph is not None:
            return paragraph
    return None


def _new_paragraph(template: etree._Element | None = None) -> etree._Element:
    paragraph = etree.Element(_w("p"), nsmap=template.nsmap if template is not None else None)
    if template is not None:
        ppr = template.find("w:pPr", namespaces=_NS)
        if ppr is not None:
            paragraph.append(copy.deepcopy(ppr))
    return paragraph


def _text_paragraph(text: str, *, template: etree._Element | None = None) -> etree._Element:
    paragraph = _new_paragraph(template)
    run_template = template.find(".//w:r", namespaces=_NS) if template is not None else None
    for index, part in enumerate(text.split("\t")):
        if index:
            paragraph.append(_make_tab_run())
        if part:
            paragraph.append(_make_text_run(part, run_template=run_template))
    return paragraph


def _make_text_run(text: str, *, underline: bool = False, run_template: etree._Element | None = None) -> etree._Element:
    run = etree.Element(_w("r"))
    if run_template is not None:
        rpr_template = run_template.find("w:rPr", namespaces=_NS)
        if rpr_template is not None:
            run.append(copy.deepcopy(rpr_template))
    if underline:
        rpr = run.find("w:rPr", namespaces=_NS)
        if rpr is None:
            rpr = etree.SubElement(run, _w("rPr"))
        underline_el = rpr.find("w:u", namespaces=_NS)
        if underline_el is None:
            underline_el = etree.SubElement(rpr, _w("u"))
        underline_el.set(_w("val"), "single")
    else:
        rpr = run.find("w:rPr", namespaces=_NS)
        if rpr is not None:
            for underline_el in list(rpr.findall("w:u", namespaces=_NS)):
                rpr.remove(underline_el)
    _ensure_run_font(run)
    text_el = etree.SubElement(run, _w("t"))
    if text.startswith(" ") or text.endswith(" ") or "  " in text:
        text_el.set(f"{{{_XML_NS}}}space", "preserve")
    text_el.text = text
    return run


def _make_tab_run() -> etree._Element:
    run = etree.Element(_w("r"))
    _ensure_run_font(run)
    etree.SubElement(run, _w("tab"))
    return run


def _ensure_run_underline(run: etree._Element) -> None:
    rpr = run.find("w:rPr", namespaces=_NS)
    if rpr is None:
        rpr = etree.Element(_w("rPr"))
        run.insert(0, rpr)
    underline = rpr.find("w:u", namespaces=_NS)
    if underline is None:
        underline = etree.SubElement(rpr, _w("u"))
    underline.set(_w("val"), "single")


def _runs_from_cell(cell: etree._Element) -> list[etree._Element]:
    runs: list[etree._Element] = []
    for paragraph in cell.findall(".//w:p", namespaces=_NS):
        for child in paragraph:
            if child.tag == _w("pPr"):
                continue
            if child.tag == _w("r"):
                runs.append(child)
    return runs


def _cell_text(cell: etree._Element) -> str:
    return _element_text(cell)


def _element_text(element: etree._Element) -> str:
    return "".join(element.xpath(".//w:t/text()", namespaces=_NS))



def _compact_text(text: str) -> str:
    return "".join(str(text or "").split())


def _match_text(text: str) -> str:
    """Normalize text for PDF-vs-DOCX matching without changing output text."""

    value = _compact_text(text)
    replacements = {
        "﹕": "：",
        "∶": "：",
        "﹒": ".",
        "．": ".",
        "“": "\"",
        "”": "\"",
        "‘": "'",
        "’": "'",
        "￥": "¥",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return value


@dataclass(frozen=True)
class _PdfCharBox:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class _PdfTextChunk:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    char_boxes: tuple[_PdfCharBox | None, ...] = ()


@dataclass(frozen=True)
class _PdfRule:
    page_index: int
    x0: float
    y: float
    x1: float
    width: float


@dataclass(frozen=True)
class _PdfVisualLine:
    page_index: int
    page_width: float
    text: str
    match_text: str
    x0: float
    y0: float
    x1: float
    y1: float
    visual_x0: float
    underline_ranges: tuple[tuple[int, int], ...] = ()
    leading_underline_spaces: int = 0
    trailing_underline_spaces: int = 0
    tab_stops: tuple[float, ...] = ()


def _reflow_pdf2docx_paragraphs_from_source(pdf_path: Path, docx_path: Path) -> int:
    """Use the source PDF's visual lines to repair pdf2docx paragraphs.

    pdf2docx occasionally merges several visual lines into one Word paragraph,
    right-aligns form fields, or drops vector underline blanks.  This pass keeps
    the converted DOCX package but rewrites only direct body paragraphs that can
    be matched back to consecutive PDF text lines.  It does not touch preserved
    tables, drawings or images.
    """

    try:
        visual_lines = _extract_pdf_visual_lines(pdf_path)
    except Exception:
        return 0
    if not visual_lines:
        return 0

    try:
        with zipfile.ZipFile(docx_path, "r") as zin:
            document_xml = zin.read("word/document.xml")
            names = zin.namelist()
    except Exception:
        return 0

    try:
        root = etree.fromstring(document_xml)
    except etree.XMLSyntaxError:
        return 0

    body = root.find("w:body", namespaces=_NS)
    if body is None:
        return 0

    page_width_twips, page_margin_left_twips = _docx_page_metrics(root)
    line_cursor = 0
    changed = 0

    for child in list(body):
        if child.tag == _w("tbl"):
            table_text = _match_text(_element_text(child))
            match = _match_pdf_lines_for_text(table_text, visual_lines, line_cursor)
            if match:
                _, _, line_cursor = match
            continue
        if child.tag != _w("p"):
            continue

        paragraph_text = _match_text(_element_text(child))
        if not paragraph_text:
            continue

        match = _match_pdf_lines_for_text(paragraph_text, visual_lines, line_cursor)
        if not match:
            _normalize_contract_paragraph_layout(child)
            continue

        matched_lines, start, next_cursor = match
        line_cursor = next_cursor

        # If one Word paragraph matched multiple PDF visual lines, do not blindly
        # keep it as one paragraph and do not split it line-by-line.  Group the
        # visual lines into logical contract paragraphs: standalone headings and
        # short numbered titles stay separate, while normal body text keeps its
        # soft wraps as a single Word paragraph with a PDF-derived first-line
        # indent.  This fixes merged blocks such as
        # "（一）整体要求本项目旨在..." without expanding the document page count.
        if len(matched_lines) > 1:
            if _should_split_pdf_visual_lines(paragraph_text, matched_lines):
                logical_groups = [[line] for line in matched_lines]
            else:
                logical_groups = _group_pdf_visual_lines_into_logical_paragraphs(matched_lines)

            if len(logical_groups) == 1:
                block_changed = _apply_pdf_block_paragraph_properties(
                    child,
                    logical_groups[0],
                    page_width_twips=page_width_twips,
                    page_margin_left_twips=page_margin_left_twips,
                )
                if _normalize_contract_paragraph_layout(child) or block_changed:
                    changed += 1
                continue

            parent = child.getparent()
            if parent is None:
                continue
            index = parent.index(child)
            template = child
            parent.remove(child)
            previous_group_last_line: _PdfVisualLine | None = None
            for offset, group in enumerate(logical_groups):
                if not group:
                    continue
                new_paragraph = _paragraph_from_pdf_line_group(
                    group,
                    template=template,
                    page_width_twips=page_width_twips,
                    page_margin_left_twips=page_margin_left_twips,
                    previous_line=previous_group_last_line,
                    is_first_in_split=offset == 0,
                )
                parent.insert(index + offset, new_paragraph)
                previous_group_last_line = group[-1]
            changed += 1
            continue

        if len(matched_lines) == 1:
            line = matched_lines[0]
            if not _should_rewrite_single_pdf_line(paragraph_text, line, child):
                if _apply_pdf_block_paragraph_properties(
                    child,
                    [line],
                    page_width_twips=page_width_twips,
                    page_margin_left_twips=page_margin_left_twips,
                ) or _normalize_contract_paragraph_layout(child):
                    changed += 1
                continue
            repaired = _rewrite_paragraph_as_pdf_line(
                child,
                line,
                page_width_twips=page_width_twips,
                page_margin_left_twips=page_margin_left_twips,
            )
            if repaired:
                changed += 1
            continue

        parent = child.getparent()
        if parent is None:
            continue
        index = parent.index(child)
        template = child
        parent.remove(child)
        previous_line: _PdfVisualLine | None = None
        for offset, line in enumerate(matched_lines):
            new_paragraph = _paragraph_from_pdf_line(
                line,
                template=template,
                page_width_twips=page_width_twips,
                page_margin_left_twips=page_margin_left_twips,
                previous_line=previous_line,
                is_first_in_split=offset == 0,
            )
            parent.insert(index + offset, new_paragraph)
            previous_line = line
        changed += 1

    if not changed:
        return 0

    updated_xml = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )
    temp_path = docx_path.with_suffix(f"{docx_path.suffix}.tmp")
    try:
        with zipfile.ZipFile(docx_path, "r") as zin, zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                data = updated_xml if name == "word/document.xml" else zin.read(name)
                zout.writestr(name, data)
        _validate_docx_package(temp_path, title="PDF 转换后处理失败")
        temp_path.replace(docx_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        return 0

    return changed


def _extract_pdf_visual_lines(pdf_path: Path) -> list[_PdfVisualLine]:
    try:
        import fitz  # PyMuPDF
    except Exception:
        return []

    visual_lines: list[_PdfVisualLine] = []
    with fitz.open(str(pdf_path)) as doc:
        for page_index, page in enumerate(doc):
            chunks = _extract_pdf_text_chunks(page)
            rules = _extract_pdf_horizontal_rules(page, page_index)
            for group in _group_pdf_chunks_into_visual_lines(chunks):
                line = _make_pdf_visual_line(group, rules, page_index=page_index, page_width=float(page.rect.width))
                if line and line.match_text:
                    visual_lines.append(line)
    return visual_lines


def _extract_pdf_text_chunks(page) -> list[_PdfTextChunk]:
    """Extract visual text chunks with per-character boxes when available.

    PyMuPDF's normal ``dict`` output gives one bbox for a whole PDF text line.
    That is too coarse for form-like contracts: a short underline under ``_5_``
    may be mapped to the neighboring punctuation, while an underline under a
    blank after a label may be lost entirely.  ``rawdict`` exposes character
    boxes, so later underline detection can map vector rules back to exact runs.
    """

    chunks: list[_PdfTextChunk] = []
    try:
        data = page.get_text("rawdict") or {}
    except Exception:
        data = page.get_text("dict") or {}

    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            char_boxes: list[_PdfCharBox | None] = []
            text_parts: list[str] = []
            spans = line.get("spans", [])
            has_raw_chars = any("chars" in span for span in spans)
            if has_raw_chars:
                for span in spans:
                    for char in span.get("chars", []) or []:
                        value = str(char.get("c", ""))
                        if not value:
                            continue
                        x0, y0, x1, y1 = char.get("bbox", (0, 0, 0, 0))
                        text_parts.append(value)
                        char_boxes.append(
                            _PdfCharBox(
                                text=value,
                                x0=float(x0),
                                y0=float(y0),
                                x1=float(x1),
                                y1=float(y1),
                            )
                        )
            else:
                for span in spans:
                    value = str(span.get("text", ""))
                    if not value:
                        continue
                    text_parts.append(value)

            text = "".join(text_parts)
            if not _compact_text(text):
                continue
            x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
            boxes: tuple[_PdfCharBox | None, ...]
            if len(char_boxes) == len(text):
                boxes = tuple(char_boxes)
            else:
                boxes = _interpolate_pdf_char_boxes(text, float(x0), float(y0), float(x1), float(y1))
            chunks.append(
                _PdfTextChunk(
                    text=text,
                    x0=float(x0),
                    y0=float(y0),
                    x1=float(x1),
                    y1=float(y1),
                    char_boxes=boxes,
                )
            )
    chunks.sort(key=lambda item: ((item.y0 + item.y1) / 2, item.x0))
    return chunks


def _interpolate_pdf_char_boxes(
    text: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
) -> tuple[_PdfCharBox | None, ...]:
    if not text:
        return ()
    width = max(1.0, x1 - x0)
    unit = width / max(1, len(text))
    boxes: list[_PdfCharBox | None] = []
    for index, char in enumerate(text):
        boxes.append(_PdfCharBox(text=char, x0=x0 + index * unit, y0=y0, x1=x0 + (index + 1) * unit, y1=y1))
    return tuple(boxes)


def _extract_pdf_horizontal_rules(page, page_index: int) -> list[_PdfRule]:
    rules: list[_PdfRule] = []
    for drawing in page.get_drawings():
        stroke_width = float(drawing.get("width") or 0.0)
        stroke_color = drawing.get("color")
        fill_color = drawing.get("fill")
        for item in drawing.get("items", []):
            if not item:
                continue
            kind = item[0]
            if kind == "l" and len(item) >= 3:
                if stroke_width > 2.0 or not _is_pdf_rule_color(stroke_color):
                    continue
                p1 = item[1]
                p2 = item[2]
                if abs(float(p1.y) - float(p2.y)) > 0.8:
                    continue
                x0 = min(float(p1.x), float(p2.x))
                x1 = max(float(p1.x), float(p2.x))
                if x1 - x0 < 4.0:
                    continue
                rules.append(_PdfRule(page_index=page_index, x0=x0, y=float(p1.y), x1=x1, width=stroke_width))
                continue
            if kind == "re" and len(item) >= 2:
                if not _is_pdf_rule_color(fill_color) and not _is_pdf_rule_color(stroke_color):
                    continue
                rect = item[1]
                try:
                    x0 = float(rect.x0)
                    x1 = float(rect.x1)
                    y0 = float(rect.y0)
                    y1 = float(rect.y1)
                except Exception:
                    continue
                rect_width = x1 - x0
                rect_height = y1 - y0
                if rect_width < 4.0 or rect_height > 2.0 or rect_height <= 0:
                    continue
                rules.append(
                    _PdfRule(
                        page_index=page_index,
                        x0=min(x0, x1),
                        y=(y0 + y1) / 2,
                        x1=max(x0, x1),
                        width=rect_height,
                    )
                )
    return rules


def _is_pdf_rule_color(color: object) -> bool:
    """Return True for black/gray source rules and False for review marks.

    Word/PDF exports often use blue/red strokes for comments, tracked changes or
    spellcheck-like marks.  Treating those as underlines makes whole clauses
    appear underlined in DOCX.  Contract blanks and typed underlines are normally
    black or gray, so keep the underline recovery monochrome-only.
    """

    if color is None:
        return True
    try:
        values = [float(value) for value in color[:3]]  # type: ignore[index]
    except Exception:
        return False
    if not values:
        return True
    return max(values) <= 0.55 and (max(values) - min(values)) <= 0.08


def _group_pdf_chunks_into_visual_lines(chunks: list[_PdfTextChunk]) -> list[list[_PdfTextChunk]]:
    groups: list[list[_PdfTextChunk]] = []
    for chunk in chunks:
        center_y = (chunk.y0 + chunk.y1) / 2
        placed = False
        for group in groups:
            group_center = sum((item.y0 + item.y1) / 2 for item in group) / len(group)
            if abs(center_y - group_center) <= 3.0 or _vertical_overlap_ratio(chunk, group[0]) >= 0.55:
                group.append(chunk)
                placed = True
                break
        if not placed:
            groups.append([chunk])
    for group in groups:
        group.sort(key=lambda item: item.x0)
    groups.sort(key=lambda group: (min(item.y0 for item in group), min(item.x0 for item in group)))
    return groups


def _vertical_overlap_ratio(a: _PdfTextChunk, b: _PdfTextChunk) -> float:
    overlap = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    height = max(1.0, min(a.y1 - a.y0, b.y1 - b.y0))
    return overlap / height


def _make_pdf_visual_line(
    chunks: list[_PdfTextChunk],
    rules: list[_PdfRule],
    *,
    page_index: int,
    page_width: float,
) -> _PdfVisualLine | None:
    if not chunks:
        return None

    text_parts: list[str] = []
    char_boxes: list[_PdfCharBox | None] = []
    tab_stops: list[float] = []
    previous: _PdfTextChunk | None = None
    for chunk in chunks:
        if previous is not None:
            gap = chunk.x0 - previous.x1
            separator = _pdf_gap_separator(gap)
            text_parts.append(separator)
            char_boxes.extend(_interpolate_pdf_char_boxes(separator, previous.x1, previous.y0, chunk.x0, chunk.y1))
            if "\t" in separator:
                tab_stops.append(chunk.x0)
        chunk_text, chunk_boxes = _chunk_text_and_boxes_with_visual_gaps(chunk)
        text_parts.append(chunk_text)
        char_boxes.extend(chunk_boxes)
        previous = chunk

    raw_text = "".join(text_parts)
    if not _compact_text(raw_text):
        return None
    left_trim = len(raw_text) - len(raw_text.lstrip())
    right_trim = len(raw_text.rstrip())
    text = raw_text[left_trim:right_trim]
    char_boxes = char_boxes[left_trim:right_trim]
    if not _compact_text(text):
        return None

    x0 = min(chunk.x0 for chunk in chunks)
    x1 = max(chunk.x1 for chunk in chunks)
    y0 = min(chunk.y0 for chunk in chunks)
    y1 = max(chunk.y1 for chunk in chunks)
    visual_x0 = x0
    underline_indices: set[int] = set()
    leading_width = 0.0
    trailing_width = 0.0

    first_box = _first_visible_pdf_char_box(char_boxes)
    last_box = _last_visible_pdf_char_box(char_boxes)

    for rule in rules:
        if rule.page_index != page_index:
            continue
        if not (y0 - 1.5 <= rule.y <= y1 + 5.5):
            continue

        overlaps_any_text = False
        for index, box in enumerate(char_boxes):
            if box is None or not box.text:
                continue
            if not _pdf_rule_overlaps_char(rule, box):
                continue
            if _skip_underlined_pdf_char(text, index):
                continue
            underline_indices.add(index)
            if _compact_text(box.text):
                overlaps_any_text = True

        # A rule immediately before the first visible text is usually an empty
        # form blank: "________内". Preserve it as underlined spaces and use the
        # rule's x-position for paragraph indentation.  Likewise, a rule after
        # the last visible character is a trailing blank, common in clauses like
        # "4. ______。" where the blank itself has no extractable text.
        if not overlaps_any_text and first_box is not None and rule.x0 < first_box.x0 and abs(rule.x1 - first_box.x0) <= 6.0:
            leading_width = max(leading_width, rule.x1 - rule.x0)
            visual_x0 = min(visual_x0, rule.x0)
        if not overlaps_any_text and last_box is not None and rule.x1 > last_box.x1 and abs(rule.x0 - last_box.x1) <= 8.0:
            trailing_width = max(trailing_width, rule.x1 - rule.x0)

    leading_spaces = _underline_space_count(leading_width) if leading_width > 0 else 0
    trailing_spaces = _underline_space_count(trailing_width) if trailing_width > 0 else 0
    return _PdfVisualLine(
        page_index=page_index,
        page_width=page_width,
        text=text,
        match_text=_match_text(text),
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        visual_x0=visual_x0,
        underline_ranges=tuple(_ranges_from_pdf_underline_indices(underline_indices, text)),
        leading_underline_spaces=leading_spaces,
        trailing_underline_spaces=trailing_spaces,
        tab_stops=tuple(tab_stops),
    )



def _chunk_text_and_boxes_with_visual_gaps(chunk: _PdfTextChunk) -> tuple[str, list[_PdfCharBox | None]]:
    """Return chunk text with PDF-internal visual gaps preserved as spaces.

    Some contracts draw a single text span like ``二零二六年三月十日`` but place
    the glyphs for ``年/月/日`` with visible gaps over an underline.  The plain
    PDF text has no spaces, so a DOCX reconstructed from text alone collapses
    the form value.  Use raw character boxes to insert spaces where the PDF has
    real horizontal gaps inside one span.
    """

    boxes = chunk.char_boxes
    if len(boxes) != len(chunk.text):
        boxes = _interpolate_pdf_char_boxes(chunk.text, chunk.x0, chunk.y0, chunk.x1, chunk.y1)
    if len(boxes) <= 1:
        return chunk.text, list(boxes)

    text_parts: list[str] = []
    out_boxes: list[_PdfCharBox | None] = []
    for index, box in enumerate(boxes):
        if box is None:
            continue
        text_parts.append(box.text)
        out_boxes.append(box)
        if index + 1 >= len(boxes):
            continue
        next_box = boxes[index + 1]
        if next_box is None:
            continue
        gap = next_box.x0 - box.x1
        separator = _pdf_char_gap_separator(box.text, next_box.text, gap)
        if not separator:
            continue
        text_parts.append(separator)
        out_boxes.extend(_interpolate_pdf_char_boxes(separator, box.x1, box.y0, next_box.x0, next_box.y1))
    return "".join(text_parts), out_boxes


def _pdf_char_gap_separator(previous_char: str, next_char: str, gap: float) -> str:
    if gap < 4.5:
        return ""
    if previous_char.isspace() or next_char.isspace():
        return ""
    # Keep normal Latin kerning/word gaps alone.  The problematic cases in
    # contracts are usually CJK form values deliberately spread over a rule.
    if previous_char.isascii() and next_char.isascii() and previous_char.isalnum() and next_char.isalnum():
        return ""
    if gap >= 14.0:
        return "  "
    return " "

def _first_visible_pdf_char_box(char_boxes: list[_PdfCharBox | None]) -> _PdfCharBox | None:
    for box in char_boxes:
        if box is not None and _compact_text(box.text):
            return box
    return None


def _last_visible_pdf_char_box(char_boxes: list[_PdfCharBox | None]) -> _PdfCharBox | None:
    for box in reversed(char_boxes):
        if box is not None and _compact_text(box.text):
            return box
    return None


def _pdf_rule_overlaps_char(rule: _PdfRule, box: _PdfCharBox) -> bool:
    overlap = min(rule.x1, box.x1) - max(rule.x0, box.x0)
    if overlap <= 0:
        return False
    width = max(1.0, box.x1 - box.x0)
    center = (box.x0 + box.x1) / 2
    return rule.x0 <= center <= rule.x1 or overlap >= min(2.0, width * 0.45)


def _skip_underlined_pdf_char(text: str, index: int) -> bool:
    char = text[index]
    # Do not let a form blank after a numbered item underline the list marker.
    if char in ".．、" and 0 <= index <= 3 and re.match(r"^\s*\d{1,2}[.．、]", text):
        return True
    # A punctuation mark after a PDF gap/tab is usually the closing punctuation
    # after a blank line, not part of the blank itself, e.g. "4. ____。".
    if char in "。．.，,；;：:" and index > 0 and text[index - 1].isspace():
        return True
    return False


def _ranges_from_pdf_underline_indices(indices: set[int], text: str) -> list[tuple[int, int]]:
    if not indices:
        return []
    # Include internal spaces when a source underline spans a filled value such
    # as "1 年". Leading/trailing spaces around the value are left untouched.
    expanded = set(indices)
    ordered = sorted(indices)
    for start, end in zip(ordered, ordered[1:]):
        if end - start > 1 and all(text[pos].isspace() for pos in range(start + 1, end)):
            expanded.update(range(start + 1, end))
    return _merge_ranges([(index, index + 1) for index in expanded])


def _pdf_gap_separator(gap: float) -> str:
    if gap <= 3.0:
        return ""
    if gap >= 90.0:
        return "\t"
    if gap >= 22.0:
        return "  "
    return " "


def _underline_space_count(width_points: float) -> int:
    # A CJK 10.5pt blank is roughly 5-6pt wide in Word.  Keep this approximate;
    # the goal is to render a visible blank, not pixel-perfect reconstruction.
    return max(2, min(40, int(round(width_points / 5.5))))


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    cleaned = sorted((max(0, a), max(0, b)) for a, b in ranges if b > a)
    if not cleaned:
        return []
    merged = [cleaned[0]]
    for start, end in cleaned[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _match_pdf_lines_for_text(
    text: str,
    lines: list[_PdfVisualLine],
    cursor: int,
) -> tuple[list[_PdfVisualLine], int, int] | None:
    if not text:
        return None
    max_start = min(len(lines), max(cursor + 160, 160))
    for start in range(cursor, max_start):
        result = _consume_pdf_lines(text, lines, start)
        if result:
            matched_lines, next_index = result
            return matched_lines, start, next_index
    # Last resort: allow matching earlier lines when pdf2docx reordered a small
    # layout table.  Keep the window small enough to avoid false positives.
    fallback_start = max(0, cursor - 20)
    for start in range(fallback_start, min(len(lines), cursor)):
        result = _consume_pdf_lines(text, lines, start)
        if result:
            matched_lines, next_index = result
            return matched_lines, start, max(cursor, next_index)
    return None


def _group_pdf_visual_lines_into_logical_paragraphs(lines: list[_PdfVisualLine]) -> list[list[_PdfVisualLine]]:
    """Group PDF visual lines into editable Word paragraphs.

    A visual PDF line is not necessarily a Word paragraph.  Wrapped body text
    should stay in one paragraph, but headings and short list titles must not be
    glued to the following body.  The decision uses only geometric cues and
    contract markers, so it is reusable across similar contract templates.
    """

    groups: list[list[_PdfVisualLine]] = []
    current: list[_PdfVisualLine] = []
    for line in lines:
        if not current:
            current = [line]
            continue
        if _pdf_line_starts_new_logical_paragraph(current, line):
            groups.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        groups.append(current)
    return groups


def _pdf_line_starts_new_logical_paragraph(current_group: list[_PdfVisualLine], line: _PdfVisualLine) -> bool:
    previous = current_group[-1]
    text = (line.text or "").strip()
    previous_text = (previous.text or "").strip()
    if not text:
        return False

    if _is_contract_standalone_marker_line(text):
        return True
    if _is_contract_standalone_marker_line(previous_text):
        return True
    if _is_contract_paragraph_start_line(text):
        return True
    if _is_short_numbered_title_line(previous_text):
        return True

    # Same-page first-line indent: continuation lines are normally farther left.
    # A new line that moves back to the right by about two CJK characters is a
    # new paragraph rather than a soft wrap.
    if line.page_index == previous.page_index and line.visual_x0 - previous.visual_x0 >= 12.0:
        return True

    # An unusually large vertical gap inside a page also marks a new paragraph.
    if line.page_index == previous.page_index and line.y0 - previous.y1 > 9.0:
        return True

    return False


def _is_contract_standalone_marker_line(text: str) -> bool:
    compact = str(text or "").strip()
    if _line_starts_contract_heading(compact):
        return True
    if re.match(r"^（[一二三四五六七八九十百]+）\S{2,30}$", compact):
        return True
    if _is_short_numbered_title_line(compact):
        return True
    return False


def _is_short_numbered_title_line(text: str) -> bool:
    compact = str(text or "").strip()
    if not re.match(r"^\d{1,2}[.．、]\s*\S+", compact):
        return False
    if len(compact) > 32:
        return False
    # Numbered title lines in contracts are usually pure noun phrases.  If the
    # line already contains sentence punctuation, treat it as a normal clause.
    return not bool(re.search(r"[。；;：:]", compact))


def _is_contract_paragraph_start_line(text: str) -> bool:
    compact = str(text or "").strip()
    return bool(
        re.match(r"^\d{1,2}[.．、]", compact)
        or re.match(r"^注[:：]", compact)
        or compact.startswith(("时间要求", "付款条件", "户名", "账号", "开户行"))
    )


def _paragraph_from_pdf_line_group(
    lines: list[_PdfVisualLine],
    *,
    template: etree._Element,
    page_width_twips: int,
    page_margin_left_twips: int,
    previous_line: _PdfVisualLine | None = None,
    is_first_in_split: bool = True,
) -> etree._Element:
    paragraph = _new_paragraph(template)
    _clear_paragraph_runs(paragraph)
    _apply_pdf_block_paragraph_properties(
        paragraph,
        lines,
        page_width_twips=page_width_twips,
        page_margin_left_twips=page_margin_left_twips,
    )
    if not is_first_in_split:
        _adjust_split_paragraph_spacing(paragraph, lines[0], previous_line)
    run_template = _first_run_in_paragraph(template)
    for line in lines:
        for run in _runs_for_pdf_visual_line(line, run_template=run_template):
            paragraph.append(run)
    _normalize_contract_paragraph_layout(paragraph)
    return paragraph


def _should_rewrite_single_pdf_line(text: str, line: _PdfVisualLine, paragraph: etree._Element | None = None) -> bool:
    if _text_contains_form_token(text):
        return True
    if line.leading_underline_spaces or line.trailing_underline_spaces:
        return True
    if line.underline_ranges:
        return True
    if _line_starts_contract_block(text):
        return True
    if paragraph is not None and _paragraph_has_non_pdf_alignment(paragraph, line):
        return True
    return False


def _should_split_pdf_visual_lines(text: str, lines: list[_PdfVisualLine]) -> bool:
    if not lines:
        return False
    # Only split visual PDF lines for form-like blocks whose layout genuinely
    # depends on per-line x positions: cover fields, account rows and signature
    # rows.  For ordinary clauses, even if a line contains an underlined value,
    # keep the content as a flowing Word paragraph; otherwise a 6-page contract
    # can expand into 8-10 pages.
    if _text_contains_form_token(text):
        return len(text) <= 260 or len(lines) <= 8
    if len(lines) <= 2 and any(line.leading_underline_spaces or line.trailing_underline_spaces for line in lines):
        return True
    return False


def _text_contains_form_token(text: str) -> bool:
    form_tokens = (
        "项目名称",
        "采购单位",
        "中标单位",
        "签订日期",
        "签署日期",
        "盖章",
        "授权代表",
        "户名",
        "开户行",
        "账号",
    )
    return any(token in text for token in form_tokens)


def _line_starts_contract_heading(text: str) -> bool:
    compact = str(text or "").strip()
    return bool(re.match(r"^[一二三四五六七八九十百]+、\S{2,40}$", compact))


def _line_starts_contract_block(text: str) -> bool:
    compact = str(text or "").strip()
    return bool(
        re.match(r"^\d{1,2}[.．、]", compact)
        or re.match(r"^（[一二三四五六七八九十百]+）", compact)
        or _line_starts_contract_heading(compact)
    )


def _paragraph_has_non_pdf_alignment(paragraph: etree._Element, line: _PdfVisualLine) -> bool:
    ppr = paragraph.find("w:pPr", namespaces=_NS)
    if ppr is None or _pdf_line_is_centered(line):
        return False
    jc = ppr.find("w:jc", namespaces=_NS)
    return jc is not None and jc.get(_w("val")) in {"center", "right", "both"}


def _consume_pdf_lines(
    text: str,
    lines: list[_PdfVisualLine],
    start: int,
) -> tuple[list[_PdfVisualLine], int] | None:
    position = 0
    matched: list[_PdfVisualLine] = []
    index = start
    while index < len(lines) and position < len(text):
        line_text = lines[index].match_text
        index += 1
        if not line_text:
            continue
        remaining = text[position:]
        if remaining.startswith(line_text):
            matched.append(lines[index - 1])
            position += len(line_text)
            continue
        # Some converters drop punctuation around quote marks or form blanks.
        # Permit very small gaps in the DOCX text when the line itself clearly
        # appears next.
        found_at = remaining.find(line_text)
        if 0 <= found_at <= 3:
            matched.append(lines[index - 1])
            position += found_at + len(line_text)
            continue
        break
    if not matched:
        return None
    coverage = position / max(1, len(text))
    if position == len(text) or (len(matched) >= 2 and coverage >= 0.92):
        return matched, index
    if len(matched) == 1 and matched[0].match_text == text:
        return matched, index
    return None


def _docx_page_metrics(root: etree._Element) -> tuple[int, int]:
    sect = root.find(".//w:sectPr", namespaces=_NS)
    if sect is None:
        return 11906, 1440
    pg_sz = sect.find("w:pgSz", namespaces=_NS)
    pg_mar = sect.find("w:pgMar", namespaces=_NS)
    page_width = int(pg_sz.get(_w("w"), "11906")) if pg_sz is not None else 11906
    left_margin = int(pg_mar.get(_w("left"), "1440")) if pg_mar is not None else 1440
    return page_width, left_margin


def _rewrite_paragraph_as_pdf_line(
    paragraph: etree._Element,
    line: _PdfVisualLine,
    *,
    page_width_twips: int,
    page_margin_left_twips: int,
) -> bool:
    original_text = _element_text(paragraph)
    original_xml = etree.tostring(paragraph)
    _clear_paragraph_runs(paragraph)
    _apply_pdf_line_paragraph_properties(
        paragraph,
        line,
        page_width_twips=page_width_twips,
        page_margin_left_twips=page_margin_left_twips,
    )
    run_template = _first_run_in_paragraph(paragraph)
    # _clear_paragraph_runs removed runs, so fall back to the original XML for a
    # run template when possible.
    if run_template is None:
        try:
            original_paragraph = etree.fromstring(original_xml)
            run_template = _first_run_in_paragraph(original_paragraph)
        except Exception:
            run_template = None
    for run in _runs_for_pdf_visual_line(line, run_template=run_template, fallback_text=original_text):
        paragraph.append(run)
    return etree.tostring(paragraph) != original_xml


def _paragraph_from_pdf_line(
    line: _PdfVisualLine,
    *,
    template: etree._Element,
    page_width_twips: int,
    page_margin_left_twips: int,
    previous_line: _PdfVisualLine | None = None,
    is_first_in_split: bool = True,
) -> etree._Element:
    paragraph = _new_paragraph(template)
    _clear_paragraph_runs(paragraph)
    _apply_pdf_line_paragraph_properties(
        paragraph,
        line,
        page_width_twips=page_width_twips,
        page_margin_left_twips=page_margin_left_twips,
    )
    if not is_first_in_split:
        _adjust_split_paragraph_spacing(paragraph, line, previous_line)
    run_template = _first_run_in_paragraph(template)
    for run in _runs_for_pdf_visual_line(line, run_template=run_template):
        paragraph.append(run)
    return paragraph


def _clear_paragraph_runs(paragraph: etree._Element) -> None:
    for child in list(paragraph):
        if child.tag != _w("pPr"):
            paragraph.remove(child)


def _first_run_in_paragraph(paragraph: etree._Element | None) -> etree._Element | None:
    if paragraph is None:
        return None
    return paragraph.find(".//w:r", namespaces=_NS)


def _apply_pdf_line_paragraph_properties(
    paragraph: etree._Element,
    line: _PdfVisualLine,
    *,
    page_width_twips: int,
    page_margin_left_twips: int,
) -> None:
    ppr = paragraph.find("w:pPr", namespaces=_NS)
    if ppr is None:
        ppr = etree.Element(_w("pPr"))
        paragraph.insert(0, ppr)

    for jc in list(ppr.findall("w:jc", namespaces=_NS)):
        ppr.remove(jc)

    centered = _pdf_line_is_centered(line)
    if centered:
        jc = etree.SubElement(ppr, _w("jc"))
        jc.set(_w("val"), "center")

    ind = ppr.find("w:ind", namespaces=_NS)
    if ind is None:
        ind = etree.SubElement(ppr, _w("ind"))
    if centered:
        ind.set(_w("left"), "0")
        ind.set(_w("firstLine"), "0")
    else:
        scale = page_width_twips / max(1.0, line.page_width)
        left = max(0, int(round(line.visual_x0 * scale)) - page_margin_left_twips)
        ind.set(_w("left"), str(left))
        ind.set(_w("firstLine"), "0")
    ind.set(_w("right"), "0")
    _apply_pdf_tab_stops(ppr, line, page_width_twips=page_width_twips, page_margin_left_twips=page_margin_left_twips)



def _apply_pdf_tab_stops(
    ppr: etree._Element,
    line: _PdfVisualLine,
    *,
    page_width_twips: int,
    page_margin_left_twips: int,
) -> None:
    for existing in list(ppr.findall("w:tabs", namespaces=_NS)):
        ppr.remove(existing)
    if not line.tab_stops:
        return
    scale = page_width_twips / max(1.0, line.page_width)
    tabs = etree.SubElement(ppr, _w("tabs"))
    seen: set[int] = set()
    for stop in line.tab_stops[:4]:
        pos = max(0, int(round(stop * scale)) - page_margin_left_twips)
        if pos in seen:
            continue
        seen.add(pos)
        tab = etree.SubElement(tabs, _w("tab"))
        tab.set(_w("val"), "left")
        tab.set(_w("pos"), str(pos))

def _apply_pdf_block_paragraph_properties(
    paragraph: etree._Element,
    lines: list[_PdfVisualLine],
    *,
    page_width_twips: int,
    page_margin_left_twips: int,
) -> bool:
    if not lines:
        return False
    original_xml = etree.tostring(paragraph)
    ppr = paragraph.find("w:pPr", namespaces=_NS)
    if ppr is None:
        ppr = etree.Element(_w("pPr"))
        paragraph.insert(0, ppr)

    for jc in list(ppr.findall("w:jc", namespaces=_NS)):
        ppr.remove(jc)

    if len(lines) == 1 and _pdf_line_is_centered(lines[0]):
        jc = etree.SubElement(ppr, _w("jc"))
        jc.set(_w("val"), "center")
        _set_paragraph_indent(ppr, left="0", first_line="0")
        return etree.tostring(paragraph) != original_xml

    scale = page_width_twips / max(1.0, lines[0].page_width)
    first_left = max(0, int(round(lines[0].visual_x0 * scale)) - page_margin_left_twips)
    same_page_continuations = [line for line in lines[1:] if line.page_index == lines[0].page_index]
    continuation_lefts = [
        max(0, int(round(line.visual_x0 * scale)) - page_margin_left_twips)
        for line in same_page_continuations
    ]

    # Chinese contracts usually encode first-line indentation by placing the
    # first visual line about two CJK characters to the right of its wrapped
    # continuation lines.  Preserve that as Word left + firstLine indentation
    # for *all* normal paragraphs, not only numbered/list paragraphs.
    if continuation_lefts and first_left - min(continuation_lefts) >= 120:
        base_left = min(continuation_lefts)
        first_line = first_left - base_left
    else:
        base_left = first_left
        first_line = 0

    _set_paragraph_indent(ppr, left=str(base_left), first_line=str(first_line))
    return etree.tostring(paragraph) != original_xml


def _adjust_split_paragraph_spacing(
    paragraph: etree._Element,
    line: _PdfVisualLine,
    previous_line: _PdfVisualLine | None,
) -> None:
    ppr = paragraph.find("w:pPr", namespaces=_NS)
    if ppr is None:
        ppr = etree.Element(_w("pPr"))
        paragraph.insert(0, ppr)
    spacing = ppr.find("w:spacing", namespaces=_NS)
    if spacing is None:
        spacing = etree.SubElement(ppr, _w("spacing"))
    if previous_line is not None and previous_line.page_index == line.page_index:
        gap = max(0.0, line.y0 - previous_line.y1)
        # Word line spacing already accounts for the normal distance between
        # wrapped PDF lines.  Only preserve extra gap beyond a normal baseline
        # interval; otherwise every reconstructed paragraph gets an artificial
        # blank line and the converted contract expands by several pages.
        before = max(0, min(360, int(round((gap - 10.0) * 20))))
    else:
        before = 0
    spacing.set(_w("before"), str(before))
    spacing.set(_w("after"), "0")


def _pdf_line_is_centered(line: _PdfVisualLine) -> bool:
    mid = (line.x0 + line.x1) / 2
    width = line.x1 - line.x0
    body_like_width = line.page_width * 0.58
    return (
        width <= body_like_width
        and abs(mid - line.page_width / 2) <= 14
        and line.x0 > 80
        and line.x1 < line.page_width - 80
    )


def _runs_for_pdf_visual_line(
    line: _PdfVisualLine,
    *,
    run_template: etree._Element | None = None,
    fallback_text: str = "",
) -> list[etree._Element]:
    runs: list[etree._Element] = []
    if line.leading_underline_spaces:
        runs.append(_make_text_run(" " * line.leading_underline_spaces, underline=True, run_template=run_template))

    text = line.text or fallback_text
    if not text:
        return runs

    ranges = list(line.underline_ranges)
    position = 0
    for start, end in ranges:
        start = max(0, min(len(text), start))
        end = max(start, min(len(text), end))
        if start > position:
            _append_runs_for_text_with_tabs(runs, text[position:start], underline=False, run_template=run_template)
        if end > start:
            _append_runs_for_text_with_tabs(runs, text[start:end], underline=True, run_template=run_template)
        position = end
    if position < len(text):
        _append_runs_for_text_with_tabs(runs, text[position:], underline=False, run_template=run_template)
    if line.trailing_underline_spaces:
        runs.append(_make_text_run(" " * line.trailing_underline_spaces, underline=True, run_template=run_template))
    return runs


def _append_runs_for_text_with_tabs(
    runs: list[etree._Element],
    text: str,
    *,
    underline: bool,
    run_template: etree._Element | None,
) -> None:
    if not text:
        return
    parts = text.split("\t")
    for index, part in enumerate(parts):
        if index:
            if underline:
                runs.append(_make_text_run("    ", underline=True, run_template=run_template))
            else:
                runs.append(_make_tab_run())
        if part:
            runs.append(_make_text_run(part, underline=underline, run_template=run_template))


def _normalize_contract_paragraph_layout(paragraph: etree._Element) -> bool:
    """Fallback layout cleanup when no exact PDF-line match is available."""

    text = _compact_text(_element_text(paragraph))
    if not text:
        return False
    ppr = paragraph.find("w:pPr", namespaces=_NS)
    if ppr is None:
        ppr = etree.Element(_w("pPr"))
        paragraph.insert(0, ppr)
    changed = False

    if re.match(r"^\d{1,2}[\.、．]", text):
        changed |= _ensure_number_prefix_spacing(paragraph)
        # Do not overwrite PDF-derived indentation.  Only provide a conservative
        # fallback for paragraphs that have no indentation at all.
        if ppr.find("w:ind", namespaces=_NS) is None:
            changed |= _set_paragraph_indent(ppr, left="780", first_line="0")
    elif re.match(r"^[一二三四五六七八九十百]+、", text) or re.match(r"^（[一二三四五六七八九十百]+）", text):
        if ppr.find("w:ind", namespaces=_NS) is None:
            changed |= _set_paragraph_indent(ppr, left="780", first_line="0")
        else:
            changed |= _remove_paragraph_alignment_value(ppr)
    elif text.startswith(("签订日期：", "签订日期:")):
        changed |= _remove_paragraph_alignment_value(ppr)
    return changed


def _ensure_number_prefix_spacing(paragraph: etree._Element) -> bool:
    text_nodes = paragraph.xpath(".//w:t", namespaces=_NS)
    if not text_nodes:
        return False
    first = text_nodes[0]
    value = first.text or ""
    updated = re.sub(r"^(\s*\d{1,2}[\.、．])(?!\s)", r"\1 ", value, count=1)
    if updated == value:
        return False
    first.text = updated
    first.set(f"{{{_XML_NS}}}space", "preserve")
    return True


def _set_paragraph_indent(ppr: etree._Element, *, left: str, first_line: str) -> bool:
    changed = False
    ind = ppr.find("w:ind", namespaces=_NS)
    if ind is None:
        ind = etree.SubElement(ppr, _w("ind"))
        changed = True
    for key, value in (("left", left), ("firstLine", first_line), ("right", "0")):
        attr = _w(key)
        if ind.get(attr) != value:
            ind.set(attr, value)
            changed = True
    changed |= _remove_paragraph_alignment_value(ppr)
    return changed


def _remove_paragraph_alignment_value(ppr: etree._Element) -> bool:
    changed = False
    for jc in list(ppr.findall("w:jc", namespaces=_NS)):
        ppr.remove(jc)
        changed = True
    return changed





def _merge_soft_wrapped_contract_paragraphs(docx_path: Path) -> int:
    """Merge pdf2docx hard line breaks back into logical paragraphs.

    Some PDF converters emit each visual line as a separate Word paragraph.
    That destroys first-line indentation and makes continuation lines look like
    extra-indented paragraphs.  This pass merges only obvious soft wraps: a
    continuation line starts at the paragraph base indent, while headings,
    numbered titles, form fields and signature rows remain standalone.
    """

    try:
        with zipfile.ZipFile(docx_path, "r") as zin:
            document_xml = zin.read("word/document.xml")
            names = zin.namelist()
    except Exception:
        return 0

    try:
        root = etree.fromstring(document_xml)
    except etree.XMLSyntaxError:
        return 0

    body = root.find("w:body", namespaces=_NS)
    if body is None:
        return 0

    changed = _merge_soft_wraps_in_body(body)
    if not changed:
        return 0

    updated_xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    temp_path = docx_path.with_suffix(f"{docx_path.suffix}.tmp")
    try:
        with zipfile.ZipFile(docx_path, "r") as zin, zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                data = updated_xml if name == "word/document.xml" else zin.read(name)
                zout.writestr(name, data)
        _validate_docx_package(temp_path, title="PDF 转换后处理失败")
        temp_path.replace(docx_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        return 0
    return changed


def _merge_soft_wraps_in_body(body: etree._Element) -> int:
    changed = 0
    children = list(body)
    index = 1
    while index < len(children):
        current = children[index]
        previous = children[index - 1]
        if current.tag != _w("p") or previous.tag != _w("p"):
            index += 1
            continue
        if not _should_merge_soft_wrapped_paragraphs(previous, current):
            index += 1
            continue

        _merge_paragraph_runs_without_separator(previous, current)
        _apply_merged_soft_wrap_indent(previous, current)
        body.remove(current)
        children.pop(index)
        changed += 1
        # Keep the cursor on the merged paragraph so additional continuation
        # lines are consumed into the same logical paragraph.
    return changed


def _should_merge_soft_wrapped_paragraphs(previous: etree._Element, current: etree._Element) -> bool:
    previous_text = _element_text(previous).strip()
    current_text = _element_text(current).strip()
    if not previous_text or not current_text:
        return False
    if _text_contains_form_token(previous_text) or _text_contains_form_token(current_text):
        return False
    if _looks_like_two_column_signature_paragraph(previous_text) or _looks_like_two_column_signature_paragraph(current_text):
        return False
    if _is_contract_standalone_marker_line(previous_text) or _is_contract_standalone_marker_line(current_text):
        return False
    if _is_contract_paragraph_start_line(current_text):
        return False

    prev_left, prev_first = _paragraph_indent_values(previous)
    curr_left, curr_first = _paragraph_indent_values(current)
    prev_start = prev_left + prev_first
    curr_start = curr_left + curr_first

    # A continuation line is usually left of the first line by two CJK chars.
    if curr_start <= prev_start - 120:
        return True

    # Once a paragraph has a first-line indent, subsequent continuation lines
    # share the paragraph base indent.
    if prev_first >= 120 and abs(curr_start - prev_left) <= 120:
        return True

    # Cross-page or same-indent soft wrap: previous line did not finish a
    # sentence and the next line is not a new contract marker.
    if not previous_text.endswith(("。", "！", "？", "!", "?", "：", ":")):
        if abs(curr_start - prev_start) <= 120 or curr_start <= prev_start:
            return True

    return False


def _merge_paragraph_runs_without_separator(previous: etree._Element, current: etree._Element) -> None:
    for child in list(current):
        if child.tag == _w("pPr"):
            continue
        previous.append(copy.deepcopy(child))


def _apply_merged_soft_wrap_indent(previous: etree._Element, current: etree._Element) -> None:
    prev_left, prev_first = _paragraph_indent_values(previous)
    curr_left, curr_first = _paragraph_indent_values(current)
    prev_start = prev_left + prev_first
    curr_start = curr_left + curr_first
    base_left = min(prev_left, curr_start, curr_left)
    first_line = max(0, prev_start - base_left)
    ppr = previous.find("w:pPr", namespaces=_NS)
    if ppr is None:
        ppr = etree.Element(_w("pPr"))
        previous.insert(0, ppr)
    _set_paragraph_indent(ppr, left=str(base_left), first_line=str(first_line))


def _paragraph_indent_values(paragraph: etree._Element) -> tuple[int, int]:
    ppr = paragraph.find("w:pPr", namespaces=_NS)
    ind = ppr.find("w:ind", namespaces=_NS) if ppr is not None else None
    if ind is None:
        return 0, 0
    left = _safe_int(ind.get(_w("left")), 0)
    first = _safe_int(ind.get(_w("firstLine")), 0)
    # Hanging indents are represented as negative first-line offsets for the
    # start-position calculation used here.
    hanging = _safe_int(ind.get(_w("hanging")), 0)
    if hanging:
        first = -hanging
    return left, first


def _safe_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except Exception:
        return default


def _repair_pdf2docx_contract_artifacts(pdf_path: Path, docx_path: Path) -> int:
    """Final conservative fixes for form dates, continuation blanks and signatures."""

    try:
        with zipfile.ZipFile(docx_path, "r") as zin:
            document_xml = zin.read("word/document.xml")
            names = zin.namelist()
    except Exception:
        return 0

    try:
        root = etree.fromstring(document_xml)
    except etree.XMLSyntaxError:
        return 0

    body = root.find("w:body", namespaces=_NS)
    if body is None:
        return 0

    changed = 0
    changed += _repair_chinese_date_form_paragraphs(body)
    changed += _normalize_cover_form_field_spacing(body)
    changed += _merge_underlined_value_continuations(body)
    changed += _repair_signature_tab_paragraphs(body, root)

    if not changed:
        return 0

    updated_xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    temp_path = docx_path.with_suffix(f"{docx_path.suffix}.tmp")
    try:
        with zipfile.ZipFile(docx_path, "r") as zin, zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                data = updated_xml if name == "word/document.xml" else zin.read(name)
                zout.writestr(name, data)
        _validate_docx_package(temp_path, title="PDF 转换后处理失败")
        temp_path.replace(docx_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        return 0
    return changed


def _normalize_cover_form_field_spacing(body: etree._Element) -> int:
    """Make consecutive cover form fields use consistent vertical gaps."""

    changed = 0
    labels = ("项目名称", "采购单位", "中标单位", "签订日期", "签署日期")
    paragraphs = [child for child in body if child.tag == _w("p")]
    cover_indices = [
        idx
        for idx, paragraph in enumerate(paragraphs)
        if _element_text(paragraph).strip().startswith(labels)
    ]
    if len(cover_indices) < 2:
        return 0

    existing_befores: list[int] = []
    for idx in cover_indices[1:]:
        before = _paragraph_spacing_before(paragraphs[idx])
        if before >= 240:
            existing_befores.append(before)
    target_before = sorted(existing_befores)[len(existing_befores) // 2] if existing_befores else 420
    target_before = max(360, min(520, target_before))

    for idx in cover_indices[1:]:
        paragraph = paragraphs[idx]
        before = _paragraph_spacing_before(paragraph)
        if before >= target_before - 80:
            continue
        spacing = _ensure_paragraph_spacing(paragraph)
        spacing.set(_w("before"), str(target_before))
        changed += 1
    return changed


def _paragraph_spacing_before(paragraph: etree._Element) -> int:
    ppr = paragraph.find("w:pPr", namespaces=_NS)
    spacing = ppr.find("w:spacing", namespaces=_NS) if ppr is not None else None
    return _safe_int(spacing.get(_w("before")) if spacing is not None else None, 0)


def _ensure_paragraph_spacing(paragraph: etree._Element) -> etree._Element:
    ppr = paragraph.find("w:pPr", namespaces=_NS)
    if ppr is None:
        ppr = etree.Element(_w("pPr"))
        paragraph.insert(0, ppr)
    spacing = ppr.find("w:spacing", namespaces=_NS)
    if spacing is None:
        spacing = etree.SubElement(ppr, _w("spacing"))
    return spacing


def _repair_chinese_date_form_paragraphs(body: etree._Element) -> int:
    changed = 0
    for paragraph in body.findall("w:p", namespaces=_NS):
        text = _element_text(paragraph)
        if not re.search(r"签[订署]日期[:：]\s*[二〇零一二三四五六七八九十]{4}年[一二三四五六七八九十]{1,3}月[一二三四五六七八九十]{1,3}日", text):
            continue
        original = etree.tostring(paragraph)
        match = re.search(r"(签[订署]日期[:：])\s*([二〇零一二三四五六七八九十]{4})年([一二三四五六七八九十]{1,3})月([一二三四五六七八九十]{1,3})日", text)
        if not match:
            continue
        label, year, month, day = match.groups()
        template = _first_run_in_paragraph(paragraph)
        _clear_paragraph_runs(paragraph)
        paragraph.append(_make_text_run(f"{label} ", run_template=template))
        paragraph.append(_make_text_run(f"{year} 年 {month} 月 {day} 日", underline=True, run_template=template))
        ppr = paragraph.find("w:pPr", namespaces=_NS)
        if ppr is None:
            ppr = etree.Element(_w("pPr"))
            paragraph.insert(0, ppr)
        _remove_paragraph_alignment_value(ppr)
        if etree.tostring(paragraph) != original:
            changed += 1
    return changed


def _merge_underlined_value_continuations(body: etree._Element) -> int:
    changed = 0
    children = list(body)
    index = 1
    while index < len(children):
        current = children[index]
        previous = children[index - 1]
        if current.tag != _w("p") or previous.tag != _w("p"):
            index += 1
            continue
        current_text = _element_text(current).strip()
        previous_text = _element_text(previous).strip()
        if not _should_merge_underlined_value_continuation(previous_text, current_text):
            index += 1
            continue
        _append_space_if_needed(previous)
        for child in list(current):
            if child.tag == _w("pPr"):
                continue
            previous.append(copy.deepcopy(child))
        body.remove(current)
        children.pop(index)
        changed += 1
    return changed


def _should_merge_underlined_value_continuation(previous_text: str, current_text: str) -> bool:
    if not previous_text or not current_text:
        return False
    if len(current_text) > 80:
        # A wrapped clause line is allowed when it starts with a short form value;
        # the point is to remove the hard paragraph break created by pdf2docx.
        current_prefix = current_text[:12]
    else:
        current_prefix = current_text
    if not re.match(r"^[_\s]*[0-9一二三四五六七八九十]{1,3}\s*(?:天|日|年|月|个工作日|％|%)", current_prefix):
        return False
    if previous_text[-1] in "。；;：:" or _line_starts_contract_heading(current_text):
        return False
    return previous_text.endswith(("起", "前", "后", "第", "为", "内", "超过", "提前")) or "之日起" in previous_text[-12:]


def _append_space_if_needed(paragraph: etree._Element) -> None:
    text = _element_text(paragraph)
    if not text or text.endswith((" ", "\t")):
        return
    paragraph.append(_make_text_run(" ", run_template=_first_run_in_paragraph(paragraph)))


def _repair_signature_tab_paragraphs(body: etree._Element, root: etree._Element) -> int:
    page_width_twips, page_margin_left_twips = _docx_page_metrics(root)
    default_right_col = max(3000, int((page_width_twips - page_margin_left_twips) * 0.47))
    changed = 0
    for paragraph in body.findall("w:p", namespaces=_NS):
        text = _element_text(paragraph)
        if not _looks_like_two_column_signature_paragraph(text):
            continue
        original = etree.tostring(paragraph)
        _normalize_signature_tabs(paragraph)
        ppr = paragraph.find("w:pPr", namespaces=_NS)
        if ppr is None:
            ppr = etree.Element(_w("pPr"))
            paragraph.insert(0, ppr)
        _remove_paragraph_alignment_value(ppr)
        tabs = ppr.find("w:tabs", namespaces=_NS)
        if tabs is None:
            tabs = etree.SubElement(ppr, _w("tabs"))
        if not tabs.findall("w:tab", namespaces=_NS):
            tab = etree.SubElement(tabs, _w("tab"))
            tab.set(_w("val"), "left")
            tab.set(_w("pos"), str(default_right_col))
        if etree.tostring(paragraph) != original:
            changed += 1
    return changed


def _looks_like_two_column_signature_paragraph(text: str) -> bool:
    compact = _compact_text(text)
    if "甲方（盖章）：乙方（盖章）：" in compact:
        return True
    if compact == "授权代表：授权代表：":
        return True
    if compact in {"年月日年月日", "年月日年月日"}:
        return True
    return False


def _normalize_signature_tabs(paragraph: etree._Element) -> None:
    text = _element_text(paragraph)
    compact = _compact_text(text)
    replacement: str | None = None
    if "甲方（盖章）：乙方（盖章）：" in compact:
        replacement = "甲方（盖章）：\t乙方（盖章）："
    elif compact == "授权代表：授权代表：":
        replacement = "授权代表：\t授权代表："
    elif compact == "年月日年月日":
        replacement = "年    月    日\t年    月    日"
    if replacement is None:
        return
    template = _first_run_in_paragraph(paragraph)
    _clear_paragraph_runs(paragraph)
    for idx, part in enumerate(replacement.split("\t")):
        if idx:
            paragraph.append(_make_tab_run())
        paragraph.append(_make_text_run(part, run_template=template))

def _split_merged_contract_marker_paragraphs(docx_path: Path) -> int:
    """Split obvious merged contract headings/items without touching tables.

    This is a conservative fallback for pdf2docx paragraphs such as
    "...采取以下第1种方式：1.合同签订后...".  It preserves run formatting while
    moving the numbered item to its own paragraph so indentation rules can apply.
    """

    try:
        with zipfile.ZipFile(docx_path, "r") as zin:
            document_xml = zin.read("word/document.xml")
            names = zin.namelist()
    except Exception:
        return 0

    try:
        root = etree.fromstring(document_xml)
    except etree.XMLSyntaxError:
        return 0

    body = root.find("w:body", namespaces=_NS)
    if body is None:
        return 0

    changed = 0
    for paragraph in list(body.findall("w:p", namespaces=_NS)):
        if paragraph.xpath(".//w:drawing | .//w:pict | .//w:object", namespaces=_NS):
            continue
        text = _element_text(paragraph)
        offsets = _contract_marker_split_offsets(text)
        if not offsets:
            continue
        replacement = _split_paragraph_preserving_runs(paragraph, offsets)
        if len(replacement) <= 1:
            continue
        index = body.index(paragraph)
        body.remove(paragraph)
        for offset, new_paragraph in enumerate(replacement):
            _normalize_contract_paragraph_layout(new_paragraph)
            body.insert(index + offset, new_paragraph)
        changed += 1

    if not changed:
        return 0

    updated_xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    temp_path = docx_path.with_suffix(f"{docx_path.suffix}.tmp")
    try:
        with zipfile.ZipFile(docx_path, "r") as zin, zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                data = updated_xml if name == "word/document.xml" else zin.read(name)
                zout.writestr(name, data)
        _validate_docx_package(temp_path, title="PDF 转换后处理失败")
        temp_path.replace(docx_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        return 0
    return changed


def _contract_marker_split_offsets(text: str) -> list[int]:
    if len(text) < 40:
        return []
    offsets: list[int] = []
    # Split only after strong punctuation to avoid matching dates, amounts,
    # legal references or "第 1 种方式" itself.
    patterns = (
        r"(?<=[：:。；;])(?=\d{1,2}[\.．、]\s*[^\d\s])",
        r"(?<=[：:。；;])(?=[一二三四五六七八九十]{1,3}、)",
        r"(?<=[：:。；;])(?=（[一二三四五六七八九十]{1,3}）)",
    )
    for pattern in patterns:
        offsets.extend(match.start() for match in re.finditer(pattern, text))
    cleaned = sorted({offset for offset in offsets if 8 <= offset <= len(text) - 8})
    return cleaned[:8]


def _split_paragraph_preserving_runs(paragraph: etree._Element, offsets: list[int]) -> list[etree._Element]:
    text = _element_text(paragraph)
    boundaries = [0] + [offset for offset in offsets if 0 < offset < len(text)] + [len(text)]
    if len(boundaries) <= 2:
        return [paragraph]

    pieces: list[etree._Element] = []
    for start, end in zip(boundaries, boundaries[1:]):
        if start >= end:
            continue
        new_paragraph = _new_paragraph(paragraph)
        _clear_paragraph_runs(new_paragraph)
        for run in _runs_for_text_slice(paragraph, start, end):
            new_paragraph.append(run)
        if _compact_text(_element_text(new_paragraph)):
            pieces.append(new_paragraph)
    return pieces or [paragraph]


def _runs_for_text_slice(paragraph: etree._Element, start: int, end: int) -> list[etree._Element]:
    runs: list[etree._Element] = []
    position = 0
    for run in paragraph.findall("w:r", namespaces=_NS):
        text_nodes = run.findall("w:t", namespaces=_NS)
        if not text_nodes:
            continue
        run_text = "".join(node.text or "" for node in text_nodes)
        run_start = position
        run_end = position + len(run_text)
        position = run_end
        overlap_start = max(start, run_start)
        overlap_end = min(end, run_end)
        if overlap_end <= overlap_start:
            continue
        sliced_text = run_text[overlap_start - run_start : overlap_end - run_start]
        if not sliced_text:
            continue
        new_run = etree.Element(_w("r"))
        rpr = run.find("w:rPr", namespaces=_NS)
        if rpr is not None:
            new_run.append(copy.deepcopy(rpr))
        text_el = etree.SubElement(new_run, _w("t"))
        if sliced_text.startswith(" ") or sliced_text.endswith(" ") or "  " in sliced_text:
            text_el.set(f"{{{_XML_NS}}}space", "preserve")
        text_el.text = sliced_text
        runs.append(new_run)
    if not runs:
        template = _first_run_in_paragraph(paragraph)
        runs.append(_make_text_run(text[start:end], run_template=template))
    return runs


def _looks_like_scanned_pdf(pdf_path: Path, *, max_pages: int = 3, min_chars: int = 20) -> bool:
    try:
        import fitz  # PyMuPDF
    except Exception as exc:  # pragma: no cover - depends on runtime environment
        raise DocumentIngestError(
            code="CONVERTER_NOT_AVAILABLE",
            title="文件转换组件未安装",
            user_message="当前服务暂不支持 PDF 解析，请联系管理员安装 PyMuPDF 后再试，或上传 .docx 文件。",
            detail=f"PyMuPDF import failed: {exc}",
        ) from exc

    try:
        with fitz.open(str(pdf_path)) as doc:
            if doc.needs_pass:
                raise DocumentIngestError(
                    code="ENCRYPTED_PDF_NOT_SUPPORTED",
                    title="加密 PDF 暂不支持",
                    user_message="该 PDF 需要密码或受到权限保护，请解除加密后再上传。",
                    detail="PDF is encrypted or permission protected.",
                )
            page_count = min(max_pages, doc.page_count)
            extracted = []
            for index in range(page_count):
                extracted.append(doc.load_page(index).get_text("text") or "")
            text = "\n".join(extracted).strip()
            return len(text) < min_chars
    except DocumentIngestError:
        raise
    except Exception as exc:
        raise DocumentIngestError(
            code="PDF_READ_FAILED",
            title="PDF 读取失败",
            user_message="无法读取该 PDF 文件，请确认文件未损坏后重试。",
            detail=str(exc),
        ) from exc


def _clean_executable_path(value: str | None) -> str:
    return str(value or "").strip().strip('"').strip("'")


def _expand_libreoffice_candidate(value: str | None) -> list[str]:
    raw = _clean_executable_path(value)
    if not raw:
        return []

    candidates = [raw]
    raw_path = Path(raw)
    # Allow users to provide /Applications/LibreOffice.app or LIBREOFFICE_HOME
    # instead of the exact soffice executable.
    if raw.lower().endswith(".app") or (raw_path / "Contents" / "MacOS").exists():
        candidates.append(str(raw_path / "Contents" / "MacOS" / "soffice"))
    candidates.extend(
        [
            str(raw_path / "program" / "soffice.exe"),
            str(raw_path / "program" / "soffice"),
            str(raw_path / "Contents" / "MacOS" / "soffice"),
            str(raw_path / "soffice"),
            str(raw_path / "libreoffice"),
        ]
    )
    return candidates


def _is_executable_file(path: str) -> bool:
    if not path:
        return False
    candidate = Path(path).expanduser()
    if not candidate.exists() or not candidate.is_file():
        return False
    if os.name == "nt":
        return True
    return os.access(candidate, os.X_OK)


def _find_libreoffice_binary() -> str | None:
    """Find a LibreOffice executable in server, desktop and local-dev setups.

    The previous implementation only checked PATH. That misses the common
    macOS installation at /Applications/LibreOffice.app/... when FastAPI is
    started from an IDE, launchctl, npm, Docker entrypoint, or another process
    with a trimmed PATH.
    """

    candidates: list[str] = []
    for env_name in ("LIBREOFFICE_PATH", "SOFFICE_PATH", "LIBREOFFICE_BINARY", "LIBREOFFICE_HOME"):
        candidates.extend(_expand_libreoffice_candidate(os.getenv(env_name)))

    for executable in ("soffice", "libreoffice"):
        found = shutil.which(executable)
        if found:
            candidates.append(found)

    candidates.extend(
        [
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            "/opt/homebrew/bin/soffice",
            "/opt/homebrew/bin/libreoffice",
            "/usr/local/bin/soffice",
            "/usr/local/bin/libreoffice",
            "/usr/bin/soffice",
            "/usr/bin/libreoffice",
            "/usr/local/lib/libreoffice/program/soffice",
            "/usr/lib/libreoffice/program/soffice",
            "/snap/bin/libreoffice",
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
    )

    seen: set[str] = set()
    for candidate in candidates:
        normalized = _clean_executable_path(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if _is_executable_file(normalized):
            return str(Path(normalized).expanduser())
    return None


def is_valid_docx_file(path: str | Path) -> bool:
    """Return True only for a readable DOCX package.

    DOCX files are ZIP packages. A non-empty file with a .docx suffix is not
    enough: if a PDF/error page/corrupt conversion output slips through, the
    browser-side docx-preview/JSZip renderer fails with
    "Can't find end of central directory". Keeping this validation at the
    ingest boundary prevents invalid files from entering normal review flows.
    """

    try:
        _validate_docx_package(Path(path), title="DOCX 文件无效")
        return True
    except DocumentIngestError:
        return False


def _ensure_docx_created(path: Path, title: str) -> None:
    _validate_docx_package(path, title=title)


def _validate_docx_package(path: Path, *, title: str) -> None:
    if not path.exists() or path.stat().st_size <= 0:
        raise DocumentIngestError(
            code="CONVERSION_OUTPUT_MISSING",
            title=title,
            user_message="转换后的 Word 文档未生成，请稍后重试或改传 .docx 文件。",
            detail=f"Expected DOCX was not created: {path}",
        )

    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile as exc:
        raise DocumentIngestError(
            code="INVALID_DOCX_PACKAGE",
            title=title,
            user_message="生成的 Word 文档不是有效的 DOCX 文件，请改传 .docx 文件或重新上传后再试。",
            detail=f"Invalid DOCX zip package: {path}: {exc}",
        ) from exc
    except Exception as exc:
        raise DocumentIngestError(
            code="DOCX_VALIDATION_FAILED",
            title=title,
            user_message="无法校验转换后的 Word 文档，请稍后重试或改传 .docx 文件。",
            detail=str(exc),
        ) from exc

    required = {"[Content_Types].xml", "word/document.xml"}
    missing = sorted(required - names)
    if missing:
        raise DocumentIngestError(
            code="INVALID_DOCX_PACKAGE",
            title=title,
            user_message="生成的 Word 文档结构不完整，请改传 .docx 文件或重新上传后再试。",
            detail=f"DOCX package missing required entries: {', '.join(missing)}",
        )
