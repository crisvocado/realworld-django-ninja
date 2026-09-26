FROM python:3.12-slim

WORKDIR /workspace

RUN rm -rf /var/lib/apt/lists/* \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::No-Cache=true update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        make \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv
