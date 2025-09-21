# app.py
import re, os, time, tempfile, shutil
from typing import Dict, Any, Optional, Tuple
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from yt_dlp import YoutubeDL
from starlette.background import BackgroundTask

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# ---------- UI (paste URL → click Download) ----------
@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>TikTok → MP4</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#0b0c10;color:#e6eef7;
         display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
    .card{background:#11131a;border:1px solid #1f2533;border-radius:16px;padding:24px;max-width:560px;width:92%;
          box-shadow:0 10px 30px rgba(0,0,0,.35)}
    h1{font-size:22px;margin:0 0 12px}
    p.hint{opacity:.75;margin:0 0 18px}
    form{display:flex;gap:10px}
    input[type="url"]{flex:1;padding:12px 14px;border-radius:12px;border:1px solid #2a3142;background:#0c1018;
                      color:#e6eef7;font-size:15px;outline:none}
    input[type="url"]::placeholder{color:#8f9bb3}
    button{padding:12px 16px;border-radius:12px;border:0;background:#4f8cff;color:#fff;font-weight:600;cursor:pointer}
    button:hover{filter:brightness(1.08)}
    .row{margin-top:12px;display:flex;gap:8px;align-items:center}
    small{opacity:.65}
    a{color:#9ac3ff}
  </style>
</head>
<body>
  <div class="card">
    <h1>TikTok → MP4 (H.264)</h1>
    <p class="hint">Paste a TikTok link and hit <b>Download MP4</b>. The server fetches it and returns a file.</p>
    <form action="/api/tiktok/download" method="get">
      <input id="u" name="url" type="url" placeholder="https://www.tiktok.com/@user/video/XXXXXXXXXXXX" required />
      <button type="submit">Download MP4</button>
    </form>
    <div class="row"><small>Tip: Press <b>Enter</b> to download.</small></div>
  </div>
  <script>
    window.addEventListener('DOMContentLoaded', () => document.getElementById('u').focus());
  </script>
</body>
</html>
"""

# ---------- yt-dlp prefs (prefer H.264/AVC) ----------
YDL_BASE: Dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "format_sort": ["codec:avc", "res", "fps", "br"],
    "format": "bv*+ba/b",
    "prefer_free_formats": False,
}

# (Optional) lightweight cache for the JSON endpoint
_CACHE: Dict[str, Dict[str, Any]] = {}
TTL_SECONDS = 600

def cache_get(url: str) -> Optional[Dict[str, Any]]:
    row = _CACHE.get(url)
    if not row: return None
    if time.time() > row["exp"]: _CACHE.pop(url, None); return None
    return row["val"]

def cache_set(url: str, val: Dict[str, Any]) -> None:
    _CACHE[url] = {"val": val, "exp": time.time() + TTL_SECONDS}

def safe_filename(name: Optional[str], fallback: str = "video") -> str:
    base = (name or fallback).strip()
    base = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", base)
    base = base[:100] or fallback
    return f"{base}.mp4"

def extract_info(tiktok_url: str) -> Dict[str, Any]:
    with YoutubeDL({**YDL_BASE, "skip_download": True}) as ydl:
        info = ydl.extract_info(tiktok_url, download=False)
    if "entries" in info and info["entries"]:
        info = info["entries"][0]
    direct = None
    if info.get("requested_formats"):
        v = info["requested_formats"][0]; direct = v.get("url")
    if not direct and info.get("url"): direct = info["url"]
    if not direct and info.get("formats"):
        fmts = info["formats"]
        pref = [f for f in fmts if f.get("ext") == "mp4" and "avc" in (f.get("vcodec") or "")]
        if pref:
            pref.sort(key=lambda f:(f.get("height") or 0, f.get("fps") or 0, f.get("tbr") or 0), reverse=True)
            direct = pref[0].get("url")
        else:
            fmts = [f for f in fmts if f.get("url")]
            if fmts:
                fmts.sort(key=lambda f:(f.get("height") or 0, f.get("fps") or 0, f.get("tbr") or 0), reverse=True)
                direct = fmts[0]["url"]
    return {
        "title": info.get("title"),
        "id": info.get("id"),
        "ext": info.get("ext", "mp4"),
        "width": info.get("width"),
        "height": info.get("height"),
        "duration": info.get("duration"),
        "direct_url": direct,
    }

def yt_dlp_download_to_temp(url: str, title_hint: Optional[str]) -> Tuple[str, str]:
    """
    Download locally (prefer AVC). Return (path, suggested_filename).
    Tries merge (needs ffmpeg), then falls back to single best MP4/AVC.
    """
    import tempfile, os, shutil
    tmpdir = tempfile.mkdtemp(prefix="ttdl_")
    outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    opts1 = {
        "quiet": True, "no_warnings": True,
        "format_sort": ["codec:avc", "res", "fps", "br"],
        "format": "bv*+ba/b",
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
    }
    opts2 = {
        "quiet": True, "no_warnings": True,
        "format": "best[ext=mp4][vcodec*=avc]/best[ext=mp4]/best",
        "outtmpl": outtmpl,
    }

    def try_download(opts) -> Optional[str]:
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if "requested_downloads" in info and info["requested_downloads"]:
                    p = info["requested_downloads"][0].get("filepath")
                    if p and os.path.exists(p): return p
                for name in os.listdir(tmpdir):
                    if name.lower().endswith(".mp4"):
                        return os.path.join(tmpdir, name)
                return None
        except Exception:
            return None

    path = try_download(opts1) or try_download(opts2)
    if not path or not os.path.exists(path):
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400, "Download failed (ffmpeg missing or unsupported format).")

    fname = safe_filename(title_hint or os.path.splitext(os.path.basename(path))[0])
    return path, fname

@app.get("/api/tiktok")
def api_tiktok(url: str = Query(..., min_length=10)):
    cached = cache_get(url)
    if cached: return JSONResponse(cached)
    data = extract_info(url)
    cache_set(url, data)
    return JSONResponse(data)

@app.get("/api/tiktok/download")
def api_tiktok_download(url: str = Query(..., min_length=10)):
    meta = extract_info(url)
    path, fname = yt_dlp_download_to_temp(url, meta.get("title") or meta.get("id"))
    def cleanup():
        try:
            d = os.path.dirname(path)
            if os.path.exists(path): os.remove(path)
            if os.path.isdir(d): shutil.rmtree(d, ignore_errors=True)
        except: pass
    return FileResponse(path, media_type="video/mp4", filename=fname, background=BackgroundTask(cleanup))
