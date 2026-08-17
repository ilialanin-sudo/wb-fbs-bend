#!/bin/bash
# Один прогон дашборда: выгрузка из WB → свод → готовая страница.
# Запускается ежечасной задачей Cowork. Токены берутся из .env рядом.
set -e
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
export WB_FIN_MAX_AGE_H="${WB_FIN_MAX_AGE_H:-20}"
python3 collect.py
python3 aggregate.py
python3 render.py
echo "СТРАНИЦА: $(pwd)/dist/wb_fbs_dashboard.html"
