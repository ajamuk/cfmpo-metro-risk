#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/crossfit-metropolitano-dashboard"
SERVICE_NAME="crossfit-metropolitano-dashboard"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Ejecuta este script en el VPS con sudo."
  exit 1
fi

mkdir -p "${APP_DIR}"
rsync -a --delete \
  --exclude ".git" \
  --exclude ".env" \
  --exclude ".aimharder_tokens.json" \
  --exclude "data" \
  --exclude "__pycache__" \
  --exclude "logs" \
  --exclude "reports/*.html" \
  --exclude "reports/*.csv" \
  --exclude "reports/*.json" \
  ./ "${APP_DIR}/"

chown -R www-data:www-data "${APP_DIR}"
chmod 640 "${APP_DIR}/.env"

cp "${APP_DIR}/deploy/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo "Servicio arrancado:"
systemctl --no-pager --full status "${SERVICE_NAME}"

