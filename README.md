# EDMHS Science Hub

A free science and math tutor for young people, live at **https://science-hub.edmhs.us**. Students pick a subject (physics, chemistry, biology, the human body, biochemistry, earth and space, math, computing), a grade band, and whether they want coaching or a full explanation. Answers come from Claude (Anthropic) through a small API that holds the key.

Built by EDM Health Services, Toledo, Ohio, as a STEM on-ramp for youth in programs such as Inner Combat Training.

## How it is built

```
browser ── science-hub.edmhs.us (Firebase Hosting, static page)
              └── /api/**  ──►  Cloud Run: scientific-chatbot-api (FastAPI, scales to zero)
                                   └── Anthropic Messages API (key in Secret Manager)
```

| Path | What it is |
| --- | --- |
| `api/app/main.py` | FastAPI app: `/api/health`, `/api/ask`, validation, rate limits, personal-detail removal |
| `api/app/prompt.py` | The tutor's system prompt: teaching style, grade levels, coach/explain modes, safety rules |
| `api/tests/` | 12 tests; the Anthropic client is faked, so no key or network is needed |
| `web/public/index.html` | The whole front end, one file, no build step |
| `web/hosting-config.json` | Firebase Hosting headers and the `/api/**` rewrite to Cloud Run |

## Safeguards for young users

- **No accounts, nothing stored.** Conversations live only in the open browser tab. Logs record subject, level and mode, never the question or answer.
- **Personal details are stripped** (emails, phone numbers, street addresses) before anything leaves the server, and the page tells students not to share them.
- **The system prompt** keeps the tutor on STEM, refuses dangerous how-tos and risky experiments, gives no diagnoses or doses, and stops the lesson to point to 988, 911 and a trusted adult if a student says they are unsafe.
- **Coach mode is the default**, so homework problems are worked through, not handed over.
- **Limits:** 20 questions per student per hour and 300 per day overall (`PER_CLIENT_PER_HOUR`, `DAILY_CAP`). The service runs with `max-instances=1` so the daily cap is exact. Set a monthly spend limit in the Anthropic Console as a second stop.
- The page says plainly that the tutor is an AI and can be wrong.

## Run and test locally

```bash
cd api
pip install -r requirements-dev.txt
pytest
ANTHROPIC_API_KEY=... uvicorn app.main:app --port 8080   # then POST to /api/ask
```

## Deploy

```bash
# 1. image (Mac builds ARM by default; Cloud Run needs amd64)
cd api
docker buildx build --platform linux/amd64 \
  -t us-central1-docker.pkg.dev/edmhs-web/science/api:$(git rev-parse --short HEAD) --push .

# 2. service
gcloud run deploy scientific-chatbot-api --project edmhs-web --region us-central1 \
  --image us-central1-docker.pkg.dev/edmhs-web/science/api:<tag> \
  --service-account science-hub@edmhs-web.iam.gserviceaccount.com \
  --set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest \
  --max-instances 1 --memory 512Mi --allow-unauthenticated

# 3. page
python3 ~/Projects/edmhs-tools/hosting_deploy.py science-hub-edmhs web/public --config web/hosting-config.json
```

The Cloud Run URL is public so that Firebase Hosting can reach it; it holds no data, and every request still passes the rate limits.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `TUTOR_MODEL` | `claude-haiku-5-5` | Claude model |
| `TUTOR_MAX_TOKENS` | `700` | Longest reply |
| `PER_CLIENT_PER_HOUR` | `20` | Questions per student per hour |
| `DAILY_CAP` | `300` | Questions per day, all students |
