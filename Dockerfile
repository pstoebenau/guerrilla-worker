# syntax=docker/dockerfile:1
ARG WORKER_RUNTIME_IMAGE=guerrilla-runtime:local
FROM ${WORKER_RUNTIME_IMAGE} AS engine-runtime

FROM python:3.12.15-slim-bookworm AS test
WORKDIR /opt/pipeline
COPY packaging/python-base.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt
COPY pipeline/*.py pipeline/*.json ./
COPY pipeline/tests/ tests/
RUN python -m unittest discover -s tests -v

FROM rust:1.99.0-bookworm AS worker-bundle
ARG WORKER_VERSION
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY rust rust
COPY pipeline/scan-settings.schema.json pipeline/scan-settings.schema.json
RUN cargo build --release --locked --workspace \
    && if [ -n "$WORKER_VERSION" ]; then test "$(target/release/guerrilla-worker --version)" = "guerrilla-worker $WORKER_VERSION"; fi

FROM engine-runtime AS legal-bundle
COPY . /source/
RUN python /source/scripts/package_notices.py /source /notices

# The prebuilt runtime contains both engines, their plugin and model assets.
# Worker source layers add only the native agent/pipeline and Python helpers.
FROM engine-runtime AS worker
COPY pipeline/*.py pipeline/scan-settings.schema.json /opt/pipeline/
COPY --from=worker-bundle /app/target/release/guerrilla-worker /usr/local/bin/guerrilla-worker
COPY --from=worker-bundle /app/target/release/guerrilla-pipeline /usr/local/bin/guerrilla-pipeline
COPY --from=legal-bundle /notices/ /usr/share/doc/guerrilla-worker/
ENV PYTHON=/opt/venv/bin/python PIPELINE_ROOT=/opt/pipeline \
    WORKER_PIPELINE_BIN=/usr/local/bin/guerrilla-pipeline \
    WORKER_AGENT_LOCK_DIRECTORY=/gpu GPU_LOCK_PATH=/gpu/pipeline.lock
LABEL org.opencontainers.image.title="Guerrilla Worker" \
    org.opencontainers.image.description="Headless Rust worker with bundled Spirula and LichtFeld engines"
ENTRYPOINT ["guerrilla-worker"]
CMD ["run"]

FROM worker AS image-notice-evidence
COPY scripts/image_notices.py /tmp/image_notices.py
RUN mkdir -p /release-evidence \
    && python /tmp/image_notices.py > /release-evidence/binary-notices.tar.gz \
    && dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\n' > /release-evidence/os-packages.tsv \
    && cp /opt/pipeline/python-environment.txt /opt/pipeline/runtime-versions.json /release-evidence/
FROM scratch AS release-evidence
COPY --from=image-notice-evidence /release-evidence/ /
FROM worker AS default-worker
