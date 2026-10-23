import os, socket, sys, threading, webbrowser
import uvicorn

PORT, HTTPS_PORT = 8765, 8766
LAN = os.environ.get("MTG_LAN", "1") != "0"   # set MTG_LAN=0 to disable the iPhone link (localhost only)
HOST = "0.0.0.0" if LAN else "127.0.0.1"
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
    from app import phone
    from app.main import app
    phone.STATE["http_port"] = PORT
    if LAN:
        try:  # optional https listener: lets the iPhone use its live camera (Safari requires https for that)
            from app import certs
            cert, key = certs.ensure([phone.lan_ip(), "127.0.0.1"])
            cfg = uvicorn.Config(app, host=HOST, port=HTTPS_PORT, ssl_certfile=str(cert), ssl_keyfile=str(key),
                                 log_level="warning", lifespan="off")
            threading.Thread(target=uvicorn.Server(cfg).run, daemon=True).start()

            def verify():  # publish the secure link only if a real https request to ourselves succeeds
                problem = phone.selfcheck_https(HTTPS_PORT, cert)
                if problem:
                    phone.STATE["https_error"] = problem
                    print("Live iPhone mode disabled (photo mode still works). Secure port test failed:", problem)
                else:
                    phone.STATE["https_port"] = HTTPS_PORT
            threading.Thread(target=verify, daemon=True).start()
        except Exception as e:
            phone.STATE["https_error"] = f"{type(e).__name__}: {e}"
            print("Live iPhone mode unavailable (photo mode still works):", e)
        print(f"iPhone link: open the QR code shown under 'iPhone camera' in the app (computer is {phone.lan_ip()})")
    threading.Timer(1.5, lambda: webbrowser.open(URL)).start()
    print(f"MTG Pile Scanner running at {URL}  (use the Quit button or close this window to stop)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
