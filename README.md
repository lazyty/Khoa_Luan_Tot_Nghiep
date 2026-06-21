# Zabbix Discord Bot – Middleware Giám Sát Hạ Tầng

Bot Discord tích hợp Zabbix, cho phép nhận alert thời gian thực và điều khiển hạ tầng trực tiếp từ Discord.

---

## Mục lục

1. [Kiến trúc tổng quan](#1-kiến-trúc-tổng-quan)
2. [Cài đặt Zabbix](#2-cài-đặt-zabbix)
3. [Yêu cầu hệ thống](#3-yêu-cầu-hệ-thống)
4. [Cài đặt Bot](#4-cài-đặt-bot)
5. [Cấu hình .env](#5-cấu-hình-env)
6. [Cấu hình Zabbix](#6-cấu-hình-zabbix)
7. [Hướng dẫn sử dụng Bot](#7-hướng-dẫn-sử-dụng-bot)
8. [Hướng dẫn sử dụng Anomaly Detector](#8-hướng-dẫn-sử-dụng-anomaly-detector)
9. [Quản lý Service](#9-quản-lý-service)
10. [Cấu trúc thư mục](#10-cấu-trúc-thư-mục)

---

## 1. Kiến trúc tổng quan

```
                    ┌─────────────────────────────┐
                    │        Zabbix Server         │
                    │  ┌──────────┐ ┌───────────┐  │
                    │  │ Triggers │ │  Actions  │  │
                    │  └────┬─────┘ └─────┬─────┘  │
                    └───────┼─────────────┼─────────┘
                            │             │ Webhook
                     Polling│(fallback)   │(realtime)
                    (300s)  │             ▼
                    ┌───────┴─────────────────────┐
                    │     Zabbix Discord Bot       │
                    │  ┌──────────┐ ┌───────────┐  │
                    │  │ Poller   │ │  Webhook  │  │
                    │  │(backup)  │ │  :8081    │  │
                    │  └──────────┘ └───────────┘  │
                    │  ┌────────────────────────┐   │
                    │  │    Dedup Cache (5min)  │   │
                    │  └────────────────────────┘   │
                    └─────────────┬───────────────┘
                                  │
                                  ▼
                         ┌────────────────┐
                         │  Discord Server │
                         │  #zabbix-alerts │
                         └────────────────┘
```

**Luồng hoạt động:**
- **Webhook (realtime):** Zabbix Action → POST `/zabbix-alert` → Bot gửi Discord ngay lập tức
- **Polling (fallback):** Bot tự poll Zabbix mỗi 300 giây để bắt event bị miss
- **Dedup Cache:** Đảm bảo không gửi trùng dù cả webhook và polling cùng bắt được event

---

## 2. Cài đặt Zabbix

Tham khảo hướng dẫn cài đặt và sử dụng Zabbix chi tiết tại:

**[https://elroydevops.tech/cach-su-dung-zabbix-de-monitoring/](https://elroydevops.tech/cach-su-dung-zabbix-de-monitoring/)**

Tóm tắt nhanh cài Zabbix 7.x trên Ubuntu 22.04:

```bash
# Thêm Zabbix repository
wget https://repo.zabbix.com/zabbix/7.0/ubuntu/pool/main/z/zabbix-release/zabbix-release_7.0-1+ubuntu22.04_all.deb
sudo dpkg -i zabbix-release_7.0-1+ubuntu22.04_all.deb
sudo apt update

# Cài Zabbix Server + Frontend + Agent
sudo apt install -y zabbix-server-mysql zabbix-frontend-php zabbix-apache-conf zabbix-sql-scripts zabbix-agent

# Cài MySQL và tạo database
sudo apt install -y mysql-server
mysql -uroot -p -e "
  CREATE DATABASE zabbix CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
  CREATE USER 'zabbix'@'localhost' IDENTIFIED BY 'password';
  GRANT ALL PRIVILEGES ON zabbix.* TO 'zabbix'@'localhost';
"

# Import schema
zcat /usr/share/zabbix-sql-scripts/mysql/server.sql.gz | mysql -uzabbix -p zabbix

# Cấu hình timezone PHP
sudo sed -i 's/# php_value date.timezone Europe\/Riga/php_value date.timezone Asia\/Ho_Chi_Minh/' /etc/zabbix/apache.conf

# Khởi động service
sudo systemctl enable --now zabbix-server zabbix-agent apache2
```

Sau đó truy cập `http://<server-ip>/zabbix` để hoàn tất setup qua Web UI.

---

## 3. Yêu cầu hệ thống

| Thành phần | Yêu cầu |
|---|---|
| OS | Ubuntu 20.04 / 22.04 |
| Python | 3.10+ |
| Zabbix | 6.0+ (khuyến nghị 7.0) |
| Discord Bot | Token hợp lệ với quyền Send Messages, Embed Links |

**Python dependencies:**
```
discord.py>=2.3
aiohttp>=3.9
python-dotenv>=1.0
```

---

## 4. Cài đặt Bot

### 4.1 Clone / copy file

```bash
mkdir ~/zabbix_discord_bot
cd ~/zabbix_discord_bot
# Copy các file: bot.py, zabbix_api.py, anomaly_detector.py, .env
```

### 4.2 Cài tự động bằng script

```bash
# Đặt tất cả file vào cùng thư mục, sau đó chạy:
sudo bash install_service.sh
```

Script sẽ tự động:
1. Tạo thư mục `/opt/zabbix-discord-bot`
2. Tạo user hệ thống `zabbixbot` (không có login shell)
3. Copy file bot vào `/opt`
4. Tạo Python virtualenv và cài dependencies
5. Phân quyền file `.env` (chmod 640)
6. Cài và kích hoạt systemd service

### 4.3 Cài thủ công (nếu không dùng script)

```bash
# Tạo virtualenv
python3 -m venv ~/zabbix_discord_bot/venv
source ~/zabbix_discord_bot/venv/bin/activate
pip install "discord.py>=2.3" "aiohttp>=3.9" "python-dotenv>=1.0"

# Chạy bot
python bot.py
```

---

## 5. Cấu hình .env

Tạo file `.env` trong thư mục bot:

```dotenv
# ── Discord ────────────────────────────────────────
DISCORD_TOKEN=your_discord_bot_token_here
ALLOWED_ROLE_IDS=        # Role ID được phép dùng lệnh
COMMAND_CHANNEL_ID=0                         # 0 = cho phép mọi kênh
ALERT_CHANNEL_ID=        # Kênh nhận alert

# ── Zabbix API ─────────────────────────────────────
ZABBIX_URL=http://<zabbix-server-ip>/api_jsonrpc.php
ZABBIX_USER=Admin
ZABBIX_PASS=your_zabbix_password

# ── Polling (fallback) ─────────────────────────────
POLL_INTERVAL=300                            # Giây, mặc định 300 (5 phút)

# ── Webhook Server (realtime) ──────────────────────
WEBHOOK_ENABLED=true
WEBHOOK_PORT=8081
WEBHOOK_SECRET=your_random_secret_here       # Tạo bằng: openssl rand -hex 20
```

**Lấy Discord Token:**
1. Vào [Discord Developer Portal](https://discord.com/developers/applications)
2. New Application → Bot → Reset Token → Copy

**Lấy Role ID / Channel ID:**
- Discord Settings → Advanced → bật Developer Mode
- Chuột phải vào Role/Channel → Copy ID

---

## 6. Cấu hình Zabbix

### 6.1 Tạo Scripts thực thi trên Agent

Vào **Administration → Scripts → Create script** cho từng mục. Hệ thống dùng **nftables** (không phải iptables) và **rc-service** vì agent chạy trên **Alpine Linux** (OpenRC, không có systemd):

| Name | Commands | Execute on |
|---|---|---|
| `Block IP` | `nft insert rule inet filter input ip saddr {$BLOCK_IP} drop && conntrack -D -s {$BLOCK_IP} 2>/dev/null; echo OK` | Zabbix agent |
| `Unblock IP` | `HANDLE=$(nft -a list chain inet filter input \| grep 'ip saddr {$BLOCK_IP} drop' \| awk '{print $NF}') && nft delete rule inet filter input handle $HANDLE && echo OK` | Zabbix agent |
| `Manual Restart DNS` | `rc-service dnsmasq restart` | Zabbix agent |
| `Manual Restart HTTP` | `rc-service nginx restart` | Zabbix agent |
| `Manual Restart SSH` | `rc-service sshd restart` | Zabbix agent |
| `Restart DNS Service` (Action operation) | `/etc/zabbix/scripts/auto_remediation.sh restart dns` | Zabbix agent |
| `Restart HTTP Service` (Action operation) | `/etc/zabbix/scripts/auto_remediation.sh restart http` | Zabbix agent |
| `Restart SSH Service` (Action operation) | `/etc/zabbix/scripts/auto_remediation.sh restart ssh` | Zabbix agent |

> Lưu ý: `Block IP` / `Manual Restart *` là script chạy tay từ Frontend, dùng macro `{$BLOCK_IP}`. Còn `Restart * Service` được gắn vào **Trigger action** để tự động khắc phục sự cố (Auto Remediation), gọi qua script tổng `auto_remediation.sh` đặt tại `/etc/zabbix/scripts/` trên agent — script này nhận tham số `ACTION` (`block_ip` / `unblock_ip` / `restart`) và `TARGET`.

Vì agent chạy bằng tài khoản không phải root, cần quyền `sudo` không mật khẩu cho `nft` và `rc-service`. Thêm vào sudoers trên mỗi agent (`/etc/sudoers.d/zabbix`):
```
zabbix ALL=(ALL) NOPASSWD: /usr/sbin/nft, /sbin/rc-service, /usr/bin/conntrack
```

### 6.2 Tạo Media Type (Webhook Realtime)

**Alerts → Media types → Create media type**

| Field | Value |
|---|---|
| Name | `Discord Bot Webhook` |
| Type | `Webhook` |

**Parameters:**

| Name | Value |
|---|---|
| `event_id` | `{EVENT.ID}` |
| `recovery_event_id` | `{EVENT.RECOVERY.ID}` |
| `event_name` | `{EVENT.NAME}` |
| `severity` | `{TRIGGER.SEVERITY}` |
| `status` | `{TRIGGER.STATUS}` |
| `host` | `{HOST.NAME}` |
| `ip` | `{HOST.IP}` |
| `clock` | `{EVENT.DATE} {EVENT.TIME}` |
| `webhook_secret` | `<giá trị WEBHOOK_SECRET trong .env>` |

**Script:**
```javascript
var params = JSON.parse(value);
var url = 'http://<BOT-SERVER-IP>:8081/zabbix-alert';
if (params.action_name) {
    url = 'http://<BOT-SERVER-IP>:8081/zabbix-action';
}
var req = new HttpRequest();
req.addHeader('Content-Type: application/json');
req.addHeader('X-Secret: ' + params.webhook_secret);
delete params.webhook_secret;
var resp = req.post(url, JSON.stringify(params));
if (req.getStatus() != 200) {
    throw 'HTTP ' + req.getStatus() + ': ' + resp;
}
return resp;
```

### 6.3 Tạo User gửi notification

**Users → Create user:**
- Username: `discord-webhook`
- Groups: `Zabbix administrators`
- Tab Media → Add → Type: `Discord Bot Webhook` → Send to: `discord`

### 6.4 Tạo Action

**Alerts → Actions → Trigger actions → Create action**

| Field | Value |
|---|---|
| Name | `Discord Webhook Notification` |
| Conditions | Trigger severity ≥ Warning |

Tab **Operations** → Add:
- Operation type: `Send message`
- Send to users: `discord-webhook`
- Send only to: `Discord Bot Webhook`

Tab **Recovery operations** → Add tương tự.

### 6.5 Mở firewall

```bash
sudo ufw allow 8081/tcp
sudo ufw status
```

---

## 7. Hướng dẫn sử dụng Bot

### Slash Commands

| Lệnh | Mô tả | Ví dụ |
|---|---|---|
| `/help` | Hiển thị danh sách lệnh | `/help` |
| `/hosts` | Liệt kê tất cả host đang giám sát | `/hosts` |
| `/status <host_id>` | Xem CPU, RAM, trạng thái host | `/status 10084` |
| `/problems [host_id]` | Xem các sự cố đang active | `/problems` hoặc `/problems 10084` |
| `/restart <host_id> <service>` | Restart dịch vụ trên host | `/restart 10084 nginx` |
| `/block <host_id> <ip>` | Chặn IP bằng nftables | `/block 10084 203.0.113.45` |
| `/unblock <host_id> <ip>` | Gỡ chặn IP | `/unblock 10084 203.0.113.45` |

### Lấy Host ID

Cách 1 — Dùng lệnh `/hosts` trong Discord.

Cách 2 — Zabbix UI: **Configuration → Hosts** → xem cột **Host ID** (bật từ cột bên phải).

Cách 3 — URL khi click vào host: `...hostid=10084...`

### Alert Notifications

Bot tự động gửi 3 loại thông báo:

**🔴 PROBLEM alert** (kèm @everyone):
```
🔴 [High] SSH Service Down
Host: Alpine 1 | Severity: High | Acknowledged: ❌ Chưa
Event ID: 2293 | Thời điểm: 2026-06-03 11:18:26 GMT+7
```

**✅ RESOLVED notification:**
```
✅ [RESOLVED] SSH Service Down
Host: Alpine 1 | Severity (cũ): High
Event ID: 2294 | Thời điểm giải quyết: 2026-06-03 11:20:00 GMT+7
```

**⚙️ AUTO ACTION notification** (khi Zabbix tự động chạy script):
```
🔒 [AUTO ACTION] Auto Block Brute Force
Đã tự động chặn IP 203.0.113.45 trên host Alpine 1
```

### Trạng thái host trong /status

| Icon | Màu | Ý nghĩa |
|---|---|---|
| 📊 | Xanh dương | Host online bình thường |
| 🔴 | Đỏ | Host OFFLINE – dữ liệu là lần đo cuối cùng |
| ⚫ | Xám | Trạng thái không xác định |

> ⚠️ Khi host offline, dữ liệu hiển thị là **lần đo cuối trước khi mất kết nối**, không phải giá trị hiện tại. Thời điểm đo được hiển thị kèm theo.

---

## 8. Hướng dẫn sử dụng Anomaly Detector

File `anomaly_detector.py` phân tích log hệ thống để phát hiện tấn công brute-force SSH và tự động chặn IP nghi ngờ.

### Cơ chế hoạt động

Script đọc `/var/log/messages`, phân tích các failed login trong **1 phút gần nhất** và tính điểm dựa trên 3 tiêu chí:

| Tiêu chí | Trọng số | Kích hoạt khi |
|---|---|---|
| **Frequency** (tần suất) | 40% | ≥7 lần thất bại (ban ngày) hoặc ≥3 lần (ban đêm 0-5h) |
| **Time-based** (thời gian) | 30% | Có failed login trong giờ 0-5h sáng |
| **Pattern** (mẫu tấn công) | 30% | Cùng username từ ≥3 IP khác nhau |

**Công thức điểm:**
```
Score = Frequency × 0.4 + Time × 0.3 + Pattern × 0.3
```

Khi `score ≥ 0.4` → tự động thêm IP vào blacklist `nftables`.

### Cài đặt

```bash
# Copy script lên agent (Alpine Linux / Ubuntu)
sudo cp anomaly_detector.py /etc/zabbix/scripts/
sudo chmod +x /etc/zabbix/scripts/anomaly_detector.py

# Tạo nftables table và set (nếu chưa có)
sudo nft add table inet security
sudo nft add set inet security blackhole { type ipv4_addr\; }
sudo nft add rule inet security input ip saddr @blackhole drop
```

### Các mode chạy

```bash
# Kiểm tra tần suất failed login
python3 anomaly_detector.py frequency
# Output: 0 hoặc 1

# Kiểm tra thời gian đáng ngờ (0-5h sáng)
python3 anomaly_detector.py timebase
# Output: 0 hoặc 1

# Kiểm tra pattern tấn công (nhiều IP cùng user)
python3 anomaly_detector.py pattern
# Output: 0 hoặc 1

# Đếm số failed login trong 1 phút gần nhất
python3 anomaly_detector.py count
# Output: số nguyên

# Tính tổng điểm + tự động block IP nếu score >= 0.4
python3 anomaly_detector.py total
# Output: 0.00 - 1.00

# Kiểm tra failed login cục bộ (không phải SSH)
python3 anomaly_detector.py local_anomaly
# Output: 0 hoặc 1

# Debug — xem toàn bộ thông tin
python3 anomaly_detector.py debug
# Output:
# Recent failed logins: 5
# Frequency: 1
# Time-based: 0
# Pattern: 1
# Total Score: 0.7
```

### Tích hợp với Zabbix

Thêm vào Zabbix Agent config (`/etc/zabbix/zabbix_agent2.conf` hoặc `zabbix_agentd.conf`):

```
UserParameter=security.ssh.frequency,python3 /etc/zabbix/scripts/anomaly_detector.py frequency
UserParameter=security.ssh.timebase,python3 /etc/zabbix/scripts/anomaly_detector.py timebase
UserParameter=security.ssh.pattern,python3 /etc/zabbix/scripts/anomaly_detector.py pattern
UserParameter=security.ssh.count,python3 /etc/zabbix/scripts/anomaly_detector.py count
UserParameter=security.anomaly.total,python3 /etc/zabbix/scripts/anomaly_detector.py total
UserParameter=security.local.anomaly,python3 /etc/zabbix/scripts/anomaly_detector.py local_anomaly
```

Restart Zabbix agent sau khi thêm:
```bash
sudo systemctl restart zabbix-agent2
```

**Tạo Triggers trong Zabbix:**

| Trigger name | Expression | Severity |
|---|---|---|
| SSH Brute Force – High Score | `last(/host/security.anomaly.total)>=0.7` | High |
| SSH Brute Force – Warning | `last(/host/security.anomaly.total)>=0.4` | Warning |
| Suspicious Login Time | `last(/host/security.ssh.timebase)=1` | Warning |
| Multi-IP Pattern Attack | `last(/host/security.ssh.pattern)=1` | Average |
| Local Login Anomaly | `last(/host/security.local.anomaly)=1` | Average |

### Sudoers cho nftables

```bash
# /etc/sudoers.d/zabbix-anomaly
zabbix ALL=(ALL) NOPASSWD: /usr/sbin/nft
```

---

## 9. Quản lý Service

### Lệnh thường dùng

```bash
# Xem trạng thái
sudo systemctl status zabbix-discord-bot

# Xem log realtime
sudo journalctl -u zabbix-discord-bot -f

# Xem 100 dòng log gần nhất
sudo journalctl -u zabbix-discord-bot -n 100 --no-pager

# Dừng bot
sudo systemctl stop zabbix-discord-bot

# Khởi động
sudo systemctl start zabbix-discord-bot

# Restart
sudo systemctl restart zabbix-discord-bot

# Tắt autostart
sudo systemctl disable zabbix-discord-bot
```

### Cập nhật bot

```bash
# Tạo shortcut botdeploy (chạy 1 lần)
cat << 'EOF' | sudo tee /usr/local/bin/botdeploy
#!/bin/bash
cp /home/zabbix-server/zabbix_discord_bot/bot.py /opt/zabbix-discord-bot/
cp /home/zabbix-server/zabbix_discord_bot/zabbix_api.py /opt/zabbix-discord-bot/
systemctl restart zabbix-discord-bot
systemctl status zabbix-discord-bot
EOF
sudo chmod +x /usr/local/bin/botdeploy

# Sau khi sửa file, chỉ cần chạy:
sudo botdeploy
```

### Test webhook

```bash
curl -X POST http://localhost:8081/zabbix-alert \
  -H "Content-Type: application/json" \
  -H "X-Secret: $(sudo grep WEBHOOK_SECRET /opt/zabbix-discord-bot/.env | cut -d= -f2)" \
  -d '{
    "event_id": "9999",
    "recovery_event_id": "",
    "event_name": "Test Webhook Alert",
    "severity": "High",
    "status": "PROBLEM",
    "host": "Test Host",
    "ip": "192.168.1.1",
    "clock": "2026-06-05 12:00:00"
  }'
```

---

## 10. Cấu trúc thư mục

```
/opt/zabbix-discord-bot/
├── bot.py                  # Discord bot chính
├── zabbix_api.py           # Zabbix JSON-RPC client
├── .env                    # Cấu hình (không commit git)
├── .dedup_cache            # Cache dedup tự động (không chỉnh tay)
├── bot.log                 # Log file
└── venv/                   # Python virtualenv

~/zabbix_discord_bot/       # Thư mục làm việc (source)
├── bot.py
├── zabbix_api.py
├── anomaly_detector.py
├── .env
├── install_service.sh
└── zabbix-discord-bot.service
```

---

## Lưu ý bảo mật

- Không commit file `.env` lên Git (chứa token và password)
- `WEBHOOK_SECRET` phải đủ dài (≥ 20 ký tự ngẫu nhiên): `openssl rand -hex 20`
- User `zabbixbot` chạy bot không có login shell và không có home directory
- File `.env` được chmod 640, chỉ `zabbixbot` đọc được
- Discord token nên rotate định kỳ tại Discord Developer Portal

---
