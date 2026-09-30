# syntax=docker/dockerfile:1.7

# Both bases are build arguments so the same recipe produces the portable CPU
# image and an NVIDIA CUDA image. See deploy/CONTAINERS.md for exact commands.
ARG BUILD_IMAGE=ubuntu:24.04
ARG RUNTIME_IMAGE=ubuntu:24.04

FROM node:22-bookworm-slim AS ui-builder
WORKDIR /source/ui
COPY ui/package.json ui/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY ui/ ./
RUN npm run build

FROM ${BUILD_IMAGE} AS build-base
ARG DEBIAN_FRONTEND=noninteractive
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      cmake \
      git \
      libcurl4-openssl-dev \
      ninja-build \
      pkg-config \
      python3 \
      python3-venv

FROM build-base AS app-builder
WORKDIR /source
COPY . .
COPY --from=ui-builder /source/src/engrai_server/static/ ./src/engrai_server/static/
RUN python3 -m venv /build/venv \
    && /build/venv/bin/pip install --disable-pip-version-check --no-cache-dir --upgrade pip \
    && /build/venv/bin/pip wheel --no-cache-dir --wheel-dir /wheels .

FROM build-base AS runtime-builder
ARG ENGRAI_BACKEND=cpu
ARG BUILD_JOBS=4
ARG CUDA_ARCHITECTURES=
WORKDIR /source
COPY scripts/fetch-engine-sources.py scripts/build-runtime.py ./scripts/
COPY runtime/ ./runtime/
COPY third_party/README.md ./third_party/README.md
RUN python3 scripts/fetch-engine-sources.py
RUN case "${ENGRAI_BACKEND}" in \
      cpu|cuda|rocm|vulkan) ;; \
      *) echo "unsupported Linux container backend: ${ENGRAI_BACKEND}" >&2; exit 2 ;; \
    esac \
    && if [ -n "${CUDA_ARCHITECTURES}" ]; then \
      python3 scripts/build-runtime.py \
        --engine llama.cpp \
        --backend "${ENGRAI_BACKEND}" \
        --jobs "${BUILD_JOBS}" \
        --build-dir /build/engrai-text \
        --output-dir /runtime-bundles \
        --cmake-option="-DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCHITECTURES}"; \
    else \
      python3 scripts/build-runtime.py \
        --engine llama.cpp \
        --backend "${ENGRAI_BACKEND}" \
        --jobs "${BUILD_JOBS}" \
        --build-dir /build/engrai-text \
        --output-dir /runtime-bundles; \
    fi
RUN if [ -n "${CUDA_ARCHITECTURES}" ]; then \
      python3 scripts/build-runtime.py \
        --engine stable-diffusion.cpp \
        --backend "${ENGRAI_BACKEND}" \
        --jobs "${BUILD_JOBS}" \
        --build-dir /build/engrai-image \
        --output-dir /runtime-bundles \
        --cmake-option="-DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCHITECTURES}"; \
    else \
      python3 scripts/build-runtime.py \
        --engine stable-diffusion.cpp \
        --backend "${ENGRAI_BACKEND}" \
        --jobs "${BUILD_JOBS}" \
        --build-dir /build/engrai-image \
        --output-dir /runtime-bundles; \
    fi

FROM ${RUNTIME_IMAGE} AS final
ARG DEBIAN_FRONTEND=noninteractive
ARG OCI_VERSION=0.1.0
LABEL org.opencontainers.image.title="ENGRAI SERVER" \
      org.opencontainers.image.description="Sovereign inference control plane with private native workers" \
      org.opencontainers.image.source="https://github.com/va-desai-dev/ENGRAISERVER" \
      org.opencontainers.image.version="${OCI_VERSION}" \
      org.opencontainers.image.licenses="LicenseRef-ENGRAI"

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      libcurl4 \
      libgomp1 \
      python3 \
      python3-venv \
      tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 engrai \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /var/lib/engrai --shell /usr/sbin/nologin engrai \
    && mkdir -p /models /opt/engrai/runtime-bundles /var/lib/engrai \
    && chown -R 10001:10001 /models /var/lib/engrai

COPY --from=app-builder /wheels/ /tmp/wheels/
RUN python3 -m venv /opt/engrai/venv \
    && /opt/engrai/venv/bin/pip install \
      --disable-pip-version-check \
      --no-cache-dir \
      --no-index \
      --find-links=/tmp/wheels \
      engrai-server \
    && rm -rf /tmp/wheels

COPY --from=runtime-builder /runtime-bundles/ /opt/engrai/runtime-bundles/
COPY deploy/container-entrypoint.sh /usr/local/bin/engrai-container-entrypoint

ENV PATH="/opt/engrai/venv/bin:${PATH}" \
    HOME=/var/lib/engrai \
    ENGRAI_HOME=/var/lib/engrai \
    ENGRAI_BIND_HOST=0.0.0.0 \
    ENGRAI_BIND_PORT=8400 \
    ENGRAI_ENGINE_HOST=127.0.0.1 \
    ENGRAI_MODEL_SEARCH_ROOTS=/models \
    ENGRAI_MODEL_DOWNLOAD_DIR=/models/.huggingface/hub \
    HF_HOME=/models/.huggingface \
    ENGRAI_INSTALL_BUNDLED_RUNTIMES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
WORKDIR /var/lib/engrai
VOLUME ["/var/lib/engrai", "/models"]
EXPOSE 8400
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["/opt/engrai/venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8400/healthz', timeout=3).read()"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/engrai-container-entrypoint"]
CMD ["engrai-server", "serve"]
