#!/usr/bin/env python3
"""端到端自检：上传 → 参数校验 → 截图 → 片段 → 错误分类。

用法：python3 scripts/selftest.py [base_url] [sample]
"""
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import video  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SAMPLE = sys.argv[2] if len(sys.argv) > 2 else "data/sample_cctv.mp4"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = video.load_config()

PASS, FAIL = 0, 0


def req(path, payload=None, raw=None, ctype="application/json", method="POST"):
    url = BASE + path
    data = None
    if raw is not None:
        data = raw
    elif payload is not None:
        data = json.dumps(payload).encode()
    r = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as resp:
        return resp.status, resp.read(), dict(resp.headers)


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def section(t):
    print(f"\n=== {t} ===")


def multipart_body(path):
    boundary = "----selftest" + uuid.uuid4().hex
    with open(path, "rb") as f:
        content = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{os.path.basename(path)}"\r\n'
        f"Content-Type: video/mp4\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def real_ffmpeg_error(extra_args):
    """用真实 ffmpeg 跑一个必然失败的命令，返回分类结果与 stderr 末行。"""
    cmd = [CFG["ffmpeg_bin"], "-hide_banner", "-nostdin", "-f", "lavfi",
           "-i", "color=c=black:s=64x64:d=0.1"] + extra_args + ["/tmp/x-out.bin"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    kind, detail = video.classify_ffmpeg_error(p.stderr)
    return p.returncode, kind, detail, p.stderr.strip().splitlines()[-1:]


def main():
    section("1. 健康检查")
    s, d = req("/api/health", method="GET") if False else (None, None)
    with urllib.request.urlopen(BASE + "/api/health", timeout=10) as r:
        health = json.loads(r.read())
    check("编码器自检通过", health["ok"] is True, health)

    section("2. 上传样片")
    body, ct = multipart_body(os.path.join(ROOT, SAMPLE))
    s, d = req("/api/upload", raw=body, ctype=ct)
    check("上传 200", s == 200, f"{s} {d}")
    vid = d.get("video_id")
    meta = d.get("meta", {})
    check("分辨率探测正确", meta.get("width") == 1280 and meta.get("height") == 720, meta)
    check("时长探测正确 (~12s)", abs(meta.get("duration", 0) - 12) < 0.5, meta)
    check("音频轨探测", meta.get("has_audio") is True, meta)

    section("3. 参数不合法（HTTP 400 / kind=param）")
    cases = [
        ("亮度超范围", {"brightness": 5}),
        ("亮度非数字", {"brightness": "abc"}),
        ("降噪 < 0", {"denoise": -0.5}),
        ("锐化 > 1", {"sharpen": 2}),
        ("裁剪宽度越界", {"crop_enabled": True, "crop_x": 0, "crop_y": 0,
                          "crop_w": 9999, "crop_h": 400}),
        ("非法 video_id", {"video_id": "../../etc/passwd"}),
        ("片段开始超过时长", {"action": "clip", "start": 999, "duration": 3}),
        ("片段时长为 0", {"action": "clip", "start": 0, "duration": 0}),
        ("片段时长超上限", {"action": "clip", "start": 0, "duration": 99}),
        ("未知 action", {"action": "burn-dvd"}),
    ]
    for name, extra in cases:
        p = {"video_id": vid, "action": "snapshot", "at": 1}
        p.update(extra)
        s, d = req("/api/process", p)
        kind = d.get("error", {}).get("kind")
        check(f"{name} -> 400/param", s == 400 and kind == "param",
              f"got {s}/{kind}: {d.get('error', {}).get('message')}")

    section("4. 上传非法文件类型")
    bad = os.path.join(ROOT, "data", "bad.txt")
    with open(bad, "w") as f:
        f.write("not a video")
    b2, ct2 = multipart_body(bad)
    s, d = req("/api/upload", raw=b2, ctype=ct2)
    check("拒绝 .txt -> 400/param", s == 400 and d["error"]["kind"] == "param", d)

    section("5. 正常截图（亮度+降噪+锐化+裁剪）")
    p = {"video_id": vid, "action": "snapshot", "at": 3,
         "brightness": 0.35, "denoise": 0.6, "sharpen": 0.5,
         "crop_enabled": True, "crop_x": 120, "crop_y": 100, "crop_w": 800, "crop_h": 500}
    s, d = req("/api/process", p)
    check("截图 200", s == 200, f"{s} {d}")
    if s == 200:
        url = d["result"]["url"]
        sc, img, hdr = get(url)
        check("PNG 可下载", sc == 200 and hdr.get("Content-Type") == "image/png",
              f"{sc} {hdr.get('Content-Type')}")
        check("PNG 非空", len(img) > 1000, f"{len(img)} bytes")
        check("滤镜参数回显含 crop/hqdn3d 输入",
              d["result"]["params"]["crop"] == {"x": 120, "y": 100, "w": 800, "h": 500},
              d["result"]["params"])
        # 用 ffprobe 验证输出尺寸 = 裁剪尺寸
        out = os.path.join(CFG["data_dir"], "previews", d["result"]["id"] + ".png")
        pr = subprocess.run([CFG["ffprobe_bin"], "-v", "error", "-select_streams", "v:0",
                             "-show_entries", "stream=width,height", "-of", "csv=p=0", out],
                            capture_output=True, text=True)
        check("截图分辨率 = 裁剪 800x500", pr.stdout.strip() == "800,500", pr.stdout)

    section("6. 截图不裁剪（原图尺寸）")
    p = {"video_id": vid, "action": "snapshot", "at": 5, "brightness": -0.3}
    s, d = req("/api/process", p)
    out = os.path.join(CFG["data_dir"], "previews", d["result"]["id"] + ".png")
    pr = subprocess.run([CFG["ffprobe_bin"], "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=width,height", "-of", "csv=p=0", out],
                        capture_output=True, text=True)
    check("无裁剪截图保持 1280x720", pr.stdout.strip() == "1280,720", pr.stdout)

    section("7. 正常短片段（H.264）")
    p = {"video_id": vid, "action": "clip", "start": 2, "duration": 4,
         "brightness": 0.2, "denoise": 0.4, "sharpen": 0.3,
         "crop_enabled": True, "crop_x": 200, "crop_y": 150, "crop_w": 640, "crop_h": 400}
    s, d = req("/api/process", p)
    check("片段 200", s == 200, f"{s} {d}")
    if s == 200:
        res = d["result"]
        out = os.path.join(CFG["data_dir"], "clips", res["id"] + ".mp4")
        pr = subprocess.run([CFG["ffprobe_bin"], "-v", "error",
                             "-show_entries", "format=duration:stream=codec_name,width,height",
                             "-of", "default=noprint_wrappers=1", out],
                            capture_output=True, text=True)
        info = pr.stdout
        check("片段编码为 h264", "codec_name=h264" in info, info)
        check("片段尺寸=裁剪 640x400", "width=640" in info and "height=400" in info, info)
        dur = [l for l in info.splitlines() if l.startswith("duration=")]
        durv = float(dur[0].split("=")[1]) if dur else 0
        check("片段时长≈4s 且 ≤8s 上限", 3.7 < durv <= 8, f"duration={durv}")
        # Range 请求验证（视频拖动播放需要）
        range_req = urllib.request.Request(BASE + res["url"], headers={"Range": "bytes=0-1023"})
        with urllib.request.urlopen(range_req, timeout=30) as rr:
            sc, hdr = rr.status, dict(rr.headers)
        check("MP4 支持 Range（206 + Content-Range）",
              sc == 206 and hdr.get("Content-Range", "").startswith("bytes 0-1023/"),
              f"status={sc}, range={hdr.get('Content-Range')}")

    section("8. 错误分类（真实 ffmpeg stderr）")
    # 8a. 编码器问题：不存在的编码器
    rc, kind, detail, last = real_ffmpeg_error(["-c:v", "libx265doesnotexist", "-f", "mp4"])
    check(f"未知编码器 -> encoder (got {kind}: {detail})", kind == "encoder", last)
    # 8b. 编码器问题：给 null muxer 配像素格式不支持的编码? 用未知 muxer
    rc, kind, detail, last = real_ffmpeg_error(["-c:v", "libx264", "-f", "muxer_xyz"])
    check(f"未知复用器 -> encoder (got {kind}: {detail})", kind == "encoder", last)
    # 8c. 参数问题：无法解析的选项值
    rc, kind, detail, last = real_ffmpeg_error(["-vf", "eq=brightness=notanumber"])
    check(f"滤镜非法值 -> param (got {kind}: {detail})", kind == "param", last)
    # 8d. 参数问题：crop 奇宽(某些 yuv 配置) -> 用无法求值表达式
    rc, kind, detail, last = real_ffmpeg_error(["-vf", "crop=w=99999:h=99999"])
    check(f"crop 越界 -> param (got {kind}: {detail})", kind == "param", last)
    # 8e. 缺 ffmpeg 二进制 -> EncoderError
    old = CFG["ffmpeg_bin"]
    try:
        CFG["ffmpeg_bin"] = "/nonexistent/ffmpeg"
        try:
            video.make_snapshot(os.path.join(ROOT, SAMPLE), "/tmp/x.png",
                                video.normalize_params({}, {"width": 1280, "height": 720}), 1, CFG)
            raised = None
        except video.EncoderError as ex:
            raised = "encoder"
        except Exception as ex:  # noqa
            raised = type(ex).__name__
        check("ffmpeg 缺失 -> EncoderError", raised == "encoder", f"got {raised}")
    finally:
        CFG["ffmpeg_bin"] = old

    section("9. 过期/不存在的结果文件")
    s, d = req("/api/process", {"video_id": "0" * 32, "action": "snapshot"})
    check("不存在的视频 -> process 404提示重新上传",
          s in (400, 500) and "重新上传" in d["error"]["message"], d)

    os.path.exists(bad) and os.remove(bad)
    print(f"\n{'='*40}\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
