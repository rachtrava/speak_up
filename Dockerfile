FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 COMMONS_HOST=0.0.0.0 COMMONS_DB=/data/commons.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY commons ./commons
COPY scripts ./scripts
COPY tests ./tests
RUN useradd -m commons && mkdir -p /data && chown commons /data
USER commons
EXPOSE 8000
CMD ["python", "-m", "commons", "serve"]
