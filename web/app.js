const $ = (id) => document.getElementById(id);
let cache = [];
let cacheKey = "";
let settings = {};
let settingsDirty = false;

// 面板上展示的地址，优先级从高到低：
//   1. 手填的「对外地址」public_url —— 用户明确指定，最权威；
//   2. 服务端算出来的 base —— 服务端会识别 X-Forwarded-Host、并记住它见过的外网
//      域名，所以即便你是用内网 IP 打开面板、外面套了反代，它也能给对外网地址；
//   3. location.origin —— 什么都没配时的兜底（就是你现在打开面板用的这个地址）。
let serverBase = "";

// 设了访问密码时，每条地址都必须带那一段路径（/密码/playlist.m3u）。
// serverBase 是服务端算好的、本身就带密码；需要补的只有手填地址和 location.origin。
function withSecret(url) {
  const secret = String(settings.secret || "").replace(/^\/+|\/+$/g, "");
  if (!url || !secret) return url;
  const tail = "/" + encodeURIComponent(secret);
  return url.endsWith(tail) || url.endsWith("/" + secret) ? url : url + tail;
}

// 面板可能挂在 /密码/ 这种子路径下，写死 /live/xxx 会掉到根目录被密码拦住，
// 所以一律按当前文档地址解析成绝对地址。
function absUrl(path) {
  return new URL(path, document.baseURI).href;
}

function base() {
  const manual = withSecret(String(settings.public_url || "").replace(/\/+$/, ""));
  if (manual) return manual;
  if (serverBase) return serverBase.replace(/\/+$/, "");
  return withSecret(location.origin.replace(/\/+$/, ""));
}

function esc(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// 模块频道清单按分类分组展示。固定顺序在前，后端新加的类别追加在后，
// 各模块（含少儿）共用一套排序，避免某个模块的分类漏显示。
const CAT_ORDER = ["央视", "卫视", "官方少儿", "动画轮播", "其它"];
function catNames(channels) {
  const map = channels && typeof channels === "object" ? channels : {};
  return Object.keys(map)
    .filter((c) => Array.isArray(map[c]) && map[c].length)
    .sort((a, b) => {
      const ia = CAT_ORDER.indexOf(a);
      const ib = CAT_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
}

function fillUrls() {
  // 订阅地址已并入「源数据与订阅地址」页右侧常驻栏，这里只填那一组输入框
  const map = {
    subUrlM3u: "/playlist.m3u",
    subUrlTxt: "/txt",
    subUrlEpg: "/epg.xml",
    subUrlJson: "/json",
  };
  for (const [id, path] of Object.entries(map)) {
    const el = $(id);
    if (!el) continue;
    // 值没变就别赋值：每 5 秒刷一次状态，赋值会把用户正在选中的文字清掉
    const next = base() + path;
    if (el.value !== next) el.value = next;
  }
}

function setDot(state) {
  const dot = $("statusDot");
  dot.className = "dot " + state;
}

// ---------------- 分区切换 ----------------
// 一页一个分区：状态 / 我的频道 / 源数据与订阅地址 / 宽带与省份 / 运行设置 / 运行日志。
// 原来的「订阅地址」单独页已合并进「源数据与订阅地址」页，不再是一个分区。
// 状态卡在 .statbar 里，不参与切换，所以切到哪个分区都还看得见。
// 新加分区必须同步登记到这里，否则 setView 会把名字当无效值退回状态页。
const VIEWS = ["status", "channels", "modules", "region", "settings", "logs"];

function setView(name, syncHash = true) {
  if (!VIEWS.includes(name)) name = "status";
  document.querySelectorAll(".view").forEach((el) => {
    el.hidden = el.dataset.view !== name;
  });
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.classList.toggle("is-on", el.dataset.view === name);
  });
  if (syncHash && location.hash.slice(1) !== name) {
    history.replaceState(null, "", "#" + name);
  }
  window.scrollTo(0, 0);
}

// 顶栏在窄屏会折行，高度不是固定的；吸顶偏移按真实高度算，写死会错位
function syncTopbarHeight() {
  const bar = document.querySelector(".topbar");
  if (!bar) return;
  const h = Math.round(bar.getBoundingClientRect().height);
  if (h > 0) document.documentElement.style.setProperty("--topbar-h", h + "px");
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

// 这条宽带是哪里的。识别不出来就整行不显示 —— 少一行字，比显示「未知」更不烦人。
// 只做提示：检测结果不参与采集，也不会替你去网上找本省的源（各省地址形态不同，
// 猜错的代价是整批超时）。真有本地运营商 IPTV 的，按这句提示自己加一条源即可。
function renderRegion(r) {
  const box = $("regionNote");
  if (!box) return;
  if (!r || !r.ok) {
    box.hidden = true;
    return;
  }
  const where = [r.province_name, r.isp_name].filter(Boolean).join(" · ");
  let text = `本机宽带：${where} · ${r.ipv6 ? "IPv6 可用" : "没有 IPv6 出口"}`;
  if (r.manual) text += "（手动指定）";
  if (r.province === "hubei" && r.isp === "mobile") {
    text += "。这条宽带能用自带的运营商源；频道里要是没有湖北本地台，" +
      "多半是通用版把它跳过了，换成 docker-compose.hubei.yml 即可。";
  } else if (r.province) {
    text += "。会自动按这个省份去取本省的运营商单播清单，只给央视 / 卫视补线路；" +
      "在「宽带与省份」页可以关掉，也能手动指定省份 / 运营商，跑 doctor --isp 能看这路通不通。";
    if (!r.ipv6) text += "没有 IPv6 时部分入口取不到也正常。";
  }
  box.hidden = false;
  box.textContent = text;
}

// 状态页的键值表：把「顶栏四张卡」没覆盖到的信息补齐（宽带识别、开关、模式、间隔）
function fillOverview(d) {
  const rep = d.report || {};
  const reg = d.region || {};
  const s = d.settings || {};
  const put = (id, text) => {
    const el = $(id);
    if (el) el.textContent = text;
  };

  put("ovVersion", "v" + (d.version || "-"));

  if (reg.ok) {
    let where = [reg.province_name, reg.isp_name].filter(Boolean).join(" · ") || "已识别";
    if (reg.manual) where += "（手动指定）";
    where += reg.ipv6 ? " · IPv6 可用" : " · 无 IPv6 出口";
    put("ovRegion", where);
  } else {
    put("ovRegion", "未识别（不影响播放，只是不会自动按省补线路）");
  }

  put("ovAutoRegion", s.auto_region === false ? "关闭" : "开启");
  put("ovPlayMode", s.play_mode === "proxy" ? "经本机转发" : "直连 CDN");

  const iv = Number(s.interval_hours);
  put("ovInterval", !iv ? "关闭（只手动更新）" : iv + " 小时");

  const hiddenCount = (s.hidden || []).length;
  const total = rep.channels_matched;
  const shown = total == null ? null : Math.max(total - hiddenCount, 0);
  put("ovCounts", `${shown == null ? "-" : shown} / ${rep.published == null ? "-" : rep.published}`
    + (hiddenCount ? `（另有 ${hiddenCount} 个已隐藏）` : ""));

  put("ovTime", d.generated_at ? shortTime(d.generated_at) : "-");
  put("ovRunning", d.running ? "正在更新…" : (d.last_error ? "上次更新失败" : "空闲"));
  const runningEl = $("ovRunning");
  if (runningEl) runningEl.title = d.last_error || "";
}

async function loadStatus() {
  try {
    const r = await fetch("api/status", { cache: "no-store" });
    const d = await r.json();
    $("version").textContent = "v" + (d.version || "-");
    const rep = d.report || {};

    settings = d.settings || {};
    // 服务端算出的 base 是「针对这次请求」的：内网直连访问就返回内网地址，
    // 经反代访问就返回外网域名。每次都无条件用它刷新，才能做到「内网看内网、
    // 外网看外网」——两个地方同时用，各自拿到各自能直接播的订阅地址。
    if (d.base) {
      serverBase = d.base;
    }
    fillUrls();

    setDot(d.running ? "busy" : (d.last_error ? "err" : "ok"));
    $("btnUpdate").disabled = !!d.running;
    $("btnUpdate").textContent = d.running ? "更新中…" : "立即更新源";

    const hidden = settings.hidden || [];
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
          `上游正在按 IP 限流，约 ${blocked} 秒内会放慢去要新地址的节奏（避免越要越死）。` +
          "正在播的台会用上一份清单顶住，卡一下会自动接回来；这时候连着切台更容易卡。";
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
    renderRegion(d.region || {});
    fillOverview(d);

    const epgNoteText = epgText(d.epg);
    if ($("subEpgNote")) $("subEpgNote").textContent = epgNoteText;
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
  if ($("setAutoRegion")) $("setAutoRegion").value = s.auto_region === false ? "0" : "1";
  if ($("setRegion")) $("setRegion").value = s.region || "";
  if ($("setIsp")) $("setIsp").value = s.isp || "";
  if ($("setAutoSwitch")) $("setAutoSwitch").value = s.auto_switch === false ? "0" : "1";
  // 缺省视为开：老部署没有这个字段时按推荐值勾上
  if ($("setYspPlain")) $("setYspPlain").checked = s.ysp_plain !== false;
  if ($("setPublicUrl")) $("setPublicUrl").value = s.public_url || "";
  if ($("setInterval")) $("setInterval").value = s.interval_hours != null ? s.interval_hours : "";
  if ($("setTtl")) $("setTtl").value = s.playlist_ttl != null ? s.playlist_ttl : "";
  if ($("setCache")) $("setCache").value = s.cache_mb != null ? s.cache_mb : "";
  if ($("setEpgSource")) $("setEpgSource").value = s.epg_source || "";
  if ($("setSecret")) $("setSecret").value = s.secret || "";
  if ($("setMiguEnabled")) $("setMiguEnabled").value = s.migu_enabled === false ? "0" : "1";
  if ($("setMiguUser")) $("setMiguUser").value = s.migu_user_id || "";
  // 令牌不回传：框里始终留空，留空 = 不修改
  if ($("setMiguToken")) $("setMiguToken").value = "";
  if ($("setMiguRate")) $("setMiguRate").value = String(s.migu_rate || 3);
  if ($("setMiguH265")) $("setMiguH265").checked = s.migu_h265 !== false;
  if ($("setMiguEdgeDispatch")) $("setMiguEdgeDispatch").checked = s.migu_edge_dispatch !== false;
  if ($("setMiguDispatch")) $("setMiguDispatch").checked = s.migu_client_dispatch !== false;
  if ($("setMiguRefresh")) $("setMiguRefresh").value = s.migu_refresh != null ? s.migu_refresh : 360;
  if ($("miguStatus") && s.migu_account_set !== undefined) {
    $("miguStatus").hidden = false;
    $("miguStatus").textContent = s.migu_account_set
      ? "已绑定咪咕账号：换票会按账号权益给画质。令牌不再显示，留空表示不修改。"
      : "当前为游客模式：最高标清 540P。要提画质请在上面填账号 ID 和令牌。";
  }
  // 源数据模块：四个内置大类置顶（点开看说明/配置），其余源收进「更多源」。
  // 清单由后端从 sources.toml + 注入源实时生成，前端不写死模块名字。
  const catalog = s.modules || {};
  const builtins = Array.isArray(catalog.builtins) ? catalog.builtins : [];
  const others = Array.isArray(catalog.others) ? catalog.others : [];
  if ($("builtinModules")) {
    $("builtinModules").innerHTML = builtins.map((m) => {
      const name = String(m.name || "");
      const members = Array.isArray(m.members) ? m.members : [];
      const types = Array.isArray(m.types) ? m.types : [];
      const channels = m.channels && typeof m.channels === "object" ? m.channels : {};
      const count = Number(m.count) || 0;
      const collected = Number(m.collected) || 0;
      const statusCls = m.enabled ? "on" : "off";
      // count = 当前列表里真有的频道；collected = 历史采集条数。
      // 开着却一条都没进列表时，别拿历史数字冒充"抓到 N 个"，要明确催一次更新。
      let statusTxt;
      if (!m.enabled) {
        statusTxt = `○ 已关闭 · 上次抓到 ${collected || count} 个频道`;
      } else if (count > 0) {
        statusTxt = `● 正常 · 抓到 ${count} 个频道`;
      } else if (collected > 0) {
        statusTxt = `● 已开启 · 上次抓到 ${collected} 条 · 尚未进入列表，点「更新此源」`;
      } else {
        statusTxt = "● 已开启 · 还没抓到频道，点「更新此源」";
      }
      // 分类频道名标签：央视 / 卫视 / 其它，每类显示前若干个名
      const cats = catNames(channels);
      const chips = cats.map((c) => {
        const names = channels[c];
        const preview = names.slice(0, 4).map(esc).join("、");
        const more = names.length > 4 ? ` 等 ${names.length} 个` : `（${names.length}）`;
        return `<span class="builtin-chip" title="${esc(names.join("、"))}">${esc(c)} ${esc(preview)}${esc(more)}</span>`;
      }).join("");
      const detail = members.length
        ? `包含底层源：${members.map(esc).join("、")}`
        : "当前配置里没有这一组的底层源，打开后下一轮才会采集。";
      // 关闭的模块不给「更新此源」：开关关着时订阅也会把它过滤掉，更新了看不到
      const updateBtn = m.enabled
        ? `<button type="button" class="builtin-update" data-update="${esc(name)}">更新此源</button>`
        : `<button type="button" class="builtin-update" disabled title="先打开开关再更新">更新此源</button>`;
      return `<div class="builtin-module" data-module="${esc(name)}">`
        + `<div class="builtin-head">`
        + `<label class="builtin-toggle">`
        + `<input type="checkbox" class="module-switch"${m.enabled ? " checked" : ""}>`
        + `</label>`
        + `<button type="button" class="builtin-name">${esc(name)}</button>`
        + `<span class="module-type">${types.map(esc).join(" / ")}</span>`
        + `</div>`
        + `<p class="builtin-status ${statusCls}">${esc(statusTxt)}</p>`
        + (chips ? `<div class="builtin-chips">${chips}</div>` : `<p class="hint">${detail}</p>`)
        + `<div class="builtin-actions">`
        + updateBtn
        + `<button type="button" class="builtin-config-btn" data-config="${esc(name)}">配置 ›</button>`
        + `</div>`
        + `</div>`;
    }).join("");
    // 少儿频道是独立采集模块：自己抓 CCTV-14 / 咪咕少儿 / 聚合列表少儿台，
    // 不依赖其它模块开关。它走独立的 kids_enabled 开关，仍由前端单独渲染卡片；
    // 开启后内容作为「少儿频道」分组出现在上面那一条 M3U 订阅里。
    const kidsOn = s.kids_enabled !== false;
    const kidsCartoon = s.kids_cartoons !== false;
    const kids = (catalog.kids && typeof catalog.kids === "object") ? catalog.kids : {};
    const kidsCh = kids.channels && typeof kids.channels === "object" ? kids.channels : {};
    const kidsCount = Number(kids.count) || 0;
    const kidsCollected = Number(kids.collected) || 0;
    // 和各模块一样，按分类列出「抓到了哪些台」，家长一眼能看到具体频道名
    const kidCats = catNames(kidsCh);
    const kidChips = kidCats.map((c) => {
      const names = kidsCh[c];
      const preview = names.slice(0, 4).map(esc).join("、");
      const more = names.length > 4 ? ` 等 ${names.length} 个` : `（${names.length}）`;
      return `<span class="builtin-chip" title="${esc(names.join("、"))}">${esc(c)} ${esc(preview)}${esc(more)}</span>`;
    }).join("");
    let kidStatus;
    if (!kidsOn) {
      kidStatus = `○ 已关闭 · 上次抓到 ${kidsCollected || kidsCount} 个频道`;
    } else if (kidsCount > 0) {
      kidStatus = `● 正常 · 抓到 ${kidsCount} 个频道 · 主订阅含「少儿频道」分组`;
    } else if (kidsCollected > 0) {
      kidStatus = `● 已开启 · 上次抓到 ${kidsCollected} 条 · 尚未进入列表，点「更新此源」`;
    } else {
      kidStatus = "● 已开启 · 还没抓到频道，点「更新此源」";
    }
    $("builtinModules").insertAdjacentHTML("beforeend",
      `<div class="builtin-module" data-module="少儿频道">`
      + `<div class="builtin-head">`
      + `<label class="builtin-toggle"><input type="checkbox" id="kidsEnabled"${kidsOn ? " checked" : ""}></label>`
      + `<span class="builtin-name">少儿频道</span>`
      + `<span class="module-type">独立少儿源</span>`
      + `</div>`
      + `<p class="builtin-status ${kidsOn ? "on" : "off"}">${esc(kidStatus)}</p>`
      + (kidChips ? `<div class="builtin-chips">${kidChips}</div>` : "")
      + `<div class="kids-cartoon-row"${kidsOn ? "" : " hidden"}>`
      + `<label class="chk"><input type="checkbox" id="kidsCartoons"${kidsCartoon ? " checked" : ""}>`
      + `包含动画轮播（海绵宝宝等，内容由上游决定）</label></div>`
      + `<div class="builtin-actions">`
      + `<button type="button" class="builtin-update" data-update="少儿频道"${kidsOn ? "" : ` disabled title="先打开开关再更新"`}>更新此源</button>`
      + `<button type="button" class="builtin-config-btn" data-config="少儿频道">配置 ›</button>`
      + `</div>`
      + `</div>`);
  }
  if ($("moduleList")) {
    $("moduleList").innerHTML = others.map((m) => {
      const name = String(m.name || "");
      return `<label class="module-row" data-module="${esc(name)}">`
        + `<input type="checkbox" class="module-switch"${m.enabled ? " checked" : ""}>`
        + `<span class="module-name">${esc(name)}</span>`
        + `<span class="module-type">${esc(String(m.type || ""))}</span>`
        + "</label>";
    }).join("") || '<p class="hint">没有其它源。</p>';
  }
  if ($("moduleStatus")) {
    $("moduleStatus").hidden = false;
    const all = builtins.concat(others);
    const on = all.filter((m) => m.enabled).length + ($("kidsEnabled")?.checked ? 1 : 0);
    $("moduleStatus").textContent = `共 ${all.length + 1} 个模块，已启用 ${on} 个。`;
  }
  // 咪咕启用以内置卡片开关为准，回填到配置块里的下拉，两者保持同步
  const miguBuiltin = builtins.find((m) => m.name === "咪咕视频");
  if ($("setMiguEnabled") && miguBuiltin) {
    $("setMiguEnabled").value = miguBuiltin.enabled ? "1" : "0";
  }
}

// 模块「配置 ›」弹窗：每个模块单独看它抓到的频道和自己的配置。
// 只有咪咕有账号/画质表单（沿用现有 miguConfig 节点，不新建 id）；央视频与
// 其它模块展示说明文字。后面要给某模块加配置项，在这里按名字加一支即可。
const MODULE_INFO = {
  "咪咕视频": "央视 / 卫视 / 体育等频道，播放时现换一张有效地址。账号与画质在下面单独保存。",
  "央视频": "央视与卫视的官方公开频道，匿名取流。会员频道登录暂未内置。",
  "第三方聚合源": "多条公开转播线路聚合，按 latency 自动选最快的。没有单独账号配置。",
  "运营商官方源": "本省运营商的官方单播/组播源，按省份和运营商自动匹配。省份在「宽带与省份」页设置。",
  "少儿频道": "独立采集 CCTV-14、咪咕少儿分类、聚合列表里的少儿台与动画轮播，不依赖其它模块开关。播放在下面选地址族。",
};

let currentModuleName = "";

function openModuleModal(name) {
  const modal = $("moduleModal");
  if (!modal) return;
  currentModuleName = name;
  const mods = (settings && settings.modules) || {};
  // 少儿频道不在 builtins 里，频道清单单独放在 modules.kids
  const builtin = name === "少儿频道"
    ? (mods.kids || {})
    : ((mods.builtins || []).find((m) => m.name === name) || {});
  const channels = builtin.channels || {};
  if ($("moduleModalTitle")) $("moduleModalTitle").textContent = name + " · 配置";

  // 地址族三态回填；运营商官方源的下发地址是 IPv6 边缘，隐藏「仅 IPv4」
  const modes = (settings && settings.module_ip_mode) || {};
  const mode = modes[name] || "auto";
  const v4Input = $("setIpModeV4");
  if (v4Input) v4Input.closest("label").hidden = name === "运营商官方源";
  const radios = document.querySelectorAll('input[name="ipMode"]');
  radios.forEach((el) => { el.checked = el.value === (mode === "ipv4" && name === "运营商官方源" ? "auto" : mode); });
  if ($("ipModeNote")) $("ipModeNote").hidden = true;
  // 央视频专有说明：地址族只管 NAS 取清单走哪一族，跟盒子自己的网络无关。
  // 出口 IPv4 被央视频单独限流时（实测：v4 取清单一律 403、v6 正常），
  // 选「仅 IPv4」会一直拿不到新清单，表现就是播一会儿报「源服务器返回错误」。
  if ($("ipModeExtra")) {
    const extra = name === "央视频";
    $("ipModeExtra").hidden = !extra;
    if (extra) {
      $("ipModeExtra").textContent = "这一项只决定 NAS 去央视频取清单时走哪一族，跟盒子自己的网络无关——盒子只有 IPv4 也可以选「自适应」。"
        + "实测：出口 IPv4 被央视频单独限流时（清单请求 v4 一律 403、v6 正常），选「仅 IPv4」会一直拿不到新清单，"
        + "画面播一会儿就报「源服务器返回错误」；换成「自适应」会改用 IPv6 取清单，分片仍由盒子自己直连，不受影响。";
    }
  }
  // 抓到的频道：按分类列频道名
  const box = $("moduleChannels");
  if (box) {
    const cats = catNames(channels);
    const total = Number(builtin.count) || 0;
    const collected = Number(builtin.collected) || 0;
    if (!cats.length) {
      // 列表里没有台时，历史采集数只做说明，不能让人以为这些台现在能播
      const hint = collected > 0
        ? `上次采集到 ${collected} 条，但当前列表里还没有它的频道。回到卡片点「更新此源」采集一次，这里就会列出来。`
        : `还没有抓到频道。回到卡片点「更新此源」采集一次，这里就会列出来。`;
      box.innerHTML = `<p class="chan-empty">${esc(hint)}</p>`;
    } else {
      box.innerHTML = `<p class="chan-empty">抓到的频道 · ${total} 个</p>` + cats.map((c) => {
        const names = channels[c];
        return `<div class="chan-group">`
          + `<div class="chan-group-name">${esc(c)}（${names.length}）</div>`
          + `<div class="chan-chips">${names.map((n) => `<span class="chan-chip" title="${esc(n)}">${esc(n)}</span>`).join("")}</div>`
          + `</div>`;
      }).join("");
    }
  }
  // 配置区：咪咕显示账号表单，央视频显示播放兼容，其它模块显示说明
  const miguCfg = $("miguConfig");
  const other = $("moduleOtherConfig");
  const isMigu = name === "咪咕视频";
  const isYsp = name === "央视频";
  if (miguCfg) miguCfg.hidden = !isMigu;
  if ($("yspConfig")) $("yspConfig").hidden = !isYsp;
  if ($("yspNote")) $("yspNote").hidden = true;
  if (other) {
    other.hidden = isMigu;
    if (!isMigu) other.textContent = MODULE_INFO[name] || "这个模块没有单独配置项，开关即可控制是否参与。";
  }
  modal.hidden = false;
}

function closeModuleModal() {
  const modal = $("moduleModal");
  if (modal) modal.hidden = true;
}

// 两个分区各有一颗「保存」，都提交同一份设置（表单控件在 DOM 里始终存在，
// 只是当前页看不到）；按钮和结果提示按传入的 id 区分，不写死。
async function saveSettings(btnId = "btnSaveSettings", noteId = "settingsNote") {
  const btn = $(btnId);
  const note = $(noteId);
  let payload = {
    play_mode: $("setPlayMode") ? $("setPlayMode").value : "direct",
    auto_region: $("setAutoRegion") ? $("setAutoRegion").value === "1" : true,
    region: $("setRegion") ? $("setRegion").value : "",
    isp: $("setIsp") ? $("setIsp").value : "",
    auto_switch: $("setAutoSwitch") ? $("setAutoSwitch").value === "1" : true,
    // 央视频分片降 http：默认开，只有明确取消勾选才关
    ysp_plain: $("setYspPlain") ? $("setYspPlain").checked : true,
    public_url: $("setPublicUrl").value.trim(),
    interval_hours: Number($("setInterval").value || 0),
    playlist_ttl: Number($("setTtl").value || 1),
    cache_mb: Number($("setCache").value || 96),
    epg_source: $("setEpgSource").value.trim(),
    // 空字符串是有意义的：表示把密码关掉
    secret: $("setSecret") ? $("setSecret").value.trim() : "",
  };
  // 央视频模块弹窗只提交它自己那一项：别的表单控件虽在 DOM 里，用户在这一页
  // 没动过，一起带上等于把别处还没保存的输入顺带写进去。
  if (btnId === "btnYspSave") {
    payload = { ysp_plain: $("setYspPlain") ? $("setYspPlain").checked : true };
  }
  // 模块开关只在「源数据与订阅地址」页保存时提交，别的页不碰它。
  // 内置卡片(.builtin-toggle)和更多源(.module-row)的开关都带 data-module，
  // 统一往上找这个属性，不写死某一层容器。
  if (btnId === "btnModuleSave") {
    payload.disabled_modules = Array.from(document.querySelectorAll(".module-switch"))
      .filter((el) => !el.checked)
      .map((el) => el.closest("[data-module]")?.dataset.module)
      .filter(Boolean);
    // 咪咕的启用以卡片开关为准，同时同步给配置块的下拉与后端 migu_enabled
    const miguSwitch = document.querySelector('.builtin-module[data-module="咪咕视频"] .module-switch');
    if (miguSwitch) {
      payload.migu_enabled = miguSwitch.checked;
      if ($("setMiguEnabled")) $("setMiguEnabled").value = miguSwitch.checked ? "1" : "0";
    }
    // 少儿频道独立开关（不混进 disabled_modules——它没有采集源）
    if ($("kidsEnabled")) {
      payload.kids_enabled = $("kidsEnabled").checked;
      payload.kids_cartoons = $("kidsCartoons") ? $("kidsCartoons").checked : true;
    }
  }
  // 地址族三态：只提交当前弹窗模块的这一项，其它表单控件一概不碰。
  if (btnId === "btnIpModeSave") {
    const picked = document.querySelector('input[name="ipMode"]:checked');
    const value = picked ? picked.value : "auto";
    payload.module_ip_mode = { [currentModuleName]: value };
  }
  // 咪咕账号/画质只在咪咕配置块自己的保存按钮提交。别的页共用一个 saveSettings，
  // 它们的表单控件虽在 DOM 里、但用户没填过，误提交会把账号清空。
  if (btnId === "btnMiguSave") {
    payload.migu_enabled = $("setMiguEnabled").value === "1";
    payload.migu_user_id = $("setMiguUser").value.trim();
    payload.migu_rate = Number($("setMiguRate").value);
    payload.migu_h265 = $("setMiguH265").checked;
    payload.migu_edge_dispatch = $("setMiguEdgeDispatch").checked;
    payload.migu_client_dispatch = $("setMiguDispatch").checked;
    payload.migu_refresh = Number($("setMiguRefresh").value || 360);
    // 令牌留空 = 不修改（后端不回传，前端框每次都是空的）
    const miguToken = $("setMiguToken").value.trim();
    if (miguToken) payload.migu_token = miguToken;
  }
  btn.disabled = true;
  try {
    const r = await fetch("api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    settingsDirty = false;
    // 密码一变，当前这个地址就失效了：要么跳到新地址，要么回首页。
    // 不跳的话，接下来的轮询全部 401，页面会显示「无法连接服务」。
    const before = String(settings.secret || "");
    const after = String((d.settings && d.settings.secret) || "");
    if (before !== after) {
      const home = after ? `${location.origin}/${encodeURIComponent(after)}/` : `${location.origin}/`;
      note.textContent = after
        ? "密码已改，正在跳到新地址…"
        : "已关闭访问密码，正在返回首页…";
      setTimeout(() => { location.href = home; }, 500);
      return;
    }
    if (btnId === "btnModuleSave") {
      const st = d.settings || {};
      const cat = st.modules || {};
      const mods = (cat.builtins || []).concat(cat.others || []);
      const on = mods.filter((m) => m.enabled).map((m) => m.name);
      const off = mods.filter((m) => !m.enabled).map((m) => m.name);
      note.textContent = `已保存：启用 ${on.length} 个模块（${on.join("、") || "无"}），`
        + `关闭 ${off.length} 个（${off.join("、") || "无"}）。`
        + "订阅链接立即按这个组合输出；关掉的模块下一轮也不再采集。";
      fillSettings(st);
    } else if (btnId === "btnIpModeSave") {
      const st = d.settings || {};
      const value = ((st.module_ip_mode || {})[currentModuleName]) || "auto";
      const label = { auto: "自适应", ipv4: "仅 IPv4", ipv6: "仅 IPv6" }[value] || "自适应";
      note.hidden = false;
      note.textContent = `已保存：${currentModuleName} 的网络地址族为「${label}」。下一次点播这个模块的频道时生效。`;
      fillSettings(st);
    } else if (btnId === "btnYspSave") {
      const st = d.settings || {};
      const on = st.ysp_plain !== false;
      const yn = $("yspNote");
      if (yn) {
        yn.hidden = false;
        yn.textContent = on
          ? "已保存：央视频分片按 http 下发，盒子不用做 TLS 握手。下次点播生效。"
          : "已保存：央视频分片改回 https 下发（面板是 https 访问时本来也不会降级）。下次点播生效。";
      }
      fillSettings(st);
    } else if (btnId === "btnMiguSave") {
      const st = d.settings || {};
      $("miguNote").textContent = `已保存：咪咕${st.migu_enabled === false ? "已关闭" : "已启用"}、`
        + `画质 ${({2: "标清 540p", 3: "高清 720p", 4: "蓝光 1080p", 7: "原画", 9: "4K"})[st.migu_rate] || "标清"}、`
        + `H.265 ${st.migu_h265 === false ? "关" : "开"}、`
        + `客户端就近取流 ${st.migu_client_dispatch === false ? "关" : "开"}。`
        + (st.migu_account_set ? "账号已绑定。" : "当前游客模式。")
        + "画质与开关立即生效；频道有变动时点一次「立即更新源」。";
      fillSettings(st);
    } else if (btnId === "btnRegionSave") {
      // 宽带与省份页：只报这一页关心的三样，别把间隔/缓存也塞进来
      const picked = (id) => {
        const el = $(id);
        return el && el.selectedOptions && el.selectedOptions[0] ? el.selectedOptions[0].textContent : "自动识别";
      };
      note.textContent = `已保存：自动按省取源${d.settings.auto_region === false ? "关闭" : "开启"}，`
        + `省份 ${picked("setRegion")}、运营商 ${picked("setIsp")}、`
        + `自动换源${d.settings.auto_switch === false ? "关闭（画面固定第一条线路）" : "开启"}。`
        + (d.settings.auto_switch === false
          ? "自动换源已关，正在播的台不会再跳线路，重新拉一次播放地址即可生效。"
          : "")
        + (d.settings.auto_region === false
          ? "开关是关的，这一页的选择暂时不生效。"
          : "下一轮更新按这个组合取源，想立刻看效果就点「立即更新源」。");
    } else {
      note.textContent = `已保存：更新间隔 ${d.settings.interval_hours} 小时、`
        + `清单刷新 ${d.settings.playlist_ttl} 秒、分片缓存 ${d.settings.cache_mb} MB、`
        + `自动按省取源${d.settings.auto_region === false ? "关闭" : "开启"}。`
        + "播放器里的地址如果变了，重新拉一次列表即可。";
    }
    await refresh();
  } catch (e) {
    note.textContent = "保存失败：" + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function loadRegionOptions() {
  try {
    const r = await fetch("api/region/options", { cache: "no-store" });
    const d = await r.json();
    const prov = $("setRegion");
    const isp = $("setIsp");
    if (prov) {
      for (const p of d.provinces || []) {
        const o = document.createElement("option");
        o.value = p.code;
        o.textContent = p.name;
        prov.appendChild(o);
      }
    }
    if (isp) {
      for (const i of d.isps || []) {
        const o = document.createElement("option");
        o.value = i.code;
        o.textContent = i.name;
        isp.appendChild(o);
      }
    }
  } catch (_) { /* 拿不到选项就只保留「自动识别」 */ }
}

async function probeRegion() {
  const note = $("regionProbeNote");
  const btn = $("btnProbe");
  const province = $("setRegion") ? $("setRegion").value : "";
  const isp = $("setIsp") ? $("setIsp").value : "";
  if (!province || !isp) {
    if (note) { note.hidden = false; note.textContent = "先选好省份和运营商，再检测；只选一个不够。"; }
    return;
  }
  if (btn) btn.disabled = true;
  if (note) { note.hidden = false; note.textContent = "检测中，最多几秒…"; }
  try {
    const r = await fetch("api/region/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ province, isp }),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    if (note) {
      const lines = [`检测结果：${d.note || ""}`];
      if (Array.isArray(d.verdict)) lines.push(...d.verdict);
      note.style.whiteSpace = "pre-line";
      note.textContent = lines.join("\n");
    }
  } catch (e) {
    if (note) note.textContent = "检测失败：" + e.message;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function scanProvince() {
  const note = $("regionProbeNote");
  const btn = $("btnScan");
  const province = $("setRegion") ? $("setRegion").value : "";
  if (!province) {
    if (note) { note.hidden = false; note.textContent = "先选一个省份，再点扫描。"; }
    return;
  }
  if (btn) btn.disabled = true;
  if (note) { note.hidden = false; note.textContent = "扫描中，4 家并发拉取公开清单…"; }
  try {
    const r = await fetch("api/region/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ province }),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    const rows = Object.values(d.isps || {}).map((x) => {
      const state = x.state === "ok"
        ? `有清单，约 ${x.entries} 条公网入口`
        : x.state === "missing"
          ? "没有这一份"
          : "取不到（网络问题，稍后重试）";
      return `${x.name}：${state}`;
    });
    if (note) {
      note.style.whiteSpace = "pre-line";
      note.textContent = `本省（${d.province}）公开清单扫描：\n` + rows.join("\n")
        + "\n\n有清单不等于能播，选好组合再点「检测这个组合」看通不通。";
    }
  } catch (e) {
    if (note) note.textContent = "扫描失败：" + e.message;
  } finally {
    if (btn) btn.disabled = false;
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

// 测出来的数字是「本机从这台下载能跑多快」，不是视频码率，鼠标悬停写清楚
function rateTitle(stream) {
  const parts = [];
  if (stream.bandwidth_kbps) parts.push(`码率 ${(stream.bandwidth_kbps / 1000).toFixed(1)} Mbps（分片大小 ÷ 分片时长）`);
  if (stream.speed_mbps) parts.push(`采样下载 ${stream.speed_mbps.toFixed(1)} Mbps（NAS 到 CDN 的下载能力）`);
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

// 当前看的是哪个分组。存进地址栏的 # 里，刷新、收藏、发给家人都能停在同一个分组。
let activeGroup = decodeURIComponent((location.hash || "").replace(/^#/, "")) || "*";

function pickGroup(name) {
  activeGroup = name || "*";
  try {
    const url = activeGroup === "*"
      ? location.pathname + location.search
      : "#" + encodeURIComponent(activeGroup);
    history.replaceState(null, "", url);
  } catch (e) {
    /* 某些内嵌浏览器不给改地址栏，不影响使用 */
  }
  renderChannels(cache);
}

function groupLabel(name) {
  const map = { CCTV: "央视", cctv: "央视", 央视: "央视", 卫视: "卫视" };
  return map[name] || name || "未分组";
}

// 卡片里只放最要紧的两样：画质和码率。分辨率这些挤进来会把窗格撑破。
function cardMeta(stream) {
  if (!stream || !Object.keys(stream).length) return '<span class="dim">没有可用源</span>';
  const rate = rateText(stream);
  // 码率是这条流本身多"重"，不是好坏；只有测速（本机拉得动拉不动）才需要标红标绿
  const cls = stream.bandwidth_kbps ? "" : rateClass(stream.speed_mbps);
  return `<span class="pill ${gradePillClass(stream.grade)}">${esc(stream.grade || "未知")}</span>`
    + `<span class="rate ${cls}" title="${esc(rateTitle(stream))}">${esc(rate)}</span>`;
}

// 浏览器（Chrome/Edge 的 MSE）解不了 H.265，官方源的 1080P 恰恰多是
// H.265——直接播就是 "分片解不出来"。这里优先挑 H.264 那条给面板用，
// 只有整台都是 H.265 时才退回第一条（Safari 之类能解，其余会给出提示）。
function pickPlayable(streams) {
  const usable = streams.filter((s) => s.slug);
  const h264 = usable.find((s) => {
    const c = String(s.codec || "").toLowerCase();
    return c && !c.includes("hvc") && !c.includes("265") && !c.includes("hev");
  });
  return h264 || usable[0] || null;
}

function chanCard(ch) {
  const visible = !ch.hidden;
  const streams = ch.streams || [];
  const pick = pickPlayable(streams);
  const slug = (pick && pick.slug) || "";
  const onlyHevc = Boolean(pick && /hvc|265|hev/i.test(String(pick.codec || "")));
  const more = streams.length > 1 ? `<span class="idx">${streams.length} 路</span>` : "";
  const play = visible
    ? (slug
        ? `<button class="btn mini" data-play="${esc(slug)}" data-name="${esc(ch.name)}"${onlyHevc ? ' title="这台只有 H.265，浏览器多半解不了，建议用外部播放器"' : ""}>播放</button>`
        : `<button class="btn mini" disabled title="这条流来自外部订阅，面板播放需要本机 /live/ 地址">播放</button>`)
    : `<button class="btn mini" data-unhide="${esc(ch.name)}">恢复</button>`;
  const hide = visible
    ? `<button class="btn mini ghost" data-hide="${esc(ch.name)}">隐藏</button>`
    : "";
  return `<article class="chan-card${visible ? "" : " is-hidden"}">
    <div class="chan-top">
      <span class="chan-dot"></span>
      <span class="chan-name" title="${esc(ch.name)}">${esc(ch.name)}</span>
      ${visible ? more : '<span class="pill hidden-pill">已隐藏</span>'}
    </div>
    <div class="chan-meta">${visible ? cardMeta(streams[0]) : '<span class="dim">不上架，随时可恢复</span>'}</div>
    <div class="chan-ops">${play}${hide}</div>
  </article>`;
}

function renderChannels(channels) {
  const box = $("channelList");
  const all = channels || [];
  const keyword = ($("search").value || "").trim().toLowerCase();
  const showHidden = $("showHidden") && $("showHidden").checked;
  const hiddenCount = all.filter((c) => c.hidden).length;

  const badge = $("hiddenCount");
  if (badge) {
    badge.hidden = !hiddenCount;
    badge.textContent = hiddenCount ? `已隐藏 ${hiddenCount}` : "";
  }
  if ($("btnUnhideAll")) $("btnUnhideAll").hidden = !hiddenCount;

  if (!all.length) {
    box.innerHTML = `<p class="empty">暂无数据，点状态区右上角「立即更新源」跑一次。</p>`;
    return;
  }

  // 搜索时在整个列表里找：不然还得先想清楚这个台在哪个分组
  const pool = all.filter((c) => !keyword
    || c.name.toLowerCase().includes(keyword)
    || (c.group || "").toLowerCase().includes(keyword));

  const groups = [];
  const byGroup = new Map();
  for (const ch of pool) {
    const key = ch.group || "";
    if (!byGroup.has(key)) {
      byGroup.set(key, []);
      groups.push(key);
    }
    byGroup.get(key).push(ch);
  }

  let current = keyword ? "*" : activeGroup;
  if (current !== "*" && !byGroup.has(current)) {
    activeGroup = "*";
    current = "*";
  }
  const picked = (current === "*" ? pool : byGroup.get(current) || [])
    .filter((c) => showHidden || !c.hidden || !!keyword);

  if (!pool.length) {
    box.innerHTML = `<p class="empty">没有匹配的频道</p>`;
    return;
  }

  const liveIn = (list) => list.filter((c) => !c.hidden).length;
  const side = [`<button class="group-item${current === "*" ? " is-on" : ""}" data-group="*">
      <span>全部</span><span class="num">${liveIn(pool)}</span></button>`]
    .concat(groups.map((g) => {
      const items = byGroup.get(g);
      return `<button class="group-item${current === g ? " is-on" : ""}" data-group="${esc(g)}">
        <span>${esc(groupLabel(g))}</span><span class="num">${liveIn(items)}</span></button>`;
    }))
    .join("");

  const items = current === "*" ? [] : byGroup.get(current) || [];
  const canHide = items.some((c) => !c.hidden);
  const canShow = items.some((c) => c.hidden);
  const title = current === "*"
    ? (keyword ? `搜索「${esc($("search").value.trim())}」` : "全部频道")
    : groupLabel(current);
  const tools = current === "*" ? "" : `
    ${canHide ? `<button class="btn mini ghost" data-hide-group="${esc(current)}">隐藏本组</button>` : ""}
    ${canShow ? `<button class="btn mini ghost" data-unhide-group="${esc(current)}">恢复本组</button>` : ""}`;

  box.innerHTML = `<aside class="chan-side">${side}</aside>
    <div class="chan-main">
      <div class="chan-head">
        <h3>${title}<span class="num">${picked.length}</span></h3>
        <div class="chan-tools">${tools}</div>
      </div>
      <div class="chan-grid">${picked.map(chanCard).join("")}</div>
    </div>`;

  const counts = {};
  for (const ch of all) {
    if (ch.hidden) continue;
    for (const s of ch.streams || []) {
      const key = s.grade || "未知";
      counts[key] = (counts[key] || 0) + 1;
    }
  }
  $("statGrades").textContent = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .map(([grade, n]) => `${grade}×${n}`)
    .join("  ") || "-";
  // 顶部的「直播流」跟着隐藏一起减，否则隐藏完数字不动，看着像没生效
  if ($("statStreams")) {
    const streams = all.filter((c) => !c.hidden).reduce((n, c) => n + (c.streams || []).length, 0);
    $("statStreams").textContent = `${streams}`;
  }
}

async function loadChannels() {
  try {
    const r = await fetch("api/channels", { cache: "no-store" });
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
    const r = await fetch("api/hidden", {
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

function unhideGroup(group) {
  const names = cache
    .filter((c) => (c.group || "") === group && c.hidden)
    .map((c) => c.name);
  setHidden(names, false);
}

async function triggerUpdate() {
  $("btnUpdate").disabled = true;
  try {
    const r = await fetch("api/update", { method: "POST" });
    const d = await r.json();
    $("log").textContent = d.message || "";
  } catch (e) {
    $("log").textContent = "触发失败：" + e;
  }
  setTimeout(refresh, 600);
}

// 模块卡片「更新此源」：一步到位。
// 先把当前所有开关（用户刚打开/关掉的）保存下去，再带模块名触发增量更新——
// 这样新打开的模块此刻是 enabled 状态，后端才采得到；顺序反了会漏采。
// 分开两个请求而不是塞进一个接口，是为了复用已有的设置保存逻辑（含密码校验）。
async function updateOneModule(name) {
  // 专用提示条：moduleStatus 会被 5 秒轮询的 fillSettings 改写，进度/结果放这里才留得住
  const note = $("moduleUpdateNote");
  const setNote = (text, cls) => {
    if (!note) return;
    note.hidden = false;
    note.textContent = text;
    note.className = "hint result" + (cls ? " " + cls : "");
  };
  // 找到这张卡片上的按钮并临时禁用，避免连点触发两次
  const card = document.querySelector(`.builtin-module[data-module="${CSS.escape(name)}"]`);
  const btn = card ? card.querySelector("[data-update]") : null;
  if (btn) btn.disabled = true;
  try {
    await saveSettings("btnModuleSave", "moduleNote");
    setNote(`已保存开关，正在只更新「${name}」…`);
    const r = await fetch("api/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ module: name }),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    setNote(
      d.started
        ? `已开始只更新「${name}」，其它源保持不动。完成后订阅会包含所有已启用的源。`
        : (d.message || "已有任务在执行，稍后再试。")
    );
  } catch (e) {
    setNote("更新失败：" + e.message, "err");
  } finally {
    // 更新是异步的，稍等再拉一次状态；之后 5 秒轮询会继续跟进进度
    setTimeout(refresh, 600);
  }
}

async function checkUpdate() {
  const btn = $("btnCheckUpdate");
  const out = $("updateResult");
  btn.disabled = true;
  out.className = "update-result";
  out.textContent = "检查中…";
  try {
    const r = await fetch("api/version?check=1", { cache: "no-store" });
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
// HLS 直播天生滞后：服务端把上游那 3 片拼成 6 片窗口（约 30 秒），播放器从
// 末尾往前 3 片起步（约 15 秒），所以进度条上的 0:17 是「窗口里的位置」，
// 不是延迟在变大。这里显示真实的「距直播最新位置还差几秒」，落后太多会自动
// 加速追回去。**liveSyncDurationCount 必须小于窗口片数**，否则播放器退回窗口
// 开头、缓冲只剩零点几秒，播到尾巴必停。

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
// 上一次整条重开的时刻。成功往前播满一分钟就把 restarts 清零——否则一场
// 直播断几次就把 8 次额度用完，之后再也不救，用户看到的就是「卡住不动」。
let restartAt = 0;
// 卡住时先原地重载清单（保住缓冲）的次数，画面一往前走就清零
let softReloads = 0;

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
    softReloads = 0;
    // 重开之后能顺畅播满一分钟，说明刚才那次是偶发，把额度还回去。
    if (restartAt && now - restartAt > 60000) {
      restarts = 0;
      restartAt = 0;
    }
  }
  const stuckMs = lastProgressAt ? now - lastProgressAt : 0;
  if (!v.paused && stuckMs > 3000) {
    parts.push(`画面停住 ${Math.floor(stuckMs / 1000)}s`);
  }
  $("playerStats").textContent = parts.join(" · ") || "连接中…";

  if (!v.paused && stuckMs > 3000 && stuckMs < 50000) {
    $("playerTip").textContent = stuckMs > 6000
      ? "上游一直没给新分片，正在换票重连…"
      : "等上游给下一段——上游清单 5 秒才滚一片，稍等一下。";
  }
  // 卡住先「原地重载清单」，别急着销毁播放器：服务端刚换完票的几秒里，
  // 手里那几秒缓冲是唯一的救命稻草，一销毁就全丢了。连续多次救不回来才重开。
  //
  // 阈值按**播放器实际缓冲**来（内置播放器实测 8 秒左右）：6 秒先试着重载
  // 清单，10 秒还没动静就整条重开。过去要到 24 秒才重开，而服务端的退避上限
  // 一旦超过缓冲，画面早就耗干了——用户看到的就是「转一下圈然后彻底不动」。
  if (!v.paused && stuckMs > 10000 && restarts < 8) {
    softReloads = 0;
    restartPlayer("画面停住了");
  } else if (!v.paused && stuckMs > 6000 && softReloads < 3) {
    softReloads += 1;
    if (hls) {
      try { hls.startLoad(); } catch (_) { /* ignore */ }
    }
  }

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
  softReloads = 0;
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
      "直播流本身就比电视台晚 2~3 个分片（这台约 15 秒）。进度条上的 0:17 是「当前可播窗口里的位置」，不是延迟在变大。";
  }
  stopStats();
  statsTimer = setInterval(playerStats, 1000);

  const url = absUrl(`live/${encodeURIComponent(slug)}.m3u8`);

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
    // 服务端的窗口是 6 片（约 30 秒），播放器从这里往后数 3 片起步，等于落后
    // 直播约 15 秒、手里还攥着 15 秒余量。**这个数必须小于窗口片数**：以前写的
    // 是 8，而窗口只有 6 片，hls.js 找不到「倒数第 8 片」，只好退回窗口开头，
    // 于是进度条一直停在 0:17 的窗口里、缓冲只有零点几秒，播到尾巴必停。
    liveSyncDurationCount: 3,
    // 落后超过 6 片（30 秒）就硬跳回直播边缘，别让延迟一路滚下去
    liveMaxLatencyDurationCount: 6,
    maxLiveSyncPlaybackRate: 1.1,
    backBufferLength: 20,
    // 窗口只有 30 秒，要 90 秒的缓冲等于永远填不满，只会让播放器贴着边缘追
    maxBufferLength: 24,
    manifestLoadingTimeOut: 15000,
    fragLoadingTimeOut: 20000,
    // 上游时不时回一两次 403/超时，重试要够多，别一次就判死
    manifestLoadingMaxRetry: 8,
    manifestLoadingRetryDelay: 1000,
    levelLoadingMaxRetry: 8,
    levelLoadingRetryDelay: 1000,
    fragLoadingMaxRetry: 8,
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
      // 先原地重载清单，保住已经缓冲的窗口——限流时整条重开反而把
      // 救命的那几十秒缓冲全丢掉。只有连续多次原地重载都失败，才整条重开。
      netRetries += 1;
      if (netRetries <= 6) {
        $("playerStats").textContent = `上游抖动，第 ${netRetries} 次续传…`;
        setTimeout(() => { if (hls) hls.startLoad(); }, 1500);
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
  restartAt = Date.now();
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
    return "分片解不出来：这台是 H.265，浏览器解不了（VLC、观屿这类播放器可以）。"
      + "点「关闭」换一个台，或者用「复制地址」丢给外部播放器。";
  }
  return detail || data.type || "未知错误";
}

async function refresh() {
  await loadStatus();
  await loadChannels();
}

document.addEventListener("DOMContentLoaded", () => {
  fillUrls();
  syncTopbarHeight();
  // 刷新页面时保留上次待的分区（地址栏 #channels 这种）
  setView(location.hash.slice(1) || "status", false);
  refresh();
  setInterval(refresh, 5000);

  document.querySelectorAll(".nav-item").forEach((el) => {
    el.addEventListener("click", () => setView(el.dataset.view));
  });
  document.querySelectorAll("[data-goto]").forEach((el) => {
    el.addEventListener("click", () => setView(el.dataset.goto));
  });
  window.addEventListener("hashchange", () => setView(location.hash.slice(1), false));
  window.addEventListener("resize", syncTopbarHeight);
  // 字体加载完、版本号/检查更新结论写进顶栏后，高度还会变一点，补一次校准
  setTimeout(syncTopbarHeight, 300);

  $("btnUpdate").addEventListener("click", triggerUpdate);
  $("btnRefresh").addEventListener("click", refresh);
  $("btnCheckUpdate").addEventListener("click", checkUpdate);
  $("btnSaveSettings").addEventListener("click", () => saveSettings());
  if ($("btnRegionSave")) $("btnRegionSave").addEventListener("click", () => saveSettings("btnRegionSave", "regionSaveNote"));
  if ($("btnMiguSave")) $("btnMiguSave").addEventListener("click", () => saveSettings("btnMiguSave", "miguNote"));
  if ($("btnIpModeSave")) $("btnIpModeSave").addEventListener("click", () => saveSettings("btnIpModeSave", "ipModeNote"));
  if ($("btnYspSave")) $("btnYspSave").addEventListener("click", () => saveSettings("btnYspSave", "yspNote"));
  if ($("btnModuleSave")) $("btnModuleSave").addEventListener("click", () => saveSettings("btnModuleSave", "moduleNote"));
  // 模块开关是动态渲染的，用事件委托标脏，避免被轮询覆盖
  if ($("moduleList")) $("moduleList").addEventListener("change", () => { settingsDirty = true; });
  // 内置模块卡片：点「配置 ›」打开该模块的独立弹窗；开关变化标脏。
  // 动态渲染，用事件委托绑在容器上。
  if ($("builtinModules")) {
    $("builtinModules").addEventListener("click", (e) => {
      const updBtn = e.target.closest("[data-update]");
      if (updBtn) {
        if (!updBtn.disabled) updateOneModule(updBtn.dataset.update);
        return;
      }
      const cfgBtn = e.target.closest("[data-config]");
      if (!cfgBtn) return;
      const card = cfgBtn.closest(".builtin-module");
      if (card) openModuleModal(card.dataset.module || cfgBtn.dataset.config);
    });
    $("builtinModules").addEventListener("change", (e) => {
      settingsDirty = true;
      if (e.target && e.target.id === "kidsEnabled") {
        const row = document.querySelector(".kids-cartoon-row");
        if (row) row.hidden = !e.target.checked;
      }
    });
  }
  if ($("btnModuleModalClose")) $("btnModuleModalClose").addEventListener("click", closeModuleModal);
  if ($("moduleModal")) {
    // 点弹窗外的遮罩关闭（点到面板本身不关）
    $("moduleModal").addEventListener("click", (e) => {
      if (e.target === $("moduleModal")) closeModuleModal();
    });
  }
  if ($("btnProbe")) $("btnProbe").addEventListener("click", probeRegion);
  if ($("btnScan")) $("btnScan").addEventListener("click", scanProvince);
  loadRegionOptions().then(() => {
    if (settings && Object.keys(settings).length && !settingsDirty) fillSettings(settings);
  });
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
    const pick = e.target.closest("[data-group]");
    if (pick) {
      pickGroup(pick.dataset.group);
      return;
    }
    const back = e.target.closest("[data-unhide-group]");
    if (back) unhideGroup(back.dataset.unhideGroup);
    const group = e.target.closest("[data-hide-group]");
    if (group) hideGroup(group.dataset.hideGroup);
  });
  if ($("btnUnhideAll")) $("btnUnhideAll").addEventListener("click", unhideAll);

  $("btnClosePlayer").addEventListener("click", closePlayer);
  $("btnGoLive").addEventListener("click", goLive);
  $("btnCopyLive").addEventListener("click", async () => {
    if (!playerSlug) return;
    const url = absUrl(`live/${encodeURIComponent(playerSlug)}.m3u8`);
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
  ["setPlayMode", "setAutoRegion", "setRegion", "setIsp", "setAutoSwitch", "setYspPlain", "setPublicUrl", "setSecret", "setInterval", "setTtl", "setCache", "setEpgSource", "setMiguEnabled", "setMiguUser", "setMiguToken", "setMiguRate", "setMiguH265", "setMiguEdgeDispatch", "setMiguDispatch", "setMiguRefresh"].forEach((id) => {
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
