# CarSaathi (Phase A owner app): one container serves the API and the built React app on the same origin.
# Build from the repository root:  docker build -f owner/Dockerfile -t carsaathi .
# Only the files below are copied, so local node_modules/ or dist/ folders never reach the image.

# ---- 1. the web app
FROM node:20-alpine AS web
WORKDIR /web
COPY owner-app/package.json owner-app/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY owner-app/index.html owner-app/vite.config.js ./
COPY owner-app/public ./public
COPY owner-app/src ./src
RUN npm run build

# ---- 2. the API
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data OWNER_STATIC_DIR=/app/owner-app/dist
COPY owner/requirements.txt ./owner/requirements.txt
RUN pip install -r owner/requirements.txt
COPY owner ./owner
COPY --from=web /web/dist ./owner-app/dist
# unprivileged user; /data is only used when no PostgreSQL URL is set (development fallback)
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data
USER app
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.getenv('PORT','10000'), timeout=4).status == 200 else 1)"
# Render sets $PORT. Client addresses for rate limits come from X-Forwarded-For only as far as OWNER_PROXY_HOPS trusts it.
CMD ["sh", "-c", "exec uvicorn --factory owner.api:create_app --host 0.0.0.0 --port ${PORT:-10000} --no-server-header --timeout-graceful-shutdown 10"]
