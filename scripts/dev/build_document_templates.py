"""Regenerate the blank document templates shipped as package data.

Run from the repo root:

    uv run python scripts/dev/build_document_templates.py

It writes ``blank.docx``, ``blank.xlsx``, ``blank.pptx`` and ``blank.ipynb`` into
``server/src/workbench_server/document_templates/``. Those files are committed and
loaded at runtime by ``services/documents.py`` — this script exists so the opaque
binaries are *reviewable and reproducible*: the XML lives here in the open, and a
reviewer can rebuild the bytes and diff them rather than trust a blob.

Deliberately stdlib-only (``zipfile`` + strings): the whole point of the template
approach is that creating a valid Office document needs **no** python-docx /
openpyxl / python-pptx runtime dependency and no Office install. The parts below
are the minimal set each format's consumer (Word / Excel / PowerPoint) accepts
without a repair prompt; every OOXML zip carries ``[Content_Types].xml`` and the
package relationships that name its main part.
"""

# This module is essentially XML data held as strings: OOXML content-type URIs and
# a colour-map attribute list are single tokens that run well past 100 columns and
# cannot be wrapped without corrupting the document. E501 is off for the file only.
# ruff: noqa: E501

from __future__ import annotations

import json
import zipfile
from pathlib import Path

# A fixed DOS timestamp for every member, so the committed bytes are stable across
# rebuilds (a wall-clock time would make every regeneration a spurious diff).
_FIXED_DATE = (1980, 1, 1, 0, 0, 0)

_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_ODREL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# ---- WordprocessingML (.docx) ----------------------------------------------

_DOCX_CONTENT_TYPES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""

_DOCX_ROOT_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_REL}">
  <Relationship Id="rId1" Type="{_ODREL}/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

_DOCX_DOCUMENT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p/>
    <w:sectPr/>
  </w:body>
</w:document>
"""

_DOCX_PARTS = {
    "[Content_Types].xml": _DOCX_CONTENT_TYPES,
    "_rels/.rels": _DOCX_ROOT_RELS,
    "word/document.xml": _DOCX_DOCUMENT,
}

# ---- SpreadsheetML (.xlsx) --------------------------------------------------

_XLSX_CONTENT_TYPES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>
"""

_XLSX_ROOT_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_REL}">
  <Relationship Id="rId1" Type="{_ODREL}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""

_XLSX_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Sheet1" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
"""

_XLSX_WORKBOOK_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_REL}">
  <Relationship Id="rId1" Type="{_ODREL}/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
"""

_XLSX_SHEET = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData/>
</worksheet>
"""

_XLSX_PARTS = {
    "[Content_Types].xml": _XLSX_CONTENT_TYPES,
    "_rels/.rels": _XLSX_ROOT_RELS,
    "xl/workbook.xml": _XLSX_WORKBOOK,
    "xl/_rels/workbook.xml.rels": _XLSX_WORKBOOK_RELS,
    "xl/worksheets/sheet1.xml": _XLSX_SHEET,
}

# ---- PresentationML (.pptx) -------------------------------------------------
# The most demanding: PowerPoint refuses a presentation that does not resolve a
# slide -> layout -> master -> theme chain, so all four are present with one
# blank slide.

_PPTX_CONTENT_TYPES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
  <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
</Types>
"""

_PPTX_ROOT_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_REL}">
  <Relationship Id="rId1" Type="{_ODREL}/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>
"""

_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"

_PPTX_PRESENTATION = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="{_A_NS}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>
  <p:sldSz cx="9144000" cy="6858000" type="screen4x3"/>
  <p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>
"""

_PPTX_PRESENTATION_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_REL}">
  <Relationship Id="rId1" Type="{_ODREL}/slideMaster" Target="slideMasters/slideMaster1.xml"/>
  <Relationship Id="rId2" Type="{_ODREL}/slide" Target="slides/slide1.xml"/>
  <Relationship Id="rId3" Type="{_ODREL}/theme" Target="theme/theme1.xml"/>
</Relationships>
"""

# An empty shape tree — the one group shape every cSld requires and nothing else.
_EMPTY_SP_TREE = """    <p:spTree>
      <p:nvGrpSpPr>
        <p:cNvPr id="1" name=""/>
        <p:cNvGrpSpPr/>
        <p:nvPr/>
      </p:nvGrpSpPr>
      <p:grpSpPr>
        <a:xfrm>
          <a:off x="0" y="0"/>
          <a:ext cx="0" cy="0"/>
          <a:chOff x="0" y="0"/>
          <a:chExt cx="0" cy="0"/>
        </a:xfrm>
      </p:grpSpPr>
    </p:spTree>"""

_PPTX_SLIDE_MASTER = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="{_A_NS}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}">
  <p:cSld>
    <p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>
{_EMPTY_SP_TREE}
  </p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst>
    <p:sldLayoutId id="2147483649" r:id="rId1"/>
  </p:sldLayoutIdLst>
  <p:txStyles>
    <p:titleStyle/>
    <p:bodyStyle/>
    <p:otherStyle/>
  </p:txStyles>
</p:sldMaster>
"""

_PPTX_SLIDE_MASTER_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_REL}">
  <Relationship Id="rId1" Type="{_ODREL}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="{_ODREL}/theme" Target="../theme/theme1.xml"/>
</Relationships>
"""

_PPTX_SLIDE_LAYOUT = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="{_A_NS}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}" type="blank" preserve="1">
  <p:cSld name="Blank">
{_EMPTY_SP_TREE}
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>
"""

_PPTX_SLIDE_LAYOUT_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_REL}">
  <Relationship Id="rId1" Type="{_ODREL}/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>
"""

_PPTX_SLIDE = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="{_A_NS}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}">
  <p:cSld>
{_EMPTY_SP_TREE}
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>
"""

_PPTX_SLIDE_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_REL}">
  <Relationship Id="rId1" Type="{_ODREL}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>
"""


def _theme_color(tag: str, srgb: str) -> str:
    return f'<a:{tag}><a:srgbClr val="{srgb}"/></a:{tag}>'


def _ln(width: int) -> str:
    """One line style for the theme's ``lnStyleLst`` (kept short for line length)."""
    return (
        f'<a:ln w="{width}" cap="flat" cmpd="sng" algn="ctr">'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:prstDash val="solid"/></a:ln>'
    )


# The default Office theme: a complete clrScheme / fontScheme / fmtScheme, which
# PowerPoint requires to be present and well-formed even for a blank deck.
_PPTX_THEME = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="{_A_NS}" name="Office Theme">
  <a:themeElements>
    <a:clrScheme name="Office">
      <a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>
      <a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
      {_theme_color("dk2", "44546A")}
      {_theme_color("lt2", "E7E6E6")}
      {_theme_color("accent1", "4472C4")}
      {_theme_color("accent2", "ED7D31")}
      {_theme_color("accent3", "A5A5A5")}
      {_theme_color("accent4", "FFC000")}
      {_theme_color("accent5", "5B9BD5")}
      {_theme_color("accent6", "70AD47")}
      {_theme_color("hlink", "0563C1")}
      {_theme_color("folHlink", "954F72")}
    </a:clrScheme>
    <a:fontScheme name="Office">
      <a:majorFont>
        <a:latin typeface="Calibri Light"/>
        <a:ea typeface=""/>
        <a:cs typeface=""/>
      </a:majorFont>
      <a:minorFont>
        <a:latin typeface="Calibri"/>
        <a:ea typeface=""/>
        <a:cs typeface=""/>
      </a:minorFont>
    </a:fontScheme>
    <a:fmtScheme name="Office">
      <a:fillStyleLst>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
      </a:fillStyleLst>
      <a:lnStyleLst>
        {_ln(6350)}
        {_ln(12700)}
        {_ln(19050)}
      </a:lnStyleLst>
      <a:effectStyleLst>
        <a:effectStyle><a:effectLst/></a:effectStyle>
        <a:effectStyle><a:effectLst/></a:effectStyle>
        <a:effectStyle><a:effectLst/></a:effectStyle>
      </a:effectStyleLst>
      <a:bgFillStyleLst>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
      </a:bgFillStyleLst>
    </a:fmtScheme>
  </a:themeElements>
</a:theme>
"""

_PPTX_PARTS = {
    "[Content_Types].xml": _PPTX_CONTENT_TYPES,
    "_rels/.rels": _PPTX_ROOT_RELS,
    "ppt/presentation.xml": _PPTX_PRESENTATION,
    "ppt/_rels/presentation.xml.rels": _PPTX_PRESENTATION_RELS,
    "ppt/slideMasters/slideMaster1.xml": _PPTX_SLIDE_MASTER,
    "ppt/slideMasters/_rels/slideMaster1.xml.rels": _PPTX_SLIDE_MASTER_RELS,
    "ppt/slideLayouts/slideLayout1.xml": _PPTX_SLIDE_LAYOUT,
    "ppt/slideLayouts/_rels/slideLayout1.xml.rels": _PPTX_SLIDE_LAYOUT_RELS,
    "ppt/slides/slide1.xml": _PPTX_SLIDE,
    "ppt/slides/_rels/slide1.xml.rels": _PPTX_SLIDE_RELS,
    "ppt/theme/theme1.xml": _PPTX_THEME,
}

# ---- Jupyter notebook (.ipynb) — JSON, not a zip ---------------------------
# nbformat v4 skeleton: cells (empty is valid), metadata, and the two version
# integers the reader keys on. Not a template zip — a notebook is a JSON document.
_IPYNB = {
    "cells": [],
    "metadata": {
        "kernelspec": {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        },
        "language_info": {"name": "python"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}


def _write_zip(path: Path, parts: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in parts.items():
            info = zipfile.ZipInfo(name, date_time=_FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, text.encode("utf-8"))


def build(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_zip(out_dir / "blank.docx", _DOCX_PARTS)
    _write_zip(out_dir / "blank.xlsx", _XLSX_PARTS)
    _write_zip(out_dir / "blank.pptx", _PPTX_PARTS)
    # write_bytes, not write_text: text mode translates "\n" to the platform line
    # ending, which would make the committed notebook CRLF on Windows and churn the
    # bytes across a regeneration. LF, always, so the file is reproducible anywhere.
    (out_dir / "blank.ipynb").write_bytes(
        (json.dumps(_IPYNB, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    )


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    out_dir = repo_root / "server" / "src" / "workbench_server" / "document_templates"
    build(out_dir)
    for child in sorted(out_dir.iterdir()):
        if child.name == "__init__.py":
            continue
        print(f"wrote {child.relative_to(repo_root).as_posix()} ({child.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
