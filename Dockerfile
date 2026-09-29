FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
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
EXPOSE 8000 8501
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
