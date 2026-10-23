# MTG Pile Scanner (Windows + macOS)

**Windows:** install Python 3.12 (python.org, tick "Add to PATH") → double-click `setup.bat` once → `run.bat`.
**macOS:** install Python 3.12 (python.org or `brew install python@3.12`) → double-click `setup.command` once → `run.command`.
If double-clicking does nothing or macOS blocks it, open Terminal, type `cd ` and drag the folder into the window, press Enter, then run `chmod +x *.command` once (or just run `bash setup.command`, later `bash run.command`). If Gatekeeper complains, right-click the file → Open.

Your browser opens at http://127.0.0.1:8765. First launch downloads Scryfall card data and then card-art fingerprints
(several hundred MB, resumable). Webcam works in Chrome, Edge, Firefox and Safari (allow camera access when asked).

Data folder (use the "Data folder" button in the Collection tab):
- Windows `%APPDATA%\MTGPileScanner`, macOS `~/Library/Application Support/MTGPileScanner`
- `scanner.db` (cards + your collection), `collection.xlsx` (auto-updated), `backups/` (daily CSV, last 14), `app.log`

Problems? Run `diagnose.bat` / `diagnose.command` and send `diagnose.txt`.

## iPhone as a camera
Click **📱 iPhone camera** in the Scan tab, point the iPhone Camera app at a QR code, tap the link (same Wi-Fi).
- Code 1 (Photos): "Take photo" opens the normal iPhone camera; each full-resolution photo is scanned automatically.
- Code 2 (Live): needs a one-time "Show Details → visit this website" security OK (self-signed certificate) and camera permission.
Windows will ask to allow network access the first time: choose **Private networks**. Only the phone pages are reachable
from other devices (and need a secret token); everything else stays localhost-only. Set `MTG_LAN=0` to turn the phone link off.
On a Mac you can also pick the iPhone directly in the webcam dropdown (macOS Continuity Camera), no link needed.

## Scan modes and Auto scan
- **Instant** (default for webcam / iPhone live): finds each card, reads only its title strip (and set/number line), confirms with
  the artwork. Meant for cards lying separately; typically well under a second. If it finds no card shapes it falls back to a quick
  single-pass read of the whole frame.
- **Full** (default for uploaded photos): reads the whole photo; use it for dense, overlapping piles. Choose a mode explicitly with the
  Speed dropdown.
- **🤖 Auto scan** (webcam or iPhone live camera): scans by itself ~0.35 s after the picture settles. It counts by card identity:
  a card already in view is never counted again, a card hidden for one scan is not forgotten, and a second physical copy of the same
  card counts once it is present alongside (or after the table was cleared). Press Clear to start over.
