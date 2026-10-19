#!/bin/bash
# Store a new Anthropic API key in Secret Manager and switch the tutor on.
# The key is pasted at a hidden prompt; it is never shown or saved on this Mac.
set -euo pipefail
G=/opt/homebrew/bin/gcloud
P=edmhs-web; R=us-central1; SVC=scientific-chatbot-api
SA=science-hub@$P.iam.gserviceaccount.com

read -r -s -p "Paste the Anthropic API key (starts with sk-ant-), then press Return: " KEY; echo
KEY=$(printf %s "$KEY" | tr -d '[:space:]')
case "$KEY" in sk-ant-*) ;; *) echo "That doesn't look like an Anthropic key. Nothing was changed."; exit 1;; esac

code=$(printf 'x-api-key: %s\n' "$KEY" | curl -s -o /dev/null -w '%{http_code}' https://api.anthropic.com/v1/messages \
  -H @- -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
  -d '{"model":"claude-haiku-5-5","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}')
if [ "$code" != "200" ]; then echo "Anthropic rejected that key (HTTP $code). Nothing was changed."; exit 1; fi
echo "Key works."

if ! $G secrets describe anthropic-api-key --project $P >/dev/null 2>&1; then
  $G secrets create anthropic-api-key --project $P --replication-policy automatic --quiet
fi
printf %s "$KEY" | $G secrets versions add anthropic-api-key --project $P --data-file=- --quiet
unset KEY
$G secrets add-iam-policy-binding anthropic-api-key --project $P \
  --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor --quiet >/dev/null
$G run services update $SVC --project $P --region $R \
  --set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest --quiet
echo "The tutor is on: https://science-hub.edmhs.us"
