"""取源器：直接向官方站点取流，不依赖任何现成播放列表。

和 collectors 的分工：

  collectors —— 别人已经整理好的列表（订阅 / 本地 / JSON 接口 / 网页正则）
  generators —— 自己现场向官方要地址（央视 CDN、省广电签名接口）

两者产物都是 Stream 列表，下游的清洗、探测、测速、输出完全一致。
新增一个平台 = 新增一个类 + 在 REGISTRY 里登记一行，不牵动管线。

## 为什么这么设计

现成列表的寿命取决于别人是否还在更新；取源器的寿命取决于官方接口是否
还在。前者会被动枯竭，后者只要跟着接口改就能一直有新地址——这是「收集」
和「生产」的区别。

## 三个内置取源器

  yangshipin 央视频加密接口，央视 + 全部卫视的 1080P，覆盖最全
  cctv       央视官方 CDN，只能拿到 CCTV-1 / CCTV-13 的真流
  zhejiang   浙江广电官网接口，签名换地址，验证到 1080P
  template   配置驱动的 URL 模板，任何「有固定地址规律」的平台都能接

## 已知边界（实测，2026-11）

央视频这条是主源：央视 23 个台 + 卫视 34 个台实测可用，全部 1080P。
CCTV-10 被官方限制为「应版权方要求暂停提供」，属于平台授权问题。

央视官方 CDN（cctv）这条对每个频道都返回 200，但**只有 CCTV-1 和
CCTV-13 是真流**：其余频道返回 540P 占位主清单，指向的子清单直接 404。
探测阶段会把它们判死，所以不会污染结果，但它只适合当 CCTV-1/13 的
额外备份线路，默认关闭。
"""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode, urlsplit

from .collectors import Collector
from .models import Stream
from .net import HttpClient


# --------------------------------------------------------------------------- #
# 央视：官方 CDN
# --------------------------------------------------------------------------- #
class CctvCollector(Collector):
    """央视官方直播 CDN。

    地址形如 ``http://{host}/ldncctvwbcd/cdrmld{code}_1/index.m3u8``，
    返回 HLS 主播放列表，最高档由探测阶段自动挑选。

    多条 CDN 线路同时产出，探测阶段自动淘汰连不上的，不需要人工挑线路。
    """

    kind = "cctv"

    HOSTS = (
        "ldncctvwbcdtxy.liveplay.myqcloud.com",
        "ldncctvwbcdali.v.myalicdn.com",
        "ldncctvwbcdcnc.v.wscdns.com",
        "ldncctvwbcdhwy.cntv.myhwcdn.cn",
        "ldncctvwbcdks.v.kcdnvip.com",
        "ldncctvwbcdbd.a.bdydns.com",
    )
    PATH_PREFIX = "/ldncctvwbcd"

    # (code, 显示名)
    CHANNELS = (
        ("cctv1", "CCTV-1 综合"),
        ("cctv2", "CCTV-2 财经"),
        ("cctv3", "CCTV-3 综艺"),
        ("cctv4", "CCTV-4 中文国际"),
        ("cctv5", "CCTV-5 体育"),
        ("cctv6", "CCTV-6 电影"),
        ("cctv7", "CCTV-7 国防军事"),
        ("cctv8", "CCTV-8 电视剧"),
        ("cctv9", "CCTV-9 纪录"),
        ("cctv10", "CCTV-10 科教"),
        ("cctv11", "CCTV-11 戏曲"),
        ("cctv12", "CCTV-12 社会与法"),
        ("cctv13", "CCTV-13 新闻"),
        ("cctv14", "CCTV-14 少儿"),
        ("cctv15", "CCTV-15 音乐"),
        ("cctv16", "CCTV-16 奥林匹克"),
        ("cctv17", "CCTV-17 农业农村"),
    )

    def collect(self, client: HttpClient) -> list[Stream]:
        hosts = tuple(self.config.get("hosts") or self.HOSTS)
        group = str(self.config.get("group", "央视"))
        only = self.config.get("only")

        wanted = None
        if only:
            wanted = {str(x).strip().lower() for x in only if str(x).strip()}

        streams: list[Stream] = []
        for code, label in self.CHANNELS:
            if wanted is not None and code not in wanted:
                continue
            path = f"{self.PATH_PREFIX}/cdrmld{code}_1/index.m3u8"
            for host in hosts:
                streams.append(
                    Stream(
                        url=f"http://{host}{path}",
                        raw_name=label,
                        group=group,
                    )
                )
        return self._tag(streams)


# --------------------------------------------------------------------------- #
# 浙江广电：签名换地址
# --------------------------------------------------------------------------- #
class ZhejiangCollector(Collector):
    """浙江新蓝网（cztv）官网直播接口。

    两步走，和官网播放器完全一致：

      1. 拉频道表  ``p.cztv.com/api/paas/channel/tv``（公开，无需签名）
      2. 逐个换地址 ``zlive-das.cztv.com/zapp/live/tv/channel/playInfo``
         —— 需要按官网播放器的算法算 ``auth_key``

    签名算法（公开前端实现，不是账号凭据）::

        md5(f"{path}-{ts}-0-0-{salt}")  →  auth_key = f"{ts}-0-0-{digest}"

    接口下发的播放地址在 ``zwebl0x`` 系列主机上，部分网络会被 403；
    同一路径在 ``ali-xwl`` 线路上可直连，所以两条都产出，交给探测阶段淘汰。
    """

    kind = "zhejiang"

    LIST_URL = "https://p.cztv.com/api/paas/channel/tv"
    PLAY_INFO = "https://zlive-das.cztv.com/zapp/live/tv/channel/playInfo"
    SALT = "CHWr9VybUeBZE1VB"
    ALT_HOST = "ali-xwl.cztv.com"
    UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def _signed_url(self, channel_id: str) -> str:
        path = "/zapp/live/tv/channel/playInfo"
        ts = str(int(time.time()))
        digest = hashlib.md5(f"{path}-{ts}-0-0-{self.SALT}".encode()).hexdigest()
        query = urlencode(
            {
                "channelId": channel_id,
                "platform": "WEB",
                "auth_key": f"{ts}-0-0-{digest}",
            }
        )
        return f"{self.PLAY_INFO}?{query}"

    def collect(self, client: HttpClient) -> list[Stream]:
        quality = str(self.config.get("quality", "1080P")).upper()
        group = str(self.config.get("group", "卫视"))
        want = self.config.get("channels")

        # 官方接口按档位下发的是单码率媒体清单，清单里没有 RESOLUTION，
        # 所以把请求的档位直接作为分辨率线索带下去，避免被当成「未知」丢弃。
        hint = {"1080P": 1080, "720P": 720, "540P": 540, "480P": 480}.get(quality, 0)

        headers = {"User-Agent": self.UA, "Referer": "https://www.cztv.com/"}
        listed = client.get(self.LIST_URL, headers=headers)
        if not listed.body:
            raise RuntimeError(f"频道表获取失败: {listed.error or listed.status}")

        import json

        try:
            payload = json.loads(listed.text)
        except ValueError as exc:
            raise RuntimeError(f"频道表不是合法 JSON: {exc}") from exc

        rows = ((payload.get("content") or {}).get("list")) or []
        if not rows:
            raise RuntimeError("频道表为空，接口可能已改版")

        wanted = None
        if want:
            wanted = {str(x).strip() for x in want if str(x).strip()}

        streams: list[Stream] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            channel_id = str(row.get("station_code") or "").strip()
            name = str(row.get("name") or "").strip()
            if not channel_id.isdigit() or not name:
                continue
            if wanted is not None and name not in wanted:
                continue

            try:
                resp = client.get(self._signed_url(channel_id), headers=headers)
                data = (json.loads(resp.text or "{}") or {}).get("data") or {}
            except Exception:  # noqa: BLE001 - 单台失败不影响其它台
                continue

            for item in data.get("multiBitrateStreamList") or []:
                if str(item.get("bitrateCode", "")).upper() != quality:
                    continue
                for raw in item.get("urlList") or []:
                    parts = urlsplit(str(raw))
                    if not parts.path:
                        continue
                    # 上游会给多条线路，其中 ali 那条对每个线路都相同，
                    # 用 seen 去重，避免重复探测同一条地址
                    for candidate in (str(raw), f"https://{self.ALT_HOST}{parts.path}"):
                        if candidate in seen:
                            continue
                        seen.add(candidate)
                        streams.append(
                            Stream(
                                url=candidate,
                                raw_name=name,
                                group=group,
                                logo=str(row.get("logo") or ""),
                                hint_height=hint,
                            )
                        )
        return self._tag(streams)


# --------------------------------------------------------------------------- #
# 通用模板：配置驱动的取源器
# --------------------------------------------------------------------------- #
class TemplateCollector(Collector):
    """按 URL 模板批量生成地址。

    适合任何「频道地址有固定规律」的平台，配置即接入，不用改代码::

        [[source]]
        type = "template"
        name = "某省卫视"
        url_template = "https://cdn.example.com/live/{code}.m3u8"
        group = "卫视"
        channels = [
          { code = "a", name = "某卫视" },
        ]
    """

    kind = "template"

    def collect(self, client: HttpClient) -> list[Stream]:
        tpl = str(self.config.get("url_template", "")).strip()
        if not tpl:
            return []
        group = str(self.config.get("group", ""))
        streams: list[Stream] = []
        for item in self.config.get("channels") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            try:
                url = tpl.format(**item)
            except (KeyError, IndexError, ValueError):
                continue
            streams.append(
                Stream(
                    url=url,
                    raw_name=name,
                    group=str(item.get("group", group) or group),
                )
            )
        return self._tag(streams)


# --------------------------------------------------------------------------- #
# 央视频：加密接口（央视 + 卫视，覆盖面最广）
# --------------------------------------------------------------------------- #
class YangshipinCollector(Collector):
    """央视频取流器。

    走 ``iptvhd.ysp`` 的加密握手，一个接口覆盖央视全部频道和全国卫视，
    默认要 1080P（``fhd`` 档）。协议细节和实测边界都写在那个模块里。

    配置项：

      channels  只取这些 slug，例如 ["cctv1", "hnws"]；留空取全部
      groups    按分组过滤，例如 ["央视"] 或 ["卫视"]
      defn      码率档，默认 fhd（1080P），可选 shd（720P）/ hd（540P）
      workers   并发数，默认 4；调大会更容易触发官方的请求频率限制
    """

    kind = "yangshipin"

    def collect(self, client: HttpClient) -> list[Stream]:
        from . import ysp

        want = {str(x).strip().lower() for x in (self.config.get("channels") or []) if str(x).strip()}
        groups = {str(x).strip() for x in (self.config.get("groups") or []) if str(x).strip()}
        defn_override = str(self.config.get("defn", "") or "").strip()
        # 接口对并发敏感，默认串行；调大只会更容易被限流，不建议
        workers = max(1, int(self.config.get("workers", 1)))
        verify = bool(self.config.get("verify_segment", False))

        rows = [
            row for row in ysp.CHANNELS
            if (not want or row[0] in want) and (not groups or _ysp_group(row[0]) in groups)
        ]
        if not rows:
            raise ValueError("筛选条件没有匹配到任何频道")

        probe_client = HttpClient(
            user_agent=ysp.UA,
            timeout=client.timeout,
            retries=2,
            proxy=client.proxy,
        )

        self.notes = []
        results: dict[str, Stream] = {}

        def resolve_one(row: tuple[str, str, str, str, str]) -> tuple[str, Stream | None, str]:
            slug, name, cnlid, livepid, defn = row
            target_defn = defn_override or defn
            try:
                url, info, first_seg = ysp.resolve(
                    cnlid, livepid, target_defn, probe_client, verify_segment=verify)
            except ysp.YspUnavailable as exc:
                return slug, None, f"{name}：{exc}"
            except Exception as exc:  # noqa: BLE001 - 单台失败不影响其余
                return slug, None, f"{name}：{exc}"
            stream = Stream(
                url=url,
                raw_name=name,
                group=_ysp_group(slug),
                slug=slug,
                tvg_id=name.split()[0].replace("-", ""),
                hint_height=ysp.DEFN_HEIGHT.get(target_defn, 1080),
                user_agent=ysp.UA,
                first_segment=first_seg,
                playlist_only=True,
            )
            return slug, stream, ""

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(resolve_one, row) for row in rows]
            for fut in as_completed(futures):
                slug, stream, note = fut.result()
                if stream is not None:
                    results[slug] = stream
                elif note:
                    self.notes.append(note)

        # 按配置顺序输出，保证每次生成的列表顺序稳定
        ordered = [results[row[0]] for row in rows if row[0] in results]
        return self._tag(ordered)


def _ysp_group(slug: str) -> str:
    return "央视" if slug.startswith(("cctv", "cgtn")) else "卫视"


GENERATORS = {
    YangshipinCollector.kind: YangshipinCollector,
    CctvCollector.kind: CctvCollector,
    ZhejiangCollector.kind: ZhejiangCollector,
    TemplateCollector.kind: TemplateCollector,
}

__all__ = [
    "GENERATORS",
    "YangshipinCollector",
    "CctvCollector",
    "ZhejiangCollector",
    "TemplateCollector",
]
