#!/usr/bin/env python3
"""Open TBox in its own application window (like a desktop app).

    python launch_tbox.py                 start TBox if needed, open it in a Chrome app window
    python launch_tbox.py --lan           also reachable from phones/tablets on the same Wi-Fi (no HTTPS!)
    python launch_tbox.py --install-shortcut   create a Desktop shortcut / app-menu entry with the TBox icon
    python launch_tbox.py --stop          stop the background server
"""
import argparse
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"


def notify(title, text):
    """Show a message even when there is no console (pythonw / double-click)."""
    if sys.stdout is not None and sys.stdout.isatty():
        print("\n%s\n%s\n" % (title, text))
        return
    try:
        import tkinter
        from tkinter import messagebox
        r = tkinter.Tk()
        r.withdraw()
        messagebox.showinfo(title, text)
        r.destroy()
    except Exception:
        print(title, text)


def healthy(url):
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def start_server(a):
    data = Path(a.data_dir).resolve()
    (data / "logs").mkdir(parents=True, exist_ok=True)
    out = data / "logs" / "server.out"
    first_run = not (data / "tbox.db").exists()
    env = dict(os.environ, TBOX_DATA_DIR=str(data), TBOX_PORT=str(a.port), TBOX_HOST="0.0.0.0" if a.lan else "127.0.0.1")
    if first_run and not a.demo:
        env.setdefault("TBOX_SEED_DEMO", "0")
    log = open(out, "ab")
    kw = {"cwd": str(ROOT / "server"), "env": env, "stdin": subprocess.DEVNULL, "stdout": log, "stderr": log}
    if IS_WIN:
        kw["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
        exe = Path(sys.executable)
        py = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
    else:
        kw["start_new_session"] = True
        py = sys.executable
    p = subprocess.Popen([str(py), str(ROOT / "server" / "run_server.py")], **kw)
    (data / "tbox.pid").write_text(str(p.pid))
    url = "http://127.0.0.1:%d" % a.port
    for _ in range(60):
        if healthy(url):
            break
        if p.poll() is not None:
            notify("TBox could not start", "See the log file:\n%s" % out)
            sys.exit(1)
        time.sleep(0.25)
    else:
        notify("TBox did not start in time", "See the log file:\n%s" % out)
        sys.exit(1)
    if first_run:
        txt = out.read_text(errors="replace")
        if "password:" in txt:
            pw = txt.split("password:")[1].split()[0]
            notify("TBox first start", "Sign in as   admin\nPassword:   %s\n\nWrite it down: it is shown only once. You will be asked to change it." % pw)


def find_chrome():
    cands = []
    if IS_WIN:
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                cands += [Path(base) / "Google/Chrome/Application/chrome.exe", Path(base) / "Microsoft/Edge/Application/msedge.exe"]
    elif sys.platform == "darwin":
        cands += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        w = shutil.which(name)
        if w:
            cands.append(Path(w))
    return next((str(c) for c in cands if c.exists()), None)


def open_window(url):
    chrome = find_chrome()
    if chrome:
        subprocess.Popen([chrome, "--app=" + url, "--window-size=1366,860"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **({} if IS_WIN else {"start_new_session": True}))
    else:
        import webbrowser
        webbrowser.open(url)


def make_icon(path):
    """64x64 TBox icon (amber T in a rounded frame on dark), PNG-in-ICO, no external libraries."""
    n = 64
    rows = []
    for y in range(n):
        row = bytearray()
        for x in range(n):
            frame = (8 <= x < 56 and 8 <= y < 56) and not (12 <= x < 52 and 12 <= y < 52)
            tee = (18 <= x < 46 and 21 <= y < 28) or (28 <= x < 36 and 21 <= y < 45)
            row += bytes((255, 176, 0, 255)) if frame or tee else bytes((14, 20, 27, 255))
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.write_bytes(struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", n, n, 0, 0, 1, 32, len(png), 22) + png)
    (path.with_suffix(".png")).write_bytes(png)


def install_shortcut(a):
    ico = ROOT / "tbox.ico"
    make_icon(ico)
    args = [str(ROOT / "launch_tbox.py")] + (["--lan"] if a.lan else []) + ["--port", str(a.port)]
    if IS_WIN:
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
        ps = ("$d=[Environment]::GetFolderPath('Desktop');$s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\\TBox.lnk\");"
              "$s.TargetPath='%s';$s.Arguments='%s';$s.WorkingDirectory='%s';$s.IconLocation='%s';$s.Description='TBox - Technical Power Systems';$s.Save()"
              % (pyw, " ".join('"%s"' % x for x in args).replace("'", "''"), ROOT, ico))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        print("Shortcut 'TBox' created on your Desktop. Pin it to the taskbar if you like.")
    elif sys.platform == "darwin":
        print("On macOS: open Chrome at http://127.0.0.1:%d, menu > Save and share > Install page as app." % a.port)
    else:
        entry = ("[Desktop Entry]\nType=Application\nName=TBox\nComment=Technical Power Systems\nExec=%s %s\nIcon=%s\nTerminal=false\nCategories=Utility;\n"
                 % (sys.executable, " ".join('"%s"' % x for x in args), ROOT / "tbox.png"))
        for d in (Path.home() / ".local/share/applications", Path.home() / "Desktop"):
            if d.exists() or d.name == "applications":
                d.mkdir(parents=True, exist_ok=True)
                f = d / "tbox.desktop"
                f.write_text(entry)
                f.chmod(0o755)
                print("Created", f)


def stop(a):
    pid_file = Path(a.data_dir) / "tbox.pid"
    if not pid_file.exists():
        return print("TBox server is not running (no pid file).")
    pid = int(pid_file.read_text())
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
        else:
            os.kill(pid, 15)
        print("TBox server stopped.")
    except OSError as e:
        print("Could not stop process %s: %s" % (pid, e))
    pid_file.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(description="Open TBox in its own window")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--lan", action="store_true", help="listen on the local network so phones/tablets can connect (plain HTTP)")
    ap.add_argument("--demo", action="store_true", help="create the SIMULATED demo site on first start")
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--install-shortcut", action="store_true")
    a = ap.parse_args()
    if a.stop:
        return stop(a)
    if a.install_shortcut:
        return install_shortcut(a)
    url = "http://127.0.0.1:%d" % a.port
    if not healthy(url):
        start_server(a)
    if a.lan:
        ip = lan_ip()
        if ip:
            notify("TBox on your network", "Other devices on the same Wi-Fi can open:\nhttp://%s:%d\n\nThis is plain HTTP: use it only on a network you trust." % (ip, a.port))
    if not a.no_window:
        open_window(url)


if __name__ == "__main__":
    main()
