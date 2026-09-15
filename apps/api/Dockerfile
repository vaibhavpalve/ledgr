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

FROM python:3.12-slim

# uv, not plain pip: uv.lock pins exact versions this codebase's own test
# suite ran against (mypy strict, ruff, the full pytest suite) - resolving
# fresh at image-build time could pull a newer transitive dependency the
# code was never actually tested with.
RUN pip install --no-cache-dir uv

WORKDIR /repo
COPY apps/api apps/api
COPY packages/i18n/catalogue packages/i18n/catalogue

WORKDIR /repo/apps/api
# --frozen: fail rather than silently re-resolve if uv.lock and
# pyproject.toml have drifted. --no-dev: ruff/mypy/pytest/hypothesis have no
# reason to exist in the running image.
RUN uv sync --frozen --no-dev

EXPOSE 8000
# Railway injects $PORT and routes to it; defaulting to 8000 keeps this
# image runnable identically outside Railway (`docker run -p 8000:8000 ...`
# with no PORT set at all).
CMD ["sh", "-c", "uv run uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
