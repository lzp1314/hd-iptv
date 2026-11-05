FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 只拷贝运行所需文件；项目零第三方依赖，无需 pip install
COPY iptvhd ./iptvhd
COPY web ./web
COPY config ./config
COPY examples ./examples

RUN mkdir -p /app/output

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "iptvhd", "serve", "--host", "0.0.0.0", "--port", "8080"]
