# pipe1 — Article → Instagram Post

Publishes an existing ttb8 article to Instagram via `social-pr-autopilot`. Follows the runbook at `docs/article-to-instagram-runbook.md` exactly.

## Prerequisites

- Article `.tsx` exists in `ttb8/app/routes/`
- Hero image exists in `ttb8/public/<slug>.jpg` **and** is copied to `social-pr-autopilot/frontend/public/<slug>.jpg`
- `zxy3/.env` has all credentials filled in (see `sensitive/env-reference.md`)
- Mamba env `auto1` exists (`mamba info --envs` to check)

## Execute now

When the user invokes `/pipe1`, run this exact sequence:

---

### Step 0 — create mamba env if missing (one-time)

```bash
mamba info --envs | grep auto1
```
If `auto1` is not listed:
```bash
mamba create -n auto1 python=3.11 -c conda-forge -y
mamba run -n auto1 pip install -r /Users/adamaslan/code/startup-ideas/social-pr-autopilot/backend/requirements.txt
```

---

### Step 1 — check / start the Cloudflare tunnel

First check if the previously-recorded tunnel is still live:
```bash
EXISTING_URL=$(grep '^INSTAGRAM_PUBLIC_BASE_URL=' /Users/adamaslan/code/startup-ideas/social-pr-autopilot/backend/.env.local 2>/dev/null | cut -d= -f2-)
if [ -n "$EXISTING_URL" ]; then
  if curl -sI --max-time 5 "$EXISTING_URL" > /dev/null 2>&1; then
    echo "Tunnel still live: $EXISTING_URL"
    TUNNEL_URL=$EXISTING_URL
  else
    echo "Tunnel dead — will start a new one"
    EXISTING_URL=""
  fi
fi
```

If no live tunnel, start a fresh one (**ask the user first** — this generates a new public URL and overwrites the value in `.env.local`):
```bash
cloudflared tunnel --url http://127.0.0.1:8102 --no-autoupdate 2>&1 | tee /tmp/cf-tunnel.log &
sleep 8
TUNNEL_URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' /tmp/cf-tunnel.log | head -1)
echo "Tunnel: $TUNNEL_URL"
```

Write the URL to `.env.local`:
```bash
ENV_LOCAL=/Users/adamaslan/code/startup-ideas/social-pr-autopilot/backend/.env.local
if grep -q "INSTAGRAM_PUBLIC_BASE_URL" "$ENV_LOCAL" 2>/dev/null; then
  sed -i '' "s|INSTAGRAM_PUBLIC_BASE_URL=.*|INSTAGRAM_PUBLIC_BASE_URL=$TUNNEL_URL|" "$ENV_LOCAL"
else
  echo "INSTAGRAM_PUBLIC_BASE_URL=$TUNNEL_URL" >> "$ENV_LOCAL"
fi
```

---

### Step 2 — start the backend (skip if already up)

Check first:
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8102/health
```
If `200`, skip. **Never kill an existing uvicorn process** — its environment holds credentials that cannot be recovered without manual retrieval from Meta Graph API Explorer.

If not running:
```bash
cd /Users/adamaslan/code/startup-ideas/social-pr-autopilot/backend
mamba run -n auto1 python3 -m uvicorn app.main:app --port 8102 > /tmp/spa-backend.log 2>&1 &
sleep 5
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8102/health
```
Expect `200`.

---

### Step 3 — verify green (do not proceed until all three pass)

```bash
curl -s http://127.0.0.1:8102/api/channels/instagram/diagnostics | python3 -m json.tool
```
Must show `"missing_config": []`. Note: `supports_autopublish` is a static field that may show `false` even when direct publish is enabled — trust the env var below, not this flag.

Confirm the direct-publish flag is loaded:
```bash
cd /Users/adamaslan/code/startup-ideas/social-pr-autopilot/backend && mamba run -n auto1 python3 -c "
import os
from app.config import load_env_files
load_env_files()
print('INSTAGRAM_DIRECT_PUBLISH_ENABLED:', os.getenv('INSTAGRAM_DIRECT_PUBLISH_ENABLED'))
print('INSTAGRAM_PUBLIC_BASE_URL:', os.getenv('INSTAGRAM_PUBLIC_BASE_URL'))
print('INSTAGRAM_ACCESS_TOKEN set:', bool(os.getenv('INSTAGRAM_ACCESS_TOKEN')))
"
```
All three must be set; `INSTAGRAM_DIRECT_PUBLISH_ENABLED` must equal `true`.

Confirm the image is reachable through the tunnel:
```bash
curl -sI "$TUNNEL_URL/media/<slug>.jpg" | head -1
# Expected: HTTP/2 200
```

If any check fails, **report the failure and stop**. Do not restart anything.

---

### Step 4 — publish

Ask the user for:
- `<slug>` — the article slug (e.g., `my-article-slug`)
- `<caption>` — up to 2200 chars including hashtags
- `<alt_text>` — descriptive alt text for the image
- `dry_run` — default `true`; only use `false` when the user says "post it", "go live", or "publish"

```bash
curl -s -X POST http://127.0.0.1:8102/api/publish \
  -H "Content-Type: application/json" \
  -d '{
    "channel": "instagram",
    "campaign_name": "<Article Title>",
    "text": "<caption>\n\n#hashtag1 #hashtag2",
    "local_image_path": "<slug>.jpg",
    "alt_text": "<alt_text>",
    "link_url": "https://tastytechbytes.com/<slug>",
    "dry_run": false
  }' | python3 -m json.tool
```

Successful response: `"status": "published"` with a non-empty `"external_id"` (the IG media ID).

---

## Env var load order (first file wins per key)

| Priority | File | Purpose |
|---|---|---|
| 1 (highest) | `zxy3/.env` | All credentials — fill real values here |
| 2 | `social-pr-autopilot/.env` | App-level defaults |
| 3 | `backend/.env` | Backend defaults |
| 4 | `backend/.env.local` | Session-specific overrides (tunnel URL) |

**Rule:** Never leave a key set to an empty string in a higher-priority file — it blocks lower files from supplying the real value. Comment out empty placeholders instead: `# KEY=`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `missing_config` not empty | Empty placeholder in `zxy3/.env` | Comment out the empty line: `# KEY=` |
| `status: exported` instead of `published` | `INSTAGRAM_DIRECT_PUBLISH_ENABLED` not `true` | Run: `mamba run -n auto1 python3 -c "import os; from app.config import load_env_files; load_env_files(); print(os.getenv('INSTAGRAM_DIRECT_PUBLISH_ENABLED'))"` |
| Meta `400 Bad Request` | Tunnel URL dead or image not served | Re-run Steps 1–3; confirm `curl -sI <tunnel>/media/<file>` → `200` |
| `mistral_configured: false` | Mistral key not in env | Verify `MISTRAL_API_KEY` has a real value in `zxy3/.env` (not empty) |
| Backend starts but `/media` returns 404 | `StaticFiles` mount missing | Ensure `main.py` has `app.mount("/media", StaticFiles(...), name="media")` |
