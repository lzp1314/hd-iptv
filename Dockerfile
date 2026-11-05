FROM python:3.12-slim

LABEL org.opencontainers.image.title="hd-iptv" \
      org.opencontainers.image.description="央视 / 卫视 1080P 直播源自动采集、清洗、测速、分发" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai

# slim 镜像里没有 zoneinfo，只设 TZ 不会生效，日志会走 UTC、比北京时间慢 8 小时
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 只拷贝运行所需文件；项目零第三方依赖，无需 pip install
COPY iptvhd ./iptvhd
COPY web ./web
COPY config ./config
COPY examples ./examples

# 配置目录会被宿主机挂载覆盖。iptvhd/defaults 里带了一份同样的模板，
# 挂载成空目录时程序启动会自动把默认配置写回去，不至于跑出 0 个频道。
RUN mkdir -p /app/output /app/config

EXPOSE 9797

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9797/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "iptvhd", "serve", "--host", "0.0.0.0", "--port", "9797"]
