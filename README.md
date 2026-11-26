# hd-iptv

**自部署的 IPTV 播放列表管理工具：整理、测速、聚合与分发节目清单，供个人与家庭内网使用。零第三方依赖，一条命令跑在 NAS 上。**

[![Version](https://img.shields.io/badge/version-0.11.3-blue.svg)](https://github.com/lzp1314/hd-iptv)
[![CI](https://github.com/lzp1314/hd-iptv/actions/workflows/ci.yml/badge.svg)](https://github.com/lzp1314/hd-iptv/actions/workflows/ci.yml)
[![Docker](https://img.shields.io/badge/image-ghcr.io-blue?logo=docker)](https://github.com/lzp1314/hd-iptv/pkgs/container/hd-iptv)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

> 仅供个人学习与家庭内网自用，详见 [DISCLAIMER.md](DISCLAIMER.md)。

## 一、先选部署文件

| 你的宽带 | 用哪一份 |
|---|---|
| **湖北省内 · 移动宽带** | `docker-compose.hubei.yml` |
| **其他所有网络**（省外、电信 / 联通 / 广电、机房、海外） | `docker-compose.yml` |

两份都是预构建镜像版，不依赖源码、不需要 `Dockerfile`，只有这段文本也能跑，可以直接贴进飞牛 / 群晖的 Docker 面板。

选错了不会坏，只是会有部分频道出不来；改回来就是换一个文件。拿不准先用通用版，启动后看面板顶部那行「本机宽带」——显示湖北移动，再换成湖北版。

## 二、部署

### 1. 建目录

```bash
mkdir -p ~/hd-iptv && cd ~/hd-iptv
```

### 2. 拿 compose 文件

能直连 GitHub 就用命令：

湖北省内移动宽带：

```bash
curl -fsSL -O https://raw.githubusercontent.com/lzp1314/hd-iptv/main/docker-compose.hubei.yml
```

其他所有网络：

```bash
curl -fsSL -O https://raw.githubusercontent.com/lzp1314/hd-iptv/main/docker-compose.yml
```

拉不下来（国内网络访问 `raw.githubusercontent.com` 经常超时）就手工建文件，把下面**对应那一份**完整复制进去，文件名按小标题里写的存。

两份只有一行不同，就是 `HDIPTV_SKIP_SOURCES`。

#### 外省版 · `docker-compose.yml`

```yaml
services:
  hd-iptv:
    image: ghcr.io/lzp1314/hd-iptv:${HDIPTV_IMAGE_TAG:-latest}
    container_name: ${CONTAINER_NAME:-hd-iptv}
    restart: unless-stopped

    ports:
      - "${HOST_PORT:-9797}:9797"

    volumes:
      - ${CONFIG_DIR:-./config}:/app/config
      - ${OUTPUT_DIR:-./output}:/app/output

    environment:
      TZ: ${TZ:-Asia/Shanghai}
      # 两份文件唯一的差别就是这一行，原样保留
      HDIPTV_SKIP_SOURCES: "${HDIPTV_SKIP_SOURCES-运营商官方源}"
      HDIPTV_INTERVAL_HOURS: ${HDIPTV_INTERVAL_HOURS:-24}
      HDIPTV_MIN_HEIGHT: ${HDIPTV_MIN_HEIGHT:-720}
      HDIPTV_MAX_HEIGHT: ${HDIPTV_MAX_HEIGHT:-2160}
      HDIPTV_PLAY_MODE: ${HDIPTV_PLAY_MODE:-direct}
      HDIPTV_RELAY_CACHE_MB: ${HDIPTV_RELAY_CACHE_MB:-96}
      HDIPTV_YSP_IPV4: "${HDIPTV_YSP_IPV4:-0}"
      HDIPTV_YSP_IP_MODE: "${HDIPTV_YSP_IP_MODE:-auto}"
      HDIPTV_MIGU_IP_MODE: "${HDIPTV_MIGU_IP_MODE:-auto}"
      HDIPTV_OFFICIAL_IP_MODE: "${HDIPTV_OFFICIAL_IP_MODE:-auto}"
      HDIPTV_AGG_IP_MODE: "${HDIPTV_AGG_IP_MODE:-auto}"
      HDIPTV_KIDS_IP_MODE: "${HDIPTV_KIDS_IP_MODE:-auto}"
      HDIPTV_YSP_PLAIN: "${HDIPTV_YSP_PLAIN:-1}"
      HDIPTV_WARM_NEXT: "${HDIPTV_WARM_NEXT:-1}"
      PUBLIC_URL: "${PUBLIC_URL:-}"
      HDIPTV_SECRET: "${HDIPTV_SECRET:-}"
      HDIPTV_AUTO_REGION: "${HDIPTV_AUTO_REGION:-1}"

    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9797/health', timeout=3).status==200 else 1)"]
      interval: 30s
      timeout: 5s
      start_period: 20s
      retries: 3

    logging:
      driver: json-file
      options:
        max-size: "5m"
        max-file: "2"
```

#### 湖北版 · `docker-compose.hubei.yml`

```yaml
services:
  hd-iptv:
    image: ghcr.io/lzp1314/hd-iptv:${HDIPTV_IMAGE_TAG:-latest}
    container_name: ${CONTAINER_NAME:-hd-iptv}
    restart: unless-stopped

    ports:
      - "${HOST_PORT:-9797}:9797"

    volumes:
      - ${CONFIG_DIR:-./config}:/app/config
      - ${OUTPUT_DIR:-./output}:/app/output

    environment:
      TZ: ${TZ:-Asia/Shanghai}
      # 两份文件唯一的差别就是这一行，原样保留
      HDIPTV_SKIP_SOURCES: "聚合源-公开转播,聚合源-精简线路"
      HDIPTV_INTERVAL_HOURS: ${HDIPTV_INTERVAL_HOURS:-24}
      HDIPTV_MIN_HEIGHT: ${HDIPTV_MIN_HEIGHT:-720}
      HDIPTV_MAX_HEIGHT: ${HDIPTV_MAX_HEIGHT:-2160}
      HDIPTV_PLAY_MODE: ${HDIPTV_PLAY_MODE:-direct}
      HDIPTV_RELAY_CACHE_MB: ${HDIPTV_RELAY_CACHE_MB:-96}
      HDIPTV_YSP_IPV4: "${HDIPTV_YSP_IPV4:-0}"
      HDIPTV_YSP_IP_MODE: "${HDIPTV_YSP_IP_MODE:-auto}"
      HDIPTV_MIGU_IP_MODE: "${HDIPTV_MIGU_IP_MODE:-auto}"
      HDIPTV_OFFICIAL_IP_MODE: "${HDIPTV_OFFICIAL_IP_MODE:-auto}"
      HDIPTV_AGG_IP_MODE: "${HDIPTV_AGG_IP_MODE:-auto}"
      HDIPTV_KIDS_IP_MODE: "${HDIPTV_KIDS_IP_MODE:-auto}"
      HDIPTV_YSP_PLAIN: "${HDIPTV_YSP_PLAIN:-1}"
      HDIPTV_WARM_NEXT: "${HDIPTV_WARM_NEXT:-1}"
      PUBLIC_URL: "${PUBLIC_URL:-}"
      HDIPTV_SECRET: "${HDIPTV_SECRET:-}"
      HDIPTV_AUTO_REGION: "${HDIPTV_AUTO_REGION:-1}"

    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9797/health', timeout=3).status==200 else 1)"]
      interval: 30s
      timeout: 5s
      start_period: 20s
      retries: 3

    logging:
      driver: json-file
      options:
        max-size: "5m"
        max-file: "2"
```

### 3. 启动

其他所有网络：

```bash
docker compose up -d
```

湖北省内移动宽带：

```bash
docker compose -f docker-compose.hubei.yml up -d
```

`config/`、`output/` 会自动创建，不用手工建。

### 4. 打开面板

浏览器访问 `http://你的NAS地址:9797`。

第一次进去列表是空的，点一次「立即更新」，等一两分钟出结果。

确认真的起来了：

```bash
docker compose ps
docker compose logs -f
```

`STATUS` 显示 `Up (healthy)` 即正常。

## 三、配置

能改的东西分两处：compose 里的环境变量，和面板里的「运行设置」。**面板优先**，改完立刻生效，不用重启容器。

### 面板里改（推荐）

| 位置 | 可以改什么 |
|---|---|
| 运行设置 | 端口以外的一切：刷新间隔、播放方式、对外地址、访问密码、清单刷新、缓存大小 |
| 宽带与省份 | 自动识别开关，或手动指定省份 / 运营商 |
| 数据源 | 哪些源参与采集、出现在订阅里 |

### 环境变量

写进 `docker-compose.yml` 的 `environment` 段，改完 `docker compose up -d` 生效。

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST_PORT` | `9797` | 宿主机端口，容器内始终是 9797 |
| `CONFIG_DIR` | `./config` | 配置目录，建议改成绝对路径 |
| `OUTPUT_DIR` | `./output` | 结果目录，建议改成绝对路径 |
| `HDIPTV_SECRET` | 空 | 访问密码（URL 路径段），见下 |
| `PUBLIC_URL` | 空 | 对外地址。留空按访问时的域名端口自动判断，套反代不用填 |
| `HDIPTV_INTERVAL_HOURS` | `24` | 自动刷新间隔（小时），0 关闭。外网使用建议 4~6 |
| `HDIPTV_MIN_HEIGHT` / `HDIPTV_MAX_HEIGHT` | `720` / `2160` | 画质区间。别把上限写成 1080 |
| `HDIPTV_PLAY_MODE` | `direct` | `direct` 分片由播放器直连；`proxy` 分片也经本机转发 |
| `HDIPTV_RELAY_CACHE_MB` | `96` | 分片缓存（MB），仅 `proxy` 模式 |
| `HDIPTV_*_IP_MODE` | `auto` | 各模块走哪个地址族：`auto` / `ipv4` / `ipv6`。面板里改更方便，这里只是兜底 |
| `HDIPTV_YSP_IPV4` / `HDIPTV_YSP_PLAIN` | `0` / `1` | 兼容开关，正常别动。某类频道整片 403 时再逐个试 |
| `HDIPTV_WARM_NEXT` | `1` | 起播时顺手预热下一个台，关掉写 `0` |
| `HDIPTV_LINES_PER_CHANNEL` | `1` | 订阅里每个台写几行。1 不重复；想要手动切线路写 2~4 |
| `HDIPTV_AUTO_REGION` | `1` | 自动按省取源，写 `0` 关闭 |
| `HDIPTV_REGION` | 空 | 手动指定省份（如 `hubei`），留空自动识别 |
| `HDIPTV_ISP` | 空 | 手动指定运营商：`mobile` / `telecom` / `unicom` / `broadnet` |

### 访问密码

设了之后所有地址都要带密码段，不带就回「身份认证失败」，局域网也一样。

```yaml
environment:
  HDIPTV_SECRET: "tv7Kd2x"
```

设完之后地址变成：

| 用途 | 地址 |
|---|---|
| 面板 | `http://NAS:9797/tv7Kd2x/` |
| 订阅 | `http://NAS:9797/tv7Kd2x/playlist.m3u` |
| 单台 | `http://NAS:9797/tv7Kd2x/live/<slug>.m3u8` |
| 台标 | `http://NAS:9797/tv7Kd2x/logo/CCTV1.png` |

只能用字母、数字和 `- _ . ~`。它不是加密，密码在 URL 里会进浏览器历史和反代日志——走 HTTPS，别把订阅链接发给不相干的人。

忘了密码：删掉 `config/panel.json` 里的 `"secret"` 那一行，重启容器。

## 四、湖北省内移动宽带

用 `docker-compose.hubei.yml`。除此之外建议把网络改成 host 模式：默认的桥接网络在部分 NAS 上拿不到 IPv6 路由，会导致一部分频道出不来。

```yaml
services:
  hd-iptv:
    network_mode: host
```

改的时候删掉原来的 `ports` 段，两者不能并存。host 模式下 `127.0.0.1:9797` 仍然可用，健康检查不用改。

其他省份和网络类型不需要开 host。

## 五、飞牛 fnOS

**面板里贴 compose（不用 SSH）**

1. 文件管理建目录 `/vol2/1000/docker/hd-iptv/`，里面再建 `config`、`output`
2. Docker → Compose → 新建项目，项目目录选 `/vol2/1000/docker/hd-iptv`
3. compose 内容贴第二部分里对应那一份的全文（湖北省内移动宽带贴「湖北版」）
4. 卷映射写**绝对路径**：`/vol2/1000/docker/hd-iptv/config:/app/config`
5. 启动，浏览器开 `http://飞牛IP:9797`

**SSH 拉源码构建**

```bash
cd /vol2/1000/docker
git clone https://github.com/lzp1314/hd-iptv.git
cd hd-iptv
docker compose -f docker-compose.build.yml up -d --build
```

> **两个坑**
> 1. `docker restart` 不会重新读卷映射，改过挂载路径必须删容器重建：`docker compose up -d --force-recreate`
> 2. 挂载目录如果不是你改的那个，配置根本不进容器。核对：`docker inspect hd-iptv --format '{{json .Mounts}}'`

## 六、ARM 板 / Armbian

支持。镜像有 `amd64`、`arm64`、`armv7` 三种。

```bash
curl -fsSL https://get.docker.com | sh
uname -m
docker compose up -d
```

绝大多数 Armbian 板子（RK3399 / RK3588 / 晶晨 S905 系列）是 `arm64`；玩客云、部分老盒子是 `armv7`。常驻内存约 50~80 MB，1 GB 内存的板子够用。

路径别照抄飞牛的 `/vol2`，换成自己的盘，例如 `/mnt/sda1/docker/hd-iptv`。

## 七、升级与回滚

```bash
docker compose pull
docker compose up -d
```

想锁死在某个版本，把 compose 里的 `:latest` 改成具体版本号，之后 `pull` 也不会动。

从源码构建的用 `docker compose -f docker-compose.build.yml up -d --build`，产出 `hd-iptv:local`，不依赖任何镜像仓库。

### 拉不动镜像

`ghcr.io` 在国内部分网络下会超时，表现为 `pull access denied` 或卡在 `Waiting`。三个办法：

1. 换个网络重试：手机热点、给路由器换 DNS，往往一次就过；
2. 给 Docker 配代理：设置 `HTTP_PROXY` / `HTTPS_PROXY` 后重新 `pull`；
3. 改用源码构建：`docker-compose.build.yml`，不依赖 GHCR，代价是要 clone 完整源码。

## 八、命令行

```bash
python -m iptvhd update
python -m iptvhd update --quiet
python -m iptvhd serve --port 9797
python -m iptvhd doctor
python -m iptvhd probe <地址...>
python -m iptvhd --version
```

`--root <目录>` 可以指向任意项目目录，用来跑多套配置。

## 九、HTTP 接口

| 路径 | 说明 |
|---|---|
| `/` | 网页面板 |
| `/playlist.m3u` | M3U 订阅（`/m3u` 是别名） |
| `/txt` `/json` | TXT 订阅 / 频道明细 |
| `/epg.xml` | 节目单（只含自己的频道） |
| `/logo/<频道名>.png` | 台标，服务端取一次存本地 |
| `/live/<slug>.m3u8` | 单频道播放列表，服务端现换地址下发 |
| `/relay/<slug>/p/<token>` | 仅 `proxy` 模式下使用，播放器自动请求 |
| `/api/live` | 可用频道 slug 清单 |
| `/api/status` | 运行状态、统计、设置、最近日志 |
| `/api/channels` | 频道与源明细 |
| `/api/settings` | 读写面板设置（`POST` 生效） |
| `POST /api/hidden` | 隐藏 / 恢复频道 |
| `/api/version?check=1` | 当前版本，顺便跟仓库比一下 |
| `/api/relay` | 中转缓存与最近错误，排查播放问题用 |
| `POST /api/update` | 手动触发一次更新 |
| `/health` | 健康检查（给 Docker HEALTHCHECK 用） |

设了 `HDIPTV_SECRET` 之后，除 `/health` 外每条路径前面都要加 `/<密码>`。

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

项目维护者收到信息完整、能够明确定位的通知后，会在合理时间内核验，并视情况移除地址、停用相关模块或采取其他必要措施。请勿在公开 Issue 中提交身份证件、账号密码、Cookie、Token 等敏感信息。

## License

[MIT](LICENSE)
