# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
RUN groupadd --system --gid 10001 datahub && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /tmp datahub
COPY pyproject.toml README.md ./
COPY datahub_sdk/ datahub_sdk/
RUN pip install --no-cache-dir .
COPY dashboard/requirements.txt dashboard/requirements.txt
RUN pip install --no-cache-dir -r dashboard/requirements.txt
COPY dashboard/ dashboard/
RUN chmod -R a+rX /app
USER 10001
EXPOSE 8080
CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
