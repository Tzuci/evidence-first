# syntax=docker/dockerfile:1.6
FROM node:20-alpine

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

WORKDIR /app

# Build context = repository root.
# We copy the entire apps/web/ directory in a single COPY. Whether
# package-lock.json exists is decided at install time inside RUN: this avoids
# Docker COPY wildcard semantics, which are not a reliable optional-copy.
COPY apps/web/ /app/

# Use npm ci when a lockfile is present (deterministic, reproducible build),
# fall back to npm install otherwise (non-reproducible, with explicit warning).
RUN if [ -f package-lock.json ]; then \
        echo "INFO: package-lock.json found, using npm ci" ; \
        npm ci --no-audit --no-fund ; \
    else \
        echo "WARN: package-lock.json not found, falling back to npm install (non-reproducible). Run 'make lock-web' on a dev machine and commit the lockfile." ; \
        npm install --no-audit --no-fund ; \
    fi

RUN npm run build

EXPOSE 3000

CMD ["npm", "start"]