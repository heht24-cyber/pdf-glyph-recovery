# PDF Glyph Recovery

Restore copy, paste, and search for PDFs with missing Unicode maps on embedded TrueType CID fonts. The command builds a new PDF and reports the evidence behind the recovered characters. It verifies that every page's pixels are identical at 144 DPI before writing the result.

## Results

| Case | Evidence | Result |
| --- | --- | --- |
| Real Vietnamese PDF reported in PyMuPDF #1768 | Reversed the embedded fonts' Unicode cmap tables | 557 of 682 affected glyph occurrences mapped uniquely. The remaining 125 share a glyph between ordinary and nonbreaking space. |
| Controlled Chinese document | Removed the PDF Unicode map and preserved the embedded font cmap | 276 of 276 Unicode code points recovered exactly against the known input. |
| Controlled mathematics document | Removed both Unicode maps and compacted the subset, renumbering glyph IDs | 180 of 180 Unicode code points recovered exactly against the known input, including non-BMP mathematical letters. |
| Same mathematics document with no reference font | Embedded geometry with no Unicode labels | No output was created. |

Rendered pages matched in all three repaired cases. For the real PDF, the totals measure coverage from the embedded font, with selected public field labels checked against their intended text. The two controlled tests compare every recovered code point against the original string.

## Use

```bash
python -m pip install pymupdf fonttools
python recover.py input.pdf repaired.pdf > recovery.json
python recover.py input.pdf repaired.pdf --reference original-font.ttf > recovery.json
```

Use a fresh path for the repaired copy. The JSON reports unique embedded mapping and outline mapping separately, and gives the glyph ID and occurrence count for unresolved characters when font attribution is available. `--normalize-spaces` permits choosing U+0020 when the only alternatives are U+0020 and U+00A0, and reports those occurrences separately.

## How it works

1. Identify embedded TrueType CID fonts with an Identity character encoding and a missing ToUnicode map.
2. Recover unique Unicode labels from the embedded font's own cmap. If that table is missing or incomplete, an optional reference with the same PostScript name can supply values by exact outline and metric comparison. The routine expands composite glyph components before comparison.
3. Follow the PDF's CID-to-GID mapping when building the new ToUnicode stream. This accommodates a subset with different glyph numbers. Unicode aliases remain unresolved by default.
4. Open the output in a fresh PDF document and compare raster hashes on every page.

The reference-based result depends on which fonts the caller supplies. The scope is missing maps on supported embedded TrueType fonts and individual characters. Equation structure, reading order, image-only pages, and the meaning of a visually indistinguishable letter remain separate problems.

## Reproduce the controls

```bash
python test_recover.py /path/to/DejaVuMathTeXGyre.ttf
```

The Chinese case uses the fallback font included with PyMuPDF. The mathematics case compares the source text to an independently rebuilt code map after deliberate loss of the cmap and a compact subset export. Tested with PyMuPDF 1.26.6 and fontTools 4.61.1.

Related work includes [system-font reconstruction](https://github.com/zhanghao50671/pdf-tounicode-fix), [shape-recognition fallbacks](https://github.com/jedarden/pdftract/blob/main/docs/research/glyph-recognition-and-unicode-recovery.md), and [inverse shaping for complex scripts](https://github.com/pymupdf/PyMuPDF/issues/4805). The [public PyMuPDF issue](https://github.com/pymupdf/PyMuPDF/issues/1768) documents the real missing-map case.
