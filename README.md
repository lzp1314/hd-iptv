# hd-iptv

**高清直播源自动采集 / 清洗 / 测速 / 分发工具。零第三方依赖，一条命令跑在 NAS 上。**

[![CI](https://github.com/lzp1314/hd-iptv/actions/workflows/ci.yml/badge.svg)](https://github.com/lzp1314/hd-iptv/actions/workflows/ci.yml)
[![Docker](https://img.shields.io/badge/image-ghcr.io-blue?logo=docker)](https://github.com/lzp1314/hd-iptv/pkgs/container/hd-iptv)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

它做的事只有一件：把你手上一堆来源不同、命名混乱、三天两头失效的直播源，整理成一份**只保留高清、只保留能播、按时自动刷新**的订阅列表。

更关键的是，它不只会「收集」——内置的**取源器**直接向平台现场要地址，不需要任何人维护现成列表。

**所以它开箱就能用**：默认配置跑完就有 55 个左右的央视和卫视频道，全部 1080P，不需要你去找任何订阅源。
仓库里不含任何直播地址——每个地址都是运行时现换的，这也是它和「搬运列表」类项目的根本区别。

> 仅供个人学习与家庭内网自用。请勿用于公开转播、二次分发或任何商业用途，详见文末[合规声明](#合规声明)。

```
取源器（向平台要）          采集器（收现成的）
平台接口 / 省广电 CDN     ←→  订阅 / 本地 / JSON 接口 / 网页
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
| 源从哪来 | 聚合别人整理好的列表，列表死了就断供 | 内置**取源器**直接向平台要地址，另有四种采集器兜底 |
| 判分辨率 | 调 ffprobe 解码，慢且吃 CPU | 直接读 HLS 主列表的 `RESOLUTION`，毫秒级，容器不用装 ffmpeg |
| 判断能不能播 | 只看 HTTP 200 | 主列表能开、子清单 404 也算死；还要真读一段分片，拿不到字节就不算活 |
| 依赖 | 一堆 pip 包 + ffmpeg | 只用 Python 标准库，`python -m iptvhd` 直接跑 |
| 采集扩展 | 改代码 | 四种源类型靠配置拼，加源不用碰 Python |
| 部署 | CLI 一把梭 | CLI + 网页面板 + Docker Compose（含多架构镜像） |

## 内置取源器

取源器不依赖现成列表：运行时向平台请求可用地址，再逐条校验画质与连通性。默认只开两个，其余留作备用。

| 取源器 | 覆盖范围 | 默认 |
|---|---|---|
| `yangshipin` | 央视全部频道 + 全国卫视，1080P | 开 |
| `zhejiang` | 浙江卫视 1080P，央视之外的第二条独立来源 | 开 |
| `cctv` | 央视部分频道的备份线路 | 关 |
| `template` | 任何「地址有规律」的平台，配置即接入 | 关 |

实现方式不在这份文档里展开，代码自解释。少数频道因版权方要求不提供信号，属于上游限制，不是取源失败。

### 地址是短时的

上游给出的地址带时效，项目用两层机制应对：

- **定时重跑**：默认每 3 小时刷新一次 `result.m3u`，保证列表里的地址新鲜
- **本机转发**：`/live/<slug>.m3u8` 由服务端现换成新地址再转给播放器。播放器看到的是 NAS 自己的地址，不用关心时效，也不受客户端限制

## 快速开始

### 0. 先看效果（不需要任何订阅源）

仓库自带一个演示项目，用的是公开的 HLS 测试流，跟版权内容无关：

```bash
git clone https://github.com/lzp1314/hd-iptv.git
cd hd-iptv
python -m iptvhd --root examples update
```

输出在 `examples/output/result.m3u`。它会识别出 1080P 并保留，360P 直接丢掉。

### 1. 扩充来源

`config/sources.toml` 里三种方式任选：开一个自己的订阅、加一个省广电模块、或者用 `template` 填 URL 模板。

```toml
[[source]]
name = "我的订阅"
type = "subscription"
url = "https://example.com/list.m3u"
enabled = true
```

### 2. 自检

```bash
python -m iptvhd doctor
```

它会告诉你：模板载入了几个频道、有几个源启用、输出目录能不能写、门槛设成多少。有 ✗ 就先修 ✗。

### 3. 跑一次

```bash
python -m iptvhd update
```

### 4. 起服务（推荐）

```bash
python -m iptvhd serve --port 9797
```

打开 `http://NAS_IP:9797` 就是面板，订阅地址：

| 用途 | 地址 |
|---|---|
| M3U 订阅 | `http://NAS_IP:9797/m3u` |
| TXT 订阅 | `http://NAS_IP:9797/txt` |
| JSON 数据 | `http://NAS_IP:9797/json` |
| 单台播放 | `http://NAS_IP:9797/live/cctv1.m3u8` |
| 可用 slug 清单 | `http://NAS_IP:9797/api/live` |

填进 TiviMate / ysc / Kodi / 影视仓 / PotPlayer 都能直接用。面板上有「检查更新」，点一下会跟仓库比版本，升级方式也写在旁边。

**两种用法怎么选：**

- **订阅列表 `/m3u`** —— 一次拿到全部频道，适合当日常主用。设了 `PUBLIC_URL` 时，列表里给的是本机转发地址，播放器直接用。
- **单台地址 `/live/<slug>.m3u8`** —— 每个频道一个固定地址，服务端现换地址再转发，永远不会失效。

`slug` 就是频道名的小写形式，比如 `cctv1`、`cctv5`、`hnws`（湖南卫视）、`zjws`（浙江卫视），完整清单看 `/api/live`。

## 部署

两种方式，按需选一种：

| 方式 | 适合谁 | 命令 |
|---|---|---|
| **源码构建** | 想改代码、看得到源码 | `docker compose up -d --build` |
| **拉现成镜像** | 只想跑起来 | `docker run ... ghcr.io/lzp1314/hd-iptv:latest` |

镜像在 GitHub Actions 里自动构建，`amd64` 和 `arm64` 都有，推 `main` 或打 `v*` 标签各出一版。

### 拉起来之后会经历什么

| 时间 | 状态 |
|---|---|
| 0~5 秒 | 服务已经能访问，面板打得开，`/health` 返回 200 |
| 5~90 秒 | 后台跑首次采集，这时 `/m3u` 返回 404，body 是「结果尚未生成，请先更新一次」 |
| 90 秒后 | 54~56 个频道就绪，`/m3u` 可取，之后每 3 小时自动刷新 |

**不挂任何卷也能跑**——镜像里自带一份默认配置，结果写在容器内的 `/app/output`。能播，但删了容器结果就没了，所以下面还是建议挂两个目录。

### 方式一：Docker Compose（推荐）

```bash
git clone https://github.com/lzp1314/hd-iptv.git
cd hd-iptv
docker compose up -d --build
```

起来后直接访问 `http://NAS_IP:9797`。**配置不用先准备**——`config/` 是空的也没关系，首次启动会自动写入一份默认配置（1080P 主源 + 一条备用来源）。

要改端口 / 目录 / 局域网地址，复制一份 `.env` 再改，别动 yml：

```bash
cp .env.example .env
```

```ini
HOST_PORT=9797
CONFIG_DIR=/vol2/1000/docker/hd-iptv/config
OUTPUT_DIR=/vol2/1000/docker/hd-iptv/output
PUBLIC_URL=http://192.168.1.10:9797
```

改完 `docker compose up -d` 重来一次即可（改环境变量会重建容器，不会重装镜像）。

### 方式二：直接跑镜像

```bash
docker run -d --name hd-iptv --restart unless-stopped \
  -p 9797:9797 \
  -v /vol2/1000/docker/hd-iptv/config:/app/config \
  -v /vol2/1000/docker/hd-iptv/output:/app/output \
  -e HDIPTV_INTERVAL_HOURS=3 \
  -e HDIPTV_MIN_HEIGHT=1080 \
  -e HDIPTV_MAX_HEIGHT=1080 \
  -e PUBLIC_URL=http://192.168.1.10:9797 \
  ghcr.io/lzp1314/hd-iptv:latest
```

`/vol2/1000/docker/...` 是飞牛的路径，换成你自己的即可，两个目录不存在会自动创建。

### 飞牛 fnOS

飞牛的 Docker 面板对 compose 支持得不错，两条路：

**A. 面板里贴 compose（不用 SSH）**

1. 文件管理建目录：`/vol2/1000/docker/hd-iptv/`，里面再建 `config`、`output`
2. Docker → Compose → 新建项目，项目目录选 `/vol2/1000/docker/hd-iptv`
3. compose 内容用仓库里的 `docker-compose.yml`，但把 `build: .` 改成 `image: ghcr.io/lzp1314/hd-iptv:latest`（面板里没有源码，构建不了）
4. 卷映射写**绝对路径**：`/vol2/1000/docker/hd-iptv/config:/app/config`
5. 启动，浏览器开 `http://飞牛IP:9797`

**B. SSH 拉源码构建（能改配置、升级方便）**

```bash
cd /vol2/1000/docker
git clone https://github.com/lzp1314/hd-iptv.git
cd hd-iptv
docker compose up -d --build
```

> **两个必看的坑**
> 1. `docker restart` **不会**重新读卷映射。改过挂载路径必须删容器重建：`docker compose up -d --force-recreate`。
> 2. 挂载的 `config` 目录如果指向别处（比如匿名卷 `/vol2/docker/volumes/<hash>/_data`），你在宿主机上改的配置根本没进容器。用 `docker inspect hd-iptv --format '{{json .Mounts}}'` 核对一下 Source 是不是你以为的目录。

### 群晖 / 极空间 / 威联通

把 `config` 和 `output` 两个目录映射出去即可，`amd64`（群晖 x86 机型）和 `arm64`（部分 ARM 机型）都有镜像。群晖的 Container Manager 支持直接导入 `docker-compose.yml`。

### 镜像从哪来

| 位置 | 说明 |
|---|---|
| `ghcr.io/lzp1314/hd-iptv` | 多架构镜像（`amd64` + `arm64`），有 `latest` 和 `main` 两个 tag |
| 本地构建 | `docker compose up -d --build` 出的 `hd-iptv:local`，不依赖任何镜像仓库 |

拉不动 `ghcr.io` 时（国内网络常见），用本地构建那条，效果一样。

## 命令行

```bash
python -m iptvhd update          # 跑一次完整更新
python -m iptvhd update --quiet  # 只打印汇总
python -m iptvhd serve --port 9797
python -m iptvhd doctor          # 配置自检
python -m iptvhd probe <地址...>  # 单条验证：能不能播、多少 P、多快
python -m iptvhd --version
```

`probe` 是接新平台时的第一件工具。找到一个候选地址，先探一下再写进配置，比跑整轮更新快得多：

```bash
$ python -m iptvhd probe "https://cdn.example.com/zjws/1080p.m3u8"
可用    1080P 1920x1080  33.3Mbps  505ms  https://cdn.example.com/...
```

通用参数 `--root <目录>` 可以指向任意项目目录，用来跑多套配置。

## HTTP 接口

| 路径 | 说明 |
|---|---|
| `/` | 网页面板（含版本检查） |
| `/m3u` `/txt` `/json` | 结果文件 |
| `/api/status` | 运行状态、统计、最近日志 |
| `/api/channels` | 频道与源明细 |
| `/live/<slug>.m3u8` | 单频道播放列表，服务端现换地址并转发 |
| `/relay/<slug>/p/<token>` | 中转内部地址，播放器自动请求，不用手填 |
| `/api/live` | 可用频道清单 |
| `/api/version?check=1` | 当前版本，顺便跟仓库比一下有没有新版 |
| `/api/relay` | 中转缓存与最近错误，排查播放问题用 |
| `POST /api/update` | 手动触发一次更新 |
| `/health` | 健康检查（给 Docker HEALTHCHECK 用） |

## 输出示例

```m3u
#EXTM3U x-tvg-url="https://epg.example.com/e.xml"
#EXTINF:-1 tvg-id="CCTV1" tvg-name="CCTV-1" group-title="央视" tvg-logo="https://logo.example.com/CCTV-1.png",CCTV-1
http://192.168.1.10:9797/live/cctv1.m3u8
```

设了 `PUBLIC_URL`，列表里给的就是这种本机地址——播放器连的是 NAS，不用管上游地址的时效和客户端限制。没设则退回上游直连地址，部分播放器可能放不出来。

一个频道多个源时，名字后面会标画质，方便在播放器里挑；排序是「分辨率优先，其次速率，再次延迟」。

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

71 个用例，包含一个端到端测试：本地起一个伪造的 HLS 服务器（主列表 + 分片 + 一个 404），跑完整管线，断言 1080P 被保留、360P 与 404 被丢弃，输出格式正确。

## 常见问题

**容器起来了，但网页里一个频道都没有**
先看日志头一行有没有 `[初始化] 已写入默认配置`。没有的话，多半是挂载没落到你改的那个目录上——程序在容器里往 `/app/config` 写，你在宿主机改的是另一份。核对实际挂载：

```bash
docker inspect hd-iptv --format '{{json .Mounts}}' | python -m json.tool
```

看 `Source` 是不是你预期的路径。改完挂载必须重建容器，`docker restart` 不生效。

**9797 端口被占了**
改 `.env` 里的 `HOST_PORT=9798`（或 compose 里 `"9798:9797"`），然后 `docker compose up -d`。容器内始终是 9797，只改左边。

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

**列表里只有一个台能播，其他点了没反应**

这是播放器 UA 被上游拒绝的典型症状：主清单能打开，取分片时换成播放器自己的 UA，就被挡回去了。项目默认让列表走本机转发，就是为了避免这件事。

前提是 `.env` 里设了 `PUBLIC_URL=http://<NAS的IP>:9797`。没设的话，列表给的是上游直连地址，就会出现这个问题。设完重新跑一次更新即可。

**播一会儿就卡、过一会儿又好了**

上游对同一地址的重复请求有频率判定，撞上冷却窗口会短暂拒绝。播放器一般会自己重试；频繁卡就等一两分钟，或者把 `interval_hours` 调小，让它更早换新地址。

**某些台这次有、下次没**

上游会把地址分到不同边缘节点，个别节点临时拒绝请求，偶发一两个台缺席是正常的，下一轮会回来。要长期盯住某个台，用 `/live/<slug>.m3u8` 那条固定地址。

**为什么跑一轮要一两分钟**

取源和探测都要控制请求节奏，发太快会被上游判为异常流量。这是后台定时任务，不需要盯着等。

## 合规声明

本项目只是一个**播放列表管理工具**，不生产、不存储、不分发任何音视频内容，仓库内不含任何频道地址或频道数据。

内置取源器的做法是：运行时向平台请求播放地址，拿到即用、不落盘、不二次分发。它依赖上游接口现状，接口一变就得跟着调整，所以不可能是一个「装上就永久有效」的东西。

- 采集到的直播流版权归各电视台 / 平台所有，请仅用于个人学习与家庭内网自用
- 不要用于商业转播、公开分发或任何侵权用途
- 请遵守当地法律法规；部分内容可能存在地区限制或需要相应授权
- 推荐优先使用运营商 IPTV 或有授权的正规渠道

## License

[MIT](LICENSE)
