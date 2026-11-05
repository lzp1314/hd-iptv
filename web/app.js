const $ = (id) => document.getElementById(id);
let cache = [];

function base() {
  return location.origin;
}

function fillUrls() {
  for (const [id, path] of [["urlM3u", "/m3u"], ["urlTxt", "/txt"], ["urlJson", "/json"]]) {
    const el = $(id);
    if (el) el.value = base() + path;
  }
}

function setDot(state) {
  const dot = $("statusDot");
  dot.className = "dot " + state;
}

async function loadStatus() {
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    const d = await r.json();
    $("version").textContent = "v" + (d.version || "-");
    if ($("curVersion")) $("curVersion").textContent = "v" + (d.version || "-");
    const rep = d.report || {};

    setDot(d.running ? "busy" : (d.last_error ? "err" : "ok"));
    $("btnUpdate").disabled = !!d.running;
    $("btnUpdate").textContent = d.running ? "更新中…" : "立即更新";

    $("statChannels").textContent = rep.channels_matched != null
      ? `${rep.channels_matched}` : "-";
    $("statStreams").textContent = rep.published != null ? rep.published : "-";
    $("statTime").textContent = d.generated_at || "-";

    const relay = d.relay || {};
    const banner = $("relayBanner");
    if (banner) {
      if (relay.failures > 0) {
        const detail = (relay.errors && relay.errors[0]) ? `（${relay.errors[0]}）` : "";
        banner.hidden = false;
        banner.textContent =
          `上游最近拒绝 ${relay.failures} 次请求，已用缓存顶住 ${relay.stale_served || 0} 次${detail}。` +
          "这段时间画面可能卡顿，通常一两分钟自动恢复；恢复前少切台能少触发请求。";
      } else {
        banner.hidden = true;
      }
    }

    const logs = (d.logs || []).join("\n");
    const box = $("log");
    box.textContent = logs || "暂无日志";
    box.scrollTop = box.scrollHeight;
  } catch (e) {
    setDot("err");
    $("log").textContent = "无法连接服务：" + e;
  }
}

function gradePill(grade) {
  const cls = grade === "4K" ? "g4k" : grade === "1080P" ? "g1080" : grade === "720P" ? "g720" : "";
  return `<span class="pill ${cls}">${grade || "未知"}</span>`;
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
  return `<span class="pill ${gradePillClass(stream.grade)}">${stream.grade || "未知"}</span>
    ${size ? `<span class="res">${size}</span>` : ""}
    <span class="rate ${cls}" title="${rateTitle(stream).replace(/"/g, "&quot;")}">${rate}</span>`;
}

function gradePillClass(grade) {
  return grade === "4K" ? "g4k" : grade === "1080P" ? "g1080" : grade === "720P" ? "g720" : "";
}

function renderChannels(channels) {
  const keyword = ($("search").value || "").trim().toLowerCase();
  const box = $("channelList");
  const list = (channels || []).filter((c) => {
    if (!keyword) return true;
    return c.name.toLowerCase().includes(keyword) || (c.group || "").toLowerCase().includes(keyword);
  });

  if (!list.length) {
    box.innerHTML = `<p class="empty">${channels && channels.length ? "没有匹配的频道" : "暂无数据，点右上角「立即更新」跑一次。"}</p>`;
    return;
  }

  let html = "";
  let group = null;
  const counts = {};
  for (const ch of list) {
    if (ch.group !== group) {
      group = ch.group;
      html += `<div class="group-title">${group || "未分组"}</div>`;
    }
    for (const s of ch.streams || []) {
      const key = s.grade || "未知";
      counts[key] = (counts[key] || 0) + 1;
    }
    // 一个台多条源时逐条列出来，方便直接挑速率高的那条
    const rows = (ch.streams || []).map((s, i) => {
      const multi = ch.streams.length > 1 ? `<span class="idx">${i + 1}</span>` : "";
      return `<div class="stream-row">${multi}${streamMeta(s)}</div>`;
    }).join("");
    html += `<div class="chan">
      <span class="name">${ch.name}</span>
      <span class="meta">${rows}</span>
    </div>`;
  }
  box.innerHTML = html;
  $("statGrades").textContent = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .map(([grade, n]) => `${grade}×${n}`)
    .join("  ") || "-";
}

async function loadChannels() {
  try {
    const r = await fetch("/api/channels", { cache: "no-store" });
    const d = await r.json();
    cache = d.channels || [];
    renderChannels(cache);
  } catch (e) {
    /* 忽略：状态接口会给出错误提示 */
  }
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
  $("search").addEventListener("input", () => renderChannels(cache));

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
