# hd-iptv

**高清直播源自动采集 / 清洗 / 测速 / 分发工具。零第三方依赖，一条命令跑在 NAS 上。**

[![CI](https://github.com/yourname/hd-iptv/actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

它做的事只有一件：把你手上一堆来源不同、命名混乱、三天两头失效的直播源，整理成一份**只保留高清、只保留能播、按时自动刷新**的订阅列表。

更关键的是，它不只会「收集」——`yangshipin`、`zhejiang` 这类**取源器**直接向官方接口现场要地址，不需要任何人维护现成列表。

**所以它开箱就能用**：默认配置跑完就有 55 个央视和卫视频道，全部 1080P，不需要你去找任何订阅源。
仓库里不含任何直播地址——每个地址都是运行时现换的，这也是它和「搬运列表」类项目的根本区别。

```
取源器（向官方要）          采集器（收别人的）
央视频 / 省广电 / 央视 CDN  ←→  订阅 / 本地 / JSON 接口 / 网页
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
| `yangshipin` | 央视频加密接口：自造 cKey 换直播地址 | **实测 55 个频道 / 56 条流，全部 1080P**（央视 23 个含 6 路 CGTN + 卫视 32 个，CCTV-10 被官方限制） |
| `cctv` | 央视官方 CDN，程序内拼地址 | 只有 CCTV-1、CCTV-13 有真流，其余是 540P 占位清单（子清单 404），默认关闭 |
| `zhejiang` | 浙江广电官网签名接口 | 浙江卫视 1080P，作为央视频之外的第二条独立来源 |
| `template` | 你自己填 URL 模板，配置即接入 | 取决于你填的地址 |

### 央视频这条是怎么打通的

它的播放地址既不在网页里，也不在任何明文接口里，而是客户端拿一个**自己加密的身份凭据**去换的：

```
设备指纹 + 时间戳 + 频道号 + 校验串
        │  TEA-CBC 加密 → 与固定字节序列异或 → base64url
        ▼
     cKey  ──GET bkliveinfo.ysp.cctv.cn──▶  playurl（带 token 的直播地址）
```

TEA 本身不难，难的是那个二进制包的字段顺序和两段固定密钥——这些值不在任何文档里。
实现、常量、以及每一步的实测结论都写在 `iptvhd/ysp.py` 里。

**接的时候踩了两个坑，都写进代码注释了：**

1. **分片必须拼在 token 目录下。** 返回的地址形如 `…/<长token>/<cnlid>.m3u8`，分片是相对名。用普通的相对路径解析会把 token 当成文件名吃掉，结果是根路径一律 403 —— 看起来像「源挂了」，其实只是拼错了。
2. **不能并发。** 同一时刻打多个请求会被判为异常流量，返回的 403 和死链长得一模一样。实测间隔 0.3 秒连续 10 次全部通过，所以取源和探测都走全局节拍器。顺带说明：加 `APPID` / `UID` 之类的「签名头」反而全部 403，正确做法是只带 `User-Agent: qqlive`。

### 地址有时效

换出来的地址带 token，会过期（接口给的重签间隔是 4 小时，实测 20 分钟仍然有效）。
两种应对都在里面：

- **静态列表**：定时任务按间隔重跑（默认 6 小时），保持 `result.m3u` 里的地址新鲜
- **实时跳转**：`/live/<slug>.m3u8` 每次请求现场换一个新地址，播放器不用管过期

### 为什么只有 CCTV-10 没有

`iretcode=12`，原文是「应版权方要求，暂停提供直播信号」。这是平台授权问题，不是取源失败，绕不过去。同理还有十几个台央视频本身就不转播（上海、福建、兵团、三沙、海峡、厦门、大湾区、延边、农林）。


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

`config/sources.toml` 里默认开着的就是央视频取源器 + 浙江广电，跑完就有央视和卫视：

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
| 单台实时跳转 | `http://NAS_IP:8080/live/cctv1.m3u8` |
| 可用 slug 清单 | `http://NAS_IP:8080/api/live` |

填进 TiviMate / ysc / Kodi / 影视仓 / PotPlayer 都能直接用。

**两种用法怎么选：**

- **订阅列表 `/m3u`** —— 一次拿到全部频道，适合当日常主用。地址有寿命，靠定时刷新维持。
- **单台跳转 `/live/<slug>.m3u8`** —— 每个频道一个固定地址，永远指向刚换出来的新 token。地址绝不会过期，但只适合一两个台的场景（每个台都要单独添加）。

`slug` 见 `iptvhd/ysp.py` 里的频道表，比如 `cctv1`、`cctv5`、`hnws`（湖南卫视）、`zjws`（浙江卫视）。

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

八种类型：四个**取源器**（自己向官方要地址），四个**采集器**（收别人的列表）。

```toml
# ===== 取源器 =====

# 0) 央视频：主源，央视 + 全国卫视，默认 1080P
[[source]]
name = "央视频"
type = "yangshipin"
enabled = true
workers = 1                      # 串行，接口对并发敏感
# groups = ["央视"]              # 只要央视
# channels = ["cctv1", "hnws"]   # 只要指定几台
# defn = "shd"                   # 降到 720P

# 0) 浙江广电签名接口：给浙江卫视多一条独立来源
[[source]]
name = "浙江广电"
type = "zhejiang"
quality = "1080P"                # 也可以 720P
channels = ["浙江卫视"]          # 留空则收录该省全部频道
enabled = true

# 0) 央视官方 CDN：只对 CCTV-1 / CCTV-13 有效，当备份线路用
[[source]]
name = "央视官方"
type = "cctv"
group = "央视"
enabled = false
# only = ["cctv1", "cctv13"]     # 只留指定台，其余不产出

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

**播一会儿就卡 / 黑屏**

央视频对非官方客户端有频率判定：同一地址连续取，会间歇性返回 403。我这边实测的数据是——

| 请求间隔 | 再次取同一地址的成功率 |
|---|---|
| 1.5 秒 | 约 50% |
| 2.5 秒 | 5/6 |
| 3 秒以上 | 基本都过 |

播放器都带重试，多数情况下能连着播；如果频繁卡，说明取片太密或正好撞上冷却窗口，等一两分钟会自己恢复。这不是代码问题，是对方的反爬策略，绕不过去也不该硬绕。

**某些台这次有、下次没**
央视频的地址是随机分配到不同边缘节点的，个别节点会临时拒绝请求。取源环节已经做了换 token 重试，但偶发一两个台缺席是正常的，下一轮会回来。要稳定追某个台，用 `/live/<slug>.m3u8` 那条路。

**CCTV-10 一直出不来**
官方限制（`iretcode=12`，应版权方要求暂停提供），不是 bug，绕不过去。

**为什么跑一轮要一两分钟**
取源必须串行——并发会被判为异常流量，返回的 403 和死链分不清。56 个频道、每个 3 次请求、间隔 0.3 秒，一分半钟是正常水平。定时任务跑，不用管它。

**接口改版了怎么办**
改 `iptvhd/ysp.py`。频道号在 `CHANNELS` 表里，加密常量在文件头部，两个都可能有变动。判断方法：日志里出现 `iretcode=14 请求格式被拒绝` 就是协议层变了。

## 合规声明

本项目只是一个**播放列表管理工具**，不生产、不存储、不分发任何音视频内容，仓库内不含任何频道地址或频道数据。

内置取源器的做法是：运行时按官方客户端的公开行为向官方接口请求播放地址，拿到即用、不落盘、不二次分发。它既不是「破解」也不是「盗链」，但确实依赖对方接口现状——接口一变就得跟着改，这也决定了它不可能是一个「装上就永久有效」的东西。

- 采集到的直播流版权归各电视台 / 平台所有，请仅用于个人学习与家庭内网自用
- 不要用于商业转播、公开分发或任何侵权用途
- 请遵守当地法律法规；部分内容可能存在地区限制或需要相应授权
- 推荐优先使用运营商 IPTV 或有授权的正规渠道

## License

[MIT](LICENSE)
