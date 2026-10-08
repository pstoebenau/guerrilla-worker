# syntax=docker/dockerfile:1
ARG CUDA_VERSION=12.8.1
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu24.04 AS lichtfeld-build
ARG LICHTFELD_COMMIT=d8c50c6a3e2273cb74130a6e9023de8d068af52d
ARG VCPKG_COMMIT=58845ed63eb19aff55e896ea1f5d51f2a0df5b66
ARG BUILD_JOBS=4
ENV DEBIAN_FRONTEND=noninteractive VCPKG_ROOT=/opt/vcpkg \
    CC=gcc-14 CXX=g++-14 VCPKG_FORCE_SYSTEM_BINARIES=1 \
    VCPKG_MAX_CONCURRENCY=${BUILD_JOBS}
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git curl unzip zip tar pkg-config python3 python3-dev python3-venv \
    gcc-14 g++-14 ccache ninja-build nasm autoconf autoconf-archive automake libtool \
    libxinerama-dev libxcursor-dev xorg-dev libglu1-mesa-dev libwayland-dev \
    libxkbcommon-dev libegl-dev libdecor-0-dev libibus-1.0-dev libdbus-1-dev \
    libsystemd-dev libgtk-3-dev bison flex && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/build-tools && /opt/build-tools/bin/pip install --no-cache-dir cmake==3.31.6
ENV PATH=/opt/build-tools/bin:${PATH}
RUN git init /opt/vcpkg && git -C /opt/vcpkg remote add origin https://github.com/microsoft/vcpkg.git \
    && git -C /opt/vcpkg fetch origin ${VCPKG_COMMIT} && git -C /opt/vcpkg checkout --detach FETCH_HEAD \
    && /opt/vcpkg/bootstrap-vcpkg.sh -disableMetrics \
    && printf '\nset(VCPKG_BUILD_TYPE release)\n' >> /opt/vcpkg/triplets/x64-linux.cmake
RUN git init /src/lichtfeld && git -C /src/lichtfeld remote add origin https://github.com/MrNeRF/LichtFeld-Studio.git \
    && git -C /src/lichtfeld fetch --depth 1 origin ${LICHTFELD_COMMIT} \
    && git -C /src/lichtfeld checkout --detach FETCH_HEAD \
    && git -C /src/lichtfeld submodule update --init --recursive --depth 1
WORKDIR /src/lichtfeld
RUN --mount=type=cache,target=/root/.cache/vcpkg \
    cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_PORTABLE=ON \
      -DBUILD_TESTS=OFF -DBUILD_PYTHON_STUBS=OFF -DBUILD_CUDA_MIN_SM=75 \
      -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
      -DLFS_DEV_IMPORT_SOURCE_PYTHON=OFF -DLFS_DEV_IMPORT_SOURCE_RESOURCES=OFF \
    && cmake --build build --parallel ${BUILD_JOBS} \
    && cmake --install build --prefix /opt/lichtfeld

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

FROM python-runtime AS legal-bundle
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

FROM python-runtime AS engine-runtime
COPY --from=lichtfeld-build /opt/lichtfeld /opt/lichtfeld
ENV LICHTFELD_BIN=/opt/lichtfeld/bin/run_lichtfeld.sh
# The NVIDIA driver is injected by `docker run --gpus`, not by `docker build`.
RUN test -x /opt/lichtfeld/bin/run_lichtfeld.sh && test -s /opt/lichtfeld/bin/LichtFeld-Studio \
    && python -c "import pycolmap; assert pycolmap.__version__ == '4.0.2'; assert pycolmap.has_cuda"

FROM engine-runtime AS runtime
COPY pipeline/*.py pipeline/scan-settings.schema.json ./
LABEL org.opencontainers.image.title="Video or photos to SPZ" \
      org.opencontainers.image.version="lichtfeld-0.5.3-colmap-4.0.2" \
      org.opencontainers.image.description="Automatic image selection, COLMAP with model retries, LichtFeld default training, SPZ export"
ENTRYPOINT ["python", "/opt/pipeline/splat_pipeline.py"]
CMD ["--help"]

FROM oven/bun:1.4.2 AS worker-bundle
WORKDIR /app
COPY package.json bun.lock ./
COPY packages/protocol packages/protocol
RUN bun install --frozen-lockfile
COPY agent agent
RUN bun build agent/main.ts --target=node --outfile=/app/worker.mjs

FROM node:22.22.0-bookworm-slim AS node-runtime
FROM engine-runtime AS worker
ARG SPIRULA_VERSION=2026.9.30
ARG SPIRULA_SHA256=123d6d0b826388abb64129b6fcf2a8d34fe0662a57ba34e53212a148c891431d
ARG DENSIFICATION_PLUGIN_COMMIT=ab0b04e35b12bff65ee87bdaacfa3177c21521d6
ARG ROMAV2_SHA256=3516ccdbbd8eb89d50dfc0bc4562ccdcc2c60b7908e1819d5aae0cbe1bf979bc
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip git ffmpeg vulkan-tools mesa-vulkan-drivers libopengl0 libgfortran5 libgomp1 libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/*
# Portable upstream packaging omits this library needed by its Python module.
COPY --from=lichtfeld-build /src/lichtfeld/build/Build/lib/libOpenMeshTools.so.11.0 /opt/lichtfeld/lib/
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
RUN mkdir -p /opt/spirula && curl -fL --retry 3 \
      https://github.com/harry7557558/spirula-studio/releases/download/v${SPIRULA_VERSION}/spirula-${SPIRULA_VERSION}-ubuntu-vulkan-x86_64.zip \
      -o /tmp/spirula.zip \
    && echo "${SPIRULA_SHA256}  /tmp/spirula.zip" | sha256sum -c - \
    && unzip -q /tmp/spirula.zip -d /opt/spirula && rm /tmp/spirula.zip \
    && executable="$(find /opt/spirula -type f -name spirula | head -1)" \
    && test -n "$executable" && chmod +x "$executable" \
    && if [ "$executable" != /opt/spirula/spirula ]; then ln -s "$executable" /opt/spirula/spirula; fi
RUN mkdir -p /opt/plugins \
    && git clone https://github.com/shadygm/lichtfeld-densification-plugin.git /opt/plugins/densification \
    && git -C /opt/plugins/densification checkout --detach ${DENSIFICATION_PLUGIN_COMMIT} \
    && git -C /opt/plugins/densification submodule update --init --recursive
COPY pipeline/docker/worker-requirements.txt /opt/pipeline/worker-requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install -r /opt/pipeline/worker-requirements.txt
RUN mkdir -p /opt/models/hub/checkpoints \
    && curl -fL --retry 3 https://github.com/Parskatt/RoMaV2/releases/download/weights/romav2.pt \
       -o /opt/models/hub/checkpoints/romav2.pt \
    && echo "${ROMAV2_SHA256}  /opt/models/hub/checkpoints/romav2.pt" | sha256sum -c -
# RoMa's pinned DINOv3 feature backbone loads repository code through torch.hub.
# Bake that source too, so a cold worker needs no first-job GitHub download.
ARG DINOV3_COMMIT=adc254450203739c8149213a7a69d8d905b4fcfa
ARG DINOV3_SHA256=923e23a8cea28c9255fb3c2674ecea3dafc9dcc23259754ab7b91dd02f14d38a
RUN curl -fL --retry 3 https://github.com/facebookresearch/dinov3/zipball/${DINOV3_COMMIT} \
      -o /tmp/dinov3.zip \
    && echo "${DINOV3_SHA256}  /tmp/dinov3.zip" | sha256sum -c - \
    && unzip -q /tmp/dinov3.zip -d /tmp/dinov3 \
    && mv /tmp/dinov3/facebookresearch-dinov3-* /opt/models/hub/facebookresearch_dinov3_${DINOV3_COMMIT} \
    && touch /opt/models/hub/trusted_list \
    && rm /tmp/dinov3.zip && rmdir /tmp/dinov3
COPY pipeline/*.py pipeline/scan-settings.schema.json /opt/pipeline/
COPY --from=worker-bundle /app/worker.mjs /opt/worker/worker.mjs
COPY --from=legal-bundle /notices/ /usr/share/doc/guerrilla-worker/
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics,video \
    LICHTFELD_DENSIFICATION_PLUGIN=/opt/plugins/densification \
    SPIRULA_BIN=/opt/spirula/spirula \
    PYTHONPATH=/opt/lichtfeld/bin:/opt/plugins \
    LD_LIBRARY_PATH=/opt/lichtfeld/lib \
    TORCH_HOME=/opt/models \
    WORKER_AGENT_LOCK_DIRECTORY=/gpu \
    GPU_LOCK_PATH=/gpu/pipeline.lock
RUN pip freeze --all > /opt/pipeline/python-environment.txt \
    && python -c "import hashlib,json; from pathlib import Path; json.dump({'spirulaRelease':'v${SPIRULA_VERSION}','reconstructionEngine':'guerrilla-pycolmap','densificationPlugin':'${DENSIFICATION_PLUGIN_COMMIT}','romaWeightsSha256':'${ROMAV2_SHA256}','dinov3Commit':'${DINOV3_COMMIT}','dinov3SourceSha256':'${DINOV3_SHA256}','pythonEnvironmentSha256':hashlib.sha256(Path('/opt/pipeline/python-environment.txt').read_bytes()).hexdigest()},open('/opt/pipeline/runtime-versions.json','w'))" \
    && /opt/spirula/spirula train --help >/dev/null \
    && node --version
ENTRYPOINT ["node", "/opt/worker/worker.mjs"]
CMD []

# Export release material from the exact build, before publishing its image.
FROM lichtfeld-build AS build-source-evidence
COPY scripts/build_source_bundle.py /tmp/build_source_bundle.py
RUN python3 /tmp/build_source_bundle.py

FROM worker AS image-notice-evidence
COPY scripts/image_notices.py /tmp/image_notices.py
RUN mkdir -p /release-evidence \
    && python /tmp/image_notices.py > /release-evidence/binary-notices.tar.gz \
    && dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\n' > /release-evidence/os-packages.tsv \
    && cp /opt/pipeline/python-environment.txt /opt/pipeline/runtime-versions.json /release-evidence/

FROM scratch AS release-evidence
COPY --from=build-source-evidence /release-evidence/ /
COPY --from=image-notice-evidence /release-evidence/ /

# Preserve the worker as the default target for ordinary docker builds.
FROM worker AS default-worker
