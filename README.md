# hd-iptv

**自部署的 IPTV 播放列表管理工具：整理、测速、聚合与分发节目清单，供个人与家庭内网使用。零第三方依赖，一条命令跑在 NAS 上。**

[![Version](https://img.shields.io/badge/version-0.8.1-blue.svg)](https://github.com/lzp1314/hd-iptv)
[![CI](https://github.com/lzp1314/hd-iptv/actions/workflows/ci.yml/badge.svg)](https://github.com/lzp1314/hd-iptv/actions/workflows/ci.yml)
[![Docker](https://img.shields.io/badge/image-ghcr.io-blue?logo=docker)](https://github.com/lzp1314/hd-iptv/pkgs/container/hd-iptv)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

> 仅供个人学习与家庭内网自用，详见文末[免责声明](#免责声明)与 [DISCLAIMER.md](DISCLAIMER.md)。

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
ARM 板 CPU 弱一点不影响播放：清单由本机整理、分片由播放器直连 CDN，本机这边
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
