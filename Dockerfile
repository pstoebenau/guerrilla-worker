# syntax=docker/dockerfile:1
ARG CUDA_VERSION=12.8.1
ARG WORKER_RUNTIME_IMAGE=guerrilla-runtime:local
FROM ${WORKER_RUNTIME_IMAGE} AS engine-runtime

FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu24.04 AS python-runtime
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility HOME=/tmp/splat-home \
    PATH=/opt/venv/bin:${PATH}
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv ca-certificates libglib2.0-0t64 libgomp1 libgl1 \
    libgtk-3-0t64 libx11-6 libxext6 libxrender1 libxi6 libxrandr2 libxinerama1 \
    libxcursor1 libsm6 libice6 libwayland-client0 libwayland-cursor0 libwayland-egl1 \
    libxkbcommon0 libegl1 libdecor-0-0 libdbus-1-3 libvulkan1 \
    && rm -rf /var/lib/apt/lists/* && python3 -m venv /opt/venv
COPY pipeline/docker/requirements.txt /opt/pipeline/requirements.txt
RUN pip install --no-cache-dir -r /opt/pipeline/requirements.txt
WORKDIR /opt/pipeline

FROM engine-runtime AS legal-bundle
ARG REQUIRE_RELEASE_LICENSE=0
COPY . /source/
RUN if [ "$REQUIRE_RELEASE_LICENSE" = "1" ]; then \
      python /source/scripts/package_notices.py /source /notices --require-license; \
    else python /source/scripts/package_notices.py /source /notices; fi

# Fast CPU/input tests without compiling LichtFeld. The final target is below.
FROM python-runtime AS test
COPY pipeline/*.py ./
COPY pipeline/scan-settings.schema.json ./
COPY pipeline/tests/ tests/
RUN python -m unittest discover -s tests -v

# Legacy standalone pipeline entry point, using the prebuilt runtime too.
FROM engine-runtime AS runtime
COPY pipeline/*.py pipeline/scan-settings.schema.json ./
ENTRYPOINT ["python", "/opt/pipeline/splat_pipeline.py"]
CMD ["--help"]

FROM oven/bun:1.4.2 AS worker-bundle
WORKDIR /app
COPY package.json bun.lock ./
COPY packages/protocol packages/protocol
RUN bun install --frozen-lockfile
COPY agent agent
RUN bun build agent/main.ts --target=node --outfile=/app/worker.mjs

# Only these layers change with worker or pipeline edits.
FROM engine-runtime AS worker
COPY pipeline/*.py pipeline/scan-settings.schema.json /opt/pipeline/
COPY --from=worker-bundle /app/worker.mjs /opt/worker/worker.mjs
COPY --from=legal-bundle /notices/ /usr/share/doc/guerrilla-worker/
LABEL org.opencontainers.image.title="Guerrilla Worker" \
    org.opencontainers.image.description="Independent Spirula and LichtFeld workflows with splat-transform exports"
ENTRYPOINT ["node", "/opt/worker/worker.mjs"]
CMD []

# Engine source assets are verified and attached directly by the release job.
FROM worker AS image-notice-evidence
COPY scripts/image_notices.py /tmp/image_notices.py
RUN mkdir -p /release-evidence \
    && python /tmp/image_notices.py > /release-evidence/binary-notices.tar.gz \
    && dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\n' > /release-evidence/os-packages.tsv \
    && cp /opt/pipeline/python-environment.txt /opt/pipeline/runtime-versions.json /release-evidence/

FROM scratch AS release-evidence
COPY --from=image-notice-evidence /release-evidence/ /

# Preserve the worker as the default target for ordinary docker builds.
FROM worker AS default-worker
