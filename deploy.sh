#!/bin/bash
# Build and deploy the Science Hub API, then the page. Safe to re-run.
set -euo pipefail
G=/opt/homebrew/bin/gcloud
P=edmhs-web; R=us-central1; SVC=scientific-chatbot-api
SA=science-hub@$P.iam.gserviceaccount.com
cd "$(dirname "$0")"
TAG=$(git rev-parse --short HEAD)
IMG=$R-docker.pkg.dev/$P/science/api:$TAG

$G artifacts repositories describe science --location $R --project $P >/dev/null 2>&1 || \
  $G artifacts repositories create science --repository-format docker --location $R --project $P \
     --description "Science Hub tutor API"
$G iam service-accounts describe $SA --project $P >/dev/null 2>&1 || \
  $G iam service-accounts create science-hub --project $P --display-name "Science Hub tutor API"

echo "== build $IMG"
(cd api && docker buildx build --platform linux/amd64 -t "$IMG" --push .)

SECRET_FLAG=()
if $G secrets versions list anthropic-api-key --project $P --filter="state=ENABLED" --format="value(name)" 2>/dev/null | grep -q .; then
  SECRET_FLAG=(--set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest)
fi

echo "== deploy $SVC"
$G run deploy $SVC --project $P --region $R --image "$IMG" \
  --service-account $SA ${SECRET_FLAG[@]+"${SECRET_FLAG[@]}"} \
  --allow-unauthenticated --max-instances 1 --concurrency 20 \
  --memory 512Mi --cpu 1 --timeout 60 \
  --set-env-vars TUTOR_MODEL=claude-haiku-5-5,DAILY_CAP=300,PER_CLIENT_PER_HOUR=20 \
  --labels app=science-hub --quiet

echo "== page"
python3 ~/Projects/edmhs-tools/hosting_deploy.py science-hub-edmhs web/public \
  --project $P --config web/hosting-config.json --message "Science Hub $TAG"
echo "== done $TAG"
