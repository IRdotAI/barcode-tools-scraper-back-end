FROM python:3.12-slim

# curl_cffi needs libcurl — install build deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       gcc g++ libcurl4-openssl-dev libssl-dev \
       wget gnupg2 ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
