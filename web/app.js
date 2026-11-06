const $ = (id) => document.getElementById(id);
let cache = [];
let cacheKey = "";
let apiBase = "";
let settingsDirty = false;

// 面板上展示的地址用服务端算出来的对外地址：套了反代时它才是外网能用的那个
function base() {
  return (apiBase || location.origin).replace(/\/+$/, "");
}

function esc(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fillUrls() {
  const map = {
    urlM3u: "/playlist.m3u",
    urlTxt: "/txt",
    urlEpg: "/epg.xml",
    urlJson: "/json",
  };
  for (const [id, path] of Object.entries(map)) {
    const el = $(id);
    if (el) el.value = base() + path;
  }
}

function setDot(state) {
  const dot = $("statusDot");
  dot.className = "dot " + state;
}

function epgText(epg) {
  if (!epg) return "节目单：-";
  const n = epg.channels || 0;
  if (n > 0) {
    const ago = epg.age == null ? "" : `（${epg.age} 秒前生成）`;
    return `节目单：已对上 ${n} 个频道${ago}`;
  }
  if (epg.error) return `节目单：${epg.error}`;
  return "节目单：等待生成（更新一次即可，或者直接打开 /epg.xml 触发）";
}

function renderMissing(rows) {
  const box = $("missingNote");
  if (!box) return;
  if (!rows.length) {
    box.hidden = true;
    return;
  }
  const absent = rows.filter((r) => (r.reason || "").includes("没有这个台"));
  const failed = rows.filter((r) => !(r.reason || "").includes("没有这个台"));
  const parts = [];
  if (failed.length) {
    parts.push(`本轮没取到 ${failed.length} 个：${failed.map((r) => r.name).join("、")}`);
  }
  if (absent.length) {
    parts.push(`上游没有 ${absent.length} 个：${absent.map((r) => r.name).join("、")}`);
  }
  box.hidden = false;
  box.textContent =
    "频道缺口 — " + parts.join("；") +
    (absent.length ? "。上游没有的台不用管，模板里已注释掉；想加就自己接一个订阅源。" : "。下一轮会自动重试，已取到过的台不会从列表里消失。");
}

async function loadStatus() {
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    const d = await r.json();
    $("version").textContent = "v" + (d.version || "-");
    const rep = d.report || {};

    if (d.base && d.base !== apiBase) {
      apiBase = d.base;
      fillUrls();
    }

    setDot(d.running ? "busy" : (d.last_error ? "err" : "ok"));
    $("btnUpdate").disabled = !!d.running;
    $("btnUpdate").textContent = d.running ? "更新中…" : "立即更新";

    const hidden = (d.settings && d.settings.hidden) || [];
    const total = rep.channels_matched != null ? rep.channels_matched : null;
    if ($("statChannels")) {
      $("statChannels").textContent =
        total == null ? "-" : (hidden.length ? `${total - hidden.length}` : `${total}`);
      $("statChannels").title = hidden.length ? `${total} 个频道，其中 ${hidden.length} 个已隐藏` : "";
    }
    $("statStreams").textContent = rep.published != null ? rep.published : "-";
    // 卡片窄，完整的 ISO 时间会把格子撑破（手机上直接横向溢出），只留到分钟
    $("statTime").textContent = shortTime(d.generated_at);
    $("statTime").title = d.generated_at || "";

    const relay = d.relay || {};
    const banner = $("relayBanner");
    if (banner) {
      const blocked = relay.blocked_for || 0;
      if (blocked > 0) {
        banner.hidden = false;
        banner.textContent =
          `上游正在按 IP 限流，${blocked} 秒内不再去要新地址（避免越要越死）。` +
          "已经播着的台不受影响，这时候连切台会更容易卡。";
      } else if (relay.failures > 0) {
        const detail = (relay.errors && relay.errors[0]) ? `（${relay.errors[0]}）` : "";
        const proxy = relay.mode === "proxy";
        banner.hidden = false;
        banner.textContent =
          `取清单时上游拒绝过 ${relay.failures} 次${detail}，其中 ${relay.stale_served || 0} 次用上一份清单顶住了。` +
          (proxy
            ? "当前是「经 NAS 转发」模式，分片也从本机走，被平台判成异常流量的概率高——建议把播放方式切回「直连」。"
            : "分片由播放器直连 CDN，不受影响；偶尔切台失败重试一次即可。");
      } else {
        banner.hidden = true;
      }
    }

    renderMissing(rep.missing || []);

    if ($("epgNote")) $("epgNote").textContent = epgText(d.epg);
    fillSettings(d.settings || {});

    const logs = (d.logs || []).join("\n");
    const box = $("log");
    box.textContent = logs || "暂无日志";
    box.scrollTop = box.scrollHeight;
  } catch (e) {
    setDot("err");
    $("log").textContent = "无法连接服务：" + e;
  }
}

function fillSettings(s) {
  if (settingsDirty) return;
  if ($("setPlayMode")) $("setPlayMode").value = s.play_mode || "direct";
  if ($("setPublicUrl")) $("setPublicUrl").value = s.public_url || "";
  if ($("setInterval")) $("setInterval").value = s.interval_hours != null ? s.interval_hours : "";
  if ($("setTtl")) $("setTtl").value = s.playlist_ttl != null ? s.playlist_ttl : "";
  if ($("setCache")) $("setCache").value = s.cache_mb != null ? s.cache_mb : "";
  if ($("setEpgSource")) $("setEpgSource").value = s.epg_source || "";
}

async function saveSettings() {
  const btn = $("btnSaveSettings");
  const note = $("settingsNote");
  const payload = {
    play_mode: $("setPlayMode") ? $("setPlayMode").value : "direct",
    public_url: $("setPublicUrl").value.trim(),
    interval_hours: Number($("setInterval").value || 0),
    playlist_ttl: Number($("setTtl").value || 1),
    cache_mb: Number($("setCache").value || 96),
    epg_source: $("setEpgSource").value.trim(),
  };
  btn.disabled = true;
  try {
    const r = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    settingsDirty = false;
    note.textContent = `已保存：更新间隔 ${d.settings.interval_hours} 小时、`
      + `清单复用 ${d.settings.playlist_ttl} 秒、分片缓存 ${d.settings.cache_mb} MB。`
      + "播放器里的地址如果变了，重新拉一次列表即可。";
    await refresh();
  } catch (e) {
    note.textContent = "保存失败：" + e.message;
  } finally {
    btn.disabled = false;
  }
}

function gradePillClass(grade) {
  return grade === "4K" ? "g4k" : grade === "1080P" ? "g1080" : grade === "720P" ? "g720" : "";
}

// 速率档位：播放器实际能不能流畅，看这个比看分辨率更准
function rateClass(mbps) {
  if (!mbps) return "";
  if (mbps >= 6) return "fast";
  if (mbps >= 2.5) return "mid";
  return "slow";
}

function rateText(stream) {
  // 码率是这条流本身有多重，测速是 NAS 能不能拉得动，两个都有意义：
  // 优先显示码率，因为它直接决定「这条流需要多少带宽才不卡」
  if (stream.bandwidth_kbps) return `码率 ${(stream.bandwidth_kbps / 1000).toFixed(1)} Mbps`;
  if (stream.speed_mbps) return `测速 ${stream.speed_mbps.toFixed(1)} Mbps`;
  return "未测速";
}

// 2026-11-06T14:30:00+08:00 → 11-06 14:30。卡片里塞不下完整时间戳
function shortTime(value) {
  if (!value) return "-";
  const m = String(value).match(/^\d{4}-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  return m ? `${m[1]}-${m[2]} ${m[3]}:${m[4]}` : String(value);
}

// 测出来的数字是「本机从这台取流能跑多快」，不是视频码率，鼠标悬停写清楚
function rateTitle(stream) {
  const parts = [];
  if (stream.bandwidth_kbps) parts.push(`码率 ${(stream.bandwidth_kbps / 1000).toFixed(1)} Mbps（分片大小 ÷ 分片时长）`);
  if (stream.speed_mbps) parts.push(`采样下载 ${stream.speed_mbps.toFixed(1)} Mbps（NAS 到 CDN 的取流能力）`);
  if (stream.delay_ms) parts.push(`首字节 ${stream.delay_ms} ms`);
  return parts.join("\n") || "这一轮没测到速率（多半被上游限流，播放不受影响）";
}

function streamMeta(stream) {
  const size = stream.width && stream.height ? `${stream.width}×${stream.height}` : "";
  const rate = rateText(stream);
  const cls = rateClass(stream.speed_mbps);
  return `<span class="pill ${gradePillClass(stream.grade)}">${esc(stream.grade || "未知")}</span>
    ${size ? `<span class="res">${size}</span>` : ""}
    <span class="rate ${cls}" title="${esc(rateTitle(stream))}">${esc(rate)}</span>`;
}

function renderChannels(channels) {
  const keyword = ($("search").value || "").trim().toLowerCase();
  const box = $("channelList");
  const all = channels || [];
  const showHidden = $("showHidden") && $("showHidden").checked;
  const hiddenCount = all.filter((c) => c.hidden).length;

  const badge = $("hiddenCount");
  if (badge) {
    badge.hidden = !hiddenCount;
    badge.textContent = hiddenCount ? `已隐藏 ${hiddenCount}` : "";
  }
  if ($("btnUnhideAll")) $("btnUnhideAll").hidden = !hiddenCount;

  // 搜索时连隐藏的也一起找出来，否则想恢复还得先勾「显示已隐藏」
  const list = all.filter((c) => {
    const matchKeyword = !keyword
      || c.name.toLowerCase().includes(keyword)
      || (c.group || "").toLowerCase().includes(keyword);
    if (!matchKeyword) return false;
    return showHidden || !c.hidden || !!keyword;
  });

  if (!list.length) {
    box.innerHTML = `<p class="empty">${all.length ? "没有匹配的频道" : "暂无数据，点右上角「立即更新」跑一次。"}</p>`;
    return;
  }

  let html = "";
  let group = null;
  const counts = {};
  for (const ch of list) {
    if (ch.group !== group) {
      group = ch.group;
      const groupVisible = list.some((c) => (c.group || "") === (group || "") && !c.hidden);
      html += `<div class="group-title">${esc(group || "未分组")}${
        groupVisible ? `<button class="btn mini ghost" data-hide-group="${esc(group || "")}">隐藏本组</button>` : ""
      }</div>`;
    }
    const visible = !ch.hidden;
    if (visible) {
      for (const s of ch.streams || []) {
        const key = s.grade || "未知";
        counts[key] = (counts[key] || 0) + 1;
      }
    }
    // 一个台多条源时逐条列出来，方便直接挑速率高的那条
    const rows = (ch.streams || []).map((s, i) => {
      const multi = ch.streams.length > 1 ? `<span class="idx">${i + 1}</span>` : "";
      return `<div class="stream-row">${multi}${streamMeta(s)}</div>`;
    }).join("");
    const slug = ((ch.streams || []).find((s) => s.slug) || {}).slug || "";
    const ops = visible
      ? `${slug
          ? `<button class="btn mini" data-play="${esc(slug)}" data-name="${esc(ch.name)}">播放</button>`
          : `<button class="btn mini" disabled title="这条流来自外部订阅，面板播放需要本机 /live/ 地址">播放</button>`}
         <button class="btn mini" data-hide="${esc(ch.name)}">隐藏</button>`
      : `<button class="btn mini" data-unhide="${esc(ch.name)}">恢复</button>`;
    html += `<div class="chan ${visible ? "" : "is-hidden"}">
      <span class="name">${esc(ch.name)}${visible ? "" : '<span class="pill hidden-pill">已隐藏</span>'}</span>
      <span class="meta">${rows}</span>
      <span class="ops">${ops}</span>
    </div>`;
  }
  box.innerHTML = html;
  $("statGrades").textContent = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .map(([grade, n]) => `${grade}×${n}`)
    .join("  ") || "-";
  // 顶部的「直播流」跟着隐藏一起减，否则隐藏完数字不动，看着像没生效
  if ($("statStreams")) {
    const streams = all.filter((c) => !c.hidden).reduce((n, c) => n + (c.streams || []).length, 0);
    $("statStreams").textContent = all.length ? `${streams}` : "-";
  }
}

async function loadChannels() {
  try {
    const r = await fetch("/api/channels", { cache: "no-store" });
    const d = await r.json();
    const rows = d.channels || [];
    // 5 秒轮询一次，数据没变就别重建 DOM——重建会把列表滚动位置顶回顶部
    const key = JSON.stringify(rows);
    if (key === cacheKey) return;
    cacheKey = key;
    cache = rows;
    renderChannels(cache);
  } catch (e) {
    /* 忽略：状态接口会给出错误提示 */
  }
}

async function setHidden(names, hidden) {
  const list = (Array.isArray(names) ? names : [names]).filter(Boolean);
  if (!list.length) return;
  try {
    const r = await fetch("/api/hidden", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names: list, hidden }),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    await Promise.all([loadChannels(), loadStatus()]);
  } catch (e) {
    $("log").textContent = "隐藏操作失败：" + e;
  }
}

function hideGroup(group) {
  const names = cache
    .filter((c) => (c.group || "") === group && !c.hidden)
    .map((c) => c.name);
  setHidden(names, true);
}

function unhideAll() {
  setHidden(cache.filter((c) => c.hidden).map((c) => c.name), false);
}

async function triggerUpdate() {
  $("btnUpdate").disabled = true;
  try {
    const r = await fetch("/api/update", { method: "POST" });
    const d = await r.json();
    $("log").textContent = d.message || "";
  } catch (e) {
    $("log").textContent = "触发失败：" + e;
  }
  setTimeout(refresh, 600);
}

async function checkUpdate() {
  const btn = $("btnCheckUpdate");
  const out = $("updateResult");
  btn.disabled = true;
  out.className = "update-result";
  out.textContent = "检查中…";
  try {
    const r = await fetch("/api/version?check=1", { cache: "no-store" });
    const d = await r.json();
    if (d.error) {
      out.className = "update-result err";
      // 每个源的失败原因都很长，正文只放结论，细节挂在 title 里
      out.textContent = "检查失败：" + (d.error_hint || "连不上更新服务器，稍后再试");
      out.title = [d.error, d.error_hint].filter(Boolean).join("\n");
    } else if (d.has_update) {
      out.className = "update-result warn";
      out.textContent = `有新版 v${d.latest}，换新镜像重启即可`;
      out.title = `当前 v${d.current}，来源 ${d.source || "-"}`;
    } else {
      out.className = "update-result ok";
      out.textContent = `已是最新（仓库 v${d.latest}）`;
      out.title = `来源 ${d.source || "-"}`;
    }
  } catch (e) {
    out.className = "update-result err";
    out.textContent = "检查失败：" + e;
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- 面板内播放 ---------------- */
// HLS 直播天生滞后：上游清单只保留 3 个分片，播放器必须落后 2~3 片才有缓冲，
// 所以「进度条从 0:18 走到 0:35」是窗口在往前滚，不是延迟越滚越大。这里显示
// 真实的「距直播最新位置还差几秒」，并且落后太多时自动加速追回去。

let hls = null;
let playerSlug = "";
let playerName = "";
let statsTimer = null;
let netRetries = 0;
let mediaRetries = 0;
// 画面卡住的自救：上一步播到哪、什么时候还在往前走
let lastTime = 0;
let lastProgressAt = 0;
let restartTimer = null;
let restarts = 0;

function playerLatency() {
  const v = $("playerVideo");
  if (hls && Number.isFinite(hls.latency)) return hls.latency;
  try {
    const s = v.seekable;
    if (s && s.length) return Math.max(0, s.end(s.length - 1) - v.currentTime);
  } catch (_) { /* 直播刚开始时 seekable 是空的 */ }
  return null;
}

function playerStats() {
  const v = $("playerVideo");
  const parts = [];
  const delay = playerLatency();
  if (delay != null) parts.push(`延迟 ${delay.toFixed(1)}s`);

  try {
    let buffered = 0;
    for (let i = 0; i < v.buffered.length; i++) {
      if (v.currentTime >= v.buffered.start(i) - 1 && v.currentTime <= v.buffered.end(i) + 1) {
        buffered = v.buffered.end(i) - v.currentTime;
        break;
      }
    }
    if (buffered > 0) parts.push(`缓冲 ${buffered.toFixed(1)}s`);
  } catch (_) { /* ignore */ }

  if (v.videoWidth) parts.push(`${v.videoWidth}×${v.videoHeight}`);
  if (hls && hls.levels && hls.currentLevel >= 0) {
    const level = hls.levels[hls.currentLevel];
    if (level && level.bitrate) parts.push(`${(level.bitrate / 1e6).toFixed(1)} Mbps`);
  }

  // 卡死看门狗：正在播却一直不往前走，就是「这十几秒播完了、新的没来」。
  // 上游清单冻住时不会有任何报错，只能靠这个发现。
  const now = Date.now();
  if (!v.paused && !v.seeking && v.currentTime > lastTime + 0.05) {
    lastTime = v.currentTime;
    lastProgressAt = now;
  }
  const stuckMs = lastProgressAt ? now - lastProgressAt : 0;
  if (!v.paused && stuckMs > 3000) {
    parts.push(`画面停住 ${Math.floor(stuckMs / 1000)}s`);
  }
  $("playerStats").textContent = parts.join(" · ") || "连接中…";

  if (!v.paused && stuckMs > 4000 && stuckMs < 50000) {
    $("playerTip").textContent = stuckMs > 12000
      ? "上游一直没给新分片，正在换票重连…"
      : "等上游给下一段——上游清单 5 秒才滚一片，稍等一下。";
  }
  if (!v.paused && stuckMs > 12000 && restarts < 8) restartPlayer("画面停住了");

  const badge = $("playerBadge");
  if (badge) {
    badge.hidden = false;
    badge.classList.toggle("warn", delay != null && delay > 25);
    badge.textContent = delay != null && delay > 25 ? "落后较多" : "直播";
  }
}

function stopStats() {
  if (statsTimer) clearInterval(statsTimer);
  statsTimer = null;
}

function closePlayer() {
  stopStats();
  if (restartTimer) {
    clearTimeout(restartTimer);
    restartTimer = null;
  }
  lastTime = 0;
  lastProgressAt = 0;
  const v = $("playerVideo");
  try { v.pause(); } catch (_) { /* ignore */ }
  if (hls) {
    try { hls.destroy(); } catch (_) { /* ignore */ }
    hls = null;
  }
  v.removeAttribute("src");
  v.load();
  $("playerMask").hidden = true;
  playerSlug = "";
}

function goLive() {
  const v = $("playerVideo");
  let target = null;
  if (hls && Number.isFinite(hls.liveSyncPosition)) {
    target = hls.liveSyncPosition;
  } else {
    try {
      const s = v.seekable;
      if (s && s.length) target = Math.max(0, s.end(s.length - 1) - 6);
    } catch (_) { /* ignore */ }
  }
  if (target != null) {
    try { v.currentTime = target; } catch (_) { /* ignore */ }
    v.play().catch(() => {});
    $("playerTip").textContent = "已跳到最新位置。再落后就再点一次。";
  }
}

function failPlayer(message) {
  $("playerStats").textContent = "播放失败：" + message;
  $("playerBadge").hidden = true;
  // 必须先停掉每秒刷新的状态栏，否则这句话一秒后就被「连接中…」盖掉了
  stopStats();
}

function openPlayer(slug, name, keepRestarts) {
  const v = $("playerVideo");
  closePlayer();
  playerSlug = slug;
  playerName = name || slug;
  netRetries = 0;
  mediaRetries = 0;
  if (!keepRestarts) restarts = 0;
  $("playerTitle").textContent = playerName;
  $("playerMask").hidden = false;
  $("playerStats").textContent = keepRestarts ? "重连中…" : "正在取清单…";
  $("playerBadge").hidden = true;
  if (!keepRestarts) {
    $("playerTip").textContent =
      "直播流本身就比电视台晚 2~3 个分片（这台约 10~15 秒）。进度条上的 0:18 是「当前可播窗口里的位置」，不是延迟在变大。";
  }
  stopStats();
  statsTimer = setInterval(playerStats, 1000);

  const url = `/live/${encodeURIComponent(slug)}.m3u8`;

  // Safari / iOS 原生就能播 HLS，不需要也不该用 hls.js
  if (v.canPlayType("application/vnd.apple.mpegurl")) {
    v.src = url;
    v.play().catch(() => {});
    return;
  }
  if (!window.Hls || !window.Hls.isSupported()) {
    failPlayer("这个浏览器不支持 HLS，请用 Chrome / Edge / Safari，或把地址复制到播放器里");
    return;
  }

  hls = new Hls({
    // 不开低延迟模式：那套要清单支持 _HLS_msn 阻塞式重载，上游的清单没有，
    // 播放器会当成「返回得太快=没新东西」立刻再问一次，几十秒就把本机的
    // 上游配额抽干，接着清单拿不到、画面停在播完的那十几秒上。
    lowLatencyMode: false,
    // 目标落后 2 个分片（约 10 秒）：再小一点上游只有 3 片，容易卡；
    // 落后超过 5 片就加速播放追回来，而不是让延迟越滚越大
    liveSyncDurationCount: 2,
    liveMaxLatencyDurationCount: 5,
    maxLiveSyncPlaybackRate: 1.25,
    backBufferLength: 30,
    manifestLoadingTimeOut: 15000,
    fragLoadingTimeOut: 20000,
    // 上游时不时回一两次 403/超时，重试要够多，别一次就判死
    manifestLoadingMaxRetry: 5,
    manifestLoadingRetryDelay: 1000,
    levelLoadingMaxRetry: 6,
    levelLoadingRetryDelay: 1000,
    fragLoadingMaxRetry: 6,
    fragLoadingRetryDelay: 800,
  });
  hls.loadSource(url);
  hls.attachMedia(v);
  hls.on(Hls.Events.MANIFEST_PARSED, () => {
    v.play().catch(() => {
      $("playerTip").textContent = "浏览器拦了自动播放，点一下画面中间的播放按钮即可。";
    });
  });
  hls.on(Hls.Events.ERROR, (_evt, data) => {
    if (!data.fatal) return;
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
      // 先原地重载清单；还是不行就整条重开。整条重开会让服务端重新换票，
      // 这正是「清单冻住」唯一有效的解法。
      netRetries += 1;
      if (netRetries <= 2) {
        $("playerStats").textContent = `网络抖动，第 ${netRetries} 次重试…`;
        setTimeout(() => { if (hls) hls.startLoad(); }, 1200);
      } else {
        restartPlayer(`取不到新的清单（第 ${netRetries} 次），重连中…`);
      }
      return;
    }
    if (data.type === Hls.ErrorTypes.MEDIA_ERROR && mediaRetries < 2) {
      mediaRetries += 1;
      $("playerStats").textContent = `解码出错，第 ${mediaRetries} 次修复…`;
      if (hls) hls.recoverMediaError();
      return;
    }
    failPlayer(playerErrorText(data));
  });
}

// 整条重开：销毁再建，等于让服务端重新换一次票。
// 「播完十几秒停住」这类卡死靠重载清单救不回来，只能这么办。
function restartPlayer(reason) {
  if (!playerSlug || restartTimer) return;
  if (restarts >= 8) {
    failPlayer("这个台连续重连都拿不到新分片，多半是上游限流了，过几分钟再点");
    return;
  }
  restarts += 1;
  $("playerTip").textContent = reason + " 上游清单可能冻住了，换票重取。";
  restartTimer = setTimeout(() => {
    restartTimer = null;
    if (!playerSlug) return;
    const slug = playerSlug;
    const name = playerName;
    openPlayer(slug, name, true);
  }, 2000);
}

// 把 hls.js 的错误码翻译成「下一步该干嘛」：绝大多数人卡在这里时，
// 真正需要知道的是去改哪个开关，而不是 fragLoadError 这种内部名词。
function playerErrorText(data) {
  const detail = data.details || "";
  const status = data.response && data.response.code;
  if (detail === "manifestLoadError" || detail === "manifestLoadTimeOut") {
    return "取不到清单——多半是上游正在限流，等一分钟再点；面板顶部会说明";
  }
  if (detail === "fragLoadError" || detail === "fragLoadTimeOut"
      || detail === "levelLoadError" || detail === "levelLoadTimeOut") {
    if (status === 403) {
      return "分片被 CDN 拒了（403）——这个台刚到时限流，或分片已过期，换台再回来。"
        + "多个台都这样，就到「运行设置」把播放方式换成 proxy 再试。";
    }
    return "分片取不到，多半是 CDN 不允许浏览器直连（跨域被拦）。"
      + "到「运行设置」把播放方式改成 proxy 保存，再点这个台。";
  }
  if (detail === "bufferStalledError") {
    return "缓冲停住了——点右下角「回到直播」追上去。";
  }
  if (detail === "fragParsingError") {
    return "分片解不出来，这个台的编码浏览器啃不动，换个台或者用外部播放器。";
  }
  return detail || data.type || "未知错误";
}

async function refresh() {
  await loadStatus();
  await loadChannels();
}

document.addEventListener("DOMContentLoaded", () => {
  fillUrls();
  refresh();
  setInterval(refresh, 5000);

  $("btnUpdate").addEventListener("click", triggerUpdate);
  $("btnRefresh").addEventListener("click", refresh);
  $("btnCheckUpdate").addEventListener("click", checkUpdate);
  $("btnSaveSettings").addEventListener("click", saveSettings);
  $("search").addEventListener("input", () => renderChannels(cache));
  if ($("showHidden")) $("showHidden").addEventListener("change", () => renderChannels(cache));

  $("channelList").addEventListener("click", (e) => {
    const play = e.target.closest("[data-play]");
    if (play) {
      openPlayer(play.dataset.play, play.dataset.name);
      return;
    }
    const hide = e.target.closest("[data-hide]");
    if (hide) {
      setHidden(hide.dataset.hide, true);
      return;
    }
    const unhide = e.target.closest("[data-unhide]");
    if (unhide) {
      setHidden(unhide.dataset.unhide, false);
      return;
    }
    const group = e.target.closest("[data-hide-group]");
    if (group) hideGroup(group.dataset.hideGroup);
  });
  if ($("btnUnhideAll")) $("btnUnhideAll").addEventListener("click", unhideAll);

  $("btnClosePlayer").addEventListener("click", closePlayer);
  $("btnGoLive").addEventListener("click", goLive);
  $("btnCopyLive").addEventListener("click", async () => {
    if (!playerSlug) return;
    const url = `${location.origin}/live/${encodeURIComponent(playerSlug)}.m3u8`;
    const btn = $("btnCopyLive");
    try {
      await navigator.clipboard.writeText(url);
      btn.textContent = "已复制";
      setTimeout(() => { btn.textContent = "复制地址"; }, 1200);
    } catch (_) {
      window.prompt("复制这个地址：", url);
    }
  });
  $("playerMask").addEventListener("click", (e) => {
    if (e.target === $("playerMask")) closePlayer();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("playerMask").hidden) closePlayer();
  });

  // 用户正在改设置时不要被轮询覆盖
  ["setPlayMode", "setPublicUrl", "setInterval", "setTtl", "setCache", "setEpgSource"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("input", () => { settingsDirty = true; });
  });

  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const input = $(btn.dataset.copy);
      const text = input.value;
      try {
        await navigator.clipboard.writeText(text);
        const old = btn.textContent;
        btn.textContent = "已复制";
        setTimeout(() => { btn.textContent = old; }, 1200);
      } catch {
        input.select();
        document.execCommand("copy");
      }
    });
  });
});
