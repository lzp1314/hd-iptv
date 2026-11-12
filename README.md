# hd-iptv

**自部署的 IPTV 播放列表管理工具：整理、测速、聚合与分发节目清单，供个人与家庭内网使用。零第三方依赖，一条命令跑在 NAS 上。**

[![Version](https://img.shields.io/badge/version-0.9.17-blue.svg)](https://github.com/lzp1314/hd-iptv)
[![CI](https://github.com/lzp1314/hd-iptv/actions/workflows/ci.yml/badge.svg)](https://github.com/lzp1314/hd-iptv/actions/workflows/ci.yml)
[![Docker](https://img.shields.io/badge/image-ghcr.io-blue?logo=docker)](https://github.com/lzp1314/hd-iptv/pkgs/container/hd-iptv)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

> 仅供个人学习与家庭内网自用，详见文末[免责声明](#免责声明)与 [DISCLAIMER.md](DISCLAIMER.md)。

## 部署

### 先选一份部署文件

| 你的宽带 | 用哪份 |
|---|---|
| **湖北省内 · 移动宽带** | `docker-compose.hubei.yml` |
| **其他所有网络**（省外、电信 / 联通 / 广电、机房、海外） | `docker-compose.yml` |

非湖北省内用户，用通用版 `docker-compose.yml` 就行，什么都不用改。

选错了也不会坏，只是有部分频道出不来；改起来就是换一个 compose 文件。拿不准自己
是哪种，先用通用版跑起来，再看面板顶部那行「本机宽带」——显示湖北移动就换成另一份。

### 方式一：预构建镜像（推荐，不用 clone 源码）

任何能跑 Docker 的机器都适用：群晖、威联通、飞牛、极空间、绿联、Unraid、
树莓派 / Armbian、普通 Linux 服务器，以及装了 Docker Desktop 的 Windows / macOS。
不需要 Python 环境，依赖全在镜像里，多架构（`amd64` / `arm64` / `armv7`）自动匹配。

三步：建一个空目录进去 → 把下面的内容存成 `docker-compose.yml` → `docker compose up -d`。
`config/`、`output/` 会自动创建，不用手工建。

```bash
mkdir -p ~/hd-iptv && cd ~/hd-iptv
# 把下面这段存成这个目录里的 docker-compose.yml
```

```yaml
# hd-iptv —— 通用部署文件
# 存成 docker-compose.yml，与 config/、output/ 同级
#
# 非湖北省内用户用这一份。湖北省内移动宽带请改用 docker-compose.hubei.yml。

services:
  hd-iptv:
    # 预构建多架构镜像（amd64 / arm64 / armv7），自动匹配你的机器
    # 想锁死版本写 :0.9.17；想跟着修 bug 写 :latest
    image: ghcr.io/lzp1314/hd-iptv:latest
    container_name: hd-iptv
    restart: unless-stopped

    ports:
      # 左边是宿主机端口（冲突了改左边），右边固定 9797 不要动
      - "9797:9797"

    volumes:
      # 配置：频道模板 channels.txt、采集源 sources.toml、config.toml、
      # 面板设置 panel.json。建议换成自己盘上的绝对路径，例如
      #   /vol2/1000/docker/hd-iptv/config:/app/config
      - ./config:/app/config
      # 结果：playlist.m3u、txt、json、台标缓存 logos/
      - ./output:/app/output

    environment:
      TZ: Asia/Shanghai

      # 按名字跳过源，逗号分隔。这里跳过「运营商官方源」——它只服务湖北省内移动宽带。
      # 湖北省内移动宽带用户删掉这一行（或改成 "" ）即可，效果和湖北版一样。
      HDIPTV_SKIP_SOURCES: "运营商官方源"

      # 自动刷新间隔（小时）。上游按 IP 限频，别调太勤；0 = 关掉定时刷新。
      # 播放地址是按需换票的，列表过期不影响正在看的人。
      HDIPTV_INTERVAL_HOURS: "24"

      # 画质区间。**别把 MAX_HEIGHT 写成 1080**：有些源只给 2160 一路，
      # 卡住上限就把唯一能用的地址整条丢掉，退回 1Mbps 的中转空壳。
      HDIPTV_MIN_HEIGHT: "720"
      HDIPTV_MAX_HEIGHT: "2160"

      # direct = 清单本机下发、分片由播放器直连 CDN（最流畅，默认）
      # proxy  = 分片也经本机转发（只有直连放不出来时才用，吃本机带宽）
      HDIPTV_PLAY_MODE: "direct"

      # 分片缓存上限（MB），只对 proxy 模式生效
      HDIPTV_RELAY_CACHE_MB: "96"

      # 订阅里每个台写几行。1 = 不会出现「重复台」；
      # 想手动切备用线路再调 2~4（VLC 会把每行都显示成一个台）
      HDIPTV_LINES_PER_CHANNEL: "1"

      # 对外地址。留空 = 按访问时的域名端口自动判断，套了反代也不用填。
      # 只有「反代挂在子路径下、又没透传 X-Forwarded-Host」时才需要写死，
      # 例如 https://tv.example.com:8443 。**不要自己加密码段**，程序会接。
      PUBLIC_URL: ""

      # 访问密码（URL 里的一段路径）。设了之后所有地址都要带 /<密码>/，
      # 不带就回「身份认证失败」，局域网也一样。
      # 只能用字母、数字和 - _ . ~ ；留空 = 不设密码。
      # 也可以在面板「运行设置」里改，改完立刻生效、不用重启。
      HDIPTV_SECRET: ""

    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9797/health', timeout=3).status==200 else 1)"]
      interval: 30s
      timeout: 5s
      start_period: 20s
      retries: 3

    logging:
      # 不限制的话，长期运行的日志会把盘吃满
      driver: json-file
      options:
        max-size: "5m"
        max-file: "2"
```

存好之后启动：

```bash
docker compose up -d
```

#### 湖北省内移动宽带：换一份 YAML

仓库里有一份 `docker-compose.hubei.yml`，直接用这份就行。或者把上面那份里的这一行
**删掉**（值改成空字符串也一样）：

```yaml
      HDIPTV_SKIP_SOURCES: "运营商官方源"        # ← 删掉这一行
```

其余一个字都不用改。

`docker-compose.hubei.yml` 是源码构建版，里面写的是 `build: .` + `image: hd-iptv:local`，
用预构建镜像的话把这两行换成：

```yaml
    image: ghcr.io/lzp1314/hd-iptv:latest
```

这份刻意不读 `.env` 里的同名变量，免得通用版的设置把它要启用的源跳掉。

浏览器打开 `http://你的IP:9797` 就是面板。第一次进去列表是空的，点「立即更新」，
等一两分钟出结果。`config/`、`output/` 空着没关系，容器会自己建并补一份默认配置。

确认真的起来了：

```bash
docker compose ps          # STATUS 显示 Up (healthy) 即正常
docker compose logs -f     # 看采集日志，Ctrl+C 退出
```

#### 拉不动镜像怎么办

`ghcr.io` 在国内部分网络下会超时，表现为 `pull access denied` 或卡在
`Waiting`。三个办法，按省事程度排：

1. **换个网络重试**：手机热点、给路由器换 DNS，往往一次就过；
2. **给 Docker 配代理**：设置 `HTTP_PROXY` / `HTTPS_PROXY` 后重新 `pull`；
3. **改用源码构建**：见下面「方式二」，完全不依赖镜像仓库，代价是多花几分钟。

方式二同样要拉 Python 基础镜像（走 Docker Hub），若 Docker Hub 也慢，先配镜像加速器。

#### 湖北省内移动宽带：建议改用 host 网络

默认的桥接网络在部分 NAS 上拿不到 IPv6 路由，运营商官方源会用不了。改法是把端口
映射换成 host 网络：

```yaml
    # 删掉 ports: 那三行，换成下面这行
    network_mode: host
```

两点注意：

- host 模式下容器直接用宿主机网络，`ports` 必须删掉，否则报错或端口冲突；
- host 模式下 `127.0.0.1:9797` 仍然可用，健康检查不用改。

公开源、央视频接口这类 IPv4 可达的源，桥接模式就够了，不必开 host。

### 方式二：从源码构建

```bash
git clone https://github.com/lzp1314/hd-iptv.git
cd hd-iptv
cp .env.example .env      # 按需改端口和目录，不改也能跑

# 非湖北省内用户：用这份（默认，自动跳过运营商官方源）
docker compose up -d --build

# 湖北省内移动宽带：换成这份（不跳过官方源）
docker compose -f docker-compose.hubei.yml up -d --build
```

两个文件内容完全独立，**换一份的时候先 `docker compose down`**，否则同名容器会
打架。`config/` 是挂载卷，换哪份都不丢配置；空着也没关系，首次启动会自动补一份
默认配置。

常用环境变量（方式二写在 `.env` 里；方式一直接在 YAML 的 `environment` 段改）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST_PORT` | `9797` | 宿主机端口，容器内始终是 9797 |
| `CONFIG_DIR` | `./config` | 配置目录，建议写绝对路径 |
| `OUTPUT_DIR` | `./output` | 结果目录，建议写绝对路径 |
| `PUBLIC_URL` | 空 | 对外地址。留空则按访问时的域名端口自动判断，套反代不用填 |
| `HDIPTV_INTERVAL_HOURS` | `24` | 自动刷新间隔（小时），0 关闭 |
| `HDIPTV_MIN_HEIGHT` / `HDIPTV_MAX_HEIGHT` | `720` / `2160` | 画质区间 |
| `HDIPTV_PLAY_MODE` | `direct` | `direct` 分片直连 CDN；`proxy` 分片经本机转发 |
| `HDIPTV_RELAY_CACHE_MB` | `96` | 分片缓存（MB），仅 `proxy` 模式 |
| `HDIPTV_LINES_PER_CHANNEL` | `1` | 订阅里每个台写几行。1 不会出现「重复台」；想手动切备用线路调 2~4 |
| `HDIPTV_SECRET` | 空 | 访问密码（URL 路径段）。设了之后所有地址都要带 `/<密码>/`，见下 |
| `HDIPTV_SKIP_SOURCES` | 空 | 按名字跳过源，逗号分隔。通用版 `docker-compose.yml` 里设成 `运营商官方源` |
| `HDIPTV_REGION` | 空 | 手动指定省份（拼音如 `hubei`）。只在自动识别判错时才需要 |
| `HDIPTV_ISP` | 空 | 手动指定运营商：`mobile` / `telecom` / `unicom` / `broadnet` |

端口、刷新间隔、播放方式、对外地址都可以直接在面板上改，改完立刻生效，
不用重启容器。

### 面板上那一行「本机宽带」

启动几秒后，面板顶部会多一行：

```
本机宽带：湖北 · 中国移动 · IPv6 可用
```

它是程序自己查出来的（`myip.ipip.net` 为主、`ipinfo.io` 兜底，结果在
`output/region.json` 缓存一天），**只用于提示，不参与采集**——识别不出来就
不显示，一个台也不会少。

这行是给你判断该用哪份部署文件的：

- 显示「湖北 · 中国移动」→ 换成 `docker-compose.hubei.yml`
- 显示别的 → 现在这份通用版就是对的
- 显示「没有 IPv6 出口」→ 运营商官方源那类地址用不了，其余频道不受影响

识别出别的省份时，这行会顺带说一句：自带的运营商源只管湖北省内移动宽带，你这边要是
也开了运营商 IPTV，得自己把清单放进 `config/`（见下文「运营商官方源」末段）。**程序
不会替你去网上找本省的源**——各省地址形态不一样，猜错的代价是整批超时，不如把这句话
说清楚，谁有需要谁自己接。

判断不准的时候（跨省出口、机房 IP、CGNAT 会被判错），手动钉死：

```yaml
      HDIPTV_REGION: "hubei"
      HDIPTV_ISP: "mobile"
```

或者写进 `config.toml` 的 `[output]` 段：`region = "hubei"`、`isp = "mobile"`。

### 访问密码

给「反代到公网」的场景准备的：设一个密码，所有地址都要带它，不带就只显示
「身份认证失败」——局域网也一样。

把上面 `docker-compose.yml` 里这一行改成你的密码（或者留着空，启动后在面板里设）：

```yaml
      HDIPTV_SECRET: "tv7Kd2x"
```

设完之后地址变成：

| 用途 | 地址 |
|---|---|
| 面板 | `http://NAS:9797/tv7Kd2x/` |
| 订阅 | `http://NAS:9797/tv7Kd2x/playlist.m3u` |
| 单台 | `http://NAS:9797/tv7Kd2x/live/<slug>.m3u8` |
| 台标 | `http://NAS:9797/tv7Kd2x/logo/CCTV1.png` |

几点要知道：

- **清单里的地址会自动带上密码段**，播放器拿到订阅就能直接播，不用二次配置。
  台标、节目单同理——它们都从同一个对外地址派生。
- **`PUBLIC_URL` 不用自己加密码段**，程序会接上；已经写了也不会重复接。
- **只能用字母、数字和 `- _ . ~`**，别带斜杠和空格（带了会直接报错，不会被悄悄改掉）。
- **它不是加密**：密码在 URL 里，会进浏览器历史、反代日志。走 HTTPS，
  并且别把订阅链接发给不相干的人。

### 在面板里改密码

「运行设置 → 访问密码」直接改，存盘后立刻生效，**不用重启容器**，改完页面会自动
跳到新地址。留空并保存 = 关掉密码，回到根地址。

优先级是 **面板 > `HDIPTV_SECRET` > `config.toml`**。面板改的值存在
`config/panel.json`——这个文件在 `.gitignore` 里，**不会跟着仓库走**，也不会被
镜像更新覆盖（它在挂载卷里）。所以别把密码写进 `config.toml`：那份是要提交、
要分享的，密码会一起流出去。

忘了密码，两种救法：

1. 进 NAS 删掉 `config/panel.json` 里的 `"secret"` 这一行，重启容器；
2. 或者把 `HDIPTV_SECRET` 从 compose 里去掉——但面板里的值优先级更高，第 1 种更直接。

## 运营商官方源（仅限湖北省内移动宽带）

> ⚠️ **这一节只对湖北省内移动宽带有意义。** 别的地方开着它不会报错，只是那部分
> 频道出不来，白等一阵、面板上多一片灰。通用版 `docker-compose.yml` 已经默认
> 把它跳过了，不用你操心。

公开聚合源的通病是「能用但随时会坏」：转发站会夹广告、会限速、会突然改成购物
频道。真正稳的是自家宽带的 IPTV 直播边缘——不换票、不夹广告、1080P、余量也够。

项目自带一份湖北省内移动的清单（全省都算，不只武汉），**在湖北版里默认启用**，
排在所有公开源前面：

```
config/sources.toml -> [[source]] name = "运营商官方源"  type = "local"
                       path = "iptvhd/defaults/hubei-mobile.m3u"  trust = true
```

用别的省份 / 别的运营商：把 `path` 换成自己的 m3u 就行，`trust = true` 保留。

下面三件事项目已经自动处理，不用改配置：

- **只下发 IPv6 边缘。** 这类源一律由本机中转（清单和分片都过 NAS），播放器有没有
  IPv6 都无所谓，`mode = "direct"` 也不用改。
- **不能并发探测。** 一口气几十条请求会被 CDN 打进「302 无限循环」，之后连单条都
  取不到。带 `trust` 的源走独立的串行慢探（`probe.official_gap`，默认 1.2 秒一条），
  失败率过半就整批旁路，等下一轮。
- **账号是共享的。** 清单里的账号来自公开列表，别人也在用；对方跑批量任务时这路会
  一起抖。真要紧的话换成自己机顶盒里的账号串。

另外 0.9.13 起会自动剔除「一址多台」的广告源：探测时记录每条流**跟随跳转后的
最终地址**，凡是三个以上的不同频道落到同一个地址（央视那批 720P 全都 302 到
`/applive/mkt.m3u8`，同一路购物直播），整批丢弃。

### 本省有没有运营商源？一条命令自测

换个省、换家宽带，最想知道的是「我这儿能不能也接本地源」。跑这个：

```bash
docker compose exec hd-iptv python -m iptvhd doctor --isp
# 源码跑：python -m iptvhd doctor --isp
```

只读诊断，不改配置、不采集。输出长这样：

```
本机宽带   湖北 · 中国移动 · IPv6 可用    来源 ipip
入口自测
  ztecdn.hb.chinamobile.com:6060            通 · 10ms
公开清单      取到 5 条可直连入口（抽样）
结论：湖北 · 中国移动能连上运营商入口，这条路值得接。
```

判断依据就三样：本机出口是哪省哪家、这一类入口通不通、社区有没有整理出本省那份
清单。三种结局对应三条路——**通**就存成 m3u 加一条 `local` 源；**只有组播**得先用
udpxy 转；**连清单都没有**基本就是没有对外单播地址，别白折腾。

## 飞牛 fnOS

**A. 面板里贴 compose（不用 SSH）**

1. 文件管理建目录 `/vol2/1000/docker/hd-iptv/`，里面再建 `config`、`output`
2. Docker → Compose → 新建项目，项目目录选 `/vol2/1000/docker/hd-iptv`
3. compose 内容直接用上面「方式一」那份完整 YAML（面板里没有源码，构建不了）。
   **湖北省内移动宽带记得删掉 `HDIPTV_SKIP_SOURCES` 那一行**（或直接用
   `docker-compose.hubei.yml`），否则官方源那批频道不会出现。
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

支持。镜像有 `amd64`、`arm64`、`armv7` 三种，Armbian 上装好 Docker 直接拉就行。
把上面「方式一」的 `docker-compose.yml` 存到板子上任意目录，然后：

```bash
curl -fsSL https://get.docker.com | sh     # 已经装了可跳过
uname -m                                   # aarch64 → arm64；armv7l → 32 位
docker compose up -d
```

绝大多数 Armbian 板子（RK3399 / RK3588 / 晶晨 S905 系列）是 **arm64**，直接可用；
玩客云、部分老盒子是 **armv7（32 位）**，镜像同样带了 armv7。程序是纯 Python
标准库、单进程，常驻内存约 50~80 MB，1 GB 内存的板子够用。

路径别照抄飞牛那套 `/vol2`，换成你自己的盘，比如 `/mnt/sda1/docker/hd-iptv`。
ARM 板 CPU 弱一点不影响播放：清单由本机整理、分片由播放器直连 CDN，本机这边
基本只做字符串改写。

## 镜像从哪来

| 位置 | 说明 |
|---|---|
| `ghcr.io/lzp1314/hd-iptv` | 多架构（`amd64` / `arm64` / `armv7`）。`latest` 跟主干；`0.9.17` / `0.9` 是打标签时的固定版本，想锁死或回滚用这个；`main` 每次推主干都会更新 |
| 本地构建 | `docker compose up -d --build`，产出 `hd-iptv:local`，不依赖任何镜像仓库 |

GitHub Actions 在推 `main` 或打 `v*` 标签时自动构建。拉了 `latest` 想升级就重新
`docker compose pull && docker compose up -d`；想停在某个版本就把 `image` 写成
`:0.9.17`，之后 pull 也不会动。拉不动 `ghcr.io` 时用本地构建那条，效果一样。

## 命令行

```bash
python -m iptvhd update          # 跑一次完整更新
python -m iptvhd update --quiet   # 只打印汇总
python -m iptvhd serve --port 9797
python -m iptvhd doctor           # 配置自检
python -m iptvhd doctor --isp     # 本省运营商的 IPTV 入口通不通（只读诊断）
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
| `/api/status` | 运行状态、统计、设置、最近日志，以及本机宽带识别结果（`region`） |
| `/api/channels` | 频道与源明细（含 `hidden` 标记） |
| `/api/settings` | 读写面板设置（`POST` 生效） |
| `POST /api/hidden` | 隐藏 / 恢复频道，`{"names":["CCTV-2"],"hidden":true}` |
| `/api/version?check=1` | 当前版本，顺便跟仓库比一下 |
| `/api/relay` | 中转缓存与最近错误，排查播放问题用 |
| `POST /api/update` | 手动触发一次更新 |
| `/health` | 健康检查（给 Docker HEALTHCHECK 用） |

设了 `HDIPTV_SECRET` 之后，除 `/health` 外的每条路径前面都要加 `/<密码>`，
例如 `/tv7Kd2x/playlist.m3u`。`/health` 保持公开，否则容器的健康检查会把自己
判成不健康。

## 免责声明

> [!WARNING]
> - 本仓库代码按 [MIT](LICENSE) 授权。以下各条针对的是经本软件接触到的第三方内容、数据与账号的权利，属于法律与合规提示，**不是对软件本身的附加许可条件**。
> - 本项目是**自托管的软件工具**，不托管第三方音视频节目；仓库及运行时可能整理互联网上公开可访问的播放地址与频道元数据，但「公开可访问」「技术上可播放」「通过可用性检查」**均不代表内容属于公有领域，也不代表已获得复制、转播或商业使用的授权**。
> - 音视频内容、频道标识、平台名称和商标的权利归各自平台、制作者及其他权利人所有；文中提及第三方名称仅为说明兼容性，不表示合作、认可、授权或隶属关系。MIT 许可只适用于本仓库代码，**不授予任何第三方内容、商标、接口或数据的使用权**。
> - 使用者应自行确认所在地区法律、内容授权范围和各平台用户协议，只接入本人拥有、合法订阅或已获明确授权的来源；不得利用本项目未经授权绕过登录、付费、DRM、地域限制或其他访问控制，不得共享、盗用或转售账号与凭据。
> - 互联网来源可能随时变更、下架或被限制，本项目不对其合法性、准确性、持续可用性或适销性作任何保证。**部署者对其自行添加、启用、公开访问和转发的来源承担全部责任**。

本项目是**自部署的播放列表管理工具**，本身不生产、不存储、不分发任何音视频内容，仓库内也不包含任何频道地址或节目数据。

- 所整理的节目清单及相关内容的版权均归各电视台 / 内容平台所有，本项目仅作个人学习与技术研究之用，请勿将其用于商业转播、公开传播或任何可能侵犯第三方权益的用途
- 推荐优先使用运营商 IPTV 或已获授权的正规渠道
- 使用本项目即表示你已知悉并自愿承担由此产生的一切后果，作者不对任何不当使用负责

完整的免责说明与风险披露请参阅 [DISCLAIMER.md](DISCLAIMER.md)。

## 项目立场

hd-iptv 是为个人学习、研究和家庭内网自用而做的**非营利项目，不是为了卖钱**。

- **不支持、不参与任何商业售卖**，包括把本项目或其修改版连同频道源预装进电视盒子、电视棒等设备出售，或以本项目为基础售卖订阅、账号或「包年直播」服务。相关责任由售卖者自行承担。
- **不接受售卖方的赞助**；已收到的任何赞助都是自愿、无条件的捐赠，不代表项目认可、授权或参与赞助者的任何产品或经营行为。
- **不得冒用项目名义宣传**，请勿使用「hd-iptv」项目名称、仓库链接或作者身份，暗示商品或服务获得了「官方授权」「官方合作」或「官方版本」。

## 版权与侵权处理

如果你是内容权利人或其合法代理人，并认为本仓库收录的某个地址、频道元数据或相关规则侵犯了你的合法权益，请通过 [GitHub Issue](https://github.com/lzp1314/hd-iptv/issues/new) 提交通知，标题注明「版权 / 侵权」，并尽量提供：

1. 权利人及联系人身份、有效联系方式，以及代理关系说明（如适用）；
2. 涉嫌侵权的具体作品、频道、URL、仓库文件或模块名称；
3. 权利归属或授权范围的必要证明；
4. 希望采取的处理方式，以及你确认通知内容真实准确的声明。

项目维护者收到信息完整、能够明确定位的通知后，会在合理时间内核验，并视情况移除地址、停用相关模块或采取其他必要措施。请勿在公开 Issue 中提交身份证件、账号密码、Cookie、Token 等敏感信息；需要补充非公开材料时，可先在 Issue 中留下不含敏感信息的联系方式。

## License

[MIT](LICENSE)
