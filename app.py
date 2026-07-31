import os, shutil, html, urllib.parse, pty, select, termios, struct, fcntl, signal, threading, json, time
from flask import Flask, request, redirect, send_file, Response
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple

app = Flask(__name__)
app.config["APPLICATION_ROOT"] = "/fm"

SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "AUTH", "COOKIE")

def is_sensitive(name):
    return any(h in name.upper() for h in SECRET_HINTS)

def mask_secrets(text):
    out = []
    for line in text.splitlines(keepends=True):
        if any(h in line.upper() for h in SECRET_HINTS) and ("=" in line or ":" in line):
            out.append(line)
        else:
            out.append(line)
    return "".join(out)

# =================================================================
# PERSISTENT PTY SHELL with scrollback (reattachable). Starts at "/".
# =================================================================
SCROLLBACK_LIMIT = 200_000   # chars of history kept per shell

class PtyShell:
    def __init__(self, name):
        self.name = name
        self.created = time.time()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.chdir("/")
            os.execvp("bash", ["bash", "-i"])
        else:
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            self._set_size(40, 120)
            self.history = ""      # full scrollback (for reattach)
            self.pending = {}      # per-viewer unread cursor: viewer_id -> index into history
            self.lock = threading.Lock()
            threading.Thread(target=self._reader, daemon=True).start()

    def _set_size(self, rows, cols):
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def _reader(self):
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.5)
                if r:
                    data = os.read(self.fd, 65536)
                    if not data:
                        break
                    with self.lock:
                        self.history += data.decode(errors="replace")
                        if len(self.history) > SCROLLBACK_LIMIT:
                            self.history = self.history[-SCROLLBACK_LIMIT:]
            except OSError:
                break

    def write(self, text):
        try:
            os.write(self.fd, text.encode())
        except OSError:
            pass

    def snapshot(self):
        # full history — used when (re)attaching to replay what happened
        with self.lock:
            return mask_secrets(self.history), len(self.history)

    def read_from(self, cursor):
        # incremental output since a given cursor position
        with self.lock:
            new = self.history[cursor:]
            return mask_secrets(new), len(self.history)

    def alive(self):
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            return pid == 0
        except OSError:
            return False

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass

# --- Session manager: named shells that outlive browser tabs ---
_shells = {}          # name -> PtyShell
_counter = {"n": 0}
_slock = threading.Lock()

def create_shell():
    with _slock:
        _counter["n"] += 1
        name = f"Terminal {_counter['n']}"
        _shells[name] = PtyShell(name)
        return name

def get_shell(name):
    sh = _shells.get(name)
    if sh and not sh.alive():
        del _shells[name]
        return None
    return sh

def list_shells():
    # prune dead ones, return live session names
    for n in list(_shells.keys()):
        if not _shells[n].alive():
            del _shells[n]
    return sorted(_shells.keys(), key=lambda n: _shells[n].created)

PAGE = """<!doctype html><html><head><title>Box</title>
<style>
body{{font-family:monospace;margin:20px;background:#0b0b0b;color:#eee}}
a{{color:#29BEFD;text-decoration:none}} a:hover{{text-decoration:underline}}
table{{border-collapse:collapse;width:100%}} td,th{{padding:4px 8px;border-bottom:1px solid #222;text-align:left}}
.bar{{background:#1a1a1a;padding:10px;margin-bottom:10px;border-radius:6px}}
.tabs a{{margin-right:14px;font-weight:bold}}
.tab{{display:inline-block;padding:6px 14px;margin-right:6px;background:#1a1a1a;border-radius:6px 6px 0 0;cursor:pointer}}
.tab.active{{background:#F46821;color:#fff}}
pre{{background:#000;color:#e6e6e6;padding:12px;border-radius:6px;overflow:auto;height:60vh;white-space:pre-wrap}}
input[type=text]{{background:#000;color:#eee;border:1px solid #333;padding:8px;font-family:monospace}}
textarea{{width:100%;background:#000;color:#47E6C1;border:1px solid #333;padding:8px}}
button{{background:#F46821;color:#fff;border:0;padding:8px 14px;border-radius:4px;cursor:pointer}}
</style></head><body>
<div class="tabs bar"><a href="/fm/">📁 Files</a><a href="/fm/term">🖥️ Terminals</a>
<a href="/reload">♻️ Reload</a><a href="/gpuinfo">🎮 GPU</a>
<span style="color:#777">test mode · ephemeral</span></div>
{body}</body></html>"""

# ---------------- WINDOWS-11-STYLE TERMINAL WORKSPACE ----------------
@app.route("/term")
def term_page():
    body = """
    <div class="bar">
      <b>Terminals</b> — sessions live on the box and survive closing the browser.
      <span style="color:#777">Reopen this page anytime to reattach.</span>
      <span style="color:#47E6C1;margin-left:12px">Paste: Ctrl/Cmd+V or right-click · Copy: select then Ctrl/Cmd+C</span>
    </div>
    <div id="tabs"></div>
    <div id="term" style="height:68vh;background:#000;border-radius:0 6px 6px 6px"></div>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css"/>
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
    <script>
      let term, fit, current = null, cursor = 0, polling = false;
      // send raw bytes to the PTY (used by both typing and pasting)
      function sendData(d){
        if(!current) return;
        fetch('/fm/term_in', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({name: current, data: d})});
      }
      // FIX 4: robust paste. Reads the clipboard and streams it to the PTY.
      async function doPaste(){
        if(!current) return;
        try {
          const text = await navigator.clipboard.readText();
          if(text) sendData(text);
        } catch(e) {
          // Clipboard API blocked (insecure context / permissions).
          // Fall back to a prompt so the user can still paste manually.
          const text = window.prompt('Paste here (clipboard access was blocked):', '');
          if(text) sendData(text);
        }
      }
      // FIX 5: copy current selection to the clipboard.
      async function doCopy(){
        const sel = term.getSelection();
        if(sel){
          try { await navigator.clipboard.writeText(sel); } catch(e) {}
        }
      }
      function initTerm(){
        document.getElementById('term').innerHTML = '';
        term = new Terminal({convertEol:false, cursorBlink:true, fontFamily:'monospace', fontSize:13});
        fit = new FitAddon.FitAddon();
        term.loadAddon(fit);
        term.open(document.getElementById('term'));
        fit.fit();
        // FIX 1: only SEND the keystroke; never echo locally.
        // The PTY (bash) echoes it back through the poll, so echoing here = duplicates.
        term.onData(d => sendData(d));
        // FIX 6: intercept clipboard shortcuts BEFORE xterm consumes them.
        // Returning false stops xterm from also handling the key.
        term.attachCustomKeyEventHandler((e) => {
          const mod = e.ctrlKey || e.metaKey;   // Ctrl on Win/Linux, Cmd on Mac
          if(e.type === 'keydown' && mod && e.key.toLowerCase() === 'v'){
            e.preventDefault();
            doPaste();
            return false;
          }
          // Only hijack Ctrl/Cmd+C for copy when there IS a selection,
          // otherwise let it through so Ctrl+C still sends SIGINT to bash.
          if(e.type === 'keydown' && mod && e.key.toLowerCase() === 'c' && term.hasSelection()){
            e.preventDefault();
            doCopy();
            return false;
          }
          return true;
        });
        // Right-click / middle-click / browser paste event fallback.
        const el = document.getElementById('term');
        el.addEventListener('paste', (e) => {
          e.preventDefault();
          const text = (e.clipboardData || window.clipboardData).getData('text');
          if(text) sendData(text);
        });
        // Right-click = paste (common terminal convention)
        el.addEventListener('contextmenu', (e) => { e.preventDefault(); doPaste(); });
      }
      window.addEventListener('resize', () => { if(fit) fit.fit(); });
      async function refreshTabs(){
        const r = await fetch('/fm/term_list');
        const names = (await r.json()).sessions;
        const wrap = document.getElementById('tabs');
        wrap.innerHTML = '';
        names.forEach(n => {
          const t = document.createElement('span');
          t.className = 'tab' + (n===current ? ' active':'');
          const label = document.createElement('span');
          label.textContent = n + ' ';
          label.onclick = () => attach(n);
          // FIX 3: explicit close button, separate from tab-switch click
          const x = document.createElement('span');
          x.textContent = '\\u2715';
          x.style.marginLeft = '8px';
          x.style.color = '#F43256';
          x.onclick = (e) => { e.stopPropagation(); closeSession(n); };
          t.appendChild(label);
          t.appendChild(x);
          wrap.appendChild(t);
        });
        const add = document.createElement('span');
        add.className = 'tab'; add.textContent = '\\uFF0B New';
        add.onclick = newSession;
        wrap.appendChild(add);
        if(current && !names.includes(current)){ current = null; }
        if(!current && names.length){ attach(names[0]); }
      }
      async function attach(name){
        if(current === name){ return; }
        current = name; cursor = 0;
        initTerm();
        const r = await fetch('/fm/term_snapshot?name=' + encodeURIComponent(name));
        const j = await r.json();
        if(j.gone){ current = null; refreshTabs(); return; }
        term.write(j.data); cursor = j.cursor;
        refreshTabs();
        startPoll();
      }
      // FIX 2: single guarded poll loop — never stack multiple loops.
      function startPoll(){
        if(polling) return;
        polling = true;
        async function poll(){
          if(current){
            try{
              const r = await fetch('/fm/term_out?name=' + encodeURIComponent(current) + '&cursor=' + cursor);
              const j = await r.json();
              if(j.gone){ current = null; refreshTabs(); }
              else { if(j.data) term.write(j.data); cursor = j.cursor; }
            }catch(e){}
          }
          setTimeout(poll, 200);
        }
        poll();
      }
      async function newSession(){
        const r = await fetch('/fm/term_new', {method:'POST'});
        const j = await r.json();
        await refreshTabs();
        attach(j.name);
      }
      async function closeSession(name){
        await fetch('/fm/term_close', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({name})});
        if(current === name){ current = null; }
        await refreshTabs();
      }
      (async () => {
        await refreshTabs();
        const r = await fetch('/fm/term_list');
        const names = (await r.json()).sessions;
        if(!names.length){ await newSession(); }
      })();
    </script>"""
    return Response(PAGE.format(body=body))

@app.route("/term_list")
def term_list():
    return Response(json.dumps({"sessions": list_shells()}), mimetype="application/json")

@app.route("/term_new", methods=["POST"])
def term_new():
    return Response(json.dumps({"name": create_shell()}), mimetype="application/json")

@app.route("/term_close", methods=["POST"])
def term_close():
    name = request.get_json(force=True).get("name")
    sh = _shells.get(name)
    if sh:
        sh.kill()                 # SIGKILL the bash process
        _shells.pop(name, None)   # remove from the session list
    return Response(json.dumps({"ok": True}), mimetype="application/json")

@app.route("/term_snapshot")
def term_snapshot():
    sh = get_shell(request.args.get("name", ""))
    if not sh:
        return Response(json.dumps({"data": "", "cursor": 0, "gone": True}), mimetype="application/json")
    data, cursor = sh.snapshot()
    return Response(json.dumps({"data": data, "cursor": cursor}), mimetype="application/json")

@app.route("/term_in", methods=["POST"])
def term_in():
    d = request.get_json(force=True)
    sh = get_shell(d.get("name", ""))
    if sh:
        sh.write(d.get("data", ""))
    return Response(json.dumps({"ok": bool(sh)}), mimetype="application/json")

@app.route("/term_out")
def term_out():
    sh = get_shell(request.args.get("name", ""))
    if not sh:
        return Response(json.dumps({"gone": True}), mimetype="application/json")
    cursor = int(request.args.get("cursor", 0))
    data, newcur = sh.read_from(cursor)
    return Response(json.dumps({"data": data, "cursor": newcur}), mimetype="application/json")

# ---------------- FILE MANAGER (unchanged) ----------------
@app.route("/")
def browse():
    path = os.path.abspath(request.args.get("path", "/"))
    if not os.path.exists(path):
        return Response(PAGE.format(body=f"<p>Not found: {html.escape(path)}</p>"), status=404)
    if os.path.isfile(path):
        return redirect(f"/fm/view?path={urllib.parse.quote(path)}")
    parent = os.path.dirname(path.rstrip("/")) or "/"
    rows = f'<tr><td><a href="/fm/?path={urllib.parse.quote(parent)}">.. (up)</a></td><td></td><td></td></tr>'
    try:
        entries = sorted(os.listdir(path))
    except PermissionError:
        return Response(PAGE.format(body=f"<p>Permission denied: {html.escape(path)}</p>"), status=403)
    for name in entries:
        full = os.path.join(path, name); q = urllib.parse.quote(full)
        try:
            size = os.path.getsize(full) if os.path.isfile(full) else ""
        except OSError:
            size = "?"
        if os.path.isdir(full):
            link = f'<a href="/fm/?path={q}">📁 {html.escape(name)}/</a>'
            actions = f'<a href="/fm/delete?path={q}">delete</a>'
        else:
            link = f'<a href="/fm/view?path={q}">📄 {html.escape(name)}</a>'
            actions = (f'<a href="/fm/download?path={q}">download</a> | '
                       f'<a href="/fm/edit?path={q}">edit</a> | '
                       f'<a href="/fm/delete?path={q}">delete</a>')
        rows += f"<tr><td>{link}</td><td>{size}</td><td>{actions}</td></tr>"
    body = f"""
    <div class="bar"><b>Path:</b> {html.escape(path)}</div>
    <div class="bar">
      <form action="/fm/mkdir" method="post" style="display:inline">
        <input type="hidden" name="path" value="{html.escape(path)}">
        <input name="name" placeholder="new folder"><button type="submit">Create folder</button>
      </form>
      <form action="/fm/touch" method="post" style="display:inline">
        <input type="hidden" name="path" value="{html.escape(path)}">
        <input name="name" placeholder="new file name"><button type="submit">Create file</button>
      </form>
      <form action="/fm/upload" method="post" enctype="multipart/form-data" style="display:inline">
        <input type="hidden" name="path" value="{html.escape(path)}">
        <input type="file" name="file"><button type="submit">Upload</button>
      </form>
    </div>
    <table><tr><th>Name</th><th>Size</th><th>Actions</th></tr>{rows}</table>"""
    return Response(PAGE.format(body=body))

@app.route("/view")
def view():
    path = os.path.abspath(request.args.get("path", ""))
    if not os.path.isfile(path):
        return redirect("/fm/")
    if is_sensitive(os.path.basename(path)):
        content = "*** masked (sensitive filename) ***"
    else:
        try:
            with open(path, "r", errors="replace") as f:
                content = mask_secrets(f.read(200000))
        except Exception as e:
            content = f"[cannot read as text: {e}]"
    q = urllib.parse.quote(path)
    body = (f'<div class="bar"><b>{html.escape(path)}</b> | '
            f'<a href="/fm/edit?path={q}">edit</a> | '
            f'<a href="/fm/download?path={q}">download</a> | '
            f'<a href="/fm/?path={urllib.parse.quote(os.path.dirname(path))}">back</a></div>'
            f"<pre>{html.escape(content)}</pre>")
    return Response(PAGE.format(body=body))

@app.route("/edit")
def edit():
    path = os.path.abspath(request.args.get("path", ""))
    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
    except Exception as e:
        content = f"[cannot open: {e}]"
    body = f"""
    <div class="bar"><b>Editing:</b> {html.escape(path)}</div>
    <form action="/fm/save" method="post">
      <input type="hidden" name="path" value="{html.escape(path)}">
      <textarea name="content" style="height:60vh">{html.escape(content)}</textarea>
      <br><button type="submit">Save (live)</button>
      <a href="/fm/?path={urllib.parse.quote(os.path.dirname(path))}">cancel</a>
    </form>"""
    return Response(PAGE.format(body=body))

@app.route("/save", methods=["POST"])
def save():
    path = os.path.abspath(request.form["path"])
    with open(path, "w") as f:
        f.write(request.form["content"])
    return redirect(f"/fm/view?path={urllib.parse.quote(path)}")

@app.route("/download")
def download():
    path = os.path.abspath(request.args.get("path", ""))
    if is_sensitive(os.path.basename(path)):
        return Response("masked (sensitive filename)", status=403)
    return send_file(path, as_attachment=True)

@app.route("/mkdir", methods=["POST"])
def mkdir():
    base = os.path.abspath(request.form["path"])
    os.makedirs(os.path.join(base, request.form["name"]), exist_ok=True)
    return redirect(f"/fm/?path={urllib.parse.quote(base)}")

@app.route("/touch", methods=["POST"])
def touch():
    base = os.path.abspath(request.form["path"])
    name = request.form.get("name", "").strip()
    if name:
        full = os.path.join(base, name)
        if not os.path.exists(full):
            open(full, "a").close()
        return redirect(f"/fm/edit?path={urllib.parse.quote(full)}")
    return redirect(f"/fm/?path={urllib.parse.quote(base)}")

@app.route("/upload", methods=["POST"])
def upload():
    base = os.path.abspath(request.form["path"])
    f = request.files.get("file")
    if f and f.filename:
        f.save(os.path.join(base, f.filename))
    return redirect(f"/fm/?path={urllib.parse.quote(base)}")

@app.route("/delete")
def delete():
    path = os.path.abspath(request.args.get("path", ""))
    parent = os.path.dirname(path)
    try:
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    except Exception as e:
        return Response(PAGE.format(body=f"<p>Delete failed: {e}</p>"), status=500)
    return redirect(f"/fm/?path={urllib.parse.quote(parent)}")

if __name__ == "__main__":
    wrapped = DispatcherMiddleware(Flask("empty"), {"/fm": app})
    run_simple("0.0.0.0", 9001, wrapped, threaded=True)
