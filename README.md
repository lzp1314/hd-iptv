# hd-iptv

**央视 + 卫视的高清直播，自动采集、自动测速、自动分发。零第三方依赖，一条命令跑在 NAS 上。**

[![CI](https://github.com/lzp1314/hd-iptv/actions/workflows/ci.yml/badge.svg)](https://github.com/lzp1314/hd-iptv/actions/workflows/ci.yml)
[![Docker](https://img.shields.io/badge/image-ghcr.io-blue?logo=docker)](https://github.com/lzp1314/hd-iptv/pkgs/container/hd-iptv)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

跑起来就有 56 个频道（CCTV 18 个 + CGTN 6 个 + 32 个卫视），1080P，不需要你去找
任何订阅源。仓库里不含任何直播地址。

**只做 1080P**：4K / 8K 三个台不采集——匿名接口给它们的也只是 1080P，拉不到真 4K，
留着只会多几个点进去黑屏的台。

> 仅供个人学习与家庭内网自用，详见文末[合规声明](#合规声明)。

## 快速开始

**Docker（推荐）**

```bash
docker run -d --name hd-iptv --restart unless-stopped \
  -p 9797:9797 \
  -v /vol2/1000/docker/hd-iptv/config:/app/config \
  -v /vol2/1000/docker/hd-iptv/output:/app/output \
  ghcr.io/lzp1314/hd-iptv:latest
```

打开 `http://NAS_IP:9797` 就是控制台。首次启动会自动写入默认配置并开始采集，
一两分钟后订阅地址就可以用了。

**源码跑**

```bash
git clone https://github.com/lzp1314/hd-iptv.git
cd hd-iptv
python -m iptvhd serve --port 9797
```

只用 Python 标准库，3.11 以上，不需要 pip install。

**播放器里填什么**

| 用途 | 地址 |
|---|---|
| M3U 订阅（主用） | `http://NAS_IP:9797/playlist.m3u` |
| TXT 订阅 | `http://NAS_IP:9797/txt` |
| 节目单 | `http://NAS_IP:9797/epg.xml` |
| 单台播放 | `http://NAS_IP:9797/live/cctv1.m3u8` |

订阅地址要以 `.m3u` 结尾。播放器会把 `.m3u8` 当成「单个直播流」解析，拿去当
订阅就会提示「播放格式不受支持」——观屿TV、TiviMate、影视仓、Kodi、PotPlayer
都适用这一条。

**哪些台没有**

央视频的公开频道就那些，下面这些**上游没有直播信号**，模板里已注释掉：
兵团、三沙、海峡、厦门、大湾区、延边、农林卫视。上海卫视是东方卫视的旧称、
福建卫视是东南卫视的旧称，已合成一条。想要这些台就自己接一个订阅源
（`config/sources.toml` 里的 `subscription` 段填地址即可）。

**为什么不内置「公开源 / 咪咕」当替补**：公开列表换台极快，拿到的多是**重新
转码的低码率流**，清晰度和稳定性都差一截，接进来只会让「卡」更严重；咪咕要
移动账号 + 签名 + DRM，匿名拿不到 1080P。真正的病根是本地请求打太密被上游按
IP 限频，所以这里选择把请求量压到最低（更新时零请求、播放时按需换票），而不是
换一堆更差的源。确实想要备用线路，`config/sources.toml` 里的 `subscription`
填你自己的订阅地址即可，会作为附加条目合并进来。

上游按 IP 限流：连续大量取流会被拒（403），恢复要按小时算。所以定时更新默认
24 小时一次，别调勤；取流是播放时按需换票的，列表旧一点不影响观看。一轮里偶尔
有七八个台拿不到地址也不会从列表里消失——上一轮取到过就沿用，播放时再换地址。

## 部署

### Docker Compose

```bash
git clone https://github.com/lzp1314/hd-iptv.git
cd hd-iptv
cp .env.example .env      # 按需改端口和目录，不改也能跑
docker compose up -d --build
```

`config/` 是空的也没关系，首次启动会自动补一份默认配置。

常用环境变量（`.env` 里写，或直接 `-e`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST_PORT` | `9797` | 宿主机端口，容器内始终是 9797 |
| `CONFIG_DIR` | `./config` | 配置目录，建议写绝对路径 |
| `OUTPUT_DIR` | `./output` | 结果目录，建议写绝对路径 |
| `PUBLIC_URL` | 空 | 对外地址。留空则按访问时的域名端口自动判断，套反代不用填 |
| `HDIPTV_INTERVAL_HOURS` | `24` | 自动刷新间隔（小时），0 关闭 |
| `HDIPTV_MIN_HEIGHT` / `HDIPTV_MAX_HEIGHT` | `720` / `1080` | 画质区间 |
| `HDIPTV_PLAY_MODE` | `direct` | `direct` 分片直连 CDN；`proxy` 分片经本机转发 |
| `HDIPTV_RELAY_CACHE_MB` | `96` | 分片缓存（MB），仅 `proxy` 模式 |

端口、刷新间隔、播放方式、对外地址都可以直接在面板上改，改完立刻生效，
不用重启容器。

**为什么分片要直连**：清单由 NAS 取（上游只放行它自己的 UA，播放器直接用自己
的 UA 去要 m3u8 会被回 403，这就是「列表里只有一个台能播」的来源），但清单里的
分片地址会换成上游绝对地址，让播放器自己连 CDN。实测如果连分片也经 NAS 转发，
平台会把这种「每片一次裸请求」判成异常流量回 403，画面就卡住；播放器直连带着
TLS 会话复用，反而稳定。个别播放器直连放不出来时，去面板把「播放方式」切成
「经 NAS 转发」再试。

### 外网访问（反向代理）

用 Lucky / Nginx / 群晖反代把 `9797` 映射出去就行，订阅地址就是
「外网域名 + 反代端口 + `/playlist.m3u`」，例如 `https://tv.example.com:8443/playlist.m3u`。

列表里的播放地址是按请求的 Host 现拼的（认 `X-Forwarded-Host` / `X-Forwarded-Proto`），
所以内网访问拿到内网地址、外网访问拿到外网地址，两边都能直接播。只有在反代
不透传 `Host` 头时，才需要去面板「运行设置」里手填一次对外地址。

反代记得关掉响应缓冲，否则直播会卡成幻灯片。

## 飞牛 fnOS

**A. 面板里贴 compose（不用 SSH）**

1. 文件管理建目录 `/vol2/1000/docker/hd-iptv/`，里面再建 `config`、`output`
2. Docker → Compose → 新建项目，项目目录选 `/vol2/1000/docker/hd-iptv`
3. compose 内容用仓库里的 `docker-compose.yml`，把 `build: .` 换成
   `image: ghcr.io/lzp1314/hd-iptv:latest`（面板里没有源码，构建不了）
4. 卷映射写**绝对路径**：`/vol2/1000/docker/hd-iptv/config:/app/config`
5. 启动，浏览器开 `http://飞牛IP:9797`

**B. SSH 拉源码构建（能改配置、升级方便）**

```bash
cd /vol2/1000/docker
git clone https://github.com/lzp1314/hd-iptv.git
cd hd-iptv
docker compose up -d --build
```

> **两个坑**
> 1. `docker restart` **不会**重新读卷映射，改过挂载路径必须删容器重建：
>    `docker compose up -d --force-recreate`。
> 2. 挂载目录如果不是你改的那个，配置根本不进容器。核对一下：
>    `docker inspect hd-iptv --format '{{json .Mounts}}'`。

## 镜像从哪来

| 位置 | 说明 |
|---|---|
| `ghcr.io/lzp1314/hd-iptv` | 多架构（`amd64` + `arm64`），tag 有 `latest` 和 `main` |
| 本地构建 | `docker compose up -d --build`，产出 `hd-iptv:local`，不依赖任何镜像仓库 |

GitHub Actions 在推 `main` 或打 `v*` 标签时自动构建。拉不动 `ghcr.io` 时用本地
构建那条，效果一样。

## 命令行

```bash
python -m iptvhd update          # 跑一次完整更新
python -m iptvhd update --quiet   # 只打印汇总
python -m iptvhd serve --port 9797
python -m iptvhd doctor           # 配置自检
python -m iptvhd probe <地址...>   # 单条验证：能不能播、多少 P、多快
python -m iptvhd --version
```

通用参数 `--root <目录>` 可以指向任意项目目录，用来跑多套配置。

## HTTP 接口

| 路径 | 说明 |
|---|---|
| `/` | 网页面板 |
| `/playlist.m3u` | M3U 订阅（`/m3u` 是别名） |
| `/txt` `/json` | TXT 订阅 / 频道明细 |
| `/epg.xml` | 节目单（只含自己的频道） |
| `/logo/<频道名>.png` | 台标，服务端取一次存本地 |
| `/live/<slug>.m3u8` | 单频道播放列表，服务端现换地址下发（分片由播放器直连） |
| `/relay/<slug>/p/<token>` | 仅 `proxy` 模式下使用，播放器自动请求，不用手填 |
| `/api/live` | 可用频道 slug 清单 |
| `/api/status` | 运行状态、统计、设置、最近日志 |
| `/api/channels` | 频道与源明细 |
| `/api/settings` | 读写面板设置（`POST` 生效） |
| `/api/version?check=1` | 当前版本，顺便跟仓库比一下 |
| `/api/relay` | 中转缓存与最近错误，排查播放问题用 |
| `POST /api/update` | 手动触发一次更新 |
| `/health` | 健康检查（给 Docker HEALTHCHECK 用） |

## 合规声明

本项目只是一个**播放列表管理工具**，不生产、不存储、不分发任何音视频内容，
仓库内不含任何频道地址或频道数据。

- 采集到的直播流版权归各电视台 / 平台所有，请仅用于个人学习与家庭内网自用
- 不要用于商业转播、公开分发或任何侵权用途
- 请遵守当地法律法规；部分内容可能存在地区限制或需要相应授权
- 推荐优先使用运营商 IPTV 或有授权的正规渠道

## License

[MIT](LICENSE)
