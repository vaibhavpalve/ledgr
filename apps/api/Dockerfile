# Built with the REPO ROOT as build context (see railway.json at the repo
# root, which points here via "dockerfilePath": "apps/api/Dockerfile") - NOT
# apps/api alone. apps/api/pyproject.toml's
# [tool.hatch.build.targets.wheel.force-include] pulls in
# ../../packages/i18n/catalogue (the message catalogue LEDGR shares with the
# web and mobile apps, per FR-LOC-001) via a path relative to that
# pyproject.toml - a build context rooted at apps/api alone cannot see that
# directory at all, and the image would boot and then raise on the first
# user-facing string. See docs/decisions/ADR-062-railway-hosting-and-non-azure-cloud-services.md
# for why this image targets Railway.

# ---------------------------------------------------------------------------
# Stage 1: build the web SPA (ADR-063: the API serves it from its own
# origin, so it has to exist inside THIS image - see apps/api/src/api/spa.py
# for why that can't be a separate static host).
#
# A separate node image, not the python one below: apps/web's whole toolchain
# (pnpm, vite, tsc) is Node-only, and none of it - not even node itself -
# ships in the final image, only the static dist/ output this stage produces.
# ---------------------------------------------------------------------------
FROM node:20-slim AS web-build

RUN corepack enable
WORKDIR /repo

# Workspace manifests before source, same layering reason the python stage
# copies uv.lock before running uv sync below: a rebuild that only touches
# component source, not a dependency, reuses this cached `pnpm install`
# layer instead of redoing it.
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json apps/web/package.json
COPY packages packages
RUN pnpm install --frozen-lockfile

COPY apps/web apps/web
RUN pnpm --filter @ledgr/web run build

# ---------------------------------------------------------------------------
# Stage 2: the API image
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# uv, not plain pip: uv.lock pins exact versions this codebase's own test
# suite ran against (mypy strict, ruff, the full pytest suite) - resolving
# fresh at image-build time could pull a newer transitive dependency the
# code was never actually tested with.
RUN pip install --no-cache-dir uv

WORKDIR /repo
COPY apps/api apps/api
COPY packages/i18n/catalogue packages/i18n/catalogue
# api.spa (ADR-063): a plain filesystem copy, independent of the Python
# package build below - see apps/api/src/api/config.py's web_dist_dir for
# why this deliberately isn't a force-include entry in pyproject.toml.
COPY --from=web-build /repo/apps/web/dist apps/web/dist
ENV WEB_DIST_DIR=/repo/apps/web/dist

WORKDIR /repo/apps/api
# --frozen: fail rather than silently re-resolve if uv.lock and
# pyproject.toml have drifted. --no-dev: ruff/mypy/pytest/hypothesis have no
# reason to exist in the running image.
RUN uv sync --frozen --no-dev

EXPOSE 8000
# Railway injects $PORT and routes to it; defaulting to 8000 keeps this
# image runnable identically outside Railway (`docker run -p 8000:8000 ...`
# with no PORT set at all).
#
# GOOGLE_APPLICATION_CREDENTIALS (read by GcpKmsKeyManagementService's ADC
# lookup, api/crypto/kms.py) has to be a file path - Railway variables are
# strings, not files, so GCP_SERVICE_ACCOUNT_JSON carries the key's JSON
# content instead and gets written out here before the app starts. Optional:
# unset in kms_provider=local/azure-key-vault deployments.
CMD ["sh", "-c", "if [ -n \"$GCP_SERVICE_ACCOUNT_JSON\" ]; then printf '%s' \"$GCP_SERVICE_ACCOUNT_JSON\" > /tmp/gcp-service-account.json && export GOOGLE_APPLICATION_CREDENTIALS=/tmp/gcp-service-account.json; fi; uv run uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
