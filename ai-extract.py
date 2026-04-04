#!/usr/bin/env python3
"""
Extract payment data from PDF invoices using Ollama.
Detects recipient, IBAN, amount, and payment reference, copies to clipboard.
Scanned PDFs are automatically detected and processed via macOS Vision OCR.

Usage: ai-extract.py <file.pdf> [file2.pdf ...]
"""

import sys
import os
import json
import subprocess
import urllib.request
import urllib.error
import tempfile
import re
import logging

# Extend PATH for Quick Actions / Services (Homebrew, Swiftly)
for p in ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.swiftly/bin")]:
    if p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = p + ":" + os.environ.get("PATH", "")

# Configuration
OLLAMA_MODEL = "qwen3.5:9b"
OLLAMA_API = "http://localhost:11434/api/chat"
MAX_TEXT_CHARS = 6000
SCAN_WORD_THRESHOLD = 0.3  # Min ratio of real words (>=3 letters) in extracted text
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
SWIFT_BINARY = os.path.join(SCRIPT_DIR, "pdf-text-extract")
OCR_BINARY = os.path.join(SCRIPT_DIR, "pdf-ocr")
LOG_FILE = os.path.join(SCRIPT_DIR, "ai-extract.log")

# Logging (max 1 MB, keeps 1 old backup)
from logging.handlers import RotatingFileHandler
_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=1)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(_handler)
logging.getLogger().setLevel(logging.DEBUG)


def notify(title, message):
    """Show macOS notification."""
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'display notification "{safe_msg}" with title "{safe_title}"'],
        capture_output=True,
    )


def compile_swift_binary(source, output_path, frameworks):
    """Compile Swift source to binary (one-time, result is cached)."""
    if os.path.isfile(output_path):
        return True

    with tempfile.NamedTemporaryFile(mode="w", suffix=".swift", delete=False) as f:
        f.write(source)
        src = f.name

    try:
        fw_args = []
        for fw in frameworks:
            fw_args += ["-framework", fw]
        r = subprocess.run(
            ["swiftc", "-O"] + fw_args + [src, "-o", output_path],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return True
        # Fallback without explicit frameworks
        r = subprocess.run(
            ["swiftc", "-O", src, "-o", output_path],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return True
        sys.stderr.write(f"Swift compilation failed: {r.stderr[:300]}\n")
        return False
    finally:
        os.unlink(src)


def compile_swift_extractor():
    """Compile Swift binary for PDF text extraction (one-time)."""
    return compile_swift_binary("""\
import PDFKit
import Foundation
guard CommandLine.arguments.count > 1 else {
    fputs("Usage: pdf-text-extract <file.pdf>\\n", stderr)
    exit(1)
}
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let doc = PDFDocument(url: url) else {
    fputs("Cannot open PDF\\n", stderr)
    exit(1)
}
print(doc.string ?? "")
""", SWIFT_BINARY, ["PDFKit", "Quartz"])


def compile_ocr_binary():
    """Compile Swift binary for Vision OCR (one-time)."""
    return compile_swift_binary("""\
import Vision
import CoreGraphics
import Foundation

guard CommandLine.arguments.count > 1 else {
    fputs("Usage: pdf-ocr <file.pdf>\\n", stderr)
    exit(1)
}

let url = URL(fileURLWithPath: CommandLine.arguments[1]) as CFURL
guard let doc = CGPDFDocument(url) else {
    fputs("Cannot open PDF\\n", stderr)
    exit(1)
}

for pageNum in 1...doc.numberOfPages {
    guard let page = doc.page(at: pageNum) else { continue }

    let box = page.getBoxRect(.mediaBox)
    let scale: CGFloat = 3.0
    let w = Int(box.width * scale)
    let h = Int(box.height * scale)

    guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
          let ctx = CGContext(data: nil, width: w, height: h,
                             bitsPerComponent: 8, bytesPerRow: 0,
                             space: cs,
                             bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue)
    else { continue }

    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: scale, y: scale)
    ctx.drawPDFPage(page)

    guard let image = ctx.makeImage() else { continue }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["de-DE", "en-US"]
    request.usesLanguageCorrection = true
    request.minimumTextHeight = 0.0

    let handler = VNImageRequestHandler(cgImage: image)
    do {
        try handler.perform([request])
    } catch {
        fputs("OCR error page \\(pageNum): \\(error)\\n", stderr)
        continue
    }

    guard let results = request.results else { continue }
    for obs in results {
        if let text = obs.topCandidates(1).first?.string {
            print(text)
        }
    }
}
""", OCR_BINARY, ["Vision", "CoreGraphics"])


def is_scanned_pdf(pdf_path):
    """Check via pdffonts whether the PDF is a scan (no embedded fonts)."""
    try:
        r = subprocess.run(
            ["pdffonts", pdf_path],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            # Header + separator = 2 lines; anything beyond = fonts
            lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
            has_fonts = len(lines) > 2
            logging.debug(f"pdffonts: {len(lines)-2} font(s) -> {'digital' if has_fonts else 'scan'}")
            return not has_fonts
    except FileNotFoundError:
        logging.warning("pdffonts not found")
    return False


def is_scan_garbage(text):
    """Check whether pdftotext output is scan noise (no real words)."""
    if not text.strip():
        return True
    words = text.split()
    if not words:
        return True
    real_words = sum(1 for w in words if len(re.sub(r'[^a-zA-ZäöüÄÖÜß]', '', w)) >= 3)
    ratio = real_words / len(words)
    logging.debug(f"Text quality: {real_words}/{len(words)} real words ({ratio:.0%})")
    return ratio < SCAN_WORD_THRESHOLD


def extract_text(pdf_path):
    """Extract text from PDF (pdftotext -> Swift/PDFKit fallback)."""
    logging.info(f"Extracting text: {pdf_path}")

    try:
        r = subprocess.run(
            ["pdftotext", pdf_path, "-"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            logging.info(f"pdftotext OK, {len(r.stdout)} chars")
            return r.stdout.strip()
        logging.warning(f"pdftotext failed (rc={r.returncode}): {r.stderr[:200]}")
    except FileNotFoundError:
        logging.warning("pdftotext not found")

    if compile_swift_extractor():
        r = subprocess.run(
            [SWIFT_BINARY, pdf_path],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            logging.info(f"Swift extractor OK, {len(r.stdout)} chars")
            return r.stdout.strip()
        logging.warning(f"Swift extractor failed: {r.stderr[:200]}")

    logging.error(f"No text extraction possible: {pdf_path}")
    return ""


def ocr_native(pdf_path):
    """OCR via macOS Vision framework (VNRecognizeTextRequest)."""
    if not compile_ocr_binary():
        raise RuntimeError("Failed to compile Vision OCR binary")

    logging.info(f"Starting Vision OCR: {pdf_path}")
    r = subprocess.run(
        [OCR_BINARY, pdf_path],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Vision OCR failed: {r.stderr[:200]}")

    text = r.stdout.strip()
    logging.info(f"Vision OCR OK, {len(text)} chars")
    return text


def query_ollama(text_pdftotext, text_ocr, filename, num_predict=512):
    """Query Ollama for payment data (chat API, thinking disabled)."""

    # Build text block from one or both sources
    if text_pdftotext and text_ocr:
        text_block = f"""Es liegen zwei Textversionen des Dokuments vor. Nutze beide zum Abgleich.
Bei Widersprüchen bevorzuge die vollständigere/klarere Version.

=== VERSION A (digital extrahiert) ===
{text_pdftotext[:MAX_TEXT_CHARS]}

=== VERSION B (optische Zeichenerkennung) ===
{text_ocr[:MAX_TEXT_CHARS]}"""
    else:
        text_block = (text_pdftotext or text_ocr or "")[:MAX_TEXT_CHARS]

    prompt = f"""Lies das folgende Dokument KOMPLETT und sorgfältig durch. Extrahiere die Zahlungsdaten für eine Überweisung.

WICHTIG: Gib NUR Informationen aus, die WÖRTLICH im Dokumenttext stehen. Erfinde oder rate NICHTS. Wenn du etwas nicht findest, lass es weg.

Aufgabe:
1. Empfänger (Name/Firma, an die überwiesen werden soll)
2. IBAN (im Format DExx xxxx xxxx xxxx xxxx xx). Falls mehrere Bankverbindungen angegeben sind, bevorzuge IMMER die Sparkasse (BLZ beginnt mit 1, 2, 3, 4, 5 — typische Sparkassen-IBANs: DE.. 5xxx, DE.. 3xxx etc.). Sparkassen-Konten erkennst du auch an Begriffen wie "Sparkasse", "Kreissparkasse", "Stadtsparkasse" im Bankname.
3. Betrag (Endbetrag/Zahlbetrag als Zahl mit Komma, z.B. "123,45")
4. Verwendungszweck — der WICHTIGSTE Teil:
   - Wähle als "verwendungszweck" die wichtigste Referenz zur Zahlungszuordnung (meist Rechnungsnummer, Vorgangsnummer, Vertragskonto, Kundennummer o.ä.)
   - Nummern und Referenzen IMMER VOLLSTÄNDIG übernehmen — niemals kürzen oder abschneiden
   - Überlege dann selbstständig: Welche weiteren Infos aus dem Dokument helfen dem Empfänger, die Zahlung zuzuordnen?
   - Liste unter "additional_info" alle nützlichen Zuordnungsinfos, die WÖRTLICH im Text stehen, z.B.:
     * Rechnungsdatum
     * Patientennummer / Kundennummer
     * Versicherungsnummer / Aktenzeichen
     * Name des Rechnungsempfängers
   - Formatiere jede Info als "Bezeichnung: Wert"
   - NICHT relevant: Steuernummern, Telefonnummern, Adressen, BLZ

Antwort NUR als JSON, ohne Erklärungen:
{{"recipient": "Name", "iban": "DExx xxxx xxxx xxxx xxxx xx", "amount": "123,45", "reference": "Rechnungsnr. 12345", "additional_info": ["Datum: 01.03.2026", "Kundennr.: 67890"]}}

REGELN:
- Nur Daten ausgeben, die WÖRTLICH im Text stehen — nichts erfinden oder raten
- "NOT FOUND" nur bei recipient, iban, amount
- additional_info: leeres Array [] falls nichts Weiteres gefunden

Dateiname: {filename}

{text_block}"""

    logging.info(f"Sending {len(prompt)} chars to Ollama ({OLLAMA_MODEL})")

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "think": False,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": num_predict},
    }).encode()

    req = urllib.request.Request(
        OLLAMA_API, data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"Ollama not reachable ({OLLAMA_API}). Is the server running? Error: {e}"
        )

    response = data.get("message", {}).get("content", "")
    logging.info(f"Ollama response ({len(response)} chars): {response[:200]}")

    match = re.search(r"\{[^{}]*\}", response)
    if match:
        result = json.loads(match.group())
        required = ["recipient", "iban", "amount", "reference"]
        if all(k in result for k in required):
            return normalize_result(result)

    raise ValueError(f"No valid JSON response: {response[:200]}")


def normalize_result(data):
    """Normalize amount and IBAN formatting."""
    # Amount: dot -> comma (22.04 -> 22,04)
    amount = data.get("amount", "")
    if amount and amount != "NOT FOUND":
        amount = re.sub(r'(\d)\.(\d{2})$', r'\1,\2', amount)
        data["amount"] = amount

    # IBAN: normalize spaces to 4-character groups
    iban = data.get("iban", "")
    if iban and iban != "NOT FOUND":
        iban_clean = iban.replace(" ", "")
        if len(iban_clean) == 22 and iban_clean[:2].isalpha():
            data["iban"] = " ".join(iban_clean[i:i+4] for i in range(0, len(iban_clean), 4))

    return data


def to_clipboard(text):
    """Copy text to macOS clipboard."""
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p.communicate(text.encode("utf-8"))


def format_payment(data):
    """Format payment data as readable text."""
    lines = [
        f"Recipient:  {data['recipient']}",
        f"IBAN:       {data['iban']}",
        f"Amount:     {data['amount']} EUR",
        f"Reference:  {data['reference']}",
    ]
    infos = data.get("additional_info", [])
    if infos:
        lines.append("Info:       " + " | ".join(infos))
    return "\n".join(lines)


def process(filepath):
    """Process a single file: extract text -> LLM -> clipboard."""
    name = os.path.basename(filepath)
    text_pdftotext = ""
    text_ocr = ""

    # Step 1: pdftotext (fast, accurate for digital PDFs)
    text_pdftotext = extract_text(filepath)
    if text_pdftotext and is_scan_garbage(text_pdftotext):
        logging.info("pdftotext output is scan noise, discarding")
        text_pdftotext = ""

    # Step 2: Always run Vision OCR for cross-referencing
    try:
        text_ocr = ocr_native(filepath)
    except Exception as e:
        logging.error(f"Vision OCR failed: {e}")

    if not text_pdftotext and not text_ocr:
        notify("Error", f"No text extractable: {name}")
        return None

    # Step 3: LLM extraction with retry on JSON failure
    try:
        result = query_ollama(text_pdftotext, text_ocr, name)
    except ValueError:
        logging.info("Retrying with higher num_predict (768)")
        result = query_ollama(text_pdftotext, text_ocr, name, num_predict=768)

    formatted = format_payment(result)

    to_clipboard(formatted)
    notify("Payment data copied", f"{result['recipient']} — {result['amount']} EUR")
    logging.info(f"OK  {name}: {result['recipient']}, {result['amount']} EUR")
    return formatted


def main():
    if len(sys.argv) < 2:
        print("Usage: ai-extract.py <file.pdf> [file2.pdf ...]")
        sys.exit(1)

    filepaths = []
    for arg in sys.argv[1:]:
        fp = os.path.abspath(arg)
        if not os.path.isfile(fp):
            print(f"Not found: {fp}")
            sys.exit(1)
        filepaths.append(fp)

    errors = 0
    for filepath in filepaths:
        try:
            result = process(filepath)
            if result:
                print(result)
                if len(filepaths) == 1:
                    print("\n-> Copied to clipboard")
                else:
                    print(f"\n-> Copied ({os.path.basename(filepath)})")
                    print("-" * 40)
            else:
                print(f"ERR: Processing failed: {os.path.basename(filepath)}")
                errors += 1
        except Exception as e:
            print(f"ERR: {os.path.basename(filepath)}: {e}")
            logging.error(f"{os.path.basename(filepath)}: {e}", exc_info=True)
            notify("Error", str(e)[:100])
            errors += 1

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
