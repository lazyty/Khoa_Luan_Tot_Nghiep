import asyncio
import logging
import json
from typing import Any, Dict, List, Optional

import aiohttp

log = logging.getLogger("zabbix_api")

# Zabbix script names that must be pre-created in
# Administration > Scripts (type: Script, Execute on: Zabbix agent)
SCRIPT_MAP = {
    # "restart_<service>": the script that runs "sudo systemctl restart <service>"
    # We build the name dynamically in bot.py for restart commands.
    "unblock_ip": "Unblock IP", # name as it appears in Zabbix UI
    "block_ip":   "Block IP", # name as it appears in Zabbix UI
}

class ZabbixAPIError(Exception):
    """Raised when Zabbix API returns an error block."""
    def __init__(self, code: int, message: str, data: str = ""):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"Zabbix API error {code}: {message}. {data}".strip())

class ZabbixAPI:
    """
    Minimal async Zabbix JSON-RPC client.

    Usage:
        api = ZabbixAPI("http://localhost/api_jsonrpc.php")
        await api.login("Admin", "zabbix")
        items = await api.item_get(host_id="10084", keys=["system.cpu.util"])
        await api.logout()
    """

    def __init__(self, url: str, timeout: int = 30):
        self.url = url
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._auth: Optional[str] = None
        self._req_id = 0
        self._session: Optional[aiohttp.ClientSession] = None

# Session management
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Content-Type": "application/json-rpc"},
                timeout=self.timeout,
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

# Core JSON-RPC call
    async def _call(self, method: str, params: Any) -> Any:
        """Send a JSON-RPC 2.0 request and return the result field."""
        self._req_id += 1
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._req_id,
        }
# Attach auth token if we have one (not needed for user.login)
        if self._auth:
            payload["auth"] = self._auth

        session = await self._get_session()
        log.debug("→ %s %s", method, json.dumps(params)[:200])
        try:
            async with session.post(self.url, json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise ZabbixAPIError(-1, "Network error", str(e)) from e

        if "error" in data:
            err = data["error"]
            raise ZabbixAPIError(
                code=err.get("code", -1),
                message=err.get("message", "Unknown error"),
                data=err.get("data", ""),
            )
        return data.get("result")

# Authentication
    async def login(self, user: str, password: str) -> str:
        """Authenticate and store the auth token."""
        result = await self._call("user.login", {"username": user, "password": password})
        self._auth = result
        log.info("Zabbix login OK (token length=%d)", len(self._auth or ""))
        return self._auth

    async def logout(self):
        """Invalidate the current auth token."""
        if self._auth:
            try:
                await self._call("user.logout", [])
            except ZabbixAPIError:
                pass   # best-effort
            finally:
                self._auth = None
                await self.close()

# Host methods
    @staticmethod
    def _resolve_availability(host: Dict[str, Any]) -> str:
        """
        Derive a single availability string from a host object.

        Zabbix ≤ 6.0: 'available' is a top-level field on the host.
          "1" = available, "2" = unavailable, "0" = unknown

        Zabbix ≥ 6.2: 'available' was removed from host output.
          Availability is now per-interface under selectInterfaces.
          interface["available"]: "1" = ok, "2" = failed, "0" = unknown
        """
        # Try legacy top-level field first
        if "available" in host:
            return host["available"]

        # Fall back to interfaces (Zabbix 6.2+)
        interfaces = host.get("interfaces") or []
        if not interfaces:
            return "0"  # unknown
        # If ANY interface is available (1), report green
        avail_values = [iface.get("available", "0") for iface in interfaces]
        if "1" in avail_values:
            return "1"
        if "2" in avail_values:
            return "2"
        return "0"

    async def host_get(self, host_id: str) -> Dict[str, Any]:
        """
        Fetch basic info for a single host.
        Works on Zabbix 5.x, 6.x and 7.x.
        """
        result = await self._call("host.get", {
            "output": ["hostid", "host", "name", "status"],
            "selectInterfaces": ["interfaceid", "available", "type"],
            "hostids": [host_id],
        })
        if not result:
            raise ZabbixAPIError(-1, f"Host {host_id!r} not found")
        h = result[0]
        h["available"] = self._resolve_availability(h)
        return h

    async def host_list(self) -> List[Dict[str, Any]]:
        """Return all enabled hosts with basic info. Works on Zabbix 5–7."""
        hosts = await self._call("host.get", {
            "output": ["hostid", "host", "name", "status"],
            "selectInterfaces": ["interfaceid", "available", "type"],
            "filter": {"status": "0"},   # 0 = monitored
            "sortfield": "name",
        })
        for h in hosts:
            h["available"] = self._resolve_availability(h)
        return hosts

# Item methods
    async def item_get(
        self, host_id: str, keys: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Fetch the latest value of a list of item keys from a host.
        Corresponds to the payload described in Section 5.4.2 of the thesis:

            {
              "method": "item.get",
              "params": {
                "output": ["itemid","key_","lastvalue","units"],
                "hostids": ["<host_id>"],
                "filter": {"key_": [...keys...]},
                "sortfield": "key_"
              }
            }
        """
        return await self._call("item.get", {
            "output": ["itemid", "key_", "lastvalue", "units", "name", "lastclock"],
            "hostids": [host_id],
            "filter": {"key_": keys},
            "sortfield": "key_",
        })

# Host macro methods
    async def _set_host_macro(self, host_id: str, macro_name: str, value: str) -> None:
        """
        Create or update a user macro on a host.
        macro_name should be in Zabbix format, e.g. '{$BLOCK_IP}'.
        Used to pass parameters to scripts in Zabbix 6.0 (which lacks manualinput API support).
        """
        existing = await self._call("usermacro.get", {
            "output": ["hostmacroid", "macro"],
            "hostids": [host_id],
            "filter": {"macro": macro_name},
        })
        if existing:
            await self._call("usermacro.update", {
                "hostmacroid": existing[0]["hostmacroid"],
                "value": value,
            })
        else:
            await self._call("usermacro.create", {
                "hostid": host_id,
                "macro": macro_name,
                "value": value,
            })
        log.debug("Set macro %s=%s on host %s", macro_name, value, host_id)

# Script execution
    async def _find_script_id(self, script_name: str) -> str:
        """
        Look up a Zabbix Script by name and return its scriptid.
        Scripts must be pre-created in Administration > Scripts.
        """
        results = await self._call("script.get", {
            "output": ["scriptid", "name"],
            "filter": {"name": script_name},
        })
        if not results:
            raise ZabbixAPIError(
                -1,
                f"Script '{script_name}' not found in Zabbix. "
                "Create it under Administration > Scripts first."
            )
        return results[0]["scriptid"]

    async def script_execute(
        self,
        host_id: str,
        script_name: str,
        params: Optional[Dict[str, str]] = None,
    ) -> str:
        if script_name.startswith("restart_"):
            service = script_name[len("restart_"):]
            _restart_map = {
                "ssh":    "Manual Restart SSH",
                "dns":    "Manual Restart DNS",
                "http":   "Manual Restart HTTP",
                "https":  "Manual Restart HTTP",
                "nginx":  "Manual Restart HTTP",
            }
            zabbix_script_name = _restart_map.get(service.lower())
            if not zabbix_script_name:
                raise ZabbixAPIError(-1, f"Service '{service}' không được hỗ trợ. Dùng: ssh, dns, http")
            manual_input = None
        elif script_name == "unblock_ip" and params and "ip" in params:
            zabbix_script_name = "Unblock IP"
            manual_input = params["ip"]
        elif script_name == "block_ip" and params and "ip" in params:
            zabbix_script_name = "Block IP"
            manual_input = params["ip"]
        else:
            zabbix_script_name = script_name
            manual_input = None

        # Zabbix 6.0: update the script command directly with the IP,
        # then execute, then restore original command.
        if manual_input and script_name in ("block_ip", "unblock_ip"):
            return await self._execute_ip_script(host_id, zabbix_script_name, manual_input)

        script_id = await self._find_script_id(zabbix_script_name)
        result = await self._call("script.execute", {
            "scriptid": script_id,
            "hostid": host_id,
        })

        if isinstance(result, dict):
            if result.get("response") != "success":
                raise ZabbixAPIError(-1, "Script execution failed", str(result))
            return result.get("value", "")
        return str(result)

    async def _execute_ip_script(
        self, host_id: str, script_name: str, ip: str
    ) -> str:
        """
        Zabbix 6.0 workaround: temporarily patch the script command with the
        real IP, execute it, then restore the original command.
        This bypasses the missing manualinput API support in Zabbix 6.0.
        """
        import re
        # Validate IP to prevent injection
        if not re.match(r'^[\d\.]+(/\d{1,2})?$', ip):
            raise ZabbixAPIError(-1, f"Invalid IP format: {ip}")

        # Fetch current script
        scripts = await self._call("script.get", {
            "output": ["scriptid", "name", "command"],
            "filter": {"name": script_name},
        })
        if not scripts:
            raise ZabbixAPIError(-1, f"Script '{script_name}' not found")
        script = scripts[0]
        script_id = script["scriptid"]
        original_command = script["command"]

        # Build patched command with real IP
        patched_command = original_command.replace("{$BLOCK_IP}", ip)

        try:
            # Patch the script command
            await self._call("script.update", {
                "scriptid": script_id,
                "command": patched_command,
            })
            # Execute
            result = await self._call("script.execute", {
                "scriptid": script_id,
                "hostid": host_id,
            })
        finally:
            # Always restore original command
            try:
                await self._call("script.update", {
                    "scriptid": script_id,
                    "command": original_command,
                })
            except ZabbixAPIError as e:
                log.error("Failed to restore script command for '%s': %s", script_name, e)

        if isinstance(result, dict):
            if result.get("response") != "success":
                raise ZabbixAPIError(-1, "Script execution failed", str(result))
            return result.get("value", "")
        return str(result)

# Problem/Event methods
    async def problem_get(self, host_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Return currently active PROBLEM events, sorted by severity DESC then clock DESC.

        NOTE: problem.get only allows sortfield "eventid" / "r_eventid".
        Sorting by "severity" is NOT supported and raises -32500.
        We fetch sorted by clock (newest first) and sort by severity in Python.
        """
        params: Dict[str, Any] = {
            "output": ["eventid", "name", "severity", "clock", "acknowledged"],
            "sortfield": "eventid",
            "sortorder": "DESC",
            "limit": 50,
        }
        if host_id:
            params["hostids"] = [host_id]

        problems = await self._call("problem.get", params)
        # Sort: severity DESC, then clock DESC
        problems.sort(key=lambda p: (int(p.get("severity", 0)), int(p.get("clock", 0))), reverse=True)
        return problems

    async def resolved_events_since(self, since_ts: int) -> List[Dict[str, Any]]:
        """
        Return RESOLVED (OK) trigger events that occurred after `since_ts`.
 
        Why event.get instead of problem.get
        --------------------------------------
        `problem.get` only returns *active* problems.  Once Zabbix resolves a
        problem it is removed from that endpoint, so the poller would never see it.
        `event.get` with value=0 (OK) is the correct way to catch recovery events.
 
        objectid on a trigger event is always the triggerid; we resolve hosts
        via a second trigger.get call — same pattern as new_problems_since.
        """
        events = await self._call("event.get", {
            "output":    ["eventid", "name", "clock", "severity", "objectid", "r_eventid"],
            "source":    0,   # trigger events only
            "object":    0,   # trigger objects only
            "value":     0,   # 0 = OK/RESOLVED  (1 = PROBLEM)
            "time_from": since_ts,
            "sortfield": "eventid",
            "sortorder": "ASC",
        })
        if not events:
            return []
 
        trigger_ids = list({e["objectid"] for e in events if e.get("objectid")})
        host_map: Dict[str, List[Dict[str, Any]]] = {}
        priority_map: Dict[str, str] = {}
        if trigger_ids:
            try:
                triggers = await self._call("trigger.get", {
                    "output":      ["triggerid", "priority"],
                    "triggerids":  trigger_ids,
                    "selectHosts": ["hostid", "name", "host"],
                })
                for t in triggers:
                    host_map[t["triggerid"]]     = t.get("hosts", [])
                    priority_map[t["triggerid"]] = str(t.get("priority", "0"))
            except ZabbixAPIError as e:
                log.warning("resolved_events_since: trigger.get failed (%s)", e)
 
        for ev in events:
            tid           = ev.get("objectid", "")
            ev["hosts"]   = host_map.get(tid, [])
            # Override the always-0 event severity with the trigger's real priority
            ev["severity"] = priority_map.get(tid, ev.get("severity", "0"))
 
        return events

    async def new_problems_since(self, since_ts: int) -> List[Dict[str, Any]]:
        """
        Return PROBLEM events that appeared after `since_ts` (UNIX timestamp).
        Used by the background alert-poller in the bot.
        """
        problems = await self._call("problem.get", {
            "output": ["eventid", "name", "severity", "clock", "acknowledged", "objectid"],
            "time_from": since_ts,
            "sortfield": "eventid",
            "sortorder": "ASC",
        })
        if not problems:
            return []

        # Collect unique trigger IDs (objectid == triggerid for trigger events)
        trigger_ids = list({p["objectid"] for p in problems if p.get("objectid")})

        host_map: Dict[str, List[Dict[str, Any]]] = {}
        if trigger_ids:
            try:
                triggers = await self._call("trigger.get", {
                    "output": ["triggerid"],
                    "triggerids": trigger_ids,
                    "selectHosts": ["hostid", "name", "host"],
                })
                for t in triggers:
                    host_map[t["triggerid"]] = t.get("hosts", [])
            except ZabbixAPIError as e:
                log.warning("new_problems_since: trigger.get failed (%s), hosts will be N/A", e)

        # Attach resolved hosts to each problem object
        for p in problems:
            p["hosts"] = host_map.get(p.get("objectid", ""), [])
        return problems
    
print("ZabbixAPI module loaded")