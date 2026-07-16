# Submission report

`Feature-Flag-Service-Report.pdf` at the repo root is generated from `report.html` here.

To regenerate after editing the HTML:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --no-pdf-header-footer \
  --print-to-pdf=../Feature-Flag-Service-Report.pdf \
  "file://$PWD/report.html"
```

Chrome rather than pandoc's PDF path, which needs a full LaTeX install for a document
that is mostly tables and code blocks.
