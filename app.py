#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================
#  DJ Track Downloader — クラウドWebアプリ版（スマホ対応）
#  ・パスワードロック必須（自分専用）
#  ・URL/曲名リストを送ると yt-dlp で mp3 化
#  ・スマホから各曲を「ファイルに保存」/ まとめてZIP
#  依存: yt-dlp, ffmpeg（Dockerfile で導入）／外部 pip Web フレームワーク不要
# =============================================================
import os, re, io, json, time, html, base64, hashlib, zipfile, threading, subprocess, csv, glob
from urllib.parse import unquote, quote, parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------- 設定（環境変数で上書き可） ----------------
PORT       = int(os.environ.get("PORT", "8765"))
PASSWORD   = os.environ.get("APP_PASSWORD", "").strip()   # ★未設定だとダウンロード不可（安全のため）
OUTDIR     = os.environ.get("OUTDIR", "downloads")
MIN_DUR    = int(os.environ.get("MIN_DUR", "90"))
MAX_DUR    = int(os.environ.get("MAX_DUR", "900"))
SEARCH_N   = int(os.environ.get("SEARCH_N", "6"))
PASSES     = int(os.environ.get("PASSES", "4"))
SLEEP_OK   = int(os.environ.get("SLEEP_OK", "8"))
SLEEP_MISS = int(os.environ.get("SLEEP_MISS", "4"))
COOLDOWN   = int(os.environ.get("COOLDOWN", "45"))
# -----------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
os.makedirs(OUTDIR, exist_ok=True)
URL_RE = re.compile(r'^https?://', re.I)

def token_for(pw): return hashlib.sha256(("scdl:" + pw).encode()).hexdigest()
EXPECTED = token_for(PASSWORD) if PASSWORD else None

STATE = {"running": False, "stop": False, "items": [], "log": [], "pass": 0, "finished": False}
LOCK = threading.Lock()

def log(msg):
    line = time.strftime("%H:%M:%S ") + str(msg)
    with LOCK:
        STATE["log"].append(line)
        if len(STATE["log"]) > 400: STATE["log"] = STATE["log"][-400:]
    print(line, flush=True)

def safe_name(idx, raw, kind):
    s = re.sub(r'^https?://', '', raw) if kind == "url" else raw
    s = re.sub(r'[/\\?:*"<>|]', '-', s).replace('\r', '').strip()
    return f"{idx:03d} - {s}"[:96]

def parse_input(text):
    items, idx = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        idx += 1
        if URL_RE.match(line):
            kind, target = "url", line
        else:
            kind, target = "search", re.sub(r'\s+', ' ', line.replace(',', ' ')).strip()
        items.append({"idx": idx, "kind": kind, "target": target, "label": line,
                      "status": "pending", "dur": None, "file": None,
                      "safe": safe_name(idx, line, kind)})
    return items

def existing_mp3(idx):
    prefix = f"{idx:03d} - "
    for f in os.listdir(OUTDIR):
        if f.startswith(prefix) and f.lower().endswith(".mp3"):
            return os.path.join(OUTDIR, f)
    return None

def download_one(it):
    safe = it["safe"]
    out = os.path.join(OUTDIR, safe + ".%(ext)s")
    common = ["yt-dlp", "--ignore-errors", "--no-warnings", "--no-progress",
              "--socket-timeout", "30", "--retries", "3", "--sleep-requests", "2",
              "-x", "--audio-format", "mp3", "--audio-quality", "0",
              "--embed-metadata", "-o", out]
    if it["kind"] == "url":
        cmd = common + [it["target"]]
    else:
        cmd = common + ["--max-downloads", "1",
                        "--match-filter", f"duration>{MIN_DUR} & duration<{MAX_DUR}",
                        f"scsearch{SEARCH_N}:{it['target']}"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        log("   ⏱ timeout")
    except FileNotFoundError:
        log("❌ yt-dlp not found"); return False, None
    f = os.path.join(OUTDIR, safe + ".mp3")
    return (True, f) if os.path.exists(f) else (False, None)

def worker(items):
    with LOCK:
        STATE.update(running=True, stop=False, items=items, finished=False, log=[])
        STATE["pass"] = 0
    total = len(items)
    log(f"=== {total} 件を開始 ===")
    for it in items:
        ex = existing_mp3(it["idx"])
        if ex: it["status"], it["file"] = "done", os.path.basename(ex)
    for p in range(1, PASSES + 1):
        with LOCK: STATE["pass"] = p
        missing = 0
        for it in items:
            with LOCK:
                if STATE["stop"]: log("■ 停止"); _finish(); return
            if it["status"] == "done": continue
            it["status"] = "downloading"
            log(f"[P{p}][{it['idx']}/{total}]({it['kind']}) {it['target']}")
            ok, f = download_one(it)
            if ok:
                it["status"], it["file"] = "done", os.path.basename(f)
                log("   ✅ OK"); time.sleep(SLEEP_OK)
            else:
                it["status"] = "missing"; missing += 1
                log("   ⏭ 未取得"); time.sleep(SLEEP_MISS)
        done = sum(1 for x in items if x["status"] == "done")
        log(f"=== パス{p}: {done}/{total} (残り{missing}) ===")
        if missing == 0: break
        if p < PASSES:
            for _ in range(COOLDOWN):
                with LOCK:
                    if STATE["stop"]: break
                time.sleep(1)
    _finish()

def _finish():
    with LOCK:
        STATE["running"] = False; STATE["finished"] = True
    log("=== 完了 ===")

# ---------------- CSVリスト（同梱の lists/ を読む） ----------------
LISTS_DIR = os.path.join(HERE, "lists")

# 列名ゆらぎを吸収（英/日）
COL_TITLE  = ("title", "曲名", "track", "name")
COL_ARTIST = ("artist", "アーティスト名", "アーティスト", "uploader")
COL_URL    = ("url", "soundcloud_url", "link")

def _pick(row, keys):
    for k in keys:
        if k in row and str(row[k]).strip():
            return str(row[k]).strip()
    return ""

def list_catalog():
    """同梱CSVの一覧（表示名＋件数）"""
    out = []
    for path in sorted(glob.glob(os.path.join(LISTS_DIR, "*.csv"))):
        fid = os.path.basename(path)
        try:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                n = sum(1 for _ in csv.DictReader(fh))
        except Exception:
            n = 0
        name = re.sub(r'\.csv$', '', fid)
        name = re.sub(r'_soundcloud_tracks$', '', name).replace('_', ' ')
        out.append({"id": fid, "name": name, "count": n})
    return out

def read_list(fid):
    """1つのCSVを {title, artist, url, query} のリストに正規化"""
    path = os.path.join(LISTS_DIR, os.path.basename(fid))
    if not os.path.abspath(path).startswith(os.path.abspath(LISTS_DIR)) or not os.path.isfile(path):
        return None
    items = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            title  = _pick(row, COL_TITLE)
            artist = _pick(row, COL_ARTIST)
            url    = _pick(row, COL_URL)
            if not (title or url):
                continue
            query = " ".join(x for x in (artist, title) if x).strip()
            items.append({"title": title or url, "artist": artist,
                          "url": url, "query": query, "has_url": bool(url)})
    return items

def search_soundcloud(query, n):
    """yt-dlp でSoundCloudをライブ検索。webpage_url（正規URL）と再生時間まで取得。"""
    n = max(1, min(int(n), 50))
    # --flat-playlist を使わずフル解決 → soundcloud.com の正規URLが取れDLで確実
    cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--ignore-errors",
           "--socket-timeout", "30", "--sleep-requests", "1",
           f"scsearch{n}:{query}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"search error: {e}"); return []
    out = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = d.get("webpage_url") or d.get("url") or ""
        if "api.soundcloud.com" in url:         # 正規URLでなければDL不可なので除外
            continue
        title = d.get("title") or url
        dur = d.get("duration")
        out.append({"title": title, "artist": d.get("uploader", ""),
                    "url": url, "query": title, "has_url": bool(url),
                    "duration": int(dur) if dur else None})
    return out

# ---------------- HTTP ----------------
def list_files():
    out = []
    for f in sorted(os.listdir(OUTDIR)):
        if f.lower().endswith(".mp3"):
            p = os.path.join(OUTDIR, f)
            out.append({"name": f, "size": os.path.getsize(p)})
    return out

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)): body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(data)

    def log_message(self, *a): pass

    def _cookies(self):
        c = self.headers.get("Cookie", "")
        d = {}
        for part in c.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1); d[k] = v
        return d

    def _authed(self):
        return True                            # ログイン不要（誰でも利用可）

    def _need_auth(self):
        return False                           # 認証スキップ

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "index.html not found", "text/plain; charset=utf-8")
        elif path == "/api/me":
            self._send(200, {"authed": True, "password_set": True})  # ログイン不要
        elif path == "/api/status":
            if self._need_auth(): return
            with LOCK:
                self._send(200, {
                    "running": STATE["running"], "finished": STATE["finished"], "pass": STATE["pass"],
                    "items": [{k: it[k] for k in ("idx","kind","target","label","status","file")} for it in STATE["items"]],
                    "log": STATE["log"][-120:],
                })
        elif path == "/api/lists":
            if self._need_auth(): return
            self._send(200, {"lists": list_catalog()})
        elif path == "/api/list":
            if self._need_auth(): return
            q = parse_qs(urlparse(self.path).query)
            fid = (q.get("id") or [""])[0]
            items = read_list(fid)
            if items is None: self._send(404, {"error": "not found"}); return
            self._send(200, {"items": items})
        elif path == "/api/search":
            if self._need_auth(): return
            q = parse_qs(urlparse(self.path).query)
            query = (q.get("q") or [""])[0].strip()
            n = (q.get("n") or ["10"])[0]
            if not query: self._send(400, {"error": "no query"}); return
            self._send(200, {"items": search_soundcloud(query, n)})
        elif path == "/api/files":
            if self._need_auth(): return
            self._send(200, {"files": list_files()})
        elif path.startswith("/file/"):
            if self._need_auth(): return
            name = unquote(path[len("/file/"):])
            target = os.path.abspath(os.path.join(OUTDIR, name))
            if not target.startswith(os.path.abspath(OUTDIR) + os.sep) or not os.path.isfile(target):
                self._send(404, {"error": "not found"}); return
            with open(target, "rb") as fh: data = fh.read()
            self._send(200, data, "audio/mpeg",
                       {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
        elif path == "/zip":
            if self._need_auth(): return
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
                for f in list_files(): z.write(os.path.join(OUTDIR, f["name"]), f["name"])
            data = buf.getvalue()
            self._send(200, data, "application/zip",
                       {"Content-Disposition": "attachment; filename=tracks.zip"})
        else:
            self._send(404, {"error": "not found"})

    def do_HEAD(self): self.do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8") if n else "{}"
        try: data = json.loads(raw)
        except json.JSONDecodeError: data = {}
        path = self.path.split("?", 1)[0]

        if path == "/api/login":
            if not PASSWORD:
                self._send(503, {"error": "no_password"}); return
            if str(data.get("password", "")) == PASSWORD:
                self._send(200, {"ok": True}, extra={
                    "Set-Cookie": f"sess={EXPECTED}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"})
            else:
                self._send(401, {"error": "bad_password"})
            return

        if self._need_auth(): return

        if path == "/api/start":
            with LOCK:
                if STATE["running"]: self._send(409, {"error": "running"}); return
            items = parse_input(data.get("text", ""))
            if not items: self._send(400, {"error": "no tracks"}); return
            threading.Thread(target=worker, args=(items,), daemon=True).start()
            self._send(200, {"ok": True, "count": len(items)})
        elif path == "/api/stop":
            with LOCK: STATE["stop"] = True
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("=" * 50)
    print(f"  DJ Track Downloader (web)  :{PORT}")
    print("  password:", "SET" if PASSWORD else "‼ NOT SET (APP_PASSWORD を設定してください)")
    print("=" * 50, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()

if __name__ == "__main__":
    main()
