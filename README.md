# hd-iptv

**央视 + 卫视的高清直播，自动采集、自动测速、自动分发。零第三方依赖，一条命令跑在 NAS 上。**

[![Version](https://img.shields.io/badge/version-0.8.1-blue.svg)](https://github.com/lzp1314/hd-iptv)
[![CI](https://github.com/lzp1314/hd-iptv/actions/workflows/ci.yml/badge.svg)](https://github.com/lzp1314/hd-iptv/actions/workflows/ci.yml)
[![Docker](https://img.shields.io/badge/image-ghcr.io-blue?logo=docker)](https://github.com/lzp1314/hd-iptv/pkgs/container/hd-iptv)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

> 仅供个人学习与家庭内网自用，详见文末[合规声明](#合规声明)。

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

## ARM 板 / Armbian

支持。镜像有 `amd64`、`arm64`、`armv7` 三种，Armbian 上装好 Docker 直接拉就行：

```bash
curl -fsSL https://get.docker.com | sh     # 已经装了可跳过
uname -m                                   # aarch64 → arm64；armv7l → 32 位
docker compose up -d
```

绝大多数 Armbian 板子（RK3399 / RK3588 / 晶晨 S905 系列）是 **arm64**，直接可用；
玩客云、部分老盒子是 **armv7（32 位）**，镜像同样带了 armv7。程序是纯 Python
标准库、单进程，常驻内存约 50~80 MB，1 GB 内存的板子够用。

路径别照抄飞牛那套 `/vol2`，换成你自己的盘，比如 `/mnt/sda1/docker/hd-iptv`。
ARM 板 CPU 弱一点不影响播放：清单由 NAS 取、分片由播放器直连 CDN，NAS 这边
基本只做字符串改写。

## 镜像从哪来

| 位置 | 说明 |
|---|---|
| `ghcr.io/lzp1314/hd-iptv` | 多架构（`amd64` / `arm64` / `armv7`），tag 有 `latest` 和 `main` |
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
| `/live/<slug>.m3u8` | 单频道播放列表，服务端现换地址下发；响应头 `X-HD-Seq` 标出当前分片序号 |
| `/relay/<slug>/p/<token>` | 仅 `proxy` 模式下使用，播放器自动请求，不用手填 |
| `/api/live` | 可用频道 slug 清单 |
| `/api/status` | 运行状态、统计、设置、最近日志 |
| `/api/channels` | 频道与源明细（含 `hidden` 标记） |
| `/api/settings` | 读写面板设置（`POST` 生效） |
| `POST /api/hidden` | 隐藏 / 恢复频道，`{"names":["CCTV-2"],"hidden":true}` |
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
