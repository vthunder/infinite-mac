#!/bin/bash

npx tsc --noEmit && \
    npx tsc --noEmit --project worker/tsconfig.json && \
    CLOUDFLARE_ENV=production npx vite build && \
    npx wrangler deploy
