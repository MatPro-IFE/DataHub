# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

FROM python:3.14-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
RUN groupadd --system --gid 10001 datahub && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /tmp datahub
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY datahub_sdk/ datahub_sdk/
RUN pip install --no-cache-dir .
COPY ml-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ml-api/app.py .
RUN chmod -R a+rX /app
USER 10001
EXPOSE 8090
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]
