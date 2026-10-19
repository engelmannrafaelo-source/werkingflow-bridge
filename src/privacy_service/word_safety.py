"""Sanitize Word sources before Writer can expose private or external content."""

import io
import logging
import posixpath
import shutil
import zipfile
from dataclasses import dataclass
from urllib.parse import urlsplit

from lxml import etree

from .word_provenance import (
    MC,
    Provenance,
    _Stamper,
    header_footer_uses,
    new_provenance,
    note_references,
    part_target,
    record_text,
    stamp_header_footer,
    stamp_part,
)
from .word_symbols import SYMBOL_UNICODE

logger = logging.getLogger("privacy-service.word-safety")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"w": W}
EXTERNAL_IMAGE_NOTICE = "[Extern verlinktes Bild nicht geladen]"
MISSING_IMAGE_NOTICE = "[Eingebettetes Bild nicht im Dokument gefunden, nicht geladen]"


def safe_link(url: str) -> bool:
    """Only explicit web/mail URLs and document-local anchors are active."""
    if any(ord(char) < 32 for char in url):
        return False
    return url.startswith("#") or urlsplit(url).scheme.lower() in {
        "http",
        "https",
        "mailto",
    }


# Budgets apply before any member is inflated, independently of compressed size.
# Writer keeps every referenced part in memory (measured: base ~160 MB plus
# about 2x the part; 200 MiB part -> 582 MB, 950 MiB part -> 2.07 GB), so the
# total stays well under the 2-GiB worker ceiling even with two conversions.
MAX_WORD_ENTRIES = 10_000
MAX_WORD_EXPANDED = 256 * 1024 * 1024
MAX_WORD_XML_PART = 50 * 1024 * 1024
MAX_WORD_XML_TOTAL = 100 * 1024 * 1024
# A large member that inflated by more than this ratio is a compression bomb,
# not content: real images/embeddings are stored nearly incompressible.
MAX_WORD_RATIO_FREE = 10 * 1024 * 1024
MAX_WORD_RATIO = 100
# Writer also decodes every referenced picture: about 4 bytes per pixel and
# cumulative (measured: 10000^2 -> 482 MB, 6 x 6000^2 -> 823 MB, 30000^2 ->
# 1.07 GB from a few KB). Pictures beyond these limits are replaced by a
# visible notice before Writer runs; the rest of the document stays.
MAX_WORD_IMAGE_PIXELS = 64_000_000
MAX_WORD_IMAGE_PIXELS_TOTAL = 192_000_000

WORD_MAIN_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
    "application/vnd.ms-word.document.macroEnabled.main+xml",
    "application/vnd.ms-word.template.macroEnabledTemplate.main+xml",
}
CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"


def _main_part(types, rels) -> str | None:
    """Resolve the main document via the package relationship and content type."""
    for rel in rels:
        if (
            rel.get("Type", "").endswith("/officeDocument")
            and rel.get("TargetMode") != "External"
        ):
            name = part_target("", rel.get("Target", ""))
            if name is None:
                return None
            override = {
                n.get("PartName"): n.get("ContentType")
                for n in types
                if n.tag == CT + "Override"
            }
            default = {
                n.get("Extension", "").lower(): n.get("ContentType")
                for n in types
                if n.tag == CT + "Default"
            }
            extension = posixpath.splitext(name)[1][1:].lower()
            kind = override.get("/" + name) or default.get(extension)
            return name if kind in WORD_MAIN_TYPES else None
    return None


def checked_word_package(content: bytes):
    """Return a bounded OOXML Word package; reject other ZIP payloads."""
    from .document_converter import DocumentTooLargeError

    try:
        source = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError(
            "Kein Word-Dokument: gültiges OOXML-Paket erforderlich"
        ) from exc
    try:
        entries = source.infolist()
        xml_parts = [i for i in entries if i.filename.endswith((".xml", ".rels"))]
        if (
            len(entries) > MAX_WORD_ENTRIES
            or sum(i.file_size for i in entries) > MAX_WORD_EXPANDED
            or sum(i.file_size for i in xml_parts) > MAX_WORD_XML_TOTAL
            or any(i.file_size > MAX_WORD_XML_PART for i in xml_parts)
            or any(
                i.file_size > MAX_WORD_RATIO_FREE
                and i.file_size > MAX_WORD_RATIO * max(i.compress_size, 1)
                for i in entries
            )
        ):
            raise DocumentTooLargeError(
                "Word package exceeds expanded size/entry budget"
            )
        names = source.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Ambiguous Word package: duplicate ZIP entries")
        if not {"[Content_Types].xml", "_rels/.rels"}.issubset(names):
            raise ValueError("Kein Word-Dokument: OOXML Word-Teile fehlen")
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        types = etree.fromstring(source.read("[Content_Types].xml"), parser)
        rels = etree.fromstring(source.read("_rels/.rels"), parser)
        main = _main_part(types, rels)
        if main is None or main not in names:
            raise ValueError("Kein Word-Dokument: OOXML Word-Hauptteil fehlt")
        document = etree.fromstring(source.read(main), parser)
        if document.tag != f"{{{W}}}document" or document.find("w:body", NS) is None:
            raise ValueError("Kein Word-Dokument: ungültiger OOXML Word-Hauptteil")
        source.word_main = main
        return source
    except BaseException:
        source.close()
        raise


@dataclass
class PreparedWord:
    content: bytes
    provenance: Provenance


def sanitize_docx(content: bytes) -> bytes:
    """Sanitized package only; see ``prepare_word``."""
    return prepare_word(content).content


def prepare_word(content: bytes) -> PreparedWord:
    """Accept insertions; discard deletions, comments and effectively hidden runs.

    Resolve run/paragraph style inheritance, including docDefaults. External
    images become visible text BEFORE LibreOffice runs, so it cannot fetch them.
    Macros are removed and named; embedded objects become a visible notice
    (Excel workbooks additionally their cell values). Finally every table cell
    and header/footer part is stamped with its own provenance bookmark.
    No ZIP members are extracted to the filesystem.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    with checked_word_package(content) as source:
        main = source.word_main
        names = source.namelist()
        parts = {
            name: source.read(name)
            for name in names
            if name.endswith((".xml", ".rels"))
        }
    provenance = new_provenance(parts)
    stamper = _Stamper(provenance)
    pictures = _PictureBudget(content, set(names))
    dropped = _drop_macros(parts, main, provenance)
    styles = {}
    defaults = {}
    if "word/styles.xml" in parts:
        root = etree.fromstring(parts["word/styles.xml"], parser)
        styles = {s.get(f"{{{W}}}styleId"): s for s in root.findall("w:style", NS)}
        defaults = _visibility(root.find("w:docDefaults/w:rPrDefault/w:rPr", NS))

    def style_flags(style_id, seen=None):
        seen = set() if seen is None else seen
        if style_id in seen:
            raise ValueError("Cyclic Word style inheritance")
        if style_id not in styles:
            return {}
        seen.add(style_id)
        style = styles[style_id]
        parent = style.find("w:basedOn", NS)
        flags = (
            style_flags(parent.get(f"{{{W}}}val"), seen) if parent is not None else {}
        )
        flags.update(_visibility(style.find("w:rPr", NS)))
        return flags

    default_styles = [
        key for key, s in styles.items() if s.get(f"{{{W}}}default") == "1"
    ]
    for key in default_styles:
        defaults.update(style_flags(key))

    main_rels_name = _rels_name(main)
    main_root = etree.fromstring(parts[main], parser)
    references = note_references(main_root)
    uses = {}
    if main_rels_name in parts:
        uses = header_footer_uses(
            main_root, etree.fromstring(parts[main_rels_name], parser), main
        )

    # Every XML part, not only word/*: OPC lets the main document and its
    # parts live under any name, and the relationships decide what Writer loads.
    for name, raw in list(parts.items()):
        if not name.endswith(".xml") or name == "[Content_Types].xml":
            continue
        root = etree.fromstring(raw, parser)
        # Empty comment parts as well as removing references: nothing downstream
        # can accidentally reintroduce comment authors or bodies.
        if root.tag == f"{{{W}}}comments":
            for child in list(root):
                root.remove(child)
        # A row/cell deletion changes structure, not just run visibility.
        for node in root.xpath(
            "//w:tr[w:trPr/w:del] | //w:tc[w:tcPr/w:cellDel]", namespaces=NS
        ):
            node.getparent().remove(node)
        _drop_empty_geometry(root)
        for node in root.xpath(
            "//w:del | //w:moveFrom | //w:commentReference | //w:commentRangeStart"
            " | //w:commentRangeEnd",
            namespaces=NS,
        ):
            node.getparent().remove(node)
        for symbol in root.findall(".//w:sym", NS):
            code = int(symbol.get(f"{{{W}}}char"), 16)
            font = symbol.get(f"{{{W}}}font", "")
            if font.lower() == "symbol":
                value = SYMBOL_UNICODE.get(code - 0xF000 if code >= 0xF000 else code)
            else:
                value = chr(code) if not (0xE000 <= code <= 0xF8FF) else None
            replacement = etree.Element(f"{{{W}}}t")
            replacement.text = (
                value
                or f"[Symbol aus {font}: U+{code:04X} nicht als Unicode verfügbar]"
            )
            symbol.getparent().replace(symbol, replacement)
        for run in list(root.findall(".//w:r", NS)):
            flags = dict(defaults)
            paragraph = next(
                (p for p in run.iterancestors() if p.tag == f"{{{W}}}p"), None
            )
            if paragraph is not None:
                style = paragraph.find("w:pPr/w:pStyle", NS)
                if style is not None:
                    flags.update(style_flags(style.get(f"{{{W}}}val")))
            style = run.find("w:rPr/w:rStyle", NS)
            if style is not None:
                flags.update(style_flags(style.get(f"{{{W}}}val")))
            flags.update(_visibility(run.find("w:rPr", NS)))
            if any(flags.values()):
                run.getparent().remove(run)
        for node in root.xpath("//w:ins | //w:moveTo", namespaces=NS):
            parent = node.getparent()
            index = parent.index(node)
            for child in list(node):
                parent.insert(index, child)
                index += 1
            parent.remove(node)

        rel_name = _rels_name(name)
        if rel_name in parts:
            rels = etree.fromstring(parts[rel_name], parser)
            blocked_images = {}
            blocked_links = set()
            for rel in list(rels):
                kind, target = rel.get("Type", ""), rel.get("Target", "")
                if kind.endswith("/image") and rel.get("TargetMode") == "External":
                    blocked_images[rel.get("Id")] = EXTERNAL_IMAGE_NOTICE
                    rels.remove(rel)
                elif kind.endswith("/image") and (
                    notice := pictures.refuse(name, target)
                ):
                    blocked_images[rel.get("Id")] = notice
                    provenance.notices.append(notice.strip("[]"))
                    rels.remove(rel)
                elif kind.endswith("/hyperlink") and not safe_link(target):
                    blocked_links.add(rel.get("Id"))
                    rels.remove(rel)
            for node in list(root.iter()):
                refs = {v for k, v in node.attrib.items() if k.startswith(f"{{{R}}}")}
                if refs & blocked_images.keys():
                    # Replace the containing drawing/pict, not a whole run that
                    # may also hold legitimate text.
                    picture = next(
                        (
                            p
                            for p in [node, *node.iterancestors()]
                            if p.tag in {f"{{{W}}}drawing", f"{{{W}}}pict"}
                        ),
                        None,
                    )
                    if picture is None:
                        # e.g. a page background fill: the relationship is
                        # already gone, so Writer cannot load it; say so.
                        logger.warning(
                            "Blocked Word image outside drawing/pict: %s", node.tag
                        )
                        provenance.notices.append(
                            "Bild außerhalb einer Zeichnung nicht geladen"
                        )
                    elif picture.getparent() is not None:
                        text = etree.Element(f"{{{W}}}t")
                        text.text = blocked_images[
                            next(iter(refs & blocked_images.keys()))
                        ]
                        picture.getparent().replace(picture, text)
                if node.tag == f"{{{W}}}hyperlink" and refs & blocked_links:
                    parent = node.getparent()
                    index = parent.index(node)
                    for child in list(node):
                        parent.insert(index, child)
                        index += 1
                    parent.remove(node)
            _replace_embedded_objects(content, name, root, rels, provenance)
            parts[rel_name] = etree.tostring(
                rels, xml_declaration=True, encoding="UTF-8"
            )
        stamp_part(provenance, stamper, name, root)
        if root.tag in {f"{{{W}}}hdr", f"{{{W}}}ftr"}:
            stamp_header_footer(provenance, stamper, name, root, uses.get(name, []))
        record_text(
            provenance, name, root, uses.get(name, []), references, stamper
        )
        parts[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    output = io.BytesIO()
    with (
        checked_word_package(content) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            if info.filename in dropped:
                continue
            if info.filename in parts:
                target.writestr(info, parts[info.filename])
            else:
                # Binary members stay out of the in-memory XML working set.
                # Stream in bounded chunks, validating ZIP CRC on the way.
                with source.open(info) as reader, target.open(info, "w") as writer:
                    shutil.copyfileobj(reader, writer, length=64 * 1024)
    return PreparedWord(output.getvalue(), provenance)


class _PictureBudget:
    """Pixel sizes from picture headers only; nothing is decoded here."""

    def __init__(self, content: bytes, names: set[str]):
        self.content = content
        self.names = names
        self.admitted: set[str] = set()
        self.total = 0

    def _size(self, member: str):
        import warnings

        from PIL import Image, UnidentifiedImageError

        try:
            with (
                zipfile.ZipFile(io.BytesIO(self.content)) as package,
                package.open(member) as raw,
                warnings.catch_warnings(
                    action="ignore", category=Image.DecompressionBombWarning
                ),
                Image.open(raw) as image,
            ):
                return image.size
        except Image.DecompressionBombError:
            return self._png_size(member)  # beyond Pillow's own ceiling
        except (UnidentifiedImageError, OSError, ValueError, zipfile.BadZipFile):
            return ()  # not a raster header Pillow knows: Writer decides as before

    def _png_size(self, member: str):
        with (
            zipfile.ZipFile(io.BytesIO(self.content)) as package,
            package.open(member) as raw,
        ):
            head = raw.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return int.from_bytes(head[16:20], "big"), int.from_bytes(
                head[20:24], "big"
            )
        return None

    def refuse(self, source: str, target: str) -> str | None:
        member = part_target(source, target, self.names)
        if member is None:
            # Unresolvable for us means unbudgeted for Writer: never pass it on.
            logger.warning("Word picture target not in package, not loaded: %r", target)
            return MISSING_IMAGE_NOTICE
        if member in self.admitted:
            return None
        size = self._size(member)
        if size == ():
            return None
        pixels = size[0] * size[1] if size else None
        label = f"{size[0]}×{size[1]} Pixel" if size else "über Pillows Grenze"
        if pixels is None or pixels > MAX_WORD_IMAGE_PIXELS:
            logger.warning(
                "Word picture over pixel budget, not loaded: %s (%s)", member, label
            )
            return f"[Eingebettetes Bild zu groß ({label}), nicht geladen]"
        if self.total + pixels > MAX_WORD_IMAGE_PIXELS_TOTAL:
            logger.warning(
                "Word pictures over total pixel budget, not loaded: %s", member
            )
            return (
                "[Eingebettetes Bild nicht geladen: Bildbudget des Dokuments "
                f"erschöpft ({label})]"
            )
        self.admitted.add(member)
        self.total += pixels
        return None


def _rels_name(name: str) -> str:
    return posixpath.join(
        posixpath.dirname(name), "_rels", posixpath.basename(name) + ".rels"
    )


def _drop_empty_geometry(root) -> None:
    """Accepting tracked deletions can empty a row or table: drop just that."""
    for table in list(root.iter(f"{{{W}}}tbl")):
        for row in list(table.iter(f"{{{W}}}tr")):
            if row.find(f".//{{{W}}}tc") is None:
                logger.warning("Word table row empty after tracked deletions: removed")
                row.getparent().remove(row)
        if table.find(f".//{{{W}}}tr") is None and table.getparent() is not None:
            logger.warning("Word table empty after tracked deletions: removed")
            parent = table.getparent()
            parent.remove(table)
            if parent.tag == f"{{{W}}}tc" and parent.find(f"{{{W}}}p") is None:
                parent.append(etree.Element(f"{{{W}}}p"))


MACRO_RELS = (
    "/vbaProject",
    "/wordVbaData",
    "/keyMapCustomizations",
    "/attachedToolbars",
)
MACRO_NOTICE = (
    "[Hinweis: Das Dokument enthielt Makros (VBA); sie wurden entfernt und nicht "
    "ausgeführt.]"
)


def _drop_macros(
    parts: dict[str, bytes], main: str, provenance: Provenance
) -> set[str]:
    """Remove VBA projects before Writer opens the package; name the removal."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    rel_name = _rels_name(main)
    if rel_name not in parts:
        return set()
    rels = etree.fromstring(parts[rel_name], parser)
    dropped, removed = set(), False
    for rel in list(rels):
        if (
            rel.get("Type", "").endswith(MACRO_RELS)
            and rel.get("TargetMode") != "External"
        ):
            target = part_target(main, rel.get("Target", ""))
            if target is not None:
                dropped |= {target, _rels_name(target)}
            rels.remove(rel)
            removed = True
    if not removed:
        return set()
    parts[rel_name] = etree.tostring(rels, xml_declaration=True, encoding="UTF-8")
    types = etree.fromstring(parts["[Content_Types].xml"], parser)
    for node in list(types):
        if (
            node.tag == CT + "Override"
            and node.get("PartName", "").lstrip("/") in dropped
        ):
            types.remove(node)
    parts["[Content_Types].xml"] = etree.tostring(
        types, xml_declaration=True, encoding="UTF-8"
    )
    for name in dropped:
        parts.pop(name, None)
    logger.warning("Word macros removed before conversion: %s", sorted(dropped))
    provenance.notices.append("Makros (VBA) entfernt, nicht ausgeführt")
    provenance.macros_removed = True
    return dropped


OFFICE = "urn:schemas-microsoft-com:office:office"
MAX_EMBEDDED_ROWS = 200
MAX_EMBEDDED_COLUMNS = 30
MAX_EMBEDDED_EXPANDED = 32 * 1024 * 1024


def _replace_embedded_objects(
    content: bytes, name: str, root, rels, provenance
) -> None:
    """OLE objects vanish in Writer's HTML; leave a notice and, for Excel, the cells."""
    targets = {
        rel.get("Id"): rel.get("Target", "")
        for rel in rels
        if rel.get("TargetMode") != "External"
    }
    for ole in list(root.iter(f"{{{OFFICE}}}OLEObject")):
        container = next(
            (
                a
                for a in ole.iterancestors()
                if a.tag in {f"{{{W}}}object", f"{{{W}}}pict"}
            ),
            None,
        )
        if container is None or container.getparent() is None:
            continue
        fallback = next(
            (
                a
                for a in container.iterancestors()
                if a.tag == f"{{{MC}}}AlternateContent"
            ),
            None,
        )
        if fallback is not None:
            container = fallback
        if root not in container.iterancestors():
            continue  # already replaced together with its mc:AlternateContent twin
        progid = ole.get("ProgID") or "unbekannt"
        target = targets.get(ole.get(f"{{{R}}}id"))
        cells = None
        if target and target.lower().endswith((".xlsx", ".xlsm")):
            member = part_target(name, target)
            cells = _embedded_workbook_cells(content, member) if member else None
        if cells is None:
            text = f"[Eingebettetes Objekt ({progid}) nicht übernommen]"
        else:
            truncated = cells[1]
            text = (
                f"[Eingebettetes Objekt ({progid}): Tabelleninhalt des aktiven Blatts "
                f"übernommen{', gekürzt' if truncated else ''}; Darstellung nicht "
                "übernommen]"
            )
        logger.warning(
            "Word embedded object replaced: %s (%s)",
            progid,
            "cells" if cells else "notice",
        )
        provenance.notices.append(text.strip("[]"))
        notice = etree.Element(f"{{{W}}}t")
        notice.text = text
        parent = container.getparent()
        if parent.tag != f"{{{W}}}r":
            run = etree.Element(f"{{{W}}}r")
            run.append(notice)
            notice = run
        paragraph = next(
            (a for a in container.iterancestors() if a.tag == f"{{{W}}}p"), None
        )
        parent.replace(container, notice)
        if cells is not None and paragraph is not None:
            table = _cells_table(cells[0])
            paragraph.addnext(table)
            if table.getnext() is None and table.getparent().tag == f"{{{W}}}tc":
                table.addnext(etree.Element(f"{{{W}}}p"))


def _embedded_workbook_cells(content: bytes, member: str):
    """Bounded read of an embedded workbook; any problem means notice only."""
    try:
        from openpyxl import load_workbook

        with zipfile.ZipFile(io.BytesIO(content)) as outer:
            embedded = outer.read(member)
        with zipfile.ZipFile(io.BytesIO(embedded)) as inner:
            infos = inner.infolist()
            if (
                len(infos) > 1000
                or sum(i.file_size for i in infos) > MAX_EMBEDDED_EXPANDED
            ):
                logger.warning("Embedded workbook over budget: %s", member)
                return None
        book = load_workbook(io.BytesIO(embedded), read_only=True, data_only=True)
        try:
            sheet = book.active
            rows, truncated = [], False
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= MAX_EMBEDDED_ROWS:
                    truncated = True
                    break
                if len(row) > MAX_EMBEDDED_COLUMNS:
                    truncated = True
                rows.append(
                    ["" if v is None else str(v) for v in row[:MAX_EMBEDDED_COLUMNS]]
                )
        finally:
            book.close()
    except Exception as exc:  # noqa: BLE001 — local notice instead of whole-document failure
        logger.warning("Embedded workbook not readable (%s): %s", member, exc)
        return None
    while rows and not any(rows[-1]):
        rows.pop()
    width = max((i + 1 for r in rows for i, v in enumerate(r) if v), default=0)
    if not width:
        return None
    return [r[:width] + [""] * (width - len(r[:width])) for r in rows], truncated


def _cells_table(rows):
    table = etree.Element(f"{{{W}}}tbl")
    grid = etree.SubElement(table, f"{{{W}}}tblGrid")
    for _ in rows[0]:
        etree.SubElement(grid, f"{{{W}}}gridCol")
    for values in rows:
        tr = etree.SubElement(table, f"{{{W}}}tr")
        for value in values:
            tc = etree.SubElement(tr, f"{{{W}}}tc")
            run = etree.SubElement(etree.SubElement(tc, f"{{{W}}}p"), f"{{{W}}}r")
            text = etree.SubElement(run, f"{{{W}}}t")
            text.text = value
            text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return table


def _visibility(properties):
    if properties is None:
        return {}
    return {
        key: el.get(f"{{{W}}}val", "true").lower() not in {"0", "false", "off"}
        for key in ("vanish", "webHidden")
        if (el := properties.find(f"w:{key}", NS)) is not None
    }
