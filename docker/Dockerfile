FROM python:3.11-slim

WORKDIR /srv/sse-agent

COPY pyproject.toml ./
COPY app ./app
COPY config ./config
COPY run.py ./

# docker CLI 不打进镜像：运行时挂载宿主机 /usr/bin/docker + docker.sock（ADR-6）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple .

EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8100/health/live',timeout=3)"

CMD ["python", "run.py"]
