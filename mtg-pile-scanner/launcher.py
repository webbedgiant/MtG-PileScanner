import socket, sys, threading, webbrowser
import uvicorn

PORT = 8765
URL = f"http://127.0.0.1:{PORT}"


def already_running():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


if __name__ == "__main__":
    if already_running():
        print("MTG Pile Scanner is already running; opening it in your browser.")
        webbrowser.open(URL)
        sys.exit(0)
    threading.Timer(1.5, lambda: webbrowser.open(URL)).start()
    print(f"MTG Pile Scanner running at {URL}  (use the Quit button or close this window to stop)")
    uvicorn.run("app.main:app", host="127.0.0.1", port=PORT, log_level="warning")
