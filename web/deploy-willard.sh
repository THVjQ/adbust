#!/bin/sh
# Deploy adbust web to Willard as the TrueNAS custom app "adbust-web".
#   web/deploy-willard.sh <host-port>
# Builds the image on the host (tagged with the git commit), then creates the
# app the first time, or points the existing app at the new image. Each
# image is kept, so rolling back is re-running app.update with an old tag.
set -eu
PORT=${1:?usage: deploy-willard.sh <host-port, e.g. 8016>}
HOST=${WILLARD:-willard}
APP=adbust-web
DIR=/mnt/Pool1-MAIN/apps/$APP
cd "$(dirname "$0")"
TAG=$APP:$(git rev-parse --short HEAD)

echo "== checking port $PORT is free on $HOST"
ssh "$HOST" "sudo ss -tlnH | awk '{print \$4}' | grep -qE ':$PORT\$'" && {
  ssh "$HOST" "sudo midclt call app.query '[[\"name\",\"=\",\"$APP\"]]'" \
    | grep -q "\"$APP\"" || { echo "port $PORT is taken"; exit 1; }
}

echo "== copying source"
rsync -a --delete --exclude client --exclude helper --exclude data \
  --exclude __pycache__ ./ "$HOST:/tmp/$APP-src/"
ssh "$HOST" "sudo mkdir -p $DIR/src $DIR/data && sudo chown 568:568 $DIR/data \
  && sudo rsync -a --delete /tmp/$APP-src/ $DIR/src/"

echo "== building $TAG"
ssh "$HOST" "sudo bash -c 'cd $DIR/src && docker build -t $TAG . > $DIR/build-${TAG#*:}.log 2>&1' \
  || { sudo tail -20 $DIR/build-${TAG#*:}.log; exit 1; }"

COMPOSE=$(cat <<EOF
{"services": {"$APP": {
  "image": "$TAG",
  "restart": "unless-stopped",
  "ports": ["$PORT:8017"],
  "environment": {"ADBUST_DATA": "/data", "ADBUST_QUIET": "1"},
  "volumes": ["$DIR/data:/data"]
}}}
EOF
)

if ssh "$HOST" "sudo midclt call app.query '[[\"name\",\"=\",\"$APP\"]]'" | grep -q "\"$APP\""; then
  echo "== updating $APP"
  printf '%s' "{\"custom_compose_config\": $COMPOSE}" | ssh "$HOST" \
    "cat > /tmp/$APP-update.json && sudo midclt call -j app.update $APP \"\$(cat /tmp/$APP-update.json)\" >/dev/null"
else
  echo "== creating $APP"
  printf '%s' "{\"app_name\": \"$APP\", \"custom_app\": true, \"custom_compose_config\": $COMPOSE}" | ssh "$HOST" \
    "cat > /tmp/$APP-create.json && sudo midclt call -j app.create \"\$(cat /tmp/$APP-create.json)\" >/dev/null"
fi

echo "== checking"
sleep 3
ssh "$HOST" "curl -fsS http://127.0.0.1:$PORT/api/health" && echo " -> $APP up on :$PORT ($TAG)"
