"""视频处理层：ffprobe 探测、滤镜构建、截图/片段生成、错误分类。

错误分类三类：
  ParamError(400)        参数不合法（数值超范围 / 裁剪不合法 / 时间段不合法）
  EncoderError(500)      编码器或 muxer 问题（Unknown encoder、codec not found 等）
  ProcessError(500)      其他 ffmpeg 运行时问题（流损坏、滤镜求值失败等）
"""
import json
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config():
    with open(os.path.join(ROOT, "config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["root"] = ROOT
    for key in ("ffmpeg_bin", "ffprobe_bin"):
        p = cfg[key]
        cfg[key] = p if os.path.isabs(p) else os.path.join(ROOT, p)
    for key in ("data_dir",):
        p = cfg[key]
        cfg[key] = p if os.path.isabs(p) else os.path.join(ROOT, p)
    return cfg


class ParamError(Exception):
    """HTTP 400：请求参数本身不合法。"""


class EncoderError(Exception):
    """HTTP 500：编码器 / 复用器不可用或初始化失败。"""


class ProcessError(Exception):
    """HTTP 500：其他 ffmpeg 处理失败（解码、滤镜、流数据等）。"""


# ---------- ffmpeg 错误日志分类 ----------

# 编码器 / 复用器（环境）问题
_ENCODER_PATTERNS = [
    r"Unknown encoder\s+'?([^'\s]+)",
    r"Encoder .* not found",
    r"not found.*encoder",
    r"Unknown decoder\s+'?([^'\s]+)",
    r"Unknown codec\s+'?([^'\s]+)",
    r"codec (?:not found|currently in use)",
    r"Unrecognized codec",
    r"could not find codec parameters",
    r"Unknown muxer\s+'?([^'\s]+)",
    r"Unknown output format",
    r"Error initializing the muxer",
    r"muxer for .*: Invalid argument",
    r"Could not find tag for codec [^\n]*stream",
    r"does not contain any streams",
    r"Conversion for pixel format [^ ]+ (?:is|are) not supported",
    r"not supported by the .* muxer",
    r"could not open encoder",
    r"Error initializing output stream",
    r"avcodec_open2",
]

# 参数不合法（用户输入导致的 ffmpeg 报错）
_PARAM_PATTERNS = [
    r"Invalid argument",
    r"Error while filtering: (?:Invalid argument|Result too large|Cannot allocate memory)",
    r"Invalid (?:ring|.)? ?buffer size",
    r"does not have the (?:same)?(?:width|height)",
    r"Invalid width or height",
    r"width not divisible by",
    r"height not divisible by",
    r"Value .* out of range",
    r"option .* not found",
    r"Unrecognized option",
    r"Error parsing option",
    r"Trailing option\(s\) found",
    r"Error applying option",
    r"Failed to set value .* for option",
    r"Option not found",
    r"Error initializing a (?:simple )?filtergraph",
    r"Invalid duration specification",
    r"Invalid sample format",
    r"Unable to parse option value",
    r"Missing argument for option",
]


def classify_ffmpeg_error(stderr_text):
    """根据 ffmpeg stderr 判定错误类别。

    返回 (kind, detail)：kind ∈ {"encoder", "param", "process"}。
    优先识别编码器问题（输出侧环境故障），再识别参数问题。
    """
    tail = stderr_text[-4000:] if stderr_text else ""

    for pat in _ENCODER_PATTERNS:
        m = re.search(pat, tail, re.IGNORECASE)
        if m:
            line = _last_matching_line(tail, pat)
            return "encoder", line or "编码器/复用器初始化失败"

    for pat in _PARAM_PATTERNS:
        m = re.search(pat, tail, re.IGNORECASE)
        if m:
            line = _last_matching_line(tail, pat)
            return "param", line or "滤镜参数不合法"

    line = _last_error_line(tail)
    return "process", line or "ffmpeg 处理失败"


def _last_matching_line(text, pattern):
    matches = [ln.strip() for ln in text.splitlines() if re.search(pattern, ln, re.IGNORECASE)]
    return matches[-1] if matches else None


def _last_error_line(text):
    for ln in reversed([l.strip() for l in text.splitlines()]):
        if ln.lower().startswith(("error", "[error", "failed", "could not", "unable", "invalid", "no such")):
            return ln
    return None


# ---------- 探测 ----------

def probe(path, cfg):
    if not os.path.exists(path):
        raise ProcessError("源文件不存在，可能已被清理，请重新上传")
    try:
        proc = subprocess.run(
            [cfg["ffprobe_bin"], "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise EncoderError("ffprobe 二进制不存在，请检查配置 ffprobe_bin")
    except subprocess.TimeoutExpired:
        raise ProcessError("ffprobe 探测超时")
    if proc.returncode != 0:
        kind, detail = classify_ffmpeg_error(proc.stderr)
        raise (EncoderError(f"无法识别视频文件（{detail}）") if kind == "encoder"
               else ProcessError(f"无法识别视频文件（{detail}）"))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ProcessError("ffprobe 输出无法解析")

    vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not vstream:
        raise EncoderError("文件中没有视频流（缺少视频轨，编码器无法处理）")

    width = int(vstream.get("width") or 0)
    height = int(vstream.get("height") or 0)
    try:
        duration = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0 and vstream.get("duration"):
        try:
            duration = float(vstream["duration"])
        except (TypeError, ValueError):
            duration = 0.0
    return {
        "width": width,
        "height": height,
        "duration": round(duration, 3),
        "codec": vstream.get("codec_name", "unknown"),
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


# ---------- 参数校验与滤镜 ----------

def _read_number(params, name, default, lo, hi, label, integer=False):
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        val = int(str(raw)) if integer else float(str(raw))
    except (TypeError, ValueError):
        raise ParamError(f"{label}必须是数字，收到：{raw!r}")
    if val < lo - 1e-9 or val > hi + 1e-9:
        raise ParamError(f"{label}超出允许范围 [{lo}, {hi}]，收到：{val}")
    return val


def normalize_params(p, meta):
    """校验并归一化全部增强参数。"""
    brightness = _read_number(p, "brightness", 0.0, -1.0, 1.0, "亮度")
    denoise = _read_number(p, "denoise", 0.0, 0.0, 1.0, "降噪强度")
    sharpen = _read_number(p, "sharpen", 0.0, 0.0, 1.0, "锐化强度")

    crop_enabled = str(p.get("crop_enabled", "false")).lower() in ("1", "true", "yes", "on")
    crop = None
    if crop_enabled:
        x = _read_number(p, "crop_x", 0, 0, meta["width"] - 1, "裁剪 X", integer=True)
        y = _read_number(p, "crop_y", 0, 0, meta["height"] - 1, "裁剪 Y", integer=True)
        w = _read_number(p, "crop_w", meta["width"], 2, meta["width"], "裁剪宽度", integer=True)
        h = _read_number(p, "crop_h", meta["height"], 2, meta["height"], "裁剪高度", integer=True)
        # 自动收敛到偶数、到画面边界，与前端的可拖范围保持一致
        w = min(w - (w % 2), meta["width"] - (meta["width"] % 2))
        h = min(h - (h % 2), meta["height"] - (meta["height"] % 2))
        if w < 2 or h < 2:
            raise ParamError("裁剪区域至少为 2x2 像素")
        x = min(x, meta["width"] - w)
        y = min(y, meta["height"] - h)
        crop = {"x": x, "y": y, "w": w, "h": h}

    return {
        "brightness": round(brightness, 3),
        "denoise": round(denoise, 3),
        "sharpen": round(sharpen, 3),
        "crop": crop,
    }


def build_filter_chain(np):
    """按 eq -> hqdn3d -> unsharp -> crop 顺序构建 filter_complex 片段。"""
    parts = []
    b = np["brightness"]
    if abs(b) > 1e-6:
        parts.append(f"eq=brightness={b:.3f}")
    d = np["denoise"]
    if d > 1e-6:
        # 位置参数在 ffmpeg 4/7 通用：luma_spatial, chroma_spatial, luma_tmp, chroma_tmp
        spatial = 1.5 + 8.5 * d        # 1.5 ~ 10
        tmp = spatial * 1.55
        parts.append(f"hqdn3d={spatial:.2f}:{spatial * 0.75:.2f}:{tmp:.2f}:{tmp * 0.75:.2f}")
    s = np["sharpen"]
    if s > 1e-6:
        amount = round(0.2 + 1.8 * s, 3)   # 0.2 ~ 2.0
        parts.append(f"unsharp=5:5:{amount}:5:5:{amount * 0.5:.3f}")
    c = np["crop"]
    if c:
        parts.append(f"crop={c['w']}:{c['h']}:{c['x']}:{c['y']}")
    return ",".join(parts)


# ---------- ffmpeg 执行 ----------

def _run_ffmpeg(cmd, cfg, timeout, purpose):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise EncoderError("ffmpeg 二进制不存在，请检查配置 ffmpeg_bin")
    except subprocess.TimeoutExpired:
        raise ProcessError(f"{purpose}处理超时（>{timeout}s），片段可能过长或分辨率过大")
    if proc.returncode != 0:
        kind, detail = classify_ffmpeg_error(proc.stderr)
        if kind == "encoder":
            raise EncoderError(f"编码器问题导致{purpose}失败：{detail}")
        if kind == "param":
            raise ParamError(f"参数不合法导致{purpose}失败：{detail}")
        raise ProcessError(f"{purpose}失败：{detail}")
    return proc


def make_snapshot(src, out_path, np, at_seconds, cfg):
    """生成单帧截图（PNG，无损承载增强效果）。"""
    cmd = [cfg["ffmpeg_bin"], "-y", "-hide_banner", "-nostdin",
           "-ss", f"{max(0.0, at_seconds):.3f}", "-i", src,
           "-frames:v", "1"]
    chain = build_filter_chain(np)
    if chain:
        cmd += ["-vf", chain]
    cmd += ["-pix_fmt", "rgb24", out_path]
    _run_ffmpeg(cmd, cfg, timeout=60, purpose="截图")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise ProcessError("截图未生成（输出为空）")


def make_clip(src, out_path, np, start, duration, meta, cfg):
    """生成短片段（H.264 + AAC，yuv420p，faststart 便于浏览器播放）。"""
    chain = build_filter_chain(np)
    vgraph = (chain + "," if chain else "") + "format=yuv420p"
    cmd = [cfg["ffmpeg_bin"], "-y", "-hide_banner", "-nostdin",
           "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", src,
           "-filter_complex", f"[0:v]{vgraph}[v]", "-map", "[v]"]
    if meta.get("has_audio"):
        cmd += ["-map", "0:a?", "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-movflags", "+faststart", "-max_muxing_queue_size", "1024", out_path]
    _run_ffmpeg(cmd, cfg, timeout=max(120, int(duration) * 30 + 60), purpose="短片段")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise ProcessError("短片段未生成（输出为空）")


def check_encoder_health(cfg):
    """启动时自检：ffmpeg 可执行 + libx264/mjpeg 可用。"""
    issues = []
    try:
        proc = subprocess.run([cfg["ffmpeg_bin"], "-hide_banner", "-encoders"],
                              capture_output=True, text=True, timeout=15)
        out = (proc.stdout or "") + (proc.stderr or "")
        for name in ("libx264", "mjpeg"):
            if not re.search(rf"\b{name}\b", out):
                issues.append(f"缺少编码器 {name}")
    except FileNotFoundError:
        issues.append("ffmpeg 二进制不存在")
    except subprocess.TimeoutExpired:
        issues.append("ffmpeg 自检超时")
    if not os.path.exists(cfg["ffprobe_bin"]):
        issues.append("ffprobe 二进制不存在")
    return issues
