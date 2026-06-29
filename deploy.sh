#!/usr/bin/env bash
# Деплой Железной Прачки на проде: подтянуть main из GitHub,
# обновить зависимости, перезапустить сервис. Запускается на сервере.
set -euo pipefail

cd /opt/zhelpra

echo "==> git fetch + reset на origin/main"
git fetch origin
git reset --hard origin/main

echo "==> зависимости"
venv/bin/pip install -r requirements.txt -q

echo "==> права для www-data"
chown -R www-data:www-data /opt/zhelpra

echo "==> рестарт сервиса"
systemctl restart zhelpra
sleep 2
systemctl is-active zhelpra

echo "DEPLOYED $(git rev-parse --short HEAD)"
