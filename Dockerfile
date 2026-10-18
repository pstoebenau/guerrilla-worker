# syntax=docker/dockerfile:1
ARG WORKER_RUNTIME_IMAGE=guerrilla-runtime:local
FROM ${WORKER_RUNTIME_IMAGE} AS engine-runtime

FROM rust:1.99.0-bookworm AS worker-bundle
ARG WORKER_VERSION
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY rust rust
COPY packages/protocol/scan-settings.schema.json packages/protocol/scan-settings.schema.json
RUN cargo build --release --locked --workspace \
    && if [ -n "$WORKER_VERSION" ]; then test "$(target/release/guerrilla-worker --version)" = "guerrilla-worker $WORKER_VERSION"; fi

FROM python:3.12.15-slim-bookworm AS legal-bundle
COPY . /source/
RUN python /source/scripts/package_notices.py /source /notices

FROM engine-runtime AS worker
COPY --from=worker-bundle /app/target/release/guerrilla-worker /usr/local/bin/guerrilla-worker
COPY --from=worker-bundle /app/target/release/guerrilla-pipeline /usr/local/bin/guerrilla-pipeline
COPY --from=legal-bundle /notices/ /usr/share/doc/guerrilla-worker/
ENV WORKER_PIPELINE_BIN=/usr/local/bin/guerrilla-pipeline \
    WORKER_AGENT_LOCK_DIRECTORY=/gpu GPU_LOCK_PATH=/gpu/pipeline.lock
LABEL org.opencontainers.image.title="Guerrilla Worker" \
    org.opencontainers.image.description="Native Rust worker with Spirula Vulkan and dense reconstruction"
ENTRYPOINT ["guerrilla-worker"]
CMD ["run"]

FROM worker AS package-evidence
RUN mkdir -p /release-evidence \
    && dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\n' > /release-evidence/os-packages.tsv
FROM python:3.12.15-slim-bookworm AS image-notice-evidence
COPY --from=worker /usr/share/doc /image/usr/share/doc
COPY --from=worker /opt/spirula /image/opt/spirula
COPY --from=worker /opt/converter/node_modules /image/opt/converter/node_modules
COPY --from=package-evidence /release-evidence /release-evidence
COPY scripts/image_notices.py /tmp/image_notices.py
RUN python /tmp/image_notices.py /image > /release-evidence/binary-notices.tar.gz
FROM scratch AS release-evidence
COPY --from=image-notice-evidence /release-evidence/ /
FROM worker AS default-worker
