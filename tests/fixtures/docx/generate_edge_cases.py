import io
import sys
import zipfile

from PIL import Image

out = sys.argv[1]
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'


def p(t, extra=""):
    return f'<w:p>{extra}<w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>'


def drawing(rid, n):
    return f'<w:r><w:drawing><wp:inline><wp:extent cx="1800000" cy="1200000"/><wp:docPr id="{n}" name="Bild{n}"/><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="{n}" name="b{n}"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:{"embed" if rid != "rExt" else "link"}="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1800000" cy="1200000"/></a:xfrm><a:prstGeom prst="rect"/></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'


cell = lambda t, pr="": f"<w:tc><w:tcPr>{pr}</w:tcPr>{p(t)}</w:tc>"
nested = (
    "<w:tbl><w:tblPr/><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid><w:tr>"
    + cell("INNEN-A")
    + cell("INNEN-B")
    + "</w:tr></w:tbl>"
)
tbl = (
    '<w:tbl><w:tblPr><w:tblBorders><w:top w:val="single"/></w:tblBorders></w:tblPr><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>'
    "<w:tr>" + cell("Kopf1") + cell("Kopf2") + cell("Kopf3") + "</w:tr>"
    "<w:tr>" + cell("QUERVERBUNDEN", '<w:gridSpan w:val="2"/>') + cell("C2") + "</w:tr>"
    "<w:tr>"
    + cell("SENKRECHT", '<w:vMerge w:val="restart"/>')
    + cell("B3")
    + "<w:tc><w:tcPr/>"
    + nested
    + "<w:p/></w:tc></w:tr>"
    "<w:tr>"
    + cell("", "<w:vMerge/>")
    + cell("B4")
    + cell("C4 | mit Pipe")
    + "</w:tr></w:tbl>"
)
body = (
    p("Absatz EINS. 1.5 kWh z0-0")
    + p("Absatz ZWEI.")
    + "<w:p><w:r><w:t>Sichtbar </w:t></w:r><w:r><w:rPr><w:vanish/></w:rPr><w:t>VERSTECKTERTEXT</w:t></w:r></w:p>"
    + '<w:p><w:r><w:t>Alt </w:t></w:r><w:del w:id="1" w:author="X" w:date="2026-01-01T00:00:00Z"><w:r><w:delText>GELOESCHTERTEXT</w:delText></w:r></w:del><w:ins w:id="2" w:author="X" w:date="2026-01-01T00:00:00Z"><w:r><w:t>EINGEFUEGTERTEXT</w:t></w:r></w:ins></w:p>'
    + '<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>Kommentierter Satz.</w:t></w:r><w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r></w:p>'
    + '<w:p><w:r><w:t>Mit Fussnote</w:t></w:r><w:r><w:rPr><w:rStyle w:val="FootnoteReference"/><w:vertAlign w:val="superscript"/></w:rPr><w:footnoteReference w:id="1"/></w:r></w:p>'
    + '<w:p><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr><w:r><w:t>Nummer eins</w:t></w:r></w:p>'
    + '<w:p><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr><w:r><w:t>Nummer zwei</w:t></w:r></w:p>'
    + '<w:p><w:hyperlink r:id="rJs"><w:r><w:t>JSLINK</w:t></w:r></w:hyperlink> <w:hyperlink r:id="rData"><w:r><w:t>DATALINK</w:t></w:r></w:hyperlink> <w:hyperlink r:id="rHttp"><w:r><w:t>HTTPLINK</w:t></w:r></w:hyperlink></w:p>'
    + "<w:p><m:oMath><m:r><m:t>E=mc^2</m:t></m:r></m:oMath></w:p>"
    + '<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wp:anchor simplePos="0" relativeHeight="1" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1"><wp:simplePos x="0" y="0"/><wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset></wp:positionH><wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV><wp:extent cx="2000000" cy="500000"/><wp:wrapNone/><wp:docPr id="20" name="TB"/><a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"><wps:wsp><wps:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="2000000" cy="500000"/></a:xfrm><a:prstGeom prst="rect"/></wps:spPr><wps:txbx><w:txbxContent>'
    + p("TEXTFELDINHALT")
    + "</w:txbxContent></wps:txbx><wps:bodyPr/></wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice></mc:AlternateContent></w:r></w:p>"
    + "<w:p>"
    + drawing("rPng", 1)
    + "</w:p><w:p>"
    + drawing("rJpg", 2)
    + "</w:p>"
    + (("<w:p>" + drawing("rExt", 3) + "</w:p>") if "ext" in sys.argv else "")
    + tbl
    + p("Ende.")
    + '<w:sectPr><w:headerReference w:type="default" r:id="rH"/><w:footerReference w:type="default" r:id="rF"/></w:sectPr>'
)
doc = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {W}><w:body>{body}</w:body></w:document>'


def img(fmt, size):
    b = io.BytesIO()
    Image.new("RGB", size, (200, 50, 50)).save(b, fmt)
    return b.getvalue()


rels = (
    '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rPng" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/a.png"/>'
    '<Relationship Id="rJpg" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/b.jpeg"/>'
    '<Relationship Id="rExt" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="http://example.invalid/x.png" TargetMode="External"/>'
    '<Relationship Id="rJs" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="javascript:alert(1)" TargetMode="External"/>'
    '<Relationship Id="rData" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="data:text/html,evil" TargetMode="External"/>'
    '<Relationship Id="rHttp" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="http://evil.invalid/track?a=1" TargetMode="External"/>'
    '<Relationship Id="rH" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>'
    '<Relationship Id="rF" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
    '<Relationship Id="rFn" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>'
    '<Relationship Id="rCm" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>'
    '<Relationship Id="rNum" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>'
    "</Relationships>"
)
hf = lambda tag, t: (
    f'<?xml version="1.0" encoding="UTF-8"?><w:{tag} {W}>{p(t)}</w:{tag}>'
)
fn = f'<?xml version="1.0" encoding="UTF-8"?><w:footnotes {W}><w:footnote w:type="separator" w:id="-1"><w:p/></w:footnote><w:footnote w:type="continuationSeparator" w:id="0"><w:p/></w:footnote><w:footnote w:id="1">{p("FUSSNOTENTEXT")}</w:footnote></w:footnotes>'
cm = f'<?xml version="1.0" encoding="UTF-8"?><w:comments {W}><w:comment w:id="0" w:author="Pruefer" w:date="2026-01-01T00:00:00Z">{p("KOMMENTARTEXT")}</w:comment></w:comments>'
num = f'<?xml version="1.0" encoding="UTF-8"?><w:numbering {W}><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>'
ct = (
    '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Default Extension="jpeg" ContentType="image/jpeg"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
    '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
    '<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>'
    '<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>'
    '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/></Types>'
)
with zipfile.ZipFile(out, "w") as z:
    z.writestr("[Content_Types].xml", ct)
    z.writestr(
        "_rels/.rels",
        '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
    )
    z.writestr("word/document.xml", doc)
    z.writestr("word/_rels/document.xml.rels", rels)
    z.writestr("word/header1.xml", hf("hdr", "KOPFZEILENTEXT"))
    z.writestr("word/footer1.xml", hf("ftr", "FUSSZEILENTEXT"))
    z.writestr("word/footnotes.xml", fn)
    z.writestr("word/comments.xml", cm)
    z.writestr("word/numbering.xml", num)
    z.writestr("word/media/a.png", img("PNG", (300, 200)))
    z.writestr("word/media/b.jpeg", img("JPEG", (3000, 2000)))
