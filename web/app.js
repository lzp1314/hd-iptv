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
    const rep = d.report || {};

    setDot(d.running ? "busy" : (d.last_error ? "err" : "ok"));
    $("btnUpdate").disabled = !!d.running;
    $("btnUpdate").textContent = d.running ? "更新中…" : "立即更新";

    $("statChannels").textContent = rep.channels_matched != null
      ? `${rep.channels_matched}` : "-";
    $("statStreams").textContent = rep.published != null ? rep.published : "-";
    $("statTime").textContent = d.generated_at || "-";

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
  let counts = { "4K": 0, "1080P": 0 };
  for (const ch of list) {
    if (ch.group !== group) {
      group = ch.group;
      html += `<div class="group-title">${group || "未分组"}</div>`;
    }
    for (const s of ch.streams || []) {
      if (s.grade === "4K") counts["4K"]++;
      if (s.grade === "1080P") counts["1080P"]++;
    }
    const best = (ch.streams || []).map((s) => s.grade).join(" / ");
    html += `<div class="chan">
      <span class="name">${ch.name}</span>
      <span class="meta">${(ch.streams || []).map((s) => gradePill(s.grade)).join("")}
        <span>${ch.streams.length} 源</span>
        ${best ? `<span>${best}</span>` : ""}</span>
    </div>`;
  }
  box.innerHTML = html;
  $("statGrades").textContent = `${counts["4K"]} / ${counts["1080P"]}`;
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
