#!/usr/bin/env python3
"""Write the test corpus used by the verification suites.

    python3 fixtures.py <output-dir>

Everything is generated from the standard library so the suites have no
dependencies of their own — the PDF and DOCX are written by hand rather than
pulled from a document library.
"""
from __future__ import annotations

import gzip
import json
import pathlib
import random
import sys
import zipfile

PARAGRAPHS = [
    "Overtime Approval. Requests relating to overtime must be submitted in writing "
    "to the responsible manager, who reviews them within five working days.",
    "Approval for overtime is granted by the department head, except where the "
    "amount exceeds the delegated limit, in which case the finance director approves.",
    "Expense Reimbursement. Employees submit receipts within thirty days of the "
    "expense being incurred. Claims without receipts are refused.",
    "Remote Work. Staff may work remotely up to three days each week with written "
    "agreement from their line manager and the people team.",
    "Records of every decision are retained for seven years in the central archive, "
    "and are available to auditors on request.",
    "Travel Booking. Flights must be booked at least fourteen days in advance. "
    "Rail travel is preferred for journeys under four hours.",
]
ONE_LINER = PARAGRAPHS[0]


def _pdf(paragraphs: list[str], padding: int = 0) -> bytes:
    """A minimal single-page PDF. Hand-built to avoid a writer dependency.

    `padding` adds an unreferenced stream object of that many bytes. No page
    points at it, so parsers skip it: the file is large on the wire but carries
    only the text above, which keeps an upload-size test fast to ingest.
    """
    lines: list[str] = []
    for para in paragraphs:
        cur = ""
        for word in para.split():
            if len(cur) + len(word) + 1 > 82:
                lines.append(cur)
                cur = word
            else:
                cur = (cur + " " + word).strip()
        lines.extend([cur, ""])

    stream = b"BT /F1 11 Tf 54 740 Td 14 TL\n"
    for line in lines:
        safe = (line.encode("ascii", "replace")
                    .replace(b"(", b"").replace(b")", b"").replace(b"\\", b""))
        stream += b"(" + safe + b") Tj T*\n"
    stream += b"ET"

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    if padding:
        # Seeded so the fixture is byte-identical on every run. Random bytes
        # rather than zeros so nothing on the path can compress it away.
        filler = random.Random(21).randbytes(padding)
        objects.append(b"<< /Length " + str(padding).encode() + b" >>\nstream\n"
                       + filler + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += ("%010d 00000 n \n" % offset).encode()
    out += (b"trailer\n<< /Size " + str(len(objects) + 1).encode()
            + b" /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    return bytes(out)


def _docx(text: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def write(target: pathlib.Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(PARAGRAPHS) + "\n"

    # One of each supported type. `.md` is here because it silently failed to
    # ingest until the `markdown` dependency was added.
    (target / "policies.txt").write_text(body)
    (target / "policies.md").write_text("# Policies\n\n" + body)
    (target / "policies.csv").write_text(
        "policy,detail\n" + "".join(f'p{i},"{p}"\n' for i, p in enumerate(PARAGRAPHS)))
    (target / "policies.json").write_text(json.dumps(
        [{"policy": f"p{i}", "detail": p} for i, p in enumerate(PARAGRAPHS)], indent=2))
    (target / "policies.pdf").write_bytes(_pdf(PARAGRAPHS))
    (target / "policies.docx").write_bytes(_docx(ONE_LINER))

    # Edge cases.
    # Two fragments under any sensible min_chunk_size, for the merge rule.
    (target / "tiny.txt").write_text("Short.\n\nAlso short.\n\n" + ONE_LINER + "\n")
    # Right extension, unparseable content — one bad file must not fail a batch.
    (target / "broken.pdf").write_bytes(b"%PDF-1.4\nnot a real pdf body\n%%EOF\n")
    # Over nginx's 1 MB default request-body limit, which once rejected every
    # real-world PDF with a 413 before the API saw it. See issue #21.
    (target / "large.pdf").write_bytes(_pdf(PARAGRAPHS, padding=3 * 1024 * 1024))
    # Unsupported types, which must be reported rather than dropped in silence.
    (target / "notes.xyz").write_text("unsupported\n")
    (target / "notes.rtf").write_text("also unsupported\n")

    # A ZIP mixing supported and unsupported members.
    with zipfile.ZipFile(target / "batch.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("policies.txt", "policies.md", "policies.pdf",
                     "notes.xyz", "notes.rtf"):
            z.write(target / name, arcname=name)

    # Issue #290. A ZIP cut before its central directory, like an incomplete
    # download: it once ended the upload in a bare HTTP 500.
    with zipfile.ZipFile(target / "truncated.zip", "w", zipfile.ZIP_STORED) as z:
        z.write(target / "policies.txt", arcname="policies.txt")
    data = (target / "truncated.zip").read_bytes()
    (target / "truncated.zip").write_bytes(data[:data.index(b"PK\x01\x02")])
    # Gzipped JSON and JSON Lines members are decompressed and ingested (#290,
    # #291); other gzip types are skipped as unsupported.
    with zipfile.ZipFile(target / "gz_batch.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.write(target / "policies.json", arcname="policies.json")
        z.writestr("bills/policies2.json.gz",
                   gzip.compress((target / "policies.json").read_bytes(), mtime=0))
        z.writestr("records.jsonl.gz", gzip.compress(b'{"a": 1}\n', mtime=0))
        z.writestr("notes.txt.gz", gzip.compress(b"unsupported\n", mtime=0))

    # Issue #295. Ordinary nested JSON with non-ASCII text, stored as readable
    # `key: value` lines rather than pretty-printed JSON with escaped characters.
    (target / "plain.json").write_text(json.dumps({
        "title": "Remote work policy",
        "body": "Remote work needs written manager approval.",
        "meta": {"author": "Zoë Müller"},
        "tags": ["policy", "remote"],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    # Issue #291. JSON Lines: one record per line, converted like ordinary JSON,
    # plain and gzipped; a line with no text adds nothing. bad.jsonl has a
    # malformed second line, which fails the whole file.
    records = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in (
        {"bill": "HR-1", "title": "Remote work act", "sponsor": {"name": "Zoë Müller"}},
        {},
        {"bill": "HR-2", "title": "Expense filing act", "tags": ["finance", "deadline"]},
    ))
    (target / "records.jsonl").write_text(records, encoding="utf-8")
    (target / "records.jsonl.gz").write_bytes(gzip.compress(records.encode("utf-8"), mtime=0))
    (target / "bad.jsonl").write_text('{"bill": "HR-3"}\n{"bill": }\n{"bill": "HR-4"}\n',
                                      encoding="utf-8")

    # Issue #298. An upload's ZIPs may expand to at most 2 GiB in all. Both
    # files are about 2 MB. zip_over_declared.zip honestly declares a member of
    # 2,049 MiB of zeros, refused before anything is read (about 3 s to write).
    # zip_over_gzip.zip declares only its 2 MB gzip member, but that member,
    # 33 concatenated gzip streams of 64 MiB of zeros, decompresses to
    # 2,112 MiB: it is refused while its output is counted.
    zeros = bytes(1024 * 1024)
    member = zipfile.ZipInfo("zeros.bin", date_time=(2026, 1, 1, 0, 0, 0))
    member.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(target / "zip_over_declared.zip", "w") as z:
        with z.open(member, "w", force_zip64=True) as out:
            for _ in range(2049):
                out.write(zeros)
    stream = gzip.compress(bytes(64 * 1024 * 1024), mtime=0)
    with zipfile.ZipFile(target / "zip_over_gzip.zip", "w", zipfile.ZIP_STORED) as z:
        z.writestr("big.json.gz", stream * 33)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: fixtures.py <output-dir>")
    out = pathlib.Path(sys.argv[1])
    write(out)
    for path in sorted(out.iterdir()):
        print(f"  {path.name:16s} {path.stat().st_size:7d} bytes")
