# 🎥 监控视频增强工具

上传一小段监控样片，在浏览器里调整 **亮度 / 降噪 / 锐化 / 裁剪区域**，后端用 FFmpeg 生成
**截图预览（PNG）** 和 **增强短片段（MP4/H.264，≤8 秒）**。无需数据库，全部配置和临时文件
都放在本地目录。

## 特性

- **四项增强**（一条 filter chain 完成，顺序固定：提亮 → 降噪 → 锐化 → 裁剪）
  - 亮度：`eq=brightness`，范围 -1 ~ +1
  - 降噪：`hqdn3d` 高质量 3D 降噪，位置参数在 FFmpeg 4/7 通用
  - 锐化：`unsharp`（亮度+色度），范围 0 ~ 1
  - 裁剪：可拖拽/缩放/框选的裁剪框，宽高自动收敛为偶数（H.264/yuv420p 要求）
- **两类结果**：截图（`-frames:v 1` 输出 PNG）、短片段（libx264 + AAC、yuv420p、+faststart，支持 Range 拖动）
- **错误明确区分三类**（HTTP 状态 + JSON `error.kind`，前端按颜色/文案渲染）：
  | kind | HTTP | 含义 | 典型触发 |
  |---|---|---|---|
  | `param` | 400 | **参数不合法**（用户输入问题） | 亮度超范围、裁剪越界、时长为 0、非法 action、坏 JSON |
  | `encoder` | 503 | **编码器问题**（服务端环境） | Unknown encoder、缺 libx264、muxer 初始化失败、ffmpeg 二进制缺失 |
  | `process` | 500 | 其他处理失败 | 源文件损坏/过期、解码失败、处理超时 |
  - 分类逻辑见 `app/video.py` 的 `classify_ffmpeg_error()`：对真实 ffmpeg stderr 做两轮正则匹配，
    未命中时归为 `process` 并附上 stderr 最后一行错误。
- **无数据库**：上传文件、截图、片段均以 `<uuid>.<ext>` 存放在 `data/`，启动和上传时按
  TTL（默认 24 小时）自动清理。
- 只依赖 **Python 3 标准库**（`http.server`），不需要 pip / Flask。

## 目录结构

```
config.json              主机/端口/ffmpeg 路径/大小上限/TTL 等配置
bin/ffmpeg, ffprobe      本地静态 FFmpeg（arm64；其它架构请替换）
app/
  server.py              HTTP 服务（上传、处理、文件分发、Range、清理）
  video.py               ffprobe 探测、参数校验、滤镜构建、错误分类
  static/                index.html / app.js / style.css（裁剪框交互）
data/
  uploads/               上传的源片（<id>.<ext> + <id>.json 元数据）
  previews/              截图 PNG
  clips/                 短片段 MP4
scripts/selftest.py      端到端自检（33 项断言）
```

## 启动

```bash
python3 app/server.py
# 打开 http://127.0.0.1:8000
```

启动时会自检 ffmpeg 与 libx264/mjpeg 编码器；也可 `GET /api/health` 查询。

## API

- `POST /api/upload`（multipart，字段 `file`）→ `{video_id, meta{width,height,duration,codec,has_audio}}`
- `POST /api/process`（JSON）
  - 截图：`{video_id, action:"snapshot", at, brightness, denoise, sharpen,
    crop_enabled, crop_x, crop_y, crop_w, crop_h}`
  - 片段：同上，`action:"clip", start, duration`（duration 0.1 ~ 8 秒）
  - → `{ok, result{kind, url, params, ...}}`
- `GET /api/file/preview/<id>.png`、`GET /api/file/clip/<id>.mp4`（支持 Range）
- `GET /api/health`

## 自检

```bash
# 需要一个测试样片，可用 ffmpeg 合成（见 scripts/selftest.py 注释）
python3 scripts/selftest.py
```

覆盖：上传探测、10 种非法参数（400/param）、非法文件类型、截图/裁剪分辨率、
片段编码/时长、Range 播放、以及用**真实 ffmpeg 失败命令**验证编码器 vs 参数错误分类。

## 配置说明（config.json）

| 键 | 默认 | 说明 |
|---|---|---|
| `ffmpeg_bin` / `ffprobe_bin` | `bin/...` | 相对路径基于项目根 |
| `max_upload_mb` | 200 | 上传大小上限 |
| `preview_max_seconds` | 8 | 短片段最大时长 |
| `result_ttl_hours` | 24 | 源片/结果保留时间 |
| `allowed_ext` | mp4/mov/avi/mkv/flv/ts/m4v/webm/wmv | 允许的扩展名 |

> 注：随附的静态 ffmpeg 未编译 libfreetype，因此没有 drawtext 滤镜，不影响本工具使用的
> eq/hqdn3d/unsharp/crop/libx264 等能力。
