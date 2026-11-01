#!/usr/bin/env bash
# Khởi động lại DST Web Panel (lưu ý: server game đang chạy cũng sẽ bị tắt và lưu lại).
set -e

systemctl restart dst-webui
sleep 2

if systemctl is-active --quiet dst-webui; then
    echo "dst-webui đã chạy lại."
else
    echo "dst-webui không chạy được, log gần nhất:"
    journalctl -u dst-webui -n 30 --no-pager
    exit 1
fi
