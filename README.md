# ai-extract

Extract payment data from PDF invoices using a local LLM. Detects recipient, IBAN, amount, and payment reference — copies everything to the clipboard, ready to paste into your banking app.

Works with both digital and scanned PDFs. Scanned documents are automatically detected and processed via macOS native OCR (Apple Vision framework).

## Example

```
$ ai-extract.py invoice.pdf

Recipient:  Stadtwerke Musterstadt GmbH
IBAN:       DE89 3704 0044 0532 0130 00
Amount:     187,43 EUR
Reference:  Vertragskonto 4810 552 003
Info:       Rechnungsnummer: 902 441 078 | Datum: 15.03.2025

-> Copied to clipboard
```

Multiple files at once:

```
$ ai-extract.py invoice1.pdf invoice2.pdf invoice3.pdf
```

## How It Works

```
PDF --+-- pdftotext --------------- digital text --+
      |                                            +-- both sent to LLM -- JSON -- clipboard
      +-- Vision OCR (always) --- OCR text --------+
```

1. **Text extraction** via `pdftotext` (fast, accurate for digital PDFs)
2. **Vision OCR** always runs in parallel as a cross-reference (macOS Vision framework, `VNRecognizeTextRequest`)
3. **Scan detection** — if `pdftotext` output is noise (< 30% real words), it is discarded; if the PDF has no embedded fonts at all, `pdftotext` is skipped entirely
4. **Both text versions** are sent to the LLM, which cross-references them and picks the more complete/accurate data
5. **LLM extraction** via Ollama (local, private) — parses recipient, IBAN, amount, and reference into structured JSON
6. **Post-processing** — normalizes IBAN spacing (4-char groups) and amount format (dot → comma)
7. **Clipboard** — formatted result is copied via `pbcopy` and a macOS notification is shown

### Scan Detection (Two-Stage)

| Stage | Method | What it catches |
|-------|--------|-----------------|
| 1 | `pdffonts` — no embedded fonts | Pure scans (image-only PDFs) |
| 2 | Word heuristic — < 30% real words | Scans with a bad OCR text layer |

### Vision OCR

A small Swift binary (`pdf-ocr`) is **compiled automatically on first run** and cached. It uses:

- `CGPDFDocument` to render each page at 3x resolution
- `VNRecognizeTextRequest` with `.accurate` recognition level
- Language detection for German and English
- Language correction enabled, minimum text height set to 0 (catches fine print)

No external OCR service or model needed — runs entirely on-device via Apple Neural Engine.

### Text Extraction Fallback Chain

| Priority | Method | When used |
|----------|--------|-----------|
| 1 | `pdftotext` (poppler) | Default for digital PDFs |
| 2 | `pdf-text-extract` (Swift/PDFKit) | Fallback if poppler not installed |
| 3 | `pdf-ocr` (Vision framework) | Always runs; sole source for scans |

`pdf-text-extract` is also compiled automatically on first run.

## Requirements

- **macOS** (uses Vision framework, `osascript` notifications, `pbcopy`)
- **Python 3.8+** (no pip dependencies — stdlib only)
- **Xcode Command Line Tools** (`swiftc` — for one-time compilation of OCR binary)
- **[Ollama](https://ollama.com)** running locally

### Optional

- **poppler** (`brew install poppler`) — provides `pdftotext` and `pdffonts` for faster digital PDF extraction and scan detection. Without it, the script falls back to Swift/PDFKit for text extraction and skips the font-based scan detection.

## Installation

```bash
# Clone the repository
git clone https://github.com/adraeger/ai-extract.git ~/.local/share/ai-extract

# Symlink into PATH
ln -s ~/.local/share/ai-extract/ai-extract.py ~/.local/bin/ai-extract.py

# Make sure ~/.local/bin is in your PATH
# (add to ~/.zshrc or ~/.bashrc if needed)
export PATH="$HOME/.local/bin:$PATH"

# Install and start Ollama
brew install ollama
ollama serve &
ollama pull qwen3.5:9b
```

On first run, the script automatically compiles the Swift helper binaries (`pdf-ocr`, `pdf-text-extract`). This takes a few seconds and only happens once.

## Configuration

Edit the constants at the top of `ai-extract.py`:

```python
OLLAMA_MODEL = "qwen3.5:9b"       # Any Ollama model that follows instructions
OLLAMA_API = "http://localhost:11434/api/chat"
MAX_TEXT_CHARS = 6000              # Max chars sent to LLM per text source
SCAN_WORD_THRESHOLD = 0.3         # Min ratio of real words to consider text valid
```

### Choosing a Model

The script works with any instruction-following model available through Ollama. Tested with:

| Model | Size | Speed | Quality |
|-------|------|-------|---------|
| `qwen3.5:9b` | 6.6 GB | ~10s | Good — recommended default |
| `qwen3.5:4b` | 3.4 GB | ~5s | Acceptable for simple invoices |

The model must be able to output valid JSON and follow German-language extraction instructions.

## macOS Quick Action

You can wrap this script as a Finder Quick Action (right-click menu) using Automator:

1. Open **Automator** → New → **Quick Action**
2. Set "Workflow receives" to **PDF files** in **Finder**
3. Add a **Run Shell Script** action:
   ```bash
   for f in "$@"; do
       ~/.local/bin/ai-extract.py "$f"
   done
   ```
4. Save as "Extract Payment Data"

Now you can right-click any PDF in Finder and extract payment data with one click.

## Output Format

The script outputs structured data and copies it to the clipboard:

```
Recipient:  <name>
IBAN:       <iban in 4-char groups>
Amount:     <amount with comma> EUR
Reference:  <invoice number or reference>
Info:       <additional info, pipe-separated>
```

The JSON returned by the LLM uses these fields:

```json
{
  "recipient": "Company Name",
  "iban": "DE89 3704 0044 0532 0130 00",
  "amount": "187,43",
  "reference": "Invoice 12345",
  "additional_info": ["Date: 15.03.2025", "Customer: Max Mustermann"]
}
```

## Logging

All activity is logged to `ai-extract.log` in the script directory. Includes text extraction results, OCR output, LLM prompts/responses, and errors.

## How It Handles Edge Cases

| Scenario | Behavior |
|----------|----------|
| Pure scan (no text layer) | Detected via `pdffonts`, uses Vision OCR only |
| Scan with bad OCR layer | `pdftotext` output discarded (word heuristic), uses Vision OCR |
| Digital PDF | Uses both `pdftotext` and Vision OCR for cross-referencing |
| Multiple IBANs | Prefers Sparkasse (savings bank) IBANs — configurable in the prompt |
| LLM returns truncated JSON | Automatic retry with higher token limit |
| LLM returns no valid JSON | Raises error, logged with full response |
| Field not found in document | Returns "NOT FOUND" for that field |
| Multiple files | Processes sequentially, last result in clipboard |

## Privacy

Everything runs locally:

- **Ollama** runs on your machine — no data leaves your network
- **Vision OCR** uses Apple's on-device neural engine
- **No cloud APIs**, no telemetry, no external dependencies at runtime

## License

MIT
