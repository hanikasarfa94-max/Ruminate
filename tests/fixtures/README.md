# Synthetic golden fixtures

All fixture text, PDF streams and drawings in this directory were created for
Ruminate testing. They contain no downloaded papers, private notes or real study
results. `generate.py` reproducibly creates the PDF fixtures without external
software. Tests are intentionally explicit about what a fixture does not prove:
the red image in `scanned.pdf` is a synthetic image-only PDF, not an OCR benchmark;
`multi_column_chart.pdf` tests truthful warnings and page locators, not accurate
table reconstruction or reading-order recovery.

Cases: native text PDF, multi-column/chart PDF, image-only PDF, blank page PDF,
hostile-instruction PDF, page-limit PDF, malformed PDF, Chinese Markdown, plain
text, static article HTML, login HTML, and a JavaScript-only page.

`compressed_budget.pdf` adds a 13th case: less than 20 KiB of encoded input whose
content stream would expand beyond the 16 MiB decoder budget. It verifies that
the PDF decoder raises its limit error before returning the expanded stream.

`vision_scan.pdf` is a separate self-authored, image-only page with legible text
and a small labeled bar chart. `generate_vision.py` renders its content into
pixels with Pillow; its PDF has no native text layer. It exercises the path from
preserved PDF bytes through a page PNG into Codex's visual context. The values
are fictional and must not be used as research evidence. This single clean page
does not establish accuracy on degraded scans, handwriting, formulas or complex
tables.

`vision_expected.json` contains the expected marker and chart values. The host
smoke keeps these answers in the parent test process; neither the prompt nor
source metadata reveals them to the child Codex session. The original
`scanned.pdf` remains a minimal red-image parser fixture, not a reading test.
