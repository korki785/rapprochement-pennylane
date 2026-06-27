#!/bin/bash
# Garde vivant le Chrome dédié (port debug 9222) pour le scraping ChatGPT/Stripe.
# Idempotent : ne relance que si le port CDP ne répond pas.
if curl -s http://localhost:9222/json/version >/dev/null 2>&1; then
    exit 0
fi
open -na "Google Chrome" --args \
    --remote-debugging-port=9222 \
    "--remote-allow-origins=*" \
    --user-data-dir="$HOME/.chrome-recon-debug" \
    "https://chatgpt.com/"
