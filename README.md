# hd-iptv

**高清直播源自动采集 / 清洗 / 测速 / 分发工具。零第三方依赖，一条命令跑在 NAS 上。**

[![CI](https://github.com/yourname/hd-iptv/actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

它做的事只有一件：把你手上一堆来源不同、命名混乱、三天两头失效的直播源，整理成一份**只保留高清、只保留能播、按时自动刷新**的订阅列表。

更关键的是，它不只会「收集」——`cctv`、`zhejiang` 这类**取源器**直接从官方站点现场要地址，不需要任何人维护现成列表。

```
取源器（向官方要）          采集器（收别人的）
央视 CDN / 省广电接口   ←→   订阅 / 本地 / JSON 接口 / 网页
        │                        │
        └──────────┬─────────────┘
                   ▼   统一成候选流
                   ▼   清洗 + 匹配        ← 频道模板决定留谁、怎么分组
   湖南卫视_HD → 湖南卫视
                   ▼   探测 + 测速        ← 解析 m3u8 主列表拿分辨率，读分片测真实速率
   1920x1080 / 6.2Mbps / 28ms
                   ▼   过滤                ← 720P 以下丢、1080P 以上丢、超时丢、404 丢
                   ▼   输出
   result.m3u ── result.txt ── result.json ── HTTP 订阅地址 + 网页面板
```

## 和同类项目的区别

| | 常见做法 | hd-iptv |
|---|---|---|
| 源从哪来 | 聚合别人整理好的列表，列表死了就断供 | 内置**取源器**直接向官方要地址，另有四种采集器兜底 |
| 判分辨率 | 调 ffprobe 解码，慢且吃 CPU | 直接读 HLS 主列表的 `RESOLUTION`，毫秒级，容器不用装 ffmpeg |
| 判断能不能播 | 只看 HTTP 200 | 主列表能开、子清单 404 也算死；还要真读一段分片，拿不到字节就不算活 |
| 依赖 | 一堆 pip 包 + ffmpeg | 只用 Python 标准库，`python -m iptvhd` 直接跑 |
| 采集扩展 | 改代码 | 四种源类型靠配置拼，加源不用碰 Python |
| 部署 | CLI 一把梭 | CLI + 网页面板 + Docker + GitHub Actions |

## 内置取源器与覆盖范围

这块我把实测结论直接写出来，省得你踩同样的坑。

| 取源器 | 机制 | 实测产出 |
|---|---|---|
| `cctv` | 央视官方 CDN，程序内拼地址，6 条线路并发 | **CCTV-1、CCTV-13 有真实 1080P**；其余 15 个台官方返回的是 540P 占位清单，子清单 404，会被判死 |
| `zhejiang` | 拉官网频道表 → 按官网播放器算法签名 → 换 1080P 地址 | 浙江卫视 1080P 稳定；其余 9 个地方台也能取，但模板默认不收录 |
| `template` | 你自己填 URL 模板，配置即接入 | 取决于你填的地址 |

**为什么央视只出来两个台？** 不是代码写错。央视网对每个 `cdrmldcctv*` 路径都返回 200，但只有 CCTV-1 / CCTV-13 是真流，其余返回一个 540P 主清单，指向的 `/cdrmldcctv2_1_hd.m3u8` 直接 404。用一个不存在的名字 `cdrmldabcxyz_1` 去请求，返回的是一模一样的占位清单——这就是证据。想拿全部央视频道的高清信号，必须走央视频 App 的 TEA 加密握手，那是另一个量级的工程量。

**所以卫视要一个个加。** 每个省广电官网的接口形态都不一样，没有通用解。`zhejiang` 那个模块就是模板，照着改一个省大概是几十行的活，改完用 `python -m iptvhd probe <地址>` 一验就知道通不通。


## 快速开始

### 0. 先看效果（不需要任何订阅源）

仓库自带一个演示项目，用的是公开的 HLS 测试流，跟版权内容无关：

```bash
git clone https://github.com/yourname/hd-iptv.git
cd hd-iptv
python -m iptvhd --root examples update
```

输出在 `examples/output/result.m3u`。它会识别出 1080P 并保留，360P 直接丢掉。

### 1. 直接开跑（不用配任何源）

`config/sources.toml` 里内置的两个取源器默认就是开着的：

```bash
python -m iptvhd update
```

它会现场向央视和浙江广电要地址，探测完输出到 `output/result.m3u`。第一次大约十几秒。

想确认某个地址到底什么画质，单独探一下：

```bash
python -m iptvhd probe "https://ali-xwl.cztv.com/live/channel011080Pnew.m3u8"
```

```
可用    1080P 0x1080  33.3Mbps  505ms  https://ali-xwl.cztv.com/live/channel011080Pnew.m3u8
```

### 2. 扩充来源

`config/sources.toml` 里三种方式任选：开一个自己的订阅、加一个省广电模块、或者用 `template` 填 URL 模板。

```toml
[[source]]
name = "我的订阅"
type = "subscription"
url = "https://example.com/list.m3u"
enabled = true
```

### 3. 自检

```bash
python -m iptvhd doctor
```

它会告诉你：模板载入了几个频道、有几个源启用、输出目录能不能写、门槛设成多少。有 ✗ 就先修 ✗。

### 4. 跑一次

```bash
python -m iptvhd update
```

### 5. 起服务（可选，推荐）

```bash
python -m iptvhd serve --port 8080
```

打开 `http://NAS_IP:8080` 就是面板，订阅地址：

| 用途 | 地址 |
|---|---|
| M3U 订阅 | `http://NAS_IP:8080/m3u` |
| TXT 订阅 | `http://NAS_IP:8080/txt` |
| JSON 数据 | `http://NAS_IP:8080/json` |

填进 TiviMate / ysc / Kodi / 影视仓 / PotPlayer 都能直接用。

## 配置

三个文件，各管一件事。

### `config/config.toml` —— 全局参数

```toml
[probe]
concurrency = 24           # 并发探测数，NAS 建议 8~16
min_height = 720           # 高清门槛：低于此分辨率直接丢；填 1080 就是「只留 FHD」
max_height = 1080          # 画质上限：高于此分辨率直接丢。不要 4K 就保持默认，想全收填 0
min_speed_mbps = 1.5       # 最低速率
max_per_channel = 4        # 每个频道保留几个源
allow_unknown_resolution = false   # 允许分辨率未知的流（脚本类源常见，开了会混进标清）

[output]
epg_url = ""               # 填了才会在 m3u 头里写 x-tvg-url
logo_base = ""             # 台标库根地址，留空则用源自带的 tvg-logo
```

完整项见文件内注释。所有项都支持环境变量覆盖：`HDIPTV_MIN_HEIGHT` `HDIPTV_MAX_HEIGHT` `HDIPTV_CONCURRENCY` `HDIPTV_INTERVAL_HOURS` `HDIPTV_SUBSCRIBE` 等。

### `config/channels.txt` —— 频道模板

决定**输出哪些频道、怎么分组、按什么顺序**。模板里没有的频道一律不输出。

```
央视,#genre#
CCTV-1,央视一套,中央一套
CCTV-5+
CCTV-4K,CCTV4K超高清

卫视,#genre#
湖南卫视
东方卫视
```

逗号后面是别名，源里叫法不同（`央视一套` / `中央一台` / `CCTV1综合`）时靠它兜底。内置了央视 1~17 套的常见叫法映射；繁体（`湖南衛視`）、画质后缀（`_HD` `FHD` `1080P` `50FPS`）、各种分隔符都会在匹配前被清洗掉。

匹配有边界保护：`CCTV1` 不会误吞 `CCTV11`。

### `config/sources.toml` —— 采集源

七种类型：三个**取源器**（自己向官方要地址），四个**采集器**（收别人的列表）。

```toml
# ===== 取源器 =====

# 0) 央视官方 CDN：零配置，开箱就有
[[source]]
name = "央视官方"
type = "cctv"
group = "央视"
enabled = true
# only = ["cctv1", "cctv13"]     # 只留指定台，其余不产出

# 0) 省广电签名接口
[[source]]
name = "浙江广电"
type = "zhejiang"
quality = "1080P"                # 也可以 720P
channels = ["浙江卫视"]          # 留空则收录该省全部频道
enabled = true

# 0) URL 模板：任何「地址有规律」的平台，配置即接入
[[source]]
name = "某省卫视"
type = "template"
url_template = "https://cdn.example.com/live/{code}.m3u8"
group = "卫视"
enabled = true
channels = [
  { code = "a", name = "某卫视" },
]

# ===== 采集器 =====

# 1) 订阅：m3u / txt 地址
[[source]]
name = "订阅A"
type = "subscription"
url = "https://example.com/list.m3u"
enabled = true
ua = "okhttp/3.12.0"        # 可选
headers = { Referer = "https://example.com/" }   # 可选

# 2) 本地：单个文件或整个目录
[[source]]
name = "本地源"
type = "local"
path = "config/local"        # 相对项目根目录

# 3) JSON 接口：字段映射自己配
[[source]]
name = "某站接口"
type = "api"
url = "https://example.com/api/channels"
enabled = true
json_path = "data.list"      # 支持 a.b.c、list[0]
name_field = "name"
url_field = "play_url"
group_field = "category"
url_template = "https://cdn.example.com/{url}"   # 可选，需要拼接时用

# 4) 网页正则
[[source]]
name = "网页抽取"
type = "webpage"
url = "https://example.com/live"
enabled = true
regex = '<a[^>]+href="([^"]+\.m3u8)"[^>]*>([^<]+)</a>'
url_group = 1
name_group = 2
```

> 项目**不内置任何数据源**。源从哪来、合不合规，由使用者自己负责。

## 部署

### Docker Compose（NAS / 服务器通用）

```bash
git clone https://github.com/yourname/hd-iptv.git
cd hd-iptv
# 先按上面的步骤配好 config/，再启动
docker compose up -d
```

或者不克隆，直接跑镜像：

```bash
docker run -d --name hd-iptv --restart unless-stopped \
  -p 8080:8080 \
  -v /vol2/1000/docker/hd-iptv/config:/app/config \
  -v /vol2/1000/docker/hd-iptv/output:/app/output \
  -e HDIPTV_INTERVAL_HOURS=6 \
  -e PUBLIC_URL=http://192.168.1.10:8080 \
  ghcr.io/yourname/hd-iptv:latest
```

### 飞牛 fnOS

1. 文件管理里建目录 `/vol2/1000/docker/hd-iptv/`，下面建 `config` 和 `output`
2. 把仓库的 `config/` 三个文件拷进去，改好订阅源
3. Docker → Compose → 新建项目，粘贴 `docker-compose.yml`，**卷映射务必写绝对路径**
4. 启动后浏览器开 `http://NAS_IP:8080`

> 踩坑提醒：`docker restart` 不会重新加载卷映射。改过挂载路径后必须删容器重建（或 `docker compose up -d --force-recreate`）。

### 群晖 / 极空间 / 威联通

同 Docker 思路，把 `config` 和 `output` 两个目录映射出去即可，架构支持 `amd64` 与 `arm64`。

### GitHub Actions（不占自己带宽）

1. Fork 本仓库
2. Settings → Secrets 添加 `IPTV_SUBSCRIBE`（订阅地址，多个用逗号分隔）
3. Settings → Pages → Source 选 `GitHub Actions`
4. 手动跑一次 `Update playlists` 工作流

之后每天北京时间 05:30 自动更新，结果发布在 `https://你的用户名.github.io/hd-iptv/`。

## 命令行

```bash
python -m iptvhd update          # 跑一次完整更新
python -m iptvhd update --quiet  # 只打印汇总
python -m iptvhd serve --port 8080
python -m iptvhd doctor          # 配置自检
python -m iptvhd probe <地址...>  # 单条验证：能不能播、多少 P、多快
python -m iptvhd --version
```

`probe` 是接新平台时的第一件工具。找到一个候选地址，先探一下再写进配置，比跑整轮更新快得多：

```bash
$ python -m iptvhd probe "https://ali-xwl.cztv.com/live/channel011080Pnew.m3u8"
可用    1080P 0x1080  33.3Mbps  505ms  https://ali-xwl.cztv.com/...
```

通用参数 `--root <目录>` 可以指向任意项目目录，用来跑多套配置。

## HTTP 接口

| 路径 | 说明 |
|---|---|
| `/` | 网页面板 |
| `/m3u` `/txt` `/json` | 结果文件 |
| `/api/status` | 运行状态、统计、最近日志 |
| `/api/channels` | 频道与源明细 |
| `POST /api/update` | 手动触发一次更新 |
| `/health` | 健康检查（给 Docker HEALTHCHECK 用） |

## 输出示例

```m3u
#EXTM3U x-tvg-url="https://epg.example.com/e.xml"
#EXTINF:-1 tvg-id="cctv1" tvg-name="CCTV-1" group-title="央视" tvg-logo="https://logo.example.com/CCTV-1.png",CCTV-1·4K
http://a.example.com/cctv1-4k.m3u8
#EXTINF:-1 tvg-id="cctv1" tvg-name="CCTV-1" group-title="央视" tvg-logo="https://logo.example.com/CCTV-1.png",CCTV-1·1080P
http://b.example.com/cctv1.m3u8
```

一个频道多个源时，名字后面会标画质，方便在播放器里挑；排序是「分辨率优先，其次速率，再次延迟」。

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

15 个用例，包含一个端到端测试：本地起一个伪造的 HLS 服务器（主列表 + 分片 + 一个 404），跑完整管线，断言 1080P 被保留、360P 与 404 被丢弃，输出格式正确。

## 常见问题

**更新后是空的 / 0 条**
1. `doctor` 看有没有启用的源
2. 模板里有没有对应频道名（`CCTV-1` 和 `cctv1` 能对上，`央视综合` 要靠别名）
3. 把 `min_height` 临时调到 0、`allow_unknown_resolution` 设为 true 再跑一次，确认是过滤太狠还是压根没采到
4. 日志里搜「未匹配」，未匹配数很高说明源里的频道名跟模板差太远

**大量 403 / 超时**
多为 UA 或 Referer 校验。给对应源加 `ua` / `headers`，或者调大 `timeout`、降低 `concurrency`。

**结果里混进标清**
`min_height` 提到 1080，并关掉 `allow_unknown_resolution`。HLS 源基本都能拿到 `RESOLUTION`，拿不到的本来就是脚本录屏类，画质不可控。

**频道对不上**
优先加别名。别名比模糊匹配可靠，模糊匹配只在名字包含时才兜底，且有数字边界保护。

**NAS 上跑起来很卡**
`concurrency` 降到 8，`sample_bytes` 降到 131072，顺便把 `max_per_channel` 设成 2。

## 合规声明

本项目只是一个**播放列表管理工具**，不生产、不存储、不分发任何音视频内容，仓库内不含任何频道数据。

- 采集到的直播流版权归各电视台 / 平台所有，请仅用于个人学习与家庭内网自用
- 不要用于商业转播、公开分发或任何侵权用途
- 请遵守当地法律法规；部分内容可能存在地区限制或需要相应授权
- 推荐优先使用运营商 IPTV 或有授权的正规渠道

## License

[MIT](LICENSE)
