FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data HF_HOME=/data/hf
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-ml.txt ./
RUN pip install -r requirements.txt pytesseract Pillow
# For BGE/E5 embeddings, build with: --build-arg WITH_ML=1
ARG WITH_ML=0
RUN if [ "$WITH_ML" = "1" ]; then pip install sentence-transformers; fi
COPY app ./app
COPY ui ./ui
COPY data ./data
COPY scripts ./scripts
COPY firmware ./firmware
# Phase 4: compile the AEB C code and its test mutants for software-in-the-loop runs, then drop the compiler
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && python scripts/sil_run.py build --mutants \
    && apt-get purge -y --auto-remove gcc libc6-dev && rm -rf /var/lib/apt/lists/*
# run as an unprivileged user; only /data is writable
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data
USER app
VOLUME /data
EXPOSE 8000 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=4).status == 200 else 1)"
# --proxy-headers: trust X-Forwarded-For only from the reverse proxy (see --forwarded-allow-ips) so per-client limits are correct
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]
