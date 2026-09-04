FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src/ src/

# fastapi / uvicorn は AS では本体依存（extra なし）
RUN pip install --no-cache-dir -e .

# 非 root で実行する。/data はボリュームのマウント先として先に作って chown する
# （named volume の初回マウント時にこの所有権が引き継がれる）。
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data \
 && chown -R app:app /app /data
USER app

ENV AS_DB_PATH=/data/assets.db

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8010/api/health', timeout=4).status==200 else 1)"]

CMD ["uvicorn", "asset_summary.web.app:app", "--host", "0.0.0.0", "--port", "8010"]
