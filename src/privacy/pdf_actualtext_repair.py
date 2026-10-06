"""ActualText-Reparatur fuer PDFs vor Docling.

Python-Port von ``apps/werking-report/src/services/docling/pdf-actualtext-reparatur.ts``
(werkingflow-production, Commit 3a7c71190). Semantik und Byte-Ausgabe sind
absichtlich identisch zur TS-Fassung — Paritaet wird in
``tests/unit/test_pdf_actualtext_repair.py`` am echten Muehl-Auszug geprueft.

Befund (Pruefagent-Versuch Muehl, 03.10.2026; Messung 06.10.2026):
Chromium/Skia bettet die variable Schrift Inter als Type3-Font ein. Inter
ersetzt per Kontext-Alternativen Zeichen vor Ziffern und Grossbuchstaben durch
eigene Glyphen (Minus, Halbgeviertstrich, Klammern). Skia schreibt fuer diese
Glyphen im ToUnicode-CMap ``<code> <0000>`` und legt den echten Text als
``/Span <</ActualText ...>> BDC ... EMC`` daneben. Docling liest nur ToUnicode:
aus "-0,87" wird "0,87", aus "10-64" wird "1064".

Reparatur: Content-Streams aller Seiten (und Form-XObjects) lesen, je
``(Font, Glyph-Code)`` den ActualText-Wert sammeln und dort, wo das CMap
``<0000>`` (oder gar nichts) traegt, diesen Wert eintragen. Die geaenderten
CMap-Objekte werden als inkrementelles Update ANGEHAENGT — die Originalbytes
bleiben unberuehrt.

Bewusst eng: nur klassische Xref-Tabellen (kein Xref-Stream), keine
Verschluesselung, nur FlateDecode/ungefilterte Streams, nur Spans mit genau
einem Glyph. Was nicht passt, bleibt unveraendert und bekommt einen ``hint``.

Wirft NIE (wie die TS-Fassung): eine gescheiterte Reparatur darf die
Konvertierung nicht mitreissen. Der Grund steht dann in ``hint`` und MUSS vom
Aufrufer geloggt werden (siehe ``src/main.py::_maybe_repair_pdf_actualtext``).

Hinweis zur Portierung: JS-Regex ``\\s`` ist NICHT Pythons ``\\s`` (Python
zaehlt u. a. \\x1c-\\x1f und \\x85 dazu). Alle Muster verwenden deshalb die
explizite JS-Klasse ``_S`` auf latin1-dekodierten Strings, ``$`` wird als ``\\Z``
geschrieben, ``\\d`` als ``[0-9]``.
"""

from __future__ import annotations

import math
import re
import zlib
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

__all__ = ["ActualTextRepair", "repair_actualtext_glyphs"]


@dataclass
class ActualTextRepair:
    #: PDF fuer den Konverter — bei 0 Reparaturen dasselbe Objekt wie die Eingabe.
    pdf: bytes
    #: Anzahl (Font, Glyph-Code)-Paare, deren Unicode-Wert eingetragen wurde.
    repaired_glyphs: int
    #: Warum eine Reparatur unterblieb oder unvollstaendig ist (nur wenn zutreffend).
    hint: Optional[str] = None


# JS-Whitespace (\s) im latin1-Bereich.
_S_CHARS = "\t\n\x0b\x0c\r \xa0"
_S = f"[{_S_CHARS}]"
_NAMECHAR = f"[^{_S_CHARS}/<>\\[\\]()]"

_WS = frozenset("\0\t\n\f\r ")
_DELIM = frozenset("()<>[]{}/%")

_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "(": "(", ")": ")", "\\": "\\"}
_OCT_RE = re.compile(r"[0-7]{1,3}")
_NONHEX_RE = re.compile(r"[^0-9a-fA-F]")
_REF_RE = re.compile(rf"([0-9]+){_S}+([0-9]+){_S}+R")
_SIMPLE_RE = re.compile(rf"/{_NAMECHAR}+|{_NAMECHAR}+")
_OBJ_RE = re.compile(rf"([0-9]+){_S}+([0-9]+){_S}+obj(?![A-Za-z0-9_])")
_ENDSTREAM_RE = re.compile(rf"{_S}*endstream")
_FLATE_RE = re.compile(rf"\[?{_S}*/FlateDecode{_S}*\]?\Z")
_NUMBER_RE = re.compile(r"[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)\Z")
_ACTUALTEXT_RE = re.compile(rf"/ActualText{_S}*")
_INLINE_EI_RE = re.compile(rf"{_S}EI(?={_S}|\Z)")
_BFRANGE_RE = re.compile(r"beginbfrange([\s\S]*?)endbfrange")
_HEXPAIR_RE = re.compile(rf"<([0-9a-fA-F]+)>{_S}*<([0-9a-fA-F]+)>")
_FONT_ENTRY_RE = re.compile(rf"/({_NAMECHAR}+){_S}+([0-9]+){_S}+[0-9]+{_S}+R")
_XOBJ_ENTRY_RE = re.compile(rf"/{_NAMECHAR}+{_S}+([0-9]+){_S}+[0-9]+{_S}+R")
_ANYREF_RE = re.compile(rf"([0-9]+){_S}+[0-9]+{_S}+R")
_STARTXREF_RE = re.compile(rf"startxref{_S}+([0-9]+)")
_TYPE0_RE = re.compile(rf"/Subtype{_S}*/Type0")
_IDENTITY_RE = re.compile(rf"/Encoding{_S}*/Identity-[HV]")
_FORM_RE = re.compile(rf"/Subtype{_S}*/Form")
_PAGE_RE = re.compile(rf"/Type{_S}*/Page(?![A-Za-z])")
_JS_DECIMAL_RE = re.compile(r"[+-]?(Infinity|([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?)\Z")


def _js_trim(s: str) -> str:
    return s.strip(_S_CHARS + "﻿")


def _js_number(roh: Optional[str]) -> float:
    """``Number(x)`` aus JS fuer die hier vorkommenden Eingaben."""
    if roh is None:
        return math.nan
    t = _js_trim(roh)
    if t == "":
        return 0.0
    low = t.lower()
    try:
        if low.startswith(("0x", "0o", "0b")) and len(t) > 2:
            return float(int(t[2:], {"x": 16, "o": 8, "b": 2}[low[1]]))
    except ValueError:
        return math.nan
    if _JS_DECIMAL_RE.match(t):
        return float(t)
    return math.nan


def _is_finite(x: float) -> bool:
    return not (math.isnan(x) or math.isinf(x))


def _read_literal(s: str, start: int) -> Tuple[str, int]:
    """Zeichenkette (latin1) einer PDF-Literal-Zeichenkette ab ``(``."""
    i = start + 1
    depth = 1
    out: List[str] = []
    n_len = len(s)
    while i < n_len:
        c = s[i]
        if c == "\\":
            n = s[i + 1] if i + 1 < n_len else None
            if n is not None and n in _ESCAPES:
                out.append(_ESCAPES[n])
                i += 2
                continue
            if n is not None and "0" <= n <= "7":
                m = _OCT_RE.match(s[i + 1:i + 4])
                assert m is not None  # n ist eine Oktalziffer
                out.append(chr(int(m.group(0), 8) & 0xFF))
                i += 1 + len(m.group(0))
                continue
            if n == "\r":
                i += 3 if (i + 2 < n_len and s[i + 2] == "\n") else 2
                continue
            if n == "\n":
                i += 2
                continue
            i += 1
            continue
        if c == "(":
            depth += 1
        if c == ")":
            depth -= 1
            if depth == 0:
                return "".join(out), i + 1
        out.append(c)
        i += 1
    return "".join(out), i


def _read_hex(s: str, start: int) -> Tuple[str, int]:
    end = s.find(">", start)
    hx = _NONHEX_RE.sub("", s[start + 1:(len(s) if end < 0 else end)])
    if len(hx) % 2 == 1:
        hx += "0"
    out = "".join(chr(int(hx[k:k + 2], 16)) for k in range(0, len(hx), 2))
    return out, (len(s) if end < 0 else end + 1)


def _dict_end(s: str, start: int) -> int:
    """Ende eines ``<< ... >>`` ab ``start`` (zeigt auf das erste ``<``)."""
    i = start + 2
    depth = 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "(":
            i = _read_literal(s, i)[1]
            continue
        nxt = s[i + 1] if i + 1 < n else ""
        if c == "<" and nxt == "<":
            depth += 1
            i += 2
            continue
        if c == ">" and nxt == ">":
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        if c == "<":
            i = _read_hex(s, i)[1]
            continue
        i += 1
    return n


def _text_string(raw: str) -> str:
    """PDF-Textstring (ActualText): UTF-16BE/LE mit BOM, sonst latin1.

    Einzelne Surrogates bleiben erhalten (wie JS ``String.fromCharCode``).
    """
    if len(raw) >= 2 and ord(raw[0]) == 0xFE and ord(raw[1]) == 0xFF:
        body = raw[2:]
        body = body[: len(body) - (len(body) % 2)]
        return body.encode("latin1").decode("utf-16-be", errors="surrogatepass")
    if len(raw) >= 2 and ord(raw[0]) == 0xFF and ord(raw[1]) == 0xFE:
        body = raw[2:]
        body = body[: len(body) - (len(body) % 2)]
        return body.encode("latin1").decode("utf-16-le", errors="surrogatepass")
    return raw


def _value(d: str, key: str) -> Optional[str]:
    """Wert eines Dict-Schluessels als Rohtext (Referenz, Name, Zahl, Dict, Array)."""
    m = re.search(rf"/{re.escape(key)}(?![A-Za-z0-9#_.\-]){_S}*", d)
    if not m:
        return None
    i = m.end()
    ref = _REF_RE.match(d[i:i + 32])
    if ref:
        return ref.group(0)
    if d.startswith("<<", i):
        return d[i:_dict_end(d, i)]
    if i < len(d) and d[i] == "[":
        end = d.find("]", i)
        return d[i:(len(d) if end < 0 else end + 1)]
    simple = _SIMPLE_RE.match(d, i)
    return simple.group(0) if simple else None


def _ref_number(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    m = _REF_RE.match(raw)
    return int(m.group(1)) if m else None


@dataclass
class _PdfObject:
    dict: str
    stream: Optional[bytes] = None


class _PdfReader:
    def __init__(self, buf: bytes) -> None:
        self._buf = buf
        self.text = buf.decode("latin1")
        self._offsets: Dict[int, Tuple[int, int]] = {}
        self._cache: Dict[int, Optional[_PdfObject]] = {}
        s = self.text
        for m in _OBJ_RE.finditer(s):
            before = "\n" if m.start() == 0 else s[m.start() - 1]
            if before not in _WS:
                continue
            # Spaetere Definition gewinnt (inkrementelle Updates).
            self._offsets[int(m.group(1))] = (m.end(), int(m.group(2)))

    @property
    def numbers(self) -> List[int]:
        return list(self._offsets.keys())

    def generation(self, nr: int) -> int:
        e = self._offsets.get(nr)
        return e[1] if e else 0

    def obj(self, nr: int) -> Optional[_PdfObject]:
        if nr in self._cache:
            return self._cache[nr]
        entry = self._offsets.get(nr)
        result: Optional[_PdfObject] = None
        s = self.text
        n = len(s)
        if entry:
            i = entry[0]
            while i < n and s[i] in _WS:
                i += 1
            if s.startswith("<<", i):
                end = _dict_end(s, i)
                d = s[i:end]
                result = _PdfObject(dict=d)
                j = end
                while j < n and s[j] in _WS:
                    j += 1
                if s.startswith("stream", j):
                    start = j + 6
                    if start < n and s[start] == "\r":
                        start += 1
                    if start < n and s[start] == "\n":
                        start += 1
                    length_raw = _value(d, "Length")
                    length_ref = _ref_number(length_raw)
                    length = (
                        _js_number(self._raw(length_ref)) if length_ref is not None
                        else _js_number(length_raw)
                    )
                    end2 = start + length if _is_finite(length) and length >= 0 else -1
                    if end2 >= 0:
                        e2 = math.trunc(end2)
                        ok = _ENDSTREAM_RE.match(s[e2:e2 + 20]) is not None
                    else:
                        ok = False
                    if not ok:
                        e2 = s.find("endstream", start)
                        if e2 < 0:
                            e2 = n
                    result.stream = self._buf[start:e2]
            else:
                raw = self._raw(nr)
                result = _PdfObject(dict=raw if raw is not None else "")
        self._cache[nr] = result
        return result

    def _raw(self, nr: int) -> Optional[str]:
        """Rohtext eines Nicht-Dict-Objekts (z. B. eine indirekte Laenge)."""
        entry = self._offsets.get(nr)
        if not entry:
            return None
        end = self.text.find("endobj", entry[0])
        return _js_trim(self.text[entry[0]:(None if end < 0 else end)])

    def resolve_dict(self, raw: Optional[str]) -> Optional[str]:
        """Referenz aufloesen oder Inline-Dict zurueckgeben."""
        if not raw:
            return None
        nr = _ref_number(raw)
        if nr is not None:
            o = self.obj(nr)
            return o.dict if o else None
        return raw if raw.startswith("<<") else None

    def stream_text(self, o: Optional[_PdfObject]) -> Optional[str]:
        """Dekodierter Stream (FlateDecode oder ungefiltert), sonst None."""
        # JS: ein leerer Buffer ist truthy — nur "kein Stream" bricht ab.
        if o is None or o.stream is None:
            return None
        flt = _value(o.dict, "Filter")
        if _value(o.dict, "DecodeParms"):
            return None
        if not flt:
            return o.stream.decode("latin1")
        if not _FLATE_RE.match(flt):
            return None
        try:
            return zlib.decompress(o.stream).decode("latin1")
        except zlib.error:
            # Kaputter Stream heisst nur "hier keine Reparatur" (wie TS).
            return None


@dataclass
class _Token:
    t: str  # 'zahl' | 'name' | 'str' | 'dict' | 'arr' | 'arrStart'
    v: str
    items: Optional[List["_Token"]] = None


@dataclass
class _Span:
    text: str
    glyphs: List[Tuple[Optional[int], str]]


def _scan_content(
    content: str,
    fonts: Dict[str, int],
    bytes_per_code: Callable[[int], int],
    collect: Callable[[int, str, str], None],
) -> None:
    """Content-Stream durchlaufen und ActualText-Spans mit genau einem Glyph sammeln."""
    ops: List[_Token] = []
    spans: List[Optional[_Span]] = []
    font: Optional[int] = None
    i = 0
    n = len(content)

    def open_span() -> Optional[_Span]:
        for k in range(len(spans) - 1, -1, -1):
            if spans[k]:
                return spans[k]
        return None

    def glyph(tok: Optional[_Token]) -> None:
        span = open_span()
        if not span or not tok:
            return
        if tok.t == "str":
            span.glyphs.append((font, tok.v))
        if tok.t == "arr":
            for it in tok.items or []:
                if it.t == "str":
                    span.glyphs.append((font, it.v))

    while i < n:
        c = content[i]
        if c in _WS:
            i += 1
            continue
        if c == "%":
            while i < n and content[i] != "\n" and content[i] != "\r":
                i += 1
            continue
        if c == "(":
            v, i = _read_literal(content, i)
            ops.append(_Token("str", v))
            continue
        if c == "<":
            if i + 1 < n and content[i + 1] == "<":
                e = _dict_end(content, i)
                ops.append(_Token("dict", content[i:e]))
                i = e
                continue
            v, i = _read_hex(content, i)
            ops.append(_Token("str", v))
            continue
        if c == "[":
            ops.append(_Token("arrStart", ""))
            i += 1
            continue
        if c == "]":
            items: List[_Token] = []
            while ops and ops[-1].t != "arrStart":
                items.insert(0, ops.pop())
            if ops:
                ops.pop()
            ops.append(_Token("arr", "", items))
            i += 1
            continue
        if c == "/":
            j = i + 1
            while j < n and content[j] not in _WS and content[j] not in _DELIM:
                j += 1
            ops.append(_Token("name", content[i + 1:j]))
            i = j
            continue
        if c in ">){}":
            i += 1
            continue
        j = i
        while j < n and content[j] not in _WS and content[j] not in _DELIM:
            j += 1
        word = content[i:j]
        i = j
        if _NUMBER_RE.match(word):
            ops.append(_Token("zahl", word))
            continue

        if word == "Tf":
            name = ops[-2] if len(ops) >= 2 else None
            font = fonts.get(name.v) if (name is not None and name.t == "name") else None
        elif word == "BDC":
            props = ops[-1] if ops else None
            text: Optional[str] = None
            if props is not None and props.t == "dict":
                m = _ACTUALTEXT_RE.search(props.v)
                if m:
                    k = m.end()
                    ch = props.v[k] if k < len(props.v) else None
                    raw: Optional[str] = None
                    if ch == "(":
                        raw = _read_literal(props.v, k)[0]
                    elif ch == "<":
                        raw = _read_hex(props.v, k)[0]
                    if raw is not None:
                        text = _text_string(raw)
            spans.append(_Span(text, []) if text is not None else None)
        elif word == "BMC":
            spans.append(None)
        elif word == "EMC":
            span = spans.pop() if spans else None
            if span and len(span.glyphs) > 0:
                f = span.glyphs[0][0]
                all_bytes = "".join(g[1] for g in span.glyphs)
                if f is not None and all(g[0] == f for g in span.glyphs):
                    width = bytes_per_code(f)
                    if width > 0 and len(all_bytes) == width:
                        hx = "".join(f"{ord(b):02x}" for b in all_bytes)
                        collect(f, hx.upper(), span.text)
        elif word in ("Tj", "'", '"', "TJ"):
            glyph(ops[-1] if ops else None)
        elif word == "ID":
            # Inline-Bild: Binaerdaten bis EI ueberspringen.
            m = _INLINE_EI_RE.search(content, i)
            i = n if m is None else m.start() + 3
        ops.clear()


def _utf16_hex(text: str) -> str:
    out: List[str] = []
    for ch in text:
        cp = ord(ch)
        if cp > 0xFFFF:
            v = cp - 0x10000
            out.append(f"{0xD800 + (v >> 10):04x}{0xDC00 + (v & 0x3FF):04x}")
        else:
            out.append(f"{cp:04x}")
    return "".join(out)


def _patch_cmap(cmap: str, entries: Dict[str, str], hex_width: int) -> Tuple[str, int]:
    """CMap-Eintraege einer (Font, Code)-Tabelle einsetzen. Gibt neuen Text + Zahl zurueck."""
    text = cmap
    count = 0
    ranges: List[Tuple[int, int]] = []
    for block in _BFRANGE_RE.finditer(cmap):
        for m in _HEXPAIR_RE.finditer(block.group(1)):
            ranges.append((int(m.group(1), 16), int(m.group(2), 16)))
    new: List[str] = []
    for code, chars in entries.items():
        if len(code) != hex_width:
            continue
        target = _utf16_hex(chars)
        if not target:
            continue
        target = target.upper()
        null_entry = re.compile(rf"<{code}>({_S}*)<0000>", re.IGNORECASE)
        if null_entry.search(text):
            text = null_entry.sub(lambda mm: f"<{code}>{mm.group(1)}<{target}>", text, count=1)
            count += 1
            continue
        present = re.search(rf"<{code}>{_S}*<[0-9a-fA-F]+>", text, re.IGNORECASE) is not None
        code_num = int(code, 16)
        if present or any(a <= code_num <= b for a, b in ranges):
            continue
        new.append(f"<{code}> <{target}>")
    if new:
        pos = text.rfind("endcmap")
        if pos >= 0:
            block_text = ""
            for k in range(0, len(new), 100):
                part = new[k:k + 100]
                block_text += f"{len(part)} beginbfchar\n" + "\n".join(part) + "\nendbfchar\n"
            text = text[:pos] + block_text + text[pos:]
            count += len(new)
    return text, count


def _repair(pdf: bytes) -> ActualTextRepair:
    reader = _PdfReader(pdf)
    s = reader.text

    # (Font-Objekt, Code) -> Zaehlung je ActualText-Wert (Einfuegereihenfolge zaehlt).
    finds: Dict[int, Dict[str, Dict[str, int]]] = {}
    widths: Dict[int, int] = {}

    def bytes_per_code(font_nr: int) -> int:
        if font_nr not in widths:
            o = reader.obj(font_nr)
            d = o.dict if o else ""
            type0 = _TYPE0_RE.search(d) is not None
            identity = _IDENTITY_RE.search(d) is not None
            widths[font_nr] = (2 if identity else 0) if type0 else 1
        return widths[font_nr]

    def collect(font_nr: int, code: str, text: str) -> None:
        if not text:
            return
        per_code = finds.setdefault(font_nr, {})
        counter = per_code.setdefault(code, {})
        counter[text] = counter.get(text, 0) + 1

    visited: set = set()

    def process(content: str, resources: Optional[str], depth: int) -> None:
        fonts: Dict[str, int] = {}
        font_dict = reader.resolve_dict(_value(resources, "Font") if resources else None)
        if font_dict:
            for m in _FONT_ENTRY_RE.finditer(font_dict):
                fonts[m.group(1)] = int(m.group(2))
        _scan_content(content, fonts, bytes_per_code, collect)
        if depth >= 3:
            return
        xobjects = reader.resolve_dict(_value(resources, "XObject") if resources else None)
        if not xobjects:
            return
        for m in _XOBJ_ENTRY_RE.finditer(xobjects):
            nr = int(m.group(1))
            if nr in visited:
                continue
            visited.add(nr)
            o = reader.obj(nr)
            if not o or not _FORM_RE.search(o.dict):
                continue
            text = reader.stream_text(o)
            if text:
                process(text, reader.resolve_dict(_value(o.dict, "Resources")) or resources, depth + 1)

    for nr in reader.numbers:
        page = reader.obj(nr)
        if not page or not _PAGE_RE.search(page.dict):
            continue
        resources = reader.resolve_dict(_value(page.dict, "Resources"))
        parent = _ref_number(_value(page.dict, "Parent"))
        k = 0
        while not resources and parent is not None and k < 16:
            p = reader.obj(parent)
            resources = reader.resolve_dict(_value(p.dict, "Resources") if p else None)
            parent = _ref_number(_value(p.dict, "Parent")) if p else None
            k += 1
        contents_raw = _value(page.dict, "Contents")
        if not contents_raw:
            continue
        refs = [int(m.group(1)) for m in _ANYREF_RE.finditer(contents_raw)]
        # Ein /Contents-Verweis kann selbst auf ein Array-Objekt zeigen.
        o0 = reader.obj(refs[0]) if len(refs) == 1 else None
        if len(refs) == 1 and (o0 is None or o0.stream is None):
            targets = [int(m.group(1)) for m in _ANYREF_RE.finditer(o0.dict if o0 else "")]
        else:
            targets = refs
        content = "\n".join((reader.stream_text(reader.obj(r)) or "") for r in targets)
        if content:
            process(content, resources, 0)

    if not finds:
        return ActualTextRepair(pdf=pdf, repaired_glyphs=0)

    # Ab hier ist eine Reparatur noetig — erst jetzt die Schreib-Voraussetzungen pruefen.
    sx = list(_STARTXREF_RE.finditer(s))
    last_start = int(sx[-1].group(1)) if sx else None
    trailer_pos = s.rfind("trailer")
    if last_start is None or not s.startswith("xref", last_start) or trailer_pos < 0:
        return ActualTextRepair(
            pdf=pdf, repaired_glyphs=0,
            hint="Xref-Stream oder unlesbarer Trailer — ActualText-Glyphen nicht repariert",
        )
    trailer_start = s.find("<<", trailer_pos)
    trailer = s[trailer_start:_dict_end(s, trailer_start)]
    if _value(trailer, "Encrypt"):
        return ActualTextRepair(
            pdf=pdf, repaired_glyphs=0,
            hint="verschluesseltes PDF — ActualText-Glyphen nicht repariert",
        )
    root = _value(trailer, "Root")
    if not root:
        return ActualTextRepair(
            pdf=pdf, repaired_glyphs=0,
            hint="Trailer ohne /Root — ActualText-Glyphen nicht repariert",
        )

    changed: Dict[int, str] = {}
    repaired = 0
    without_tounicode = 0
    for font_nr, per_code in finds.items():
        fo = reader.obj(font_nr)
        font_dict = fo.dict if fo else ""
        cmap_nr = _ref_number(_value(font_dict, "ToUnicode"))
        if cmap_nr is None:
            without_tounicode += 1
            continue
        cmap = changed.get(cmap_nr)
        if cmap is None:
            cmap = reader.stream_text(reader.obj(cmap_nr))
        if not cmap or "begincmap" not in cmap:
            continue
        # Mehrdeutiger Code (gleiche Glyphe, verschiedene ActualTexte): der haeufigste gewinnt
        # (stabil: bei Gleichstand der zuerst gesehene, wie Array.prototype.sort).
        entries: Dict[str, str] = {}
        for code, counter in per_code.items():
            best = sorted(counter.items(), key=lambda kv: -kv[1])[0]
            entries[code] = best[0]
        text, count = _patch_cmap(cmap, entries, bytes_per_code(font_nr) * 2)
        if count > 0:
            changed[cmap_nr] = text
            repaired += count

    hint = (
        f"{without_tounicode} Font(s) mit ActualText ohne ToUnicode — nicht repariert"
        if without_tounicode > 0 else None
    )
    if not changed:
        return ActualTextRepair(pdf=pdf, repaired_glyphs=0, hint=hint)

    # Inkrementelles Update anhaengen.
    parts: List[bytes] = [pdf]
    length = len(pdf)

    def append(b: bytes) -> None:
        nonlocal length
        parts.append(b)
        length += len(b)

    if pdf[-1] != 0x0A:
        append(b"\n")
    offsets: List[Tuple[int, int, int]] = []
    for nr in sorted(changed):
        gen = reader.generation(nr)
        body = changed[nr].encode("latin1")
        offsets.append((nr, length, gen))
        append(f"{nr} {gen} obj\n<< /Length {len(body)} >>\nstream\n".encode("latin1"))
        append(body)
        append(b"\nendstream\nendobj\n")
    xref_off = length
    xref = "xref\n"
    for nr, off, gen in offsets:
        xref += f"{nr} 1\n{off:010d} {gen:05d} n \n"
    size_num = _js_number(_value(trailer, "Size"))
    size_val = size_num if (_is_finite(size_num) and size_num != 0) else 0
    size = max([size_val] + [x + 1 for x in reader.numbers])
    size_str = str(int(size)) if float(size).is_integer() else str(size)
    info = _value(trailer, "Info")
    id_ = _value(trailer, "ID")
    xref += (
        f"trailer\n<< /Size {size_str} /Root {root}"
        f"{f' /Info {info}' if info else ''}{f' /ID {id_}' if id_ else ''} /Prev {last_start} >>\n"
        f"startxref\n{xref_off}\n%%EOF\n"
    )
    append(xref.encode("latin1"))

    return ActualTextRepair(pdf=b"".join(parts), repaired_glyphs=repaired, hint=hint)


def repair_actualtext_glyphs(pdf: bytes) -> ActualTextRepair:
    """ActualText-Glyphen eines PDFs reparieren (siehe Modulkopf).

    Wirft nie: Scheitern liefert das Original zurueck, mit ``hint``. Der
    Aufrufer loggt den ``hint`` (laut, nicht still).
    """
    try:
        return _repair(pdf)
    except Exception as exc:  # noqa: BLE001 — Result-Pattern wie in der TS-Fassung
        return ActualTextRepair(
            pdf=pdf, repaired_glyphs=0,
            hint=f"Reparatur gescheitert: {type(exc).__name__}: {exc}",
        )
