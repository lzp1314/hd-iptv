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

  cctv      央视官方 CDN，程序内生成地址，零配置、零维护
  zhejiang  浙江广电官网接口，签名换地址，验证到 1080P
  template  配置驱动的 URL 模板，任何「有固定地址规律」的平台都能接

## 已知边界（实测，2026-11）

央视这条 CDN 对每个频道都返回 200，但**只有 CCTV-1 和 CCTV-13 是真流**：
其余频道返回的是 540P 占位主清单，指向的子清单直接 404。探测阶段会把
它们判死，所以不会污染结果，但也意味着这块只稳定产出两个台。

想要全部央视频道的高清信号，得走央视频的加密握手（bkliveinfo 那套
TEA 加密封包），那是另一个量级的工程，本项目不做。央视网自己的
CNTV 接口现在已经要求签名，普通请求一律返回「该视频不存在」。
"""

from __future__ import annotations

import hashlib
import time
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
                    # 原线路 + 已验证可直连的 ali 线路，都交给探测阶段裁决
                    streams.append(
                        Stream(
                            url=str(raw),
                            raw_name=name,
                            group=group,
                            logo=str(row.get("logo") or ""),
                            hint_height=hint,
                        )
                    )
                    alt = f"https://{self.ALT_HOST}{parts.path}"
                    if alt != str(raw):
                        streams.append(
                            Stream(
                                url=alt,
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


GENERATORS = {
    CctvCollector.kind: CctvCollector,
    ZhejiangCollector.kind: ZhejiangCollector,
    TemplateCollector.kind: TemplateCollector,
}

__all__ = ["GENERATORS", "CctvCollector", "ZhejiangCollector", "TemplateCollector"]
