# MtG-PileScanner

Windows: install Python 3.12 (python.org, tick "Add to PATH") → double-click setup.bat once → run.bat. macOS: install Python 3.12 (python.org or brew install python@3.12) → double-click setup.command once → run.command. If double-clicking does nothing or macOS blocks it, open Terminal, type cd and drag the folder into the window, press Enter, then run chmod +x *.command once (or just run bash setup.command, later bash run.command). If Gatekeeper complains, right-click the file → Open.

Your browser opens at http://127.0.0.1:8765. First launch downloads Scryfall card data and then card-art fingerprints (several hundred MB, resumable). Webcam works in Chrome, Edge, Firefox and Safari (allow camera access when asked).

Data folder (use the "Data folder" button in the Collection tab):

    Windows %APPDATA%\MTGPileScanner, macOS ~/Library/Application Support/MTGPileScanner
    scanner.db (cards + your collection), collection.xlsx (auto-updated), backups/ (daily CSV, last 14), app.log

Problems? Run diagnose.bat / diagnose.command and send diagnose.txt.
