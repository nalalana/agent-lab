# syntax=docker/dockerfile:1

# Tencent AGSX Code Interpreter base image.
# Production builds must use an approved immutable linux/amd64 digest.
ARG AGSX_BASE_IMAGE=ccr.ccs.tencentyun.com/ags-image/sandbox-code@sha256:21b067114e8615b607b7b377e7a2e5ef8e6160c6525059f1d9628a9ec637003a
FROM ${AGSX_BASE_IMAGE}

ARG IMAGE_CREATED="unknown"
ARG IMAGE_REVISION="unknown"

USER root
WORKDIR /

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg

LABEL org.opencontainers.image.title="touming-agsx-code-interpreter" \
      org.opencontainers.image.description="Touming production AGSX isolated Python code interpreter" \
      org.opencontainers.image.source="touming" \
      org.opencontainers.image.created="${IMAGE_CREATED}" \
      org.opencontainers.image.revision="${IMAGE_REVISION}" \
      com.touming.component="agent-runtime-sandbox" \
      com.touming.environment="production" \
      com.touming.schema-version="1"

# The backend requirements must not be installed here:
# - Django, Celery, Redis and database clients run in the backend service.
# - e2b-code-interpreter is the backend-side AGSX client.
# - COS upload/download is handled by the backend artifact store.
#
# The Tencent base image already provides the Python/Jupyter execution services
# and the data-processing packages required by the current python.execute tool.
# Validate them during the build so an incompatible base-image update fails
# before it reaches production.
RUN set -eux; \
    test "$(uname -m)" = "x86_64"; \
    test "$(id -u)" = "0"; \
    command -v python; \
    command -v bash; \
    command -v mkdir; \
    python -m pip check; \
    python -c "import IPython, ipykernel, requests, yaml"; \
    python -c "import numpy, pandas, scipy, sklearn, pyarrow"; \
    python -c "import matplotlib, PIL, openpyxl, docx"; \
    python -c "from pathlib import Path; print('AGSX Python dependencies verified')"; \
    install -d -o root -g root -m 1777 /tmp/touming-runs

# Do not define ENTRYPOINT or CMD here.
# The Tencent base image ENTRYPOINT ["/init"] must be preserved so its S6,
# envd, commands.run, files.read/write and run_code services start correctly.
# The AGSX Tool request explicitly supplies:
#   Command: ["/init"]
#   Args: ["sleep", "infinity"]
