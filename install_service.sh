#!/bin/bash
# install_service.sh
# Chạy với quyền root: sudo bash install_service.sh
set -e

BOT_DIR="/opt/zabbix-discord-bot"
SERVICE_NAME="zabbix-discord-bot"
SERVICE_USER="zabbixbot"

echo "=== [1/6] Tạo thư mục bot ==="
mkdir -p "$BOT_DIR"

echo "=== [2/6] Tạo user hệ thống (không có home, không login shell) ==="
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "    Đã tạo user: $SERVICE_USER"
else
    echo "    User $SERVICE_USER đã tồn tại, bỏ qua."
fi

echo "=== [3/6] Copy file bot vào $BOT_DIR ==="
# Thay đường dẫn bên dưới nếu file bot đang ở chỗ khác
cp Bot.py          "$BOT_DIR/"
cp zabbix_api.py   "$BOT_DIR/"
cp .env            "$BOT_DIR/"

echo "=== [4/6] Tạo virtualenv và cài dependencies ==="
python3 -m venv "$BOT_DIR/venv"
"$BOT_DIR/venv/bin/pip" install --quiet --upgrade pip
"$BOT_DIR/venv/bin/pip" install --quiet \
    "discord.py>=2.3" \
    "aiohttp>=3.9" \
    "python-dotenv>=1.0"

echo "=== [5/6] Phân quyền ==="
chown -R "$SERVICE_USER:$SERVICE_USER" "$BOT_DIR"
chmod 750 "$BOT_DIR"
chmod 640 "$BOT_DIR/.env"   # .env chứa token — chỉ owner đọc được

echo "=== [6/6] Cài đặt và kích hoạt systemd service ==="
cp zabbix-discord-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable  "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo ""
echo "✅ Hoàn tất! Bot đang chạy. Kiểm tra trạng thái:"
echo "   sudo systemctl status $SERVICE_NAME"
echo "   sudo journalctl -u $SERVICE_NAME -f"
