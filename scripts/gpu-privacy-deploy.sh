#!/usr/bin/env bash
# Dedicated GPU privacy rollout, dispatched by bridge-deploy.sh.
# Only a clean fast-forward release is accepted. Build before waiting for idle;
# retain the previous image and restore it on a failed readiness check.
set -euo pipefail
ref="${1:-origin/codex/haiku55-gpu-20261026}"
[[ "$ref" =~ ^origin/[A-Za-z0-9][A-Za-z0-9_./-]*$ && "$ref" != *..* ]] || exit 1
sudo -n -u claude-user ssh -o BatchMode=yes -o ConnectTimeout=10 root@100.65.149.39 bash -s -- "$ref" <<'REMOTE'
set -euo pipefail
exec 9>/root/.privacy-deploy.lock
flock -n 9 || { echo 'Another GPU deploy is running'; exit 1; }
cd /root/werkingflow-bridge
git diff --quiet && git diff --cached --quiet || { echo 'Tracked GPU files modified'; exit 1; }
old_sha=$(git rev-parse HEAD)
old_image=$(docker inspect --format '{{.Image}}' gpu-privacy-pdf-service)
branch="${1#origin/}"
git fetch origin "$branch:refs/remotes/origin/$branch"
target=$(git rev-parse "$1^{commit}")
git merge-base --is-ancestor "$old_sha" "$target" || { echo 'Not a fast-forward'; exit 1; }
# This release must not ship unrelated changes on the shared privacy host.
unexpected=$(git diff --name-only "$old_sha" "$target" | grep -vE '^(src/privacy_service/app.py|src/privacy_service/image_describer.py|src/privacy_service/image_triage.py|src/vision_provider.py|src/providers/credit_pool.py|scripts/bridge-deploy.sh|scripts/gpu-privacy-deploy.sh)$' || true)
[[ -z "$unexpected" ]] || { echo "Unexpected files: $unexpected"; exit 1; }
git merge --ff-only "$target"
override=$(mktemp /root/privacy-release-XXXXXX.yml)
rollback=$(mktemp /root/privacy-rollback-XXXXXX.yml)
trap 'rm -f "$override" "$rollback"' EXIT
python3 - "$override" "$rollback" "$target" "$old_sha" "$old_image" <<'PYCONFIG'
import sys,json,pathlib
out,back,new,old,image=sys.argv[1:]
service={"labels":{"bridge.git.commit":new}}
credit_env='/root/werkingflow-bridge/secrets/api-credit-pool-client.env'
if pathlib.Path(credit_env).exists():
 service['env_file']=[credit_env]
json.dump({"services":{"privacy-service":service}},open(out,"w"))
json.dump({"services":{"privacy-service":{"image":image,"labels":{"bridge.git.commit":old}}}},open(back,"w"))
PYCONFIG
base=(-f /root/docker-compose.privacy-gpu.yml -f /root/docker-compose.privacy-gpu.override.yml)
docker compose "${base[@]}" -f "$override" build privacy-service
ready() {
 docker exec -i gpu-privacy-pdf-service python - <<'PYREADY'
import os,json,urllib.request
req=urllib.request.Request("http://127.0.0.1:8100/ready",headers={"Authorization":"Bearer "+os.environ["PRIVACY_SERVICE_TOKEN"]})
with urllib.request.urlopen(req,timeout=10) as r:d=json.load(r)
print("active_requests",d.get("active_requests"))
raise SystemExit(0 if d.get("ready_for_shutdown") is True else 1)
PYREADY
}
deadline=$((SECONDS+900))
until ready; do
 ((SECONDS<deadline)) || { echo 'GPU still busy; runtime unchanged'; exit 1; }
 sleep 10
done
health() {
 docker exec gpu-privacy-pdf-service python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8100/health",timeout=10)'
}
rollback_runtime() {
 echo 'Restoring previous GPU image'
 docker compose "${base[@]}" -f "$rollback" up -d --no-build --no-deps --force-recreate privacy-service
}
if ! docker compose "${base[@]}" -f "$override" up -d --no-build --no-deps --timeout 180 privacy-service; then
 rollback_runtime; exit 1
fi
healthy=0
for attempt in $(seq 1 60); do
 if health >/dev/null 2>&1; then healthy=1; break; fi
 sleep 5
done
if [[ "$healthy" != 1 ]]; then rollback_runtime; exit 1; fi
docker exec gpu-privacy-pdf-service python -c 'import ast,pathlib; tree=ast.parse(pathlib.Path("/app/src/privacy_service/app.py").read_text()); model=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=="HAIKU_MODEL" for t in n.targets)); assert model=="claude-haiku-5-5"; print("GPU model:", model)' || { rollback_runtime; exit 1; }
printf '%s\n' "$target" > .privacy-deployed-sha
echo "GPU deploy healthy: $target"
REMOTE
