import logging
import os
import time
import json
from datetime import datetime, timezone
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from zabbix_api import ZabbixAPI, ZabbixAPIError
import asyncio
from zoneinfo import ZoneInfo
from aiohttp import web
# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("discord_bot")
# Configuration
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
ZABBIX_URL = os.getenv("ZABBIX_URL", "http://localhost/api_jsonrpc.php")
ZABBIX_USER = os.getenv("ZABBIX_USER", "Admin")
ZABBIX_PASS = os.getenv("ZABBIX_PASS", "zabbix")
ALLOWED_ROLE_IDS = [
    int(rid.strip())
    for rid in os.getenv("ALLOWED_ROLE_IDS", "").split(",")
    if rid.strip().isdigit()
]
COMMAND_CHANNEL_ID = int(os.getenv("COMMAND_CHANNEL_ID", "0"))
ALERT_CHANNEL_ID   = int(os.getenv("ALERT_CHANNEL_ID", ""))
POLL_INTERVAL      = int(os.getenv("POLL_INTERVAL", "60*5"))

# Webhook server config
WEBHOOK_ENABLED    = os.getenv("WEBHOOK_ENABLED", "true").lower() == "true"
WEBHOOK_PORT       = int(os.getenv("WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "") # X-Secret header value for authenticating incoming webhooks

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="?unused", intents=intents)

# Helpers
def _now_ts() -> float:
    return time.time()

def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(
        ts,
        tz=ZoneInfo("Asia/Ho_Chi_Minh")
    ).strftime("%Y-%m-%d %H:%M:%S GMT+7")

async def _get_zabbix() -> ZabbixAPI:
    api = ZabbixAPI(ZABBIX_URL)
    await api.login(ZABBIX_USER, ZABBIX_PASS)
    return api

def _has_permission(interaction: discord.Interaction) -> bool:
    """Check role and channel restrictions."""
    # Channel check
    if COMMAND_CHANNEL_ID != 0 and interaction.channel_id != COMMAND_CHANNEL_ID:
        return False
    # Role check
    if not ALLOWED_ROLE_IDS:
        return True
    if not hasattr(interaction.user, "roles"):
        return False
    author_role_ids = {role.id for role in interaction.user.roles}
    return bool(author_role_ids & set(ALLOWED_ROLE_IDS))

def _no_perm_embed() -> discord.Embed:
    return discord.Embed(
        title="🚫 Không có quyền",
        description="Bạn không có quyền thực hiện lệnh này hoặc sai kênh.",
        color=0xE53935,
    )

async def _safe_defer(interaction: discord.Interaction) -> bool:
    """
    Defer an interaction safely.
    Returns True if successful, False if the interaction already expired (10062).
    Discord gives only 3 seconds to acknowledge an interaction; on slow networks
    this window can be missed.  Rather than crashing, we log and return False so
    the caller can exit cleanly.
    """
    try:
        await interaction.response.defer()
        return True
    except discord.NotFound:
        log.warning(
            "Interaction expired before defer() (10062) — command=/%s user=%s",
            interaction.command.name if interaction.command else "?",
            interaction.user,
        )
        return False
    except discord.HTTPException as e:
        log.error("defer() HTTP error: %s", e)
        return False

SEV_EMOJI = {"0": "⚪", "1": "🔵", "2": "🟡", "3": "🟠", "4": "🔴", "5": "💀"}
SEV_NAME  = {
    "0": "Not classified", "1": "Information", "2": "Warning",
    "3": "Average",        "4": "High",        "5": "Disaster",
}
SEV_COLOR = {
    "0": 0x9E9E9E, # Not classified — grey
    "1": 0x1E88E5, # Information — blue
    "2": 0xFDD835, # Warning — yellow
    "3": 0xFB8C00, # Average — orange
    "4": 0xF4511E, # High — deep orange
    "5": 0xE53935, # Disaster — red
}
# Tracks the last poll timestamp for announce new problems
_last_poll_ts: int = int(_now_ts())
_posted_problem_ids: dict = {}
_posted_resolved_ids: dict = {}
_dedup_lock: asyncio.Lock = None  # type: ignore
_DEDUP_MAX              = 500
_DEDUP_TTL_SECONDS      = 5 * 60 
_WEBHOOK_DEDUP_TTL      = 60       

# File lưu dedup state
_DEDUP_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dedup_cache")

def _dedup_add(d: dict, key: str):
    """Thêm key vào dedup dict với timestamp hiện tại. Giới hạn _DEDUP_MAX entry."""
    d[key] = int(_now_ts())
    if len(d) > _DEDUP_MAX:
        oldest = min(d, key=lambda k: d[k])
        del d[oldest]

def _dedup_check(d: dict, key: str) -> bool:
    """True nếu key còn trong dedup và chưa hết TTL."""
    ts = d.get(key)
    if ts is None:
        return False
    if int(_now_ts()) - ts > _DEDUP_TTL_SECONDS:
        del d[key]
        return False
    return True

def _dedup_purge():
    """Xóa tất cả entry đã hết TTL khỏi cả hai dedup dicts."""
    now = int(_now_ts())
    for d in (_posted_problem_ids, _posted_resolved_ids):
        expired = [k for k, ts in d.items() if now - ts > _DEDUP_TTL_SECONDS]
        for k in expired:
            del d[k]

def _load_dedup_cache():
    """Load dedup dicts và last_poll_ts từ file (gọi lúc bot khởi động)."""
    global _posted_problem_ids, _posted_resolved_ids, _last_poll_ts
    try:
        with open(_DEDUP_CACHE_FILE, "r") as f:
            data = json.load(f)
        _posted_problem_ids  = data.get("problem_ids",  {})
        _posted_resolved_ids = data.get("resolved_ids", {})
        saved_ts = data.get("last_poll_ts", 0)
        if saved_ts > 0:
            _last_poll_ts = saved_ts
        # Purge expired entries ngay lúc load
        _dedup_purge()
        log.info(
            "Dedup cache loaded: %d problems, %d resolved, last_poll_ts=%s",
            len(_posted_problem_ids), len(_posted_resolved_ids), _fmt_ts(_last_poll_ts),
        )
    except FileNotFoundError:
        log.info("No dedup cache file found, starting fresh.")
    except Exception as e:
        log.warning("Could not load dedup cache: %s — starting fresh.", e)

def _save_dedup_cache():
    """Persist dedup dicts và last_poll_ts vào file."""
    _dedup_purge()   # dọn trước khi lưu
    try:
        with open(_DEDUP_CACHE_FILE, "w") as f:
            json.dump({
                "problem_ids":  _posted_problem_ids,
                "resolved_ids": _posted_resolved_ids,
                "last_poll_ts": _last_poll_ts,
            }, f)
    except Exception as e:
        log.warning("Could not save dedup cache: %s", e)

# Load cache ngay khi module khởi động
_load_dedup_cache()

# Background alert poller
@tasks.loop(seconds=POLL_INTERVAL)
async def poll_zabbix_alerts():
    """
    Polls Zabbix every POLL_INTERVAL seconds for problems that appeared
    after the previous poll and posts an embed to ALERT_CHANNEL_ID.
    """
    global _last_poll_ts, _posted_problem_ids, _posted_resolved_ids

    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ALERT_CHANNEL_ID)
        except discord.NotFound:
            log.error("poll_zabbix_alerts: channel %d does not exist", ALERT_CHANNEL_ID)
            return
        except discord.Forbidden:
            log.error("poll_zabbix_alerts: bot lacks permission to access channel %d", ALERT_CHANNEL_ID)
            return
        except discord.HTTPException as e:
            log.error("poll_zabbix_alerts: could not fetch channel %d — %s", ALERT_CHANNEL_ID, e)
            return

    since = _last_poll_ts
    now   = int(_now_ts())
    problems: list = []
    resolved: list = []

    try:
        api      = await _get_zabbix()
        problems = await api.new_problems_since(since_ts=since)
        await api.logout()
        log.debug("poll: fetched %d new problems", len(problems))
    except Exception as e:
        log.error("poll_zabbix_alerts: failed to fetch problems — %s", e)

    try:
        api_r    = await _get_zabbix()
        resolved = await api_r.resolved_events_since(since_ts=since)
        await api_r.logout()
        log.debug("poll: fetched %d resolved events", len(resolved))
    except Exception as e:
        log.error("poll_zabbix_alerts: failed to fetch resolved events — %s", e)

    # Advance cursor regardless of results to avoid re-announcing
    _last_poll_ts = now
    _save_dedup_cache()

    def _host_label(record: dict) -> str:
        hosts_in_event = record.get("hosts") or []
        if hosts_in_event:
            return ", ".join(
                h.get("name") or h.get("host", "?") for h in hosts_in_event
            )
        return "N/A"

    all_problems = problems
    all_resolved = resolved
    problems = [p for p in problems if not _dedup_check(_posted_problem_ids, p.get("eventid", ""))]
    resolved = [e for e in resolved if not _dedup_check(_posted_resolved_ids, e.get("eventid", ""))]

    skipped_p = len(all_problems) - len(problems)
    skipped_r = len(all_resolved) - len(resolved)
    if skipped_p or skipped_r:
        log.info("poll dedup: skipped %d problem(s), %d resolved (already sent by webhook)", skipped_p, skipped_r)

    for p in problems:
        sev      = str(p.get("severity", "0"))
        emoji    = SEV_EMOJI.get(sev, "⚪")
        sev_name = SEV_NAME.get(sev, "Unknown")
        color    = SEV_COLOR.get(sev, 0x7289DA)
        ts       = int(p.get("clock", 0))
        host_label = _host_label(p)
        eid = p.get("eventid", "")

        embed = discord.Embed(
            title=f"{emoji} [{sev_name}] {p.get('name', 'Unknown problem')}",
            color=color,
            timestamp=datetime.now(tz=timezone.utc),
        )
        embed.add_field(name="Host", value=host_label, inline=True)
        embed.add_field(name="Severity", value=sev_name, inline=True)
        embed.add_field(name="Acknowledged", value="✅ Có" if p.get("acknowledged") == "1" else "❌ Không", inline=True)
        embed.add_field(name="Event ID", value=f"`{eid}`", inline=True)
        embed.add_field(name="Thời điểm", value=f"`{_fmt_ts(ts)}`" if ts else "N/A", inline=True)
        embed.set_footer(text="Zabbix Alert")
        # Acquire lock per-event: webhook có thể chen vào giữa các event của polling
        async with _dedup_lock:
            if _dedup_check(_posted_problem_ids, eid):
                log.info("poll dedup (late): skipped eventid=%s (webhook vừa gửi)", eid)
                continue
            try:
                await channel.send(content="@everyone", embed=embed)
                _dedup_add(_posted_problem_ids, eid)
                log.info("Alert posted | eventid=%s sev=%s host=%s", eid, sev_name, host_label)
            except discord.DiscordException as e:
                log.error("poll_zabbix_alerts: failed to send embed — %s", e)

    for ev in resolved:
        ts         = int(ev.get("clock", 0))
        host_label = _host_label(ev)
        sev        = str(ev.get("severity", "0"))
        sev_name   = SEV_NAME.get(sev, "Unknown")
        eid = ev.get("eventid", "")

        embed = discord.Embed(
            title=f"✅ [RESOLVED] {ev.get('name', 'Unknown problem')}",
            color=0x43A047,   # green
            timestamp=datetime.now(tz=timezone.utc),
        )
        embed.add_field(name="Host", value=host_label, inline=True)
        embed.add_field(name="Severity", value=sev_name, inline=True)
        embed.add_field(name="Event ID", value=f"`{eid}`", inline=True)
        embed.add_field(name="Thời điểm giải quyết", value=f"`{_fmt_ts(ts)}`" if ts else "N/A", inline=True)
        embed.set_footer(text="Zabbix Alert")
        async with _dedup_lock:
            if _dedup_check(_posted_resolved_ids, eid):
                log.info("poll dedup (late): skipped resolved eventid=%s (webhook vừa gửi)", eid)
                continue
            try:
                await channel.send(embed=embed)
                _dedup_add(_posted_resolved_ids, eid)
                log.info("Resolved posted | eventid=%s host=%s", eid, host_label)
            except discord.DiscordException as e:
                log.error("poll_zabbix_alerts: failed to send resolved embed — %s", e)

    _save_dedup_cache()

@poll_zabbix_alerts.before_loop
async def before_poll():
    await bot.wait_until_ready()

# Webhook server
async def _webhook_handler(request: web.Request) -> web.Response:
    """
    POST /zabbix-alert
    Nhận payload JSON từ Zabbix Media Type (HTTP) và gửi embed lên Discord.

    Expected JSON body (configure in Zabbix Media Type):
    {
      "event_id":          "{EVENT.ID}",
      "recovery_event_id": "{EVENT.RECOVERY.ID}",
      "event_name":        "{EVENT.NAME}",
      "severity":          "{TRIGGER.SEVERITY}",
      "status":            "{TRIGGER.STATUS}",
      "host":              "{HOST.NAME}",
      "ip":                "{HOST.IP}",
      "clock":             "{EVENT.DATE} {EVENT.TIME}"
    }
    """
    # Auth check
    if WEBHOOK_SECRET:
        incoming = request.headers.get("X-Secret", "")
        if incoming != WEBHOOK_SECRET:
            log.warning("Webhook: rejected request — invalid X-Secret from %s", request.remote)
            return web.Response(status=403, text="Forbidden")
    # Parse body
    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    event_id          = str(payload.get("event_id",          "?"))
    recovery_event_id = str(payload.get("recovery_event_id", ""))
    event_name = payload.get("event_name", "Unknown problem")
    severity   = payload.get("severity",   "Not classified").strip()
    status     = payload.get("status",     "PROBLEM").strip().upper()
    # Zabbix dùng "OK" cho recovery, normalize về "RESOLVED"
    if status == "OK":
        status = "RESOLVED"
    host    = payload.get("host",    "N/A")
    clock   = payload.get("clock",   "")    # thời điểm problem bắt đầu
    r_clock = payload.get("r_clock", "")    # thời điểm resolved ({EVENT.RECOVERY.DATE} {EVENT.RECOVERY.TIME})

    # Zabbix đôi khi không resolve macro → giữ nguyên literal như "{EVENT.RECOVERY.ID}"
    # Coi những giá trị đó là rỗng
    def _is_unresolved(val: str) -> bool:
        return val.startswith("{") and val.endswith("}")

    if _is_unresolved(recovery_event_id):
        recovery_event_id = ""
    if _is_unresolved(status):
        # {TRIGGER.STATUS} không được resolve — suy ra từ recovery_event_id:
        # nếu có recovery_event_id hợp lệ thì đây là RESOLVED, ngược lại là PROBLEM
        status = "RESOLVED" if recovery_event_id else "PROBLEM"
        log.warning("Webhook: {TRIGGER.STATUS} unresolved, inferred status=%s from recovery_event_id=%r", status, recovery_event_id)
    if _is_unresolved(severity):
        severity = "Not classified"
    if _is_unresolved(event_name):
        event_name = "Unknown problem"

    log.info("Webhook received | event=%s recovery=%s status=%s host=%s", event_id, recovery_event_id, status, host)

    # ── Dedup ──────────────────────────────────────────────────────────────
    # Webhook check dedup để chặn Zabbix retry gửi trùng cùng 1 eventid.
    # Khác polling: webhook dùng _WEBHOOK_DEDUP_TTL ngắn (30s) thay vì 5 phút
    # để chỉ lọc retry nhanh của Zabbix, không block webhook hợp lệ sau này.
    # PROBLEM  → dedup bằng problem event ID
    # RESOLVED → dedup bằng recovery event ID
    if status == "PROBLEM":
        dedup_id  = event_id
        dedup_set = _posted_problem_ids
    else:
        dedup_id  = recovery_event_id if recovery_event_id and recovery_event_id not in ("", "?") else event_id
        dedup_set = _posted_resolved_ids

    sev_lower = severity.lower()
    if status == "RESOLVED":
        color = 0x43A047
        title = f"✅ [RESOLVED] {event_name}"
        mention = ""
    else:
        color   = SEV_COLOR.get(_sev_key(sev_lower), 0x7289DA)
        emoji   = SEV_EMOJI.get(_sev_key(sev_lower), "🔔")
        title   = f"{emoji} [{severity}] {event_name}"
        mention = "@everyone"

    embed = discord.Embed(
        title=title,
        color=color,
        timestamp=datetime.now(tz=timezone.utc),
    )
    embed.add_field(name="Host", value=host, inline=True)
    embed.add_field(name="Severity", value=severity, inline=True)
    embed.add_field(name="Event ID", value=f"`{event_id}`", inline=True)
    if status == "RESOLVED":
        display_time = r_clock if r_clock and not _is_unresolved(r_clock) else clock
        if display_time:
            embed.add_field(name="Thời điểm giải quyết", value=f"`{display_time}`", inline=True)
    else:
        if clock:
            embed.add_field(name="Thời điểm", value=f"`{clock}`", inline=True)
    embed.set_footer(text="Zabbix Alert")

    # Gửi Discord — acquire lock để webhook được ưu tiên trước polling.
    # Toàn bộ check-send-add là atomic: polling sẽ chờ lock này release
    # trước khi kiểm tra cache, tránh gửi trùng khi cả hai xảy ra cùng lúc.
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if channel is None:
        log.error("Webhook: ALERT_CHANNEL_ID %d not found", ALERT_CHANNEL_ID)
        return web.Response(status=500, text="Channel not found")
    async with _dedup_lock:
        # Check dedup TTL ngắn để lọc Zabbix retry (gửi 2 lần trong vài giây)
        ts = dedup_set.get(dedup_id)
        if ts and int(_now_ts()) - ts < _WEBHOOK_DEDUP_TTL:
            log.info("Webhook dedup (retry) skip | dedup_id=%s (sent %ds ago)", dedup_id, int(_now_ts()) - ts)
            return web.Response(status=200, text="OK (dedup)")
        try:
            await channel.send(content=mention if mention else None, embed=embed)
            # Thêm vào cache SAU KHI gửi thành công để polling bỏ qua
            _dedup_add(dedup_set, dedup_id)
            _save_dedup_cache()
            log.info("Webhook alert sent | event=%s status=%s host=%s", event_id, status, host)
        except discord.DiscordException as e:
            log.error("Webhook: failed to send Discord message — %s", e)
            return web.Response(status=500, text=str(e))
    return web.Response(status=200, text="OK")

def _sev_key(sev_lower: str) -> str:
    """Map Zabbix severity string → SEV_COLOR/SEV_EMOJI key."""
    mapping = {
        "not classified": "0", "information": "1",
        "warning": "2",        "average": "3",
        "high": "4",           "disaster": "5",
    }
    return mapping.get(sev_lower, "0")

async def _start_webhook_server():
    """Start the aiohttp webhook server on WEBHOOK_PORT."""
    if not WEBHOOK_ENABLED:
        log.info("Webhook server disabled (WEBHOOK_ENABLED=false)")
        return
    app = web.Application()
    app.router.add_post("/zabbix-alert",  _webhook_handler)
    app.router.add_post("/zabbix-action", _action_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    log.info("Webhook server listening on port %d", WEBHOOK_PORT)

async def _action_handler(request: web.Request) -> web.Response:
    """
    POST /zabbix-action
    Nhận thông báo khi Zabbix Action tự động thực thi script (block IP, restart service...).

    Expected JSON body (configure in Zabbix Action → Operations → Send message):
    {
      "event_id":    "{EVENT.ID}",
      "event_name":  "{EVENT.NAME}",
      "action_name": "{ACTION.NAME}",
      "host":        "{HOST.NAME}",
      "ip":          "{HOST.IP}",
      "severity":    "{TRIGGER.SEVERITY}",
      "clock":       "{EVENT.DATE} {EVENT.TIME}"
    }
    """
    # Auth
    if WEBHOOK_SECRET:
        if request.headers.get("X-Secret", "") != WEBHOOK_SECRET:
            log.warning("Action webhook: rejected — invalid X-Secret from %s", request.remote)
            return web.Response(status=403, text="Forbidden")
    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    event_id    = str(payload.get("event_id",    "?"))
    event_name  = payload.get("event_name",  "Unknown trigger")
    action_name = payload.get("action_name", "Unknown action")
    host        = payload.get("host",        "N/A")
    ip          = payload.get("ip",          "N/A")
    severity    = payload.get("severity",    "N/A")
    clock       = payload.get("clock",       "")
    log.info("Action webhook | event=%s action=%s host=%s", event_id, action_name, host)
    # Chọn icon dựa theo tên action 
    name_lower = action_name.lower()
    if "block" in name_lower:
        icon        = "🔒"
        color       = 0xF4511E
        description = f"Đã tự động **chặn IP** `{ip}` trên host **{host}**"
    elif "unblock" in name_lower:
        icon        = "🔓"
        color       = 0x43A047
        description = f"Đã tự động **gỡ chặn IP** `{ip}` trên host **{host}**"
    elif "restart" in name_lower:
        icon        = "🔄"
        color       = 0xFB8C00
        description = f"Đã tự động **restart dịch vụ** trên host **{host}**"
    else:
        icon        = "⚙️"
        color       = 0x7289DA
        description = f"Action thực thi trên host **{host}**"
    embed = discord.Embed(
        title=f"{icon} [AUTO ACTION] {action_name}",
        description=description,
        color=color,
        timestamp=datetime.now(tz=timezone.utc),
    )
    embed.add_field(name="Host",        value=host,              inline=True)
    embed.add_field(name="IP",          value=f"`{ip}`",         inline=True)
    embed.add_field(name="Severity",    value=severity,          inline=True)
    embed.add_field(name="Trigger",     value=event_name,        inline=True)
    embed.add_field(name="Event ID",    value=f"`{event_id}`",   inline=True)
    if clock:
        embed.add_field(name="Thời điểm", value=f"`{clock}`",   inline=True)
    embed.set_footer(text="Zabbix Alert")
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if channel is None:
        log.error("Action webhook: channel %d not found", ALERT_CHANNEL_ID)
        return web.Response(status=500, text="Channel not found")

    try:
        await channel.send(embed=embed)
        log.info("Action embed sent | event=%s action=%s", event_id, action_name)
    except discord.DiscordException as e:
        log.error("Action webhook: Discord send failed — %s", e)
        return web.Response(status=500, text=str(e))
    return web.Response(status=200, text="OK")

# Bot events
@bot.event
async def on_ready():
    log.info("Bot connected as %s (ID: %s)", bot.user, bot.user.id)
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="Zabbix | /help")
    )
    if not poll_zabbix_alerts.is_running():
        poll_zabbix_alerts.change_interval(seconds=POLL_INTERVAL)
        poll_zabbix_alerts.start()
        log.info("Alert poller started (interval=%ds, channel=%d)", POLL_INTERVAL, ALERT_CHANNEL_ID)

async def _setup_hook():
    """
    Chạy trong event loop trước khi bot connect — đúng nơi để init Lock và
    start webhook server. Gán vào bot.setup_hook thay vì dùng @bot.event
    vì setup_hook không phải dispatch event thông thường.
    """
    global _dedup_lock
    _dedup_lock = asyncio.Lock()   # tạo trong event loop, tránh "no running event loop"
    asyncio.create_task(_sync_commands())
    asyncio.create_task(_start_webhook_server())

bot.setup_hook = _setup_hook

async def _sync_commands():
    try:
        synced = await bot.tree.sync()
        log.info("Slash commands synced (%d commands)", len(synced))
    except Exception as e:
        log.error("Failed to sync slash commands: %s", e)

# /help
@bot.tree.command(name="help", description="Hiển thị danh sách lệnh của bot")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Zabbix Middleware Bot – Hướng dẫn",
        description=(
            "Bot điều khiển Zabbix từ Discord.\n"
            "Các lệnh dưới đây gọi **Zabbix API** và trả kết quả ngay trên kênh này."
        ),
        color=0x7289DA,
    )
    embed.add_field(
        name="🔄 `/restart <host_id> <service_name>`",
        value="Khởi động lại dịch vụ trên máy chủ.\nVí dụ: `/restart 10084 nginx`",
        inline=False,
    )
    embed.add_field(
        name="🔒 `/block <host_id> <ip_address>`",
        value="Chặn IP bằng rule DROP iptables trên máy chủ.\nVí dụ: `/block 10084 203.0.113.45`",
        inline=False,
    )
    embed.add_field(
        name="🔓 `/unblock <host_id> <ip_address>`",
        value="Gỡ bỏ rule DROP iptables cho IP đã bị chặn.\nVí dụ: `/unblock 10084 203.0.113.45`",
        inline=False,
    )
    embed.add_field(
        name="📊 `/status <host_id>`",
        value="Truy vấn nhanh CPU, RAM và các dịch vụ trọng yếu.\nVí dụ: `/status 10084`",
        inline=False,
    )
    embed.add_field(
        name="🖥️ `/hosts`",
        value="Liệt kê tất cả host đang được giám sát trong Zabbix.",
        inline=False,
    )
    embed.add_field(
        name="🚨 `/problems [host_id]`",
        value="Hiển thị các sự cố đang kích hoạt (PROBLEM), có thể lọc theo host.",
        inline=False,
    )
    embed.set_footer(text="Zabbix Monitoring System – UIT KLTN 2025-2026")
    await interaction.response.send_message(embed=embed)

# /restart
@bot.tree.command(name="restart", description="Khởi động lại một dịch vụ trên máy chủ qua Zabbix API")
@app_commands.describe(
    host_id="ID của máy chủ trên Zabbix (vd: 10084)",
    service_name="Tên dịch vụ cần khởi động lại (vd: nginx, sshd)",
)
async def slash_restart(interaction: discord.Interaction, host_id: str, service_name: str):
    if not _has_permission(interaction):
        await interaction.response.send_message(embed=_no_perm_embed(), ephemeral=True)
        return

    # Defer immediately — shows "Bot is thinking…" while we call Zabbix
    if not await _safe_defer(interaction):
        return
    t_start = _now_ts()

    log.info("CMD /restart | host=%s service=%s | user=%s", host_id, service_name, interaction.user)

    try:
        api = await _get_zabbix()
        result = await api.script_execute(host_id=host_id, script_name=f"restart_{service_name}")
        await api.logout()

        latency = round(_now_ts() - t_start, 3)
        embed = discord.Embed(title="✅ Khởi động lại thành công", color=0x43A047)
        embed.add_field(name="Host ID",       value=f"`{host_id}`",      inline=True)
        embed.add_field(name="Dịch vụ",       value=f"`{service_name}`", inline=True)
        embed.add_field(name="Kết quả API",   value=f"```{result}```",   inline=False)
        embed.add_field(name="Alert Latency", value=f"`{latency}s`",     inline=True)
        embed.add_field(name="Thực hiện lúc", value=f"`<t:{int(_now_ts())}:F>`", inline=True)
        embed.set_footer(text=f"Yêu cầu bởi: {interaction.user}")
        await interaction.followup.send(embed=embed)
        log.info("CMD /restart OK | host=%s service=%s | latency=%.3fs", host_id, service_name, latency)

    except ZabbixAPIError as e:
        latency = round(_now_ts() - t_start, 3)
        embed = discord.Embed(title="❌ Lỗi Zabbix API", description=str(e), color=0xE53935)
        embed.add_field(name="Host ID",       value=f"`{host_id}`",      inline=True)
        embed.add_field(name="Dịch vụ",       value=f"`{service_name}`", inline=True)
        embed.add_field(name="Alert Latency", value=f"`{latency}s`",     inline=True)
        embed.set_footer(text=f"Yêu cầu bởi: {interaction.user}")
        await interaction.followup.send(embed=embed)
        log.error("CMD /restart FAIL | %s", e)

    except Exception as e:
        await interaction.followup.send(
            embed=discord.Embed(title="❌ Lỗi không mong đợi", description=str(e), color=0xE53935)
        )
        log.exception("CMD /restart UNEXPECTED")

# /block
@bot.tree.command(name="block", description="Chặn một địa chỉ IP bằng rule DROP iptables trên máy chủ")
@app_commands.describe(
    host_id="ID của máy chủ trên Zabbix (vd: 10084)",
    ip_address="Địa chỉ IP cần chặn (vd: 203.0.113.45)",
)
async def slash_block(interaction: discord.Interaction, host_id: str, ip_address: str):
    if not _has_permission(interaction):
        await interaction.response.send_message(embed=_no_perm_embed(), ephemeral=True)
        return
 
    if not await _safe_defer(interaction):
        return
    t_start = _now_ts()
 
    log.info("CMD /block | host=%s ip=%s | user=%s", host_id, ip_address, interaction.user)
 
    try:
        api = await _get_zabbix()
        result = await api.script_execute(
            host_id=host_id, script_name="block_ip", params={"ip": ip_address}
        )
        await api.logout()
 
        latency = round(_now_ts() - t_start, 3)
        embed = discord.Embed(title="🔒 Chặn IP thành công", color=0xFB8C00)
        embed.add_field(name="Host ID",         value=f"`{host_id}`",    inline=True)
        embed.add_field(name="IP bị chặn",      value=f"`{ip_address}`", inline=True)
        embed.add_field(name="Kết quả API",      value=f"```{result}```", inline=False)
        embed.add_field(name="Alert Latency",    value=f"`{latency}s`",   inline=True)
        embed.add_field(name="Thực hiện lúc",    value=f"`<t:{int(_now_ts())}:F>`", inline=True)
        embed.set_footer(text=f"Yêu cầu bởi: {interaction.user}")
        await interaction.followup.send(embed=embed)
        log.info("CMD /block OK | host=%s ip=%s | latency=%.3fs", host_id, ip_address, latency)
 
    except ZabbixAPIError as e:
        latency = round(_now_ts() - t_start, 3)
        embed = discord.Embed(title="❌ Lỗi Zabbix API", description=str(e), color=0xE53935)
        embed.add_field(name="Host ID",       value=f"`{host_id}`",    inline=True)
        embed.add_field(name="IP",            value=f"`{ip_address}`", inline=True)
        embed.add_field(name="Alert Latency", value=f"`{latency}s`",  inline=True)
        embed.set_footer(text=f"Yêu cầu bởi: {interaction.user}")
        await interaction.followup.send(embed=embed)
        log.error("CMD /block FAIL | %s", e)
 
    except Exception as e:
        await interaction.followup.send(
            embed=discord.Embed(title="❌ Lỗi không mong đợi", description=str(e), color=0xE53935)
        )
        log.exception("CMD /block UNEXPECTED")

# /unblock
@bot.tree.command(name="unblock", description="Gỡ bỏ rule DROP iptables cho một IP trên máy chủ")
@app_commands.describe(
    host_id="ID của máy chủ trên Zabbix (vd: 10084)",
    ip_address="Địa chỉ IP cần gỡ chặn (vd: 203.0.113.45)",
)
async def slash_unblock(interaction: discord.Interaction, host_id: str, ip_address: str):
    if not _has_permission(interaction):
        await interaction.response.send_message(embed=_no_perm_embed(), ephemeral=True)
        return

    if not await _safe_defer(interaction):
        return
    t_start = _now_ts()

    log.info("CMD /unblock | host=%s ip=%s | user=%s", host_id, ip_address, interaction.user)

    try:
        api = await _get_zabbix()
        result = await api.script_execute(
            host_id=host_id, script_name="unblock_ip", params={"ip": ip_address}
        )
        await api.logout()

        latency = round(_now_ts() - t_start, 3)
        embed = discord.Embed(title="✅ Gỡ chặn IP thành công", color=0x43A047)
        embed.add_field(name="Host ID", value=f"`{host_id}`",    inline=True)
        embed.add_field(name="IP được gỡ chặn", value=f"`{ip_address}`", inline=True)
        embed.add_field(name="Kết quả API", value=f"```{result}```", inline=False)
        embed.add_field(name="Alert Latency", value=f"`{latency}s`",  inline=True)
        embed.add_field(name="Thực hiện lúc", value=f"`<t:{int(_now_ts())}:F>`", inline=True)
        embed.set_footer(text=f"Yêu cầu bởi: {interaction.user}")
        await interaction.followup.send(embed=embed)
        log.info("CMD /unblock OK | host=%s ip=%s | latency=%.3fs", host_id, ip_address, latency)

    except ZabbixAPIError as e:
        latency = round(_now_ts() - t_start, 3)
        embed = discord.Embed(title="❌ Lỗi Zabbix API", description=str(e), color=0xE53935)
        embed.add_field(name="Host ID",       value=f"`{host_id}`",    inline=True)
        embed.add_field(name="IP",            value=f"`{ip_address}`", inline=True)
        embed.add_field(name="Alert Latency", value=f"`{latency}s`",  inline=True)
        embed.set_footer(text=f"Yêu cầu bởi: {interaction.user}")
        await interaction.followup.send(embed=embed)
        log.error("CMD /unblock FAIL | %s", e)

    except Exception as e:
        await interaction.followup.send(
            embed=discord.Embed(title="❌ Lỗi không mong đợi", description=str(e), color=0xE53935)
        )
        log.exception("CMD /unblock UNEXPECTED")

# /status
ITEM_KEYS = {
    "system.cpu.util":         "CPU (%)",
    "vm.memory.size[pused]":   "RAM used (%)",
    "vfs.fs.size[/,pused]":    "Disk / used (%)",
    "net.if.in[eth0]":         "Net In (B/s)",
    "net.if.out[eth0]":        "Net Out (B/s)",
    "proc.num[nginx]":         "nginx",
    "proc.num[sshd]":          "sshd",
    "proc.num[zabbix_agentd]": "Zabbix Agent",
}

@bot.tree.command(name="status", description="Truy vấn CPU, RAM và trạng thái dịch vụ của một host")
@app_commands.describe(host_id="ID của máy chủ trên Zabbix (vd: 10084)")
async def slash_status(interaction: discord.Interaction, host_id: str):
    if not await _safe_defer(interaction):
        return
    t_start = _now_ts()
    log.info("CMD /status | host=%s | user=%s", host_id, interaction.user)
    try:
        api = await _get_zabbix()
        items = await api.item_get(host_id=host_id, keys=list(ITEM_KEYS.keys()))
        host_info = await api.host_get(host_id=host_id)
        await api.logout()

        latency = round(_now_ts() - t_start, 3)
        hostname = host_info.get("host", host_id)
        visible_name = host_info.get("name", hostname)
        avail = host_info.get("available", "0")

        if avail == "1":
            embed_color = 0x1E88E5
            title_icon  = "📊"
        elif avail == "2":
            embed_color = 0xE53935
            title_icon  = "🔴"
        else:
            embed_color = 0x9E9E9E
            title_icon  = "⚫"

        embed = discord.Embed(
            title=f"{title_icon} Trạng thái host: {visible_name}",
            description=f"Host ID: `{host_id}` | Zabbix name: `{hostname}`",
            color=embed_color,
            timestamp=datetime.now(tz=timezone.utc),
        )

        if avail == "2":
            embed.description += (
                "\n\n🚨 **Host hiện OFFLINE** — Zabbix agent không phản hồi.\n"
                "Dữ liệu bên dưới là **lần đo cuối cùng** trước khi mất kết nối, "
                "**không phải giá trị hiện tại**."
            )
        elif avail == "0":
            embed.description += (
                "\n\n⚫ **Trạng thái host không xác định** — dữ liệu có thể đã cũ."
            )

        if not items:
            embed.description += "\n\n⚠️ Không tìm thấy item nào. Kiểm tra lại host ID và template."
        else:
            for item in items:
                key   = item.get("key_", "")
                label = ITEM_KEYS.get(key, key)
                value = item.get("lastvalue", "N/A")
                units = item.get("units", "")
                lastclock  = int(item.get("lastclock", 0))
                if avail != "1" and lastclock:
                    field_value = (
                        f"`{value}{' ' + units if units else ''}`\n"
                        f"⏱ {_fmt_ts(lastclock)}"
                    )
                else:
                    field_value = f"`{value}{' ' + units if units else ''}`"

                embed.add_field(name=label, value=field_value, inline=True)

        embed.add_field(name="Latency truy vấn", value=f"`{latency}s`", inline=False)
        embed.set_footer(text=f"Yêu cầu bởi: {interaction.user}")
        await interaction.followup.send(embed=embed)
        log.info("CMD /status OK | host=%s | items=%d | latency=%.3fs", host_id, len(items), latency)

    except ZabbixAPIError as e:
        embed = discord.Embed(title="❌ Lỗi Zabbix API", description=str(e), color=0xE53935)
        embed.add_field(name="Host ID", value=f"`{host_id}`", inline=True)
        embed.set_footer(text=f"Yêu cầu bởi: {interaction.user}")
        await interaction.followup.send(embed=embed)
        log.error("CMD /status FAIL | %s", e)

    except Exception as e:
        await interaction.followup.send(
            embed=discord.Embed(title="❌ Lỗi không mong đợi", description=str(e), color=0xE53935)
        )
        log.exception("CMD /status UNEXPECTED")

# /hosts
@bot.tree.command(name="hosts", description="Liệt kê tất cả host đang được giám sát trong Zabbix")
async def slash_hosts(interaction: discord.Interaction):
    if not await _safe_defer(interaction):
        return
    try:
        api   = await _get_zabbix()
        hosts = await api.host_list()
        await api.logout()
        if not hosts:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="ℹ️ Không có host nào",
                    description="Zabbix chưa có host nào được cấu hình.",
                    color=0x9E9E9E,
                )
            )
            return
        lines = []
        for h in hosts:
            avail  = h.get("available", "0")
            status = "🟢 Online" if avail == "1" else ("🔴 Offline" if avail == "2" else "⚫ Unknown")
            lines.append(f"• `{h['hostid']}` **{h.get('name', h['host'])}** — {status}")

        embed = discord.Embed(
            title=f"🖥️ Danh sách host ({len(hosts)})",
            description="\n".join(lines[:25]),
            color=0x7289DA,
        )
        if len(hosts) > 25:
            embed.set_footer(text=f"Chỉ hiển thị 25/{len(hosts)} host đầu tiên. | Yêu cầu bởi: {interaction.user}")
        else:
            embed.set_footer(text=f"Yêu cầu bởi: {interaction.user}")
        await interaction.followup.send(embed=embed)

    except ZabbixAPIError as e:
        await interaction.followup.send(
            embed=discord.Embed(title="❌ Lỗi Zabbix API", description=str(e), color=0xE53935)
        )
    except Exception as e:
        await interaction.followup.send(
            embed=discord.Embed(title="❌ Lỗi không mong đợi", description=str(e), color=0xE53935)
        )
        log.exception("CMD /hosts UNEXPECTED")

# /problems     
@bot.tree.command(name="problems", description="Hiển thị các sự cố đang kích hoạt (PROBLEM) trong Zabbix")
@app_commands.describe(host_id="(Tuỳ chọn) Lọc theo ID máy chủ cụ thể")
async def slash_problems(interaction: discord.Interaction, host_id: Optional[str] = None):
    if not await _safe_defer(interaction):
        return
    try:
        api      = await _get_zabbix()
        problems = await api.problem_get(host_id=host_id)
        await api.logout()

        if not problems:
            desc = "Không có sự cố nào đang kích hoạt."
            if host_id:
                desc += f" (host `{host_id}`)"
            await interaction.followup.send(
                embed=discord.Embed(title="✅ Hệ thống bình thường", description=desc, color=0x43A047)
            )
            return
        # Sort problems by time (newest first)
        problems = sorted(problems, key=lambda p: int(p.get("clock", 0)), reverse=True)
        embed = discord.Embed(
            title=f"🚨 Sự cố đang kích hoạt: {len(problems)}",
            color=0xE53935,
            timestamp=datetime.now(tz=timezone.utc),
        )
        for p in problems[:10]:
            sev      = str(p.get("severity", "0"))
            emoji    = SEV_EMOJI.get(sev, "⚪")
            sev_name = SEV_NAME.get(sev, "Unknown")
            ts       = int(p.get("clock", 0))
            since = _fmt_ts(ts) if ts else "N/A"
            embed.add_field(
                name=f"{emoji} [{sev_name}] {p.get('name', 'Unknown')}",
                value=f"Event ID: `{p.get('eventid', '?')}` | Kể từ: `{since}`",
                inline=False,
            )
        footer_text = f"Yêu cầu bởi: {interaction.user}"
        if len(problems) > 10:
            footer_text += f" | Chỉ hiển thị 10/{len(problems)} sự cố đầu tiên"
        embed.set_footer(text=footer_text)
        await interaction.followup.send(embed=embed)

    except ZabbixAPIError as e:
        await interaction.followup.send(
            embed=discord.Embed(title="❌ Lỗi Zabbix API", description=str(e), color=0xE53935)
        )
    except Exception as e:
        await interaction.followup.send(
            embed=discord.Embed(title="❌ Lỗi không mong đợi", description=str(e), color=0xE53935)
        )
        log.exception("CMD /problems UNEXPECTED")
# Entry point
def main():
    if not DISCORD_TOKEN:
        log.critical("DISCORD_TOKEN is not set. Please configure .env")
        raise SystemExit(1)
    log.info("Starting Zabbix Discord Bot.....")
    bot.run(DISCORD_TOKEN, log_handler=None)

if __name__ == "__main__":
    main()