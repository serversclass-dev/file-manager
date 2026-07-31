"""
File Manager + Terminal Backend API (JSON only)
-----------------------------------------------
Pure backend. No HTML / JS / frontend rendering.
Every endpoint returns JSON. Consumed by the single-file PHP
frontend (box.php).

Run:
    python app.py
Serves on: http://0.0.0.0:2245

For security, prefer binding to localhost so only the PHP layer
can reach it:
    change app.run(host="0.0.0.0", ...) to host="127.0.0.1"
"""

import os
import shutil
import pty
import select
import termios
import struct
import fcntl
import signal
import threading
import time
import traceback
import sys
BASE = "/srv/disk18/4773793/www/smartclass.mywebcommunity.org"
SITE_PACKAGES = os.path.join(BASE, "venv", "lib", "python3.13", "site-packages")
if SITE_PACKAGES not in sys.path:
    sys.path.insert(0, SITE_PACKAGES)
from flask import Flask, request, jsonify, send_file, abort
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

# ---------------------------------------------------------------
# Secret masking helpers
# ---------------------------------------------------------------
SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "AUTH", "COOKIE")


def is_sensitive(name: str) -> bool:
    return any(hint in name.upper() for hint in SECRET_HINTS)


def mask_secrets(text: str) -> str:
    # Hook kept for future filtering. Currently a pass-through.
    return text


# ===============================================================
# PERSISTENT TERMINAL / PTY ENGINE
# ===============================================================
SCROLLBACK_LIMIT = 200_000  # characters of scrollback kept per shell


class PtyShell:
    def __init__(self, name):
        self.name = name
        self.created = time.time()
        self.history = ""
        self.lock = threading.Lock()

        # Find bash. Fall back to sh if bash is unavailable.
        self.shell = shutil.which("bash") or shutil.which("sh")

        if not self.shell:
            raise RuntimeError(
                "No shell was found. Install bash or provide /bin/sh."
            )

        try:
            self.pid, self.fd = pty.fork()
        except Exception as error:
            raise RuntimeError(
                f"Could not create PTY: {type(error).__name__}: {error}"
            ) from error

        if self.pid == 0:
            # Child process: this becomes the terminal shell.
            try:
                os.environ["TERM"] = "xterm-256color"
                os.environ["PS1"] = r"\u@\h:\w\$ "
                os.chdir("/")

                if os.path.basename(self.shell) == "bash":
                    os.execv(
                        self.shell,
                        ["bash", "--noprofile", "--norc", "-i"]
                    )
                else:
                    os.execv(self.shell, ["sh", "-i"])

            except Exception:
                traceback.print_exc()
                os._exit(1)

        # Parent process: configure and read PTY output.
        try:
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except Exception as error:
            self.kill()
            raise RuntimeError(
                f"Could not configure PTY: {type(error).__name__}: {error}"
            ) from error

        self._set_size(40, 120)

        threading.Thread(
            target=self._reader,
            daemon=True,
            name=f"pty-reader-{name}"
        ).start()

    def _set_size(self, rows, cols):
        try:
            fcntl.ioctl(
                self.fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0)
            )
        except Exception:
            pass

    def _reader(self):
        while True:
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.5)

                if not ready:
                    continue

                data = os.read(self.fd, 65536)

                if not data:
                    break

                text = data.decode(errors="replace")

                with self.lock:
                    self.history += text
                    if len(self.history) > SCROLLBACK_LIMIT:
                        self.history = self.history[-SCROLLBACK_LIMIT:]

            except OSError:
                break
            except Exception:
                break

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)

        try:
            os.write(self.fd, text.encode())
        except OSError:
            pass

    def snapshot(self):
        with self.lock:
            return mask_secrets(self.history), len(self.history)

    def read_from(self, cursor):
        with self.lock:
            cursor = max(0, min(cursor, len(self.history)))
            new = self.history[cursor:]
            return mask_secrets(new), len(self.history)

    def alive(self):
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            return pid == 0
        except ChildProcessError:
            return False
        except OSError:
            return False

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass

        try:
            os.close(self.fd)
        except OSError:
            pass


# ===============================================================
# TERMINAL SESSION MANAGER
# ===============================================================
_shells = {}
_counter = {"n": 0}
_slock = threading.Lock()


def create_shell():
    with _slock:
        _counter["n"] += 1
        name = f"Terminal {_counter['n']}"
        shell = PtyShell(name)
        _shells[name] = shell
        return name


def get_shell(name):
    shell = _shells.get(name)

    if shell is not None and not shell.alive():
        _shells.pop(name, None)
        return None

    return shell


def list_shells():
    with _slock:
        for name in list(_shells.keys()):
            if not _shells[name].alive():
                _shells.pop(name, None)

        return sorted(
            _shells.keys(),
            key=lambda name: _shells[name].created
        )


# ===============================================================
# FILE MANAGER API
# ===============================================================
@app.route("/api/fs/list")
def fs_list():
    path = os.path.abspath(request.args.get("path", "/"))

    if not os.path.exists(path):
        return jsonify(ok=False, error="not_found", path=path), 404

    if os.path.isfile(path):
        return jsonify(ok=False, error="is_file", path=path), 400

    try:
        entries = sorted(os.listdir(path))
    except PermissionError:
        return jsonify(ok=False, error="permission_denied", path=path), 403

    items = []
    for name in entries:
        full = os.path.join(path, name)
        is_dir = os.path.isdir(full)

        try:
            size = os.path.getsize(full) if os.path.isfile(full) else None
        except OSError:
            size = None

        items.append({
            "name": name,
            "path": full,
            "is_dir": is_dir,
            "size": size,
            "sensitive": is_sensitive(name),
        })

    parent = os.path.dirname(path.rstrip("/")) or "/"
    return jsonify(ok=True, path=path, parent=parent, items=items)


@app.route("/api/fs/read")
def fs_read():
    path = os.path.abspath(request.args.get("path", ""))

    if not os.path.isfile(path):
        return jsonify(ok=False, error="not_a_file", path=path), 404

    if is_sensitive(os.path.basename(path)):
        return jsonify(
            ok=True,
            path=path,
            masked=True,
            content="*** masked (sensitive filename) ***"
        )

    try:
        with open(path, "r", errors="replace") as f:
            content = mask_secrets(f.read(200000))
    except Exception as e:
        return jsonify(ok=False, error="read_failed", detail=str(e)), 500

    return jsonify(ok=True, path=path, masked=False, content=content)


@app.route("/api/fs/write", methods=["POST"])
def fs_write():
    data = request.get_json(force=True, silent=True) or {}
    path = os.path.abspath(data.get("path", ""))

    if not path:
        return jsonify(ok=False, error="missing_path"), 400

    try:
        with open(path, "w") as f:
            f.write(data.get("content", ""))
    except Exception as e:
        return jsonify(ok=False, error="write_failed", detail=str(e)), 500

    return jsonify(ok=True, path=path)


@app.route("/api/fs/mkdir", methods=["POST"])
def fs_mkdir():
    data = request.get_json(force=True, silent=True) or {}
    base = os.path.abspath(data.get("path", ""))
    name = (data.get("name") or "").strip()

    if not name:
        return jsonify(ok=False, error="missing_name"), 400

    full = os.path.join(base, name)

    try:
        os.makedirs(full, exist_ok=True)
    except Exception as e:
        return jsonify(ok=False, error="mkdir_failed", detail=str(e)), 500

    return jsonify(ok=True, path=full)


@app.route("/api/fs/touch", methods=["POST"])
def fs_touch():
    data = request.get_json(force=True, silent=True) or {}
    base = os.path.abspath(data.get("path", ""))
    name = (data.get("name") or "").strip()

    if not name:
        return jsonify(ok=False, error="missing_name"), 400

    full = os.path.join(base, name)

    try:
        if not os.path.exists(full):
            open(full, "a").close()
    except Exception as e:
        return jsonify(ok=False, error="touch_failed", detail=str(e)), 500

    return jsonify(ok=True, path=full)


@app.route("/api/fs/upload", methods=["POST"])
def fs_upload():
    base = os.path.abspath(request.form.get("path", ""))
    f = request.files.get("file")

    if not (f and f.filename):
        return jsonify(ok=False, error="no_file"), 400

    dest = os.path.join(base, f.filename)

    try:
        f.save(dest)
    except Exception as e:
        return jsonify(ok=False, error="upload_failed", detail=str(e)), 500

    return jsonify(ok=True, path=dest)


@app.route("/api/fs/download")
def fs_download():
    path = os.path.abspath(request.args.get("path", ""))

    if is_sensitive(os.path.basename(path)):
        return jsonify(ok=False, error="masked_sensitive"), 403

    if not os.path.isfile(path):
        return jsonify(ok=False, error="not_a_file"), 404

    return send_file(path, as_attachment=True)


@app.route("/api/fs/delete", methods=["POST"])
def fs_delete():
    data = request.get_json(force=True, silent=True) or {}
    path = os.path.abspath(data.get("path", ""))

    if not path or path == "/":
        return jsonify(ok=False, error="invalid_path"), 400

    parent = os.path.dirname(path)

    try:
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    except Exception as e:
        return jsonify(ok=False, error="delete_failed", detail=str(e)), 500

    return jsonify(ok=True, parent=parent)


# ===============================================================
# TERMINAL API ROUTES
# ===============================================================
@app.route("/api/term/list")
def term_list():
    return jsonify(ok=True, sessions=list_shells())


@app.route("/api/term/new", methods=["POST"])
def term_new():
    try:
        name = create_shell()
        return jsonify(ok=True, name=name)
    except Exception as error:
        # Full traceback goes to the Python log.
        app.logger.exception("Terminal creation failed")
        # Real reason is surfaced to the PHP frontend.
        return jsonify(
            ok=False,
            error="terminal_create_failed",
            detail=f"{type(error).__name__}: {error}"
        ), 500


@app.route("/api/term/close", methods=["POST"])
def term_close():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name", "")

    with _slock:
        shell = _shells.pop(name, None)

    if shell:
        shell.kill()

    return jsonify(ok=True)


@app.route("/api/term/snapshot")
def term_snapshot():
    name = request.args.get("name", "")
    shell = get_shell(name)

    if not shell:
        return jsonify(ok=False, gone=True, data="", cursor=0)

    data, cursor = shell.snapshot()
    return jsonify(ok=True, data=data, cursor=cursor)


@app.route("/api/term/input", methods=["POST"])
def term_input():
    data = request.get_json(force=True, silent=True) or {}
    shell = get_shell(data.get("name", ""))

    if not shell:
        return jsonify(ok=False, gone=True)

    shell.write(data.get("data", ""))
    return jsonify(ok=True)


@app.route("/api/term/output")
def term_output():
    name = request.args.get("name", "")
    shell = get_shell(name)

    if not shell:
        return jsonify(ok=False, gone=True, data="", cursor=0)

    try:
        cursor = int(request.args.get("cursor", 0))
    except ValueError:
        cursor = 0

    output, new_cursor = shell.read_from(cursor)
    return jsonify(ok=True, data=output, cursor=new_cursor)


# ===============================================================
# HEALTH
# ===============================================================
@app.route("/api/health")
def health():
    return jsonify(
        ok=True,
        service="fm-term-backend",
        sessions=len(_shells),
        shell=shutil.which("bash") or shutil.which("sh") or None
    )


if __name__ == "__main__":
    # For production, bind to 127.0.0.1 so only the PHP layer can reach it.
    app.run(host="0.0.0.0", port=2245, threaded=True)