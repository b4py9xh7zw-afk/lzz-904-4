'use strict';

const state = {
  videoId: null,
  meta: null,
  objectUrl: null,
  crop: null,           // {x,y,w,h} 视频像素坐标
  busy: false,
};

const $ = (id) => document.getElementById(id);
const els = {
  fileInput: $('fileInput'), fileName: $('fileName'), uploadStatus: $('uploadStatus'),
  stage: $('stage'), video: $('video'), cropBox: $('cropBox'),
  cropEnabled: $('cropEnabled'), cropInfo: $('cropInfo'), resetCrop: $('resetCrop'),
  brightness: $('brightness'), denoise: $('denoise'), sharpen: $('sharpen'),
  brightnessVal: $('brightnessVal'), denoiseVal: $('denoiseVal'), sharpenVal: $('sharpenVal'),
  at: $('at'), atVal: $('atVal'), start: $('start'), duration: $('duration'), maxDur: $('maxDur'),
  snapBtn: $('snapBtn'), clipBtn: $('clipBtn'),
  alertBox: $('alertBox'), result: $('result'), resultTitle: $('resultTitle'),
  resultBody: $('resultBody'), resultParams: $('resultParams'), downloadLink: $('downloadLink'),
  meta: $('meta'), mRes: $('mRes'), mDur: $('mDur'), mCodec: $('mCodec'), mAudio: $('mAudio'),
};

const MAX_CLIP = 8;

// ---------- 工具 ----------
function fmtTime(s) {
  s = Math.max(0, s || 0);
  const m = Math.floor(s / 60);
  const sec = (s % 60).toFixed(1).padStart(4, '0');
  return `${m}:${sec}`;
}
function even(n) { return n - (n % 2); }
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function showError(err) {
  const map = {
    param:   { cls: 'param',   title: '参数不合法', hint: '请检查页面上的输入数值' },
    encoder: { cls: 'encoder', title: '编码器问题', hint: '属于服务端编码环境故障，请联系管理员检查 ffmpeg / libx264' },
    process: { cls: 'process', title: '处理失败',   hint: '源文件或处理过程出错，可尝试更换样片' },
  };
  const kind = (err && err.kind) || 'process';
  const info = map[kind] || map.process;
  const msg = (err && err.message) || '未知错误';
  els.alertBox.className = `alert ${info.cls}`;
  els.alertBox.innerHTML =
    `<span class="tag">${kind.toUpperCase()}</span><b>${info.title}</b>：${msg}` +
    `<span class="detail">建议：${info.hint}</span>`;
}
function clearError() { els.alertBox.className = 'alert hidden'; els.alertBox.innerHTML = ''; }

function setBusy(on, text) {
  state.busy = on;
  els.uploadStatus.textContent = on ? (text || '处理中…') : '';
  els.uploadStatus.className = 'status ' + (on ? 'busy' : '');
  els.snapBtn.disabled = on || !state.videoId;
  els.clipBtn.disabled = on || !state.videoId;
}

// ---------- 数值滑杆 ----------
[['brightness', els.brightnessVal, 2],
 ['denoise', els.denoiseVal, 2],
 ['sharpen', els.sharpenVal, 2],
 ['at', els.atVal, 1]].forEach(([key, lab, dig]) => {
  $(key).addEventListener('input', () => { lab.textContent = Number($(key).value).toFixed(dig); });
});

// ---------- 上传 ----------
els.fileInput.addEventListener('change', async () => {
  const file = els.fileInput.files[0];
  if (!file) return;
  clearError(); hideResult();
  els.fileName.textContent = file.name;
  setBusy(true, '上传并探测视频中…');
  try {
    const fd = new FormData();
    fd.append('file', file);
    const resp = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await resp.json();
    if (!resp.ok) throw data.error || { message: '上传失败' };

    state.videoId = data.video_id;
    state.meta = data.meta;
    if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = URL.createObjectURL(file);
    els.video.src = state.objectUrl;
    els.stage.classList.add('has-video');
    els.stage.classList.remove('empty');

    els.mRes.textContent = `${data.meta.width}×${data.meta.height}`;
    els.mDur.textContent = `${fmtTime(data.meta.duration)}（${data.meta.duration.toFixed(2)}s）`;
    els.mCodec.textContent = data.meta.codec;
    els.mAudio.textContent = data.meta.has_audio ? '有' : '无';
    els.meta.classList.remove('hidden');

    const dur = data.meta.duration || 0;
    els.at.max = dur; els.at.value = 0; els.atVal.textContent = '0.0';
    els.at.disabled = false;
    els.start.value = 0;
    els.duration.value = Math.min(3, dur || 3);
    els.start.disabled = false; els.duration.disabled = false;
    els.maxDur.textContent = MAX_CLIP;
    els.snapBtn.disabled = false; els.clipBtn.disabled = false;

    els.uploadStatus.textContent = '上传成功';
    els.uploadStatus.className = 'status ok';
    resetCrop();
  } catch (e) {
    els.uploadStatus.textContent = '';
    showError(e);
  } finally {
    state.busy = false;
    els.snapBtn.disabled = !state.videoId;
    els.clipBtn.disabled = !state.videoId;
  }
});

// ---------- 裁剪框：坐标映射 ----------
function mediaRect() {
  const sw = els.stage.clientWidth, sh = els.stage.clientHeight;
  const vw = els.video.videoWidth || state.meta.width;
  const vh = els.video.videoHeight || state.meta.height;
  const scale = Math.min(sw / vw, sh / vh);
  const rw = vw * scale, rh = vh * scale;
  return { scale, vw, vh, ox: (sw - rw) / 2, oy: (sh - rh) / 2, rw, rh };
}
function renderCrop() {
  if (!state.crop) { els.cropBox.hidden = true; return; }
  const r = mediaRect();
  const b = els.cropBox;
  b.hidden = !els.cropEnabled.checked;
  b.style.left = (r.ox + state.crop.x * r.scale) + 'px';
  b.style.top = (r.oy + state.crop.y * r.scale) + 'px';
  b.style.width = (state.crop.w * r.scale) + 'px';
  b.style.height = (state.crop.h * r.scale) + 'px';
  els.cropInfo.textContent =
    `X:${state.crop.x} Y:${state.crop.y}  ${state.crop.w}×${state.crop.h}（视频像素）`;
}
function resetCrop() {
  if (!state.meta) return;
  state.crop = { x: 0, y: 0,
    w: state.meta.width - (state.meta.width % 2),
    h: state.meta.height - (state.meta.height % 2) };
  renderCrop();
}
window.addEventListener('resize', renderCrop);
els.video.addEventListener('loadedmetadata', () => { if (state.crop) renderCrop(); });

els.cropEnabled.addEventListener('change', () => {
  els.stage.classList.toggle('cropping', els.cropEnabled.checked);
  els.resetCrop.hidden = !els.cropEnabled.checked;
  if (els.cropEnabled.checked && !state.crop) resetCrop();
  renderCrop();
});
els.resetCrop.addEventListener('click', () => { resetCrop(); });

// ---------- 裁剪框：拖拽 / 缩放 / 框选 ----------
function eventToVideo(e) {
  const r = mediaRect();
  const sr = els.stage.getBoundingClientRect();
  const px = e.clientX - sr.left, py = e.clientY - sr.top;
  return {
    r,
    vx: clamp((px - r.ox) / r.scale, 0, r.vw),
    vy: clamp((py - r.oy) / r.scale, 0, r.vh),
  };
}

els.stage.addEventListener('pointerdown', (e) => {
  if (!els.cropEnabled.checked || !state.meta || state.busy) return;
  if (e.target.closest('.crop-box')) return;  // 交给框自己处理
  // 在空白处拖动 = 重新框选
  const { vx, vy } = eventToVideo(e);
  const start = { x: vx, y: vy };
  els.stage.setPointerCapture(e.pointerId);
  const onMove = (ev) => {
    const p = eventToVideo(ev);
    const x = Math.min(start.x, p.vx), y = Math.min(start.y, p.vy);
    const w = even(Math.max(2, Math.round(Math.abs(p.vx - start.x))));
    const h = even(Math.max(2, Math.round(Math.abs(p.vy - start.y))));
    state.crop = {
      x: Math.round(clamp(x, 0, p.r.vw - w)),
      y: Math.round(clamp(y, 0, p.r.vh - h)),
      w: Math.min(w, p.r.vw - (p.r.vw % 2)),
      h: Math.min(h, p.r.vh - (p.r.vh % 2)),
    };
    renderCrop();
  };
  const onUp = () => {
    els.stage.removeEventListener('pointermove', onMove);
    els.stage.removeEventListener('pointerup', onUp);
  };
  els.stage.addEventListener('pointermove', onMove);
  els.stage.addEventListener('pointerup', onUp);
});

els.cropBox.addEventListener('pointerdown', (e) => {
  if (state.busy) return;
  const dir = e.target.dataset.dir || 'move';
  e.preventDefault();
  e.stopPropagation();
  els.cropBox.setPointerCapture(e.pointerId);
  const begin = eventToVideo(e);
  const orig = { ...state.crop };

  const onMove = (ev) => {
    const p = eventToVideo(ev);
    const dx = p.vx - begin.vx, dy = p.vy - begin.vy;
    let { x, y, w, h } = orig;
    if (dir === 'move') {
      x = Math.round(clamp(orig.x + dx, 0, p.r.vw - orig.w));
      y = Math.round(clamp(orig.y + dy, 0, p.r.vh - orig.h));
    } else {
      const min = 2;
      if (dir.includes('w')) {
        const nx = clamp(orig.x + dx, 0, orig.x + orig.w - min);
        w = even(Math.round(orig.x + orig.w - nx));
        x = orig.x + orig.w - w;
      }
      if (dir.includes('e')) {
        w = even(Math.round(clamp(orig.w + dx, min, p.r.vw - orig.x)));
      }
      if (dir.includes('n')) {
        const ny = clamp(orig.y + dy, 0, orig.y + orig.h - min);
        h = even(Math.round(orig.y + orig.h - ny));
        y = orig.y + orig.h - h;
      }
      if (dir.includes('s')) {
        h = even(Math.round(clamp(orig.h + dy, min, p.r.vh - orig.y)));
      }
    }
    state.crop = { x, y, w, h };
    renderCrop();
  };
  const onUp = () => {
    els.cropBox.removeEventListener('pointermove', onMove);
    els.cropBox.removeEventListener('pointerup', onUp);
  };
  els.cropBox.addEventListener('pointermove', onMove);
  els.cropBox.addEventListener('pointerup', onUp);
});

// ---------- 结果 ----------
function hideResult() {
  els.result.classList.add('hidden');
  els.resultBody.innerHTML = '';
}
function showResult(kind, url, params, extra) {
  els.result.classList.remove('hidden');
  els.downloadLink.href = url;
  els.downloadLink.setAttribute('download', kind === 'snapshot' ? 'snapshot.png' : 'enhanced.mp4');
  if (kind === 'snapshot') {
    els.resultTitle.textContent = `截图预览（t=${extra.at}s）`;
    els.resultBody.innerHTML = `<img src="${url}&_=${Date.now()}" alt="截图预览">`;
  } else {
    els.resultTitle.textContent = `增强短片段（${extra.start}s 起，${extra.duration}s）`;
    els.resultBody.innerHTML =
      `<video src="${url}&_=${Date.now()}" controls preload="metadata"></video>`;
  }
  els.resultParams.textContent = '后端实际使用参数：\n' + JSON.stringify(params, null, 2);
}

function collectParams(action) {
  const p = {
    video_id: state.videoId,
    action,
    brightness: Number(els.brightness.value),
    denoise: Number(els.denoise.value),
    sharpen: Number(els.sharpen.value),
    crop_enabled: els.cropEnabled.checked,
  };
  if (state.crop && els.cropEnabled.checked) {
    p.crop_x = state.crop.x; p.crop_y = state.crop.y;
    p.crop_w = state.crop.w; p.crop_h = state.crop.h;
  }
  return p;
}

async function callProcess(params, busyText) {
  clearError();
  setBusy(true, busyText);
  try {
    const resp = await fetch('/api/process', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
    const data = await resp.json();
    if (!resp.ok) throw data.error || { kind: 'process', message: '请求失败' };
    const r = data.result;
    showResult(r.kind, r.url, r.params, r);
  } catch (e) {
    if (e instanceof TypeError) showError({ kind: 'process', message: '无法连接后端服务' });
    else showError(e);
  } finally {
    setBusy(false);
  }
}

els.snapBtn.addEventListener('click', () => {
  const params = collectParams('snapshot');
  params.at = Number(els.at.value);
  callProcess(params, '正在生成截图…');
});
els.clipBtn.addEventListener('click', () => {
  const params = collectParams('clip');
  const start = Number(els.start.value);
  const duration = Number(els.duration.value);
  if (!Number.isFinite(start) || start < 0) {
    showError({ kind: 'param', message: '开始时间必须是 ≥ 0 的数字' });
    return;
  }
  if (!Number.isFinite(duration) || duration <= 0) {
    showError({ kind: 'param', message: '片段时长必须大于 0' });
    return;
  }
  params.start = start;
  params.duration = duration;
  callProcess(params, '正在编码短片段（H.264）…');
});
