# syntax=docker/dockerfile:1.7
# ==============================================================================
# AIRS - AI Resume Screening Platform
# Multi-stage image for linux/arm64 (AWS Graviton, Ampere, Apple Silicon).
#
# FILENAME IS LOAD-BEARING - do not rename this to `Dockerfile`.
# jenkins-shared-lib/vars/deployToDev.groovy (dev branch) builds with a
# hardcoded `--file arm.Dockerfile` and aborts the pipeline if the file is
# absent. It is not configurable through the `deployToDev(...)` map, so this
# name is what the CD pipeline requires. There is deliberately no second
# `Dockerfile` alongside it: UMS keeps both and they have already drifted
# apart (its arm.Dockerfile carries an extra mariadb-connector-c-dev the
# other lacks). One file, no drift.
#
# Build from a non-arm64 host (e.g. Docker Desktop on Windows/amd64) - buildx
# emulates aarch64 via QEMU, which is correct but slow; torch dominates:
#
#     docker buildx build --platform linux/arm64 -f arm.Dockerfile -t airs:latest --load .
#
# Build natively on an arm64 host:
#
#     docker build -f arm.Dockerfile -t airs:latest .
#
# One image, four roles - selected by the command (see docker-entrypoint.sh):
#
#     docker run --env-file .env -p 8002:8002 airs:latest api
#     docker run --env-file .env                airs:latest worker
#     docker run --env-file .env                airs:latest beat
#     docker run --env-file .env                airs:latest migrate
#
# NOTE: the `migrate` role will currently FAIL on this repo - alembic's
# revision graph is broken (revision 7b3f6a92e1c4 is defined in two files, and
# b6dda6ad1824 lists a merge parent 9a1c2f3e6b7d that no file defines). That
# is a codebase problem, not an image problem; the role is wired and ready for
# once the graph is repaired.
# ==============================================================================

ARG PYTHON_VERSION=3.11
ARG BASE_IMAGE=python:${PYTHON_VERSION}-slim-bookworm


# ------------------------------------------------------------------------------
# Stage 1: builder - compile every dependency into a self-contained virtualenv.
#
# Kept separate so the toolchain (build-essential, libpq-dev, ~400MB) never
# reaches the runtime image; only the finished /opt/venv is copied forward.
# ------------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# build-essential + python3-dev: for any sdist with no prebuilt aarch64 wheel.
#   Most of this set (numpy, scipy, scikit-learn, pandas, lxml, cryptography,
#   tokenizers, safetensors, pillow, torch) does publish manylinux aarch64
#   wheels, so in practice almost nothing compiles - but arm64 wheel coverage
#   is thinner than x86_64 and one missing wheel would fail the build outright.
# libpq-dev: psycopg2-binary bundles its own libpq, but this keeps the build
#   working unchanged if anyone swaps back to source psycopg2.
# libffi-dev / libssl-dev: cffi and cryptography.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        python3-dev \
        libpq-dev \
        libffi-dev \
        libssl-dev \
        pkg-config

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

RUN python -m pip install --upgrade pip setuptools wheel

# torch on linux/arm64 - INSTALLED FIRST, AND FROM PYTORCH'S CPU INDEX.
#
# It is tempting to assume the aarch64 wheel is CPU-only because there is no
# consumer GPU on arm64. It is not: PyPI's aarch64 torch==2.13.0 resolves to
# 2.13.0+cu130, the Grace/sbsa CUDA build, and it drags in the entire nvidia-*
# CUDA runtime (cublas, cudnn, nccl, cusolver, ...) plus triton - several GB of
# GPU libraries baked into an image that will never see a GPU. This build
# caught exactly that; the CPU aarch64 wheel is published only on PyTorch's own
# index, as torch-2.13.0+cpu-cp311-cp311-manylinux_2_28_aarch64.whl.
#
# Installing it here, before the requirements file, means the resolver below
# finds torch already satisfied - PEP 440 says the specifier `==2.13.0` matches
# the local version `2.13.0+cpu` - so it never reaches for the CUDA build. The
# assertion in the next step is what keeps this honest.
ARG TORCH_VERSION=2.13.0
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --extra-index-url https://download.pytorch.org/whl/cpu \
        "torch==${TORCH_VERSION}+cpu"

# Copied alone, before the app source, so the dependency layer - by far the
# most expensive one on emulated arm64 - is rebuilt only when pins change.
COPY requirements.prod.txt ./
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install -r requirements.prod.txt

# Fail at build time, not container-start time, if either dependency landmine
# from requirements.txt creeps back in: `jose` shadowing python-jose (which
# breaks every authenticated request), or a non-CPU torch.
RUN python -c "\
import importlib.metadata as md, torch; \
from jose import JWTError, jwt; \
names = {d.metadata['Name'] for d in md.distributions() if d.metadata['Name']}; \
assert 'python-jose' in names, 'python-jose is missing - from jose import JWTError will fail'; \
assert 'jose' not in names, 'the standalone jose package is installed and shadows python-jose'; \
assert '+cu' not in torch.__version__, 'expected a CPU torch build, got ' + torch.__version__; \
cuda = sorted(n for n in names if n.startswith('nvidia-') or n in {'triton', 'cuda-toolkit', 'cuda-bindings'}); \
assert not cuda, 'CUDA packages leaked into an arm64 CPU image: ' + ', '.join(cuda); \
print('builder OK | torch=' + torch.__version__ + ' | no CUDA deps')"


# ------------------------------------------------------------------------------
# Stage 2: model-cache - bake the sentence-transformers model into the image.
#
# Without this, every container downloads all-MiniLM-L6-v2 from HuggingFace on
# its first embedding call (EmbeddingService._get_model), which makes the first
# request after each deploy slow, adds a hard runtime dependency on
# huggingface.co being reachable, and breaks outright on a read-only rootfs.
# Its own stage so a dependency-only rebuild does not re-download the weights.
# ------------------------------------------------------------------------------
FROM builder AS model-cache

# Must match EMBEDDING_MODEL in .env. Change both together, or the running
# container falls back to downloading a model that was never baked in.
ARG EMBEDDING_MODEL=all-MiniLM-L6-v2

ENV PATH="/opt/venv/bin:${PATH}" \
    HF_HOME=/opt/hf \
    HF_HUB_DISABLE_TELEMETRY=1 \
    EMBEDDING_MODEL=${EMBEDDING_MODEL}

RUN python -c "\
import os; \
from sentence_transformers import SentenceTransformer; \
name = os.environ['EMBEDDING_MODEL']; \
model = SentenceTransformer(name); \
print('baked ' + name + ' | dim=' + str(model.get_sentence_embedding_dimension()))"


# ------------------------------------------------------------------------------
# Stage 3: runtime - slim final image. No compiler, no pip cache, no sdists;
# just the venv, the model cache, and the application.
# ------------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS runtime

LABEL org.opencontainers.image.title="AIRS - AI Resume Screening Platform" \
      org.opencontainers.image.description="FastAPI + Celery resume screening backend" \
      org.opencontainers.image.source="https://github.com/PavesTechnologies/Ai_Hiring_Module"

# libgomp1 is REQUIRED, not optional: torch and scikit-learn link against
#   OpenMP, and on the slim base its absence surfaces as an obscure
#   "libgomp.so.1: cannot open shared object file" at import time.
# libpq5: PostgreSQL client library (see the libpq-dev note in the builder).
# ca-certificates: TLS to RDS (sslmode=require), Supabase, SES, Gemini.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        libpq5 \
        ca-certificates \
    && update-ca-certificates

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:${PATH}" \
    HF_HOME=/opt/hf \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=1 \
    HOST=0.0.0.0 \
    PORT=8002 \
    UVICORN_WORKERS=1 \
    CELERY_CONCURRENCY=2 \
    CELERY_LOGLEVEL=info \
    CELERYBEAT_SCHEDULE=/var/run/celery/celerybeat-schedule

# OMP_NUM_THREADS=1 above is load-bearing, for two independent reasons:
#
#   1. Correctness under emulation. Importing app.main in this image on a
#      qemu-aarch64 host hangs forever - every thread parked in futex_do_wait -
#      because torch's libgomp pool deadlocks under QEMU. With the variable set
#      the same import completes. Anyone smoke-testing an arm64 image on an
#      amd64 laptop hits this, so the default belongs in the image, not in the
#      test command.
#
#   2. Sizing on real hardware. libgomp otherwise spawns one thread per visible
#      core, which is the *host* core count - a CPU-limited container then
#      oversubscribes and thrashes. Embedding work here is short, per-request,
#      and already parallel across Celery children, so intra-op threading buys
#      nothing. Raise it deliberately (and together with CELERY_CONCURRENCY) if
#      a batch embedding job ever justifies it.

# Non-root. Fixed uid/gid so a mounted volume's ownership stays predictable.
RUN groupadd --gid 10001 airs \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin airs

COPY --from=builder     /opt/venv /opt/venv
COPY --from=model-cache /opt/hf   /opt/hf

WORKDIR /app

# alembic/ and alembic.ini are included on purpose - the `migrate` role needs
# them. .dockerignore keeps .env, .venv, tests/, docs/ and the scratch files
# out; secrets are injected at runtime, never baked into a layer.
COPY --chown=airs:airs alembic.ini ./
COPY --chown=airs:airs alembic ./alembic
COPY --chown=airs:airs scripts ./scripts
COPY --chown=airs:airs app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

# celery beat persists its schedule to disk and needs a writable location that
# is not the read-only app directory (the dev script leaves a celerybeat-schedule
# lying in the repo root).
RUN install -d -o airs -g airs /var/run/celery

USER airs
EXPOSE 8002

# Role-aware: `healthcheck` dispatches on the container's role inside the
# entrypoint, so this one directive is correct for api, worker and beat alike.
# start-period is generous - boot imports torch and runs two DB-touching
# FastAPI startup hooks (embedding-model-version sync, stalled-upload recovery).
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD ["/usr/local/bin/docker-entrypoint.sh", "healthcheck"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["api"]
