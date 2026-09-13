"""监控视频增强工具 —— HTTP 服务（仅依赖 Python 标准库）。"""
import json
import os
import re
import shutil
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from video import (load_config, ParamError, EncoderError, ProcessError,
                   probe, normalize_params, make_snapshot, make_clip,
                   check_encoder_health)

CFG = load_config()
UPLOAD_DIR = os.path.join(CFG["data_dir"], "uploads")
PREVIEW_DIR = os.path.join(CFG["data_dir"], "previews")
CLIP_DIR = os.path.join(CFG["data_dir"], "clips")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
for d in (UPLOAD_DIR, PREVIEW_DIR, CLIP_DIR):
    os.makedirs(d, exist_ok=True)

ID_RE = re.compile(r"^[0-9a-f]{32}$")
MEDIA = {"preview": (PREVIEW_DIR, "image/png"), "clip": (CLIP_DIR, "video/mp4")}


# ---------- 临时文件清理 ----------

def cleanup_old_files():
    ttl = CFG.get("result_ttl_hours", 24) * 3600
    now = time.time()
    removed = 0
    for folder in (UPLOAD_DIR, PREVIEW_DIR, CLIP_DIR):
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            try:
                if now - os.path.getmtime(p) > ttl:
                    os.remove(p)
                    removed += 1
            except OSError:
                pass
    return removed


# ---------- multipart 上传解析（最小实现） ----------

def parse_multipart(body, boundary):
    delim = b"--" + boundary.encode("latin1")
    parts = body.split(delim)
    for part in parts:
        if not part or part in (b"--", b"--\r\n", b"\r\n"):
            continue
        part = part.strip(b"\r\n")
        header_end = part.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        header = part[:header_end].decode("utf-8", "replace")
        content = part[header_end + 4:]
        if content.endswith(b"\r\n"):
            content = content[:-2]
        cd = next((l for l in header.split("\r\n")
                   if l.lower().startswith("content-disposition")), "")
        m = re.search(r'name="([^"]+)"', cd)
        mf = re.search(r'filename="([^"]*)"', cd)
        if not m:
            continue
        yield m.group(1), (mf.group(1) if mf else None), content


# ---------- 业务处理 ----------

def save_upload(body, boundary):
    files = list(parse_multipart(body, boundary))
    file_items = [(name, fname, data) for name, fname, data in files if fname is not None]
    if not file_items:
        raise ParamError("上传请求中未包含文件字段（字段名需为 file）")
    _, orig_name, data = file_items[0]
    if not data:
        raise ParamError("上传的文件为空")
    max_bytes = CFG["max_upload_mb"] * 1024 * 1024
    if len(data) > max_bytes:
        raise ParamError(f"文件超过大小上限 {CFG['max_upload_mb']}MB")
    ext = os.path.splitext(orig_name)[1].lower()
    if ext not in CFG["allowed_ext"]:
        raise ParamError(
            f"不支持的文件类型 {ext or '(无扩展名)'}，允许：{', '.join(CFG['allowed_ext'])}")

    uid = uuid.uuid4().hex
    dst = os.path.join(UPLOAD_DIR, uid + ext)
    with open(dst, "wb") as f:
        f.write(data)
    try:
        meta = probe(dst, CFG)
    except Exception:
        _safe_remove(dst)
        raise
    if meta["width"] == 0 or meta["height"] == 0:
        _safe_remove(dst)
        raise ParamError("无法读取视频分辨率，该文件可能不是有效视频")
    with open(os.path.join(UPLOAD_DIR, uid + ".json"), "w", encoding="utf-8") as f:
        json.dump({"orig": os.path.basename(orig_name), "meta": meta}, f, ensure_ascii=False)
    return uid, meta


def _safe_remove(p):
    try:
        os.remove(p)
    except OSError:
        pass


def find_upload(uid):
    if not uid or not ID_RE.match(uid):
        raise ParamError("非法的视频 ID")
    for ext in CFG["allowed_ext"]:
        p = os.path.join(UPLOAD_DIR, uid + ext)
        if os.path.exists(p):
            sidecar = os.path.join(UPLOAD_DIR, uid + ".json")
            meta = None
            if os.path.exists(sidecar):
                try:
                    meta = json.load(open(sidecar, encoding="utf-8")).get("meta")
                except (OSError, json.JSONDecodeError):
                    meta = None
            return p, meta or probe(p, CFG)
    raise ProcessError("源视频不存在或已过期，请重新上传")


def handle_process(params):
    uid = params.get("video_id")
    if not uid or not re.match(r"^[0-9a-f]{32}$", str(uid)):
        raise ParamError("非法的视频 ID")
    action = params.get("action", "snapshot")
    if action not in ("snapshot", "clip"):
        raise ParamError(f"未知 action：{action!r}（支持 snapshot / clip）")

    # 参数本身的合法性（范围/类型）优先于源文件检查
    np = normalize_params(params, {"width": 10 ** 6, "height": 10 ** 6})
    if np["crop"]:
        src, meta = find_upload(uid)
        np = normalize_params(params, meta)  # 按真实分辨率再次校验/收敛裁剪
    else:
        src, meta = find_upload(uid)

    if action == "snapshot":
        at = _float(params.get("at"), "截图时间", 0.0)
        if meta["duration"] > 0 and at > meta["duration"]:
            at = meta["duration"] / 2
        at = max(0.0, at)
        mid = uuid.uuid4().hex
        out = os.path.join(PREVIEW_DIR, mid + ".png")
        make_snapshot(src, out, np, at, CFG)
        return {"kind": "snapshot", "id": mid, "url": f"/api/file/preview/{mid}.png",
                "params": np, "at": round(at, 3)}

    if action == "clip":
        dur = _float(params.get("duration"), "片段时长",
                     CFG["preview_max_seconds"], lo=0.1,
                     hi=CFG["preview_max_seconds"])
        start = _float(params.get("start"), "开始时间", 0.0, lo=0.0)
        if meta["duration"] > 0 and start >= meta["duration"]:
            raise ParamError(
                f"开始时间 {start:.2f}s 超过视频总时长 {meta['duration']:.2f}s")
        actual_dur = dur
        if meta["duration"] > 0:
            actual_dur = min(dur, meta["duration"] - start)
        if actual_dur < 0.1:
            raise ParamError("片段剩余时长不足 0.1 秒")
        mid = uuid.uuid4().hex
        out = os.path.join(CLIP_DIR, mid + ".mp4")
        make_clip(src, out, np, start, actual_dur, meta, CFG)
        return {"kind": "clip", "id": mid, "url": f"/api/file/clip/{mid}.mp4",
                "params": np, "start": round(start, 3),
                "duration": round(actual_dur, 3)}


def _float(raw, label, default, lo=None, hi=None):
    if raw is None or raw == "":
        val = default
    else:
        try:
            val = float(raw)
        except (TypeError, ValueError):
            raise ParamError(f"{label}必须是数字，收到：{raw!r}")
    if lo is not None and val < lo - 1e-9:
        raise ParamError(f"{label}不能小于 {lo}")
    if hi is not None and val > hi + 1e-9:
        raise ParamError(f"{label}不能大于 {hi}")
    return val


# ---------- HTTP Handler ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "CctvEnhance/1.0"

    def log_message(self, fmt, *args):
        print("[http] %s - %s" % (self.address_string(), fmt % args))

    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _err(self, e):
        if isinstance(e, ParamError):
            self._json({"error": {"kind": "param", "code": 400,
                                  "message": str(e)}}, 400)
        elif isinstance(e, EncoderError):
            self._json({"error": {"kind": "encoder", "code": 503,
                                  "message": str(e)}}, 503)
        else:
            msg = str(e) if isinstance(e, ProcessError) else f"内部错误：{e}"
            self._json({"error": {"kind": "process", "code": 500,
                                  "message": msg}}, 500)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/" or path == "/index.html":
                self._serve_static("index.html", "text/html; charset=utf-8")
            elif path.startswith("/static/"):
                name = path[len("/static/"):]
                ctype = {"app.js": "application/javascript; charset=utf-8",
                         "style.css": "text/css; charset=utf-8"}.get(
                    os.path.basename(name), "application/octet-stream")
                self._serve_static(name, ctype)
            elif path == "/api/health":
                issues = check_encoder_health(CFG)
                self._json({"ok": not issues, "issues": issues,
                            "ffmpeg": CFG["ffmpeg_bin"]})
            elif path.startswith("/api/file/"):
                self._serve_media(path)
            else:
                self._json({"error": {"kind": "not_found", "code": 404,
                                      "message": f"路径不存在：{path}"}}, 404)
        except (ParamError, EncoderError, ProcessError) as e:
            self._err(e)
        except Exception as e:  # noqa: BLE001
            self._err(ProcessError(str(e)))

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise ParamError("请求体为空")
            if length > (CFG["max_upload_mb"] + 5) * 1024 * 1024:
                raise ParamError(f"请求体超过上限 {CFG['max_upload_mb']}MB")
            body = self.rfile.read(length)

            if parsed.path == "/api/upload":
                ctype = self.headers.get("Content-Type", "")
                m = re.search(r"boundary=([^;]+)", ctype)
                if "multipart/form-data" not in ctype or not m:
                    raise ParamError("上传必须使用 multipart/form-data（字段名 file）")
                boundary = m.group(1).strip('"')
                cleanup_old_files()
                uid, meta = save_upload(body, boundary)
                self._json({"ok": True, "video_id": uid, "meta": meta})
            elif parsed.path == "/api/process":
                try:
                    params = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ParamError("请求体必须是合法 JSON")
                if not isinstance(params, dict):
                    raise ParamError("请求参数必须是 JSON 对象")
                result = handle_process(params)
                self._json({"ok": True, "result": result})
            else:
                self._json({"error": {"kind": "not_found", "code": 404,
                                      "message": f"路径不存在：{parsed.path}"}}, 404)
        except (ParamError, EncoderError, ProcessError) as e:
            self._err(e)
        except Exception as e:  # noqa: BLE001
            self._err(ProcessError(str(e)))

    def _serve_static(self, name, ctype):
        # 防止路径穿越
        safe = os.path.normpath(os.path.join(STATIC_DIR, name))
        if not safe.startswith(STATIC_DIR + os.sep) and safe != STATIC_DIR:
            raise ParamError("非法路径")
        if not os.path.isfile(safe):
            raise ProcessError(f"静态文件缺失：{name}")
        with open(safe, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_media(self, path):
        # /api/file/<kind>/<filename>
        m = re.match(r"^/api/file/([a-z]+)/([0-9a-f]{32}\.(?:png|mp4))$", path)
        if not m:
            raise ParamError("非法的文件路径")
        kind, fname = m.group(1), m.group(2)
        if kind not in MEDIA:
            raise ParamError(f"未知文件类型：{kind}")
        folder, ctype = MEDIA[kind]
        full = os.path.join(folder, fname)
        if not os.path.isfile(full):
            raise ProcessError("结果文件不存在或已过期，请重新生成")
        size = os.path.getsize(full)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng:
            rm = re.match(r"bytes=(\d+)-(\d*)", rng)
            if rm:
                start = int(rm.group(1))
                if rm.group(2):
                    end = int(rm.group(2))
                end = min(end, size - 1)
                partial = start <= end
        if not partial and rng:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(full, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main():
    cleanup_old_files()
    issues = check_encoder_health(CFG)
    if issues:
        print("[startup] 编码器自检告警：" + "; ".join(issues))
    else:
        print("[startup] 编码器自检通过（libx264 / mjpeg 可用）")
    server = ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)
    print(f"[startup] 监控视频增强工具监听 http://{CFG['host']}:{CFG['port']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[startup] 已退出")


if __name__ == "__main__":
    main()
