"""Recover missing Unicode maps in embedded TrueType PDF fonts."""
import argparse
import hashlib
import io
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTFont, TTLibError


def font_name(name):
    bare = re.sub(r"^[A-Z]{6}\+", "", name).casefold()
    return "".join(char for char in bare if char.isalnum())


def reverse_cmap(font):
    names = defaultdict(set)
    if "cmap" in font:
        for table in font["cmap"].tables:
            if table.isUnicode():
                for code, glyph in table.cmap.items():
                    if (0 <= code <= 0x10FFFF and unicodedata.category(chr(code))
                            not in {"Cc", "Cs", "Co", "Cn"}):
                        names[glyph].add(code)
    return names


def outline(font, glyphs, name):
    pen = DecomposingRecordingPen(glyphs)
    glyphs[name].draw(pen)
    if not pen.value:
        return None
    commands = tuple((op, tuple(points)) for op, points in pen.value)
    return font["head"].unitsPerEm, font["hmtx"][name], commands


def postscript_name(font):
    if "name" not in font:
        return ""
    names = font["name"]
    identifier = names.getDebugName(6)
    return font_name(identifier or "")


def mapping(font, references, normalize_spaces):
    glyph_order = font.getGlyphOrder()
    candidates = reverse_cmap(font)
    direct_gids = {font.getGlyphID(name) for name, codes in candidates.items() if len(codes) == 1}
    name = postscript_name(font)
    relevant = [ref for ref in references if name and postscript_name(ref) == name and "glyf" in ref]
    if relevant:
        glyphs = font.getGlyphSet()
        unknown = defaultdict(list)
        for glyph in glyph_order:
            if glyph not in candidates:
                key = outline(font, glyphs, glyph)
                if key is not None:
                    unknown[key].append(glyph)
        for ref in relevant:
            ref_glyphs = ref.getGlyphSet()
            for glyph, codes in reverse_cmap(ref).items():
                for target in unknown.get(outline(ref, ref_glyphs, glyph), ()):
                    candidates[target].update(codes)
    resolved = {font.getGlyphID(glyph): next(iter(codes))
                for glyph, codes in candidates.items() if len(codes) == 1}
    ambiguous = {font.getGlyphID(glyph) for glyph, codes in candidates.items() if len(codes) > 1}
    normalized = set()
    if normalize_spaces:
        for glyph, codes in candidates.items():
            if codes == {0x20, 0xA0}:
                gid = font.getGlyphID(glyph)
                normalized.add(gid)
                resolved[gid] = 0x20
    return resolved, direct_gids, ambiguous - normalized, normalized


def pairs_for_font(doc, xref, references, normalize_spaces):
    if doc.xref_get_key(xref, "ToUnicode")[0] != "null":
        return "existing-map", {}, set(), set(), set(), set()
    if doc.xref_get_key(xref, "Encoding") not in {
        ("name", "/Identity-H"), ("name", "/Identity-V")
    }:
        return "unsupported-encoding", {}, set(), set(), set(), set()
    kind, value = doc.xref_get_key(xref, "DescendantFonts")
    refs = re.findall(r"(\d+)\s+\d+\s+R", value)
    if kind != "array" or len(refs) != 1:
        return "unsupported-descendant", {}, set(), set(), set(), set()
    descendant = int(refs[0])
    if doc.xref_get_key(descendant, "Subtype") != ("name", "/CIDFontType2"):
        return "unsupported-font-type", {}, set(), set(), set(), set()
    raw = doc.extract_font(xref)[3]
    if not raw:
        return "unembedded-font", {}, set(), set(), set(), set()
    with TTFont(io.BytesIO(raw)) as font:
        if "glyf" not in font:
            return "unsupported-font-type", {}, set(), set(), set(), set()
        resolved, direct, ambiguous, normalized = mapping(font, references, normalize_spaces)
    kind, value = doc.xref_get_key(descendant, "CIDToGIDMap")
    if kind == "null" or (kind, value) == ("name", "/Identity"):
        linked = ((gid, gid) for gid in resolved)
    elif kind == "xref":
        raw_map = doc.xref_stream(int(value.split()[0]))
        if not raw_map or len(raw_map) % 2 or len(raw_map) > 131072:
            return "invalid-cid-map", {}, set(), set(), set(), set()
        linked = ((i // 2, int.from_bytes(raw_map[i:i + 2], "big"))
                  for i in range(0, len(raw_map), 2))
    else:
        return "unsupported-cid-map", {}, set(), set(), set(), set()
    pairs, emitted = {}, set()
    for cid, gid in linked:
        if 0 < gid <= 65535 and gid in resolved:
            pairs[cid] = resolved[gid]
            emitted.add(gid)
    return ("recovered" if pairs else "insufficient-evidence",
            pairs, emitted & direct, emitted - direct - normalized, ambiguous, emitted & normalized)


def unicode_stream(pairs):
    lines = [
        "/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Recovered-UCS def", "/CMapType 2 def",
        "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange",
    ]
    entries = sorted(pairs.items())
    for start in range(0, len(entries), 100):
        block = entries[start:start + 100]
        lines.append(f"{len(block)} beginbfchar")
        for cid, code in block:
            dest = chr(code).encode("utf-16-be").hex().upper()
            lines.append(f"<{cid:04X}> <{dest}>")
        lines.append("endbfchar")
    lines.extend(["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end", ""])
    return "\n".join(lines).encode("ascii")


def pixel_hashes(doc):
    for page in doc:
        img = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
        yield img.width, img.height, hashlib.sha256(img.samples_mv).hexdigest()


def audit_and_recover(source, references, normalize_spaces=False):
    with pymupdf.open(source) as doc:
        font_usage, glyph_usage = {}, defaultdict(Counter)
        for page in doc:
            by_name = defaultdict(set)
            counts = Counter()
            for span in page.get_texttrace():
                name = font_name(span["font"])
                counts.update((name, char[1]) for char in span["chars"] if char[1] >= 0)
            for xref, _, kind, name, *_ in page.get_fonts():
                by_name[font_name(name)].add(xref)
                if kind == "Type0" and xref not in font_usage:
                    font_usage[xref] = {"font": name, "trace_incomplete": False}
            for name, xrefs in by_name.items():
                for xref in xrefs & font_usage.keys():
                    if len(xrefs) != 1 or not any(n == name for n, _ in counts):
                        font_usage[xref]["trace_incomplete"] = True
                    elif counts:
                        glyph_usage[xref].update({gid: count for (n, gid), count in counts.items()
                                                  if n == name})
        changes = {}
        for xref, report in font_usage.items():
            try:
                status, pairs, direct, outlines, ambiguous, spaces = pairs_for_font(
                    doc, xref, references, normalize_spaces)
            except (TTLibError, KeyError, ValueError, RuntimeError) as error:
                status, pairs, direct, outlines, ambiguous, spaces = type(error).__name__, {}, set(), set(), set(), set()
            report.update(status=status, mapped_codes=len(pairs))
            if status in {"recovered", "insufficient-evidence"} and not report["trace_incomplete"]:
                seen = glyph_usage[xref]
                unknown = seen.keys() - direct - outlines - ambiguous - spaces
                report.update(occurrences=sum(seen.values()),
                              from_embedded_cmap=sum(seen[g] for g in direct),
                              from_exact_outlines=sum(seen[g] for g in outlines),
                              ambiguous=sum(seen[g] for g in ambiguous),
                              normalized_spaces=sum(seen[g] for g in spaces),
                              without_mapping=sum(seen[g] for g in unknown),
                              unresolved=[{"glyph_id": gid, "occurrences": seen[gid],
                                           "reason": "ambiguous" if gid in ambiguous else "missing"}
                                          for gid in sorted((ambiguous | unknown) & seen.keys())])
            if pairs:
                changes[xref] = pairs

        if not changes:
            return None, {"fonts": font_usage, "pixel_checks": 0}
        before = tuple(pixel_hashes(doc))
        for xref, pairs in changes.items():
            stream = doc.get_new_xref()
            doc.update_object(stream, "<<>>")
            doc.update_stream(stream, unicode_stream(pairs))
            doc.xref_set_key(xref, "ToUnicode", f"{stream} 0 R")
        data = doc.tobytes(deflate=True)
    with pymupdf.open(stream=data, filetype="pdf") as check:
        if tuple(pixel_hashes(check)) != before:
            raise RuntimeError("rendered content changed; output was not written")
    return data, {"fonts": font_usage, "pixel_checks": len(before)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--reference", action="append", type=Path, default=[],
                        help="matching original TrueType font, repeatable")
    parser.add_argument("--normalize-spaces", action="store_true",
                        help="normalize an ambiguous ordinary/non-breaking space to ordinary space")
    args = parser.parse_args()
    references = [TTFont(path) for path in args.reference]
    try:
        data, report = audit_and_recover(args.input, references, args.normalize_spaces)
    finally:
        for reference in references:
            reference.close()
    if data is not None:
        with args.output.open("xb") as stream:
            stream.write(data)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if data is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
