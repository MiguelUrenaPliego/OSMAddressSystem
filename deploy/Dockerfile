# Community address app. Build from code/OSMAddressSystem:  docker build -f deploy/Dockerfile -t address-app .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY osmaddresssystem ./osmaddresssystem
RUN pip install ".[app,planning,server]"
COPY projects ./projects
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && useradd -m app && mkdir /data && chown app /data
USER app
ENV ADDRESS_PROJECT=/data/project ADDRESS_TEMPLATE=/app/projects/fotouni PORT=8000
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/health')"
ENTRYPOINT ["/entrypoint.sh"]
