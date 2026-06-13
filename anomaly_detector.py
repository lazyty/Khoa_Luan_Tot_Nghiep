#!/usr/bin/env python3
import sys
import re
import os
from datetime import datetime, timedelta
from collections import defaultdict

LOG_FILE = "/var/log/messages"
SUSPICIOUS_HOURS = range(0, 5)

def read_log():
    try:
        with open(LOG_FILE, "r") as f:
            return f.readlines()
    except:
        return []

def parse_log_time(line):
    try:
        match_iso = re.search(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', line)
        if match_iso:
            return datetime.fromisoformat(match_iso.group(1))

        current_year = datetime.now().year
        match_old = re.search(r'^([A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2})', line)
        if match_old:
            timestamp_str = match_old.group(1)
            timestamp_str = re.sub(r'\s+', ' ', timestamp_str)
            return datetime.strptime(f"{current_year} {timestamp_str}", "%Y %b %d %H:%M:%S")
        return None
    except:
        return None

def get_recent_failed_logins(filter_type="all"):
    lines = read_log()
    now = datetime.now()
    one_min_ago = now - timedelta(minutes=1)
    recent_events = []

    for line in lines:
        log_time = parse_log_time(line)
        if not log_time or log_time < one_min_ago:
            continue
        if filter_type == "ssh":
            if "Failed password" in line and "sshd" in line:
                recent_events.append((log_time, line))
        elif filter_type == "local":
            if ("FAILED LOGIN" in line or "Failed password" in line) and "sshd" not in line:
                recent_events.append((log_time, line))
        else:
            if "Failed password" in line or "FAILED LOGIN" in line:
                recent_events.append((log_time, line))

    return recent_events

def frequency_check():
    events = get_recent_failed_logins(filter_type="ssh")
    count = len(events)
    is_night = any(log_time.hour in SUSPICIOUS_HOURS for log_time, _ in events if log_time)

    if is_night:
        return 1 if count >= 3 else 0
    else:
        return 1 if count >= 7 else 0

def timebased_check():
    events = get_recent_failed_logins(filter_type="ssh")
    for log_time, _ in events:
        if log_time and log_time.hour in SUSPICIOUS_HOURS:
            return 1

    return 0

def pattern_check():
    events = get_recent_failed_logins(filter_type="ssh")
    account_ips = defaultdict(set)
    for _, line in events:
        match = re.search(
            r"Failed password for (?:invalid user )?(\S+) from (\S+)",
            line
        )
        if match:
            user = match.group(1)
            ip = match.group(2)
            account_ips[user].add(ip)

    for _, ips in account_ips.items():
        if len(ips) >= 3:
            return 1
    return 0

def local_anomaly_check():
    local_events = get_recent_failed_logins(filter_type="local")
    return 1 if len(local_events) >= 3 else 0

def total_score():
    freq = frequency_check()
    time = timebased_check()
    pattern = pattern_check()
    score = (
        freq * 0.4 +
        time * 0.3 +
        pattern * 0.3
    )
    final_score = round(score, 2)
    if final_score >= 0.4:
        events = get_recent_failed_logins(filter_type="ssh")
        ips = set()
        for _, line in events:
            match = re.search(r"from (\S+) port", line)
            if match:
                ips.add(match.group(1))
                
        for ip in ips:
            nft_cmd = f"nft add element inet security blackhole {{ {ip} }}"
            os.system(nft_cmd + " >/dev/null 2>&1")
    return final_score

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: anomaly_detector.py "
            "[frequency|timebase|pattern|total|count|local_anomaly|debug]"
        )
        sys.exit(1)
    mode = sys.argv[1]
    if mode == "frequency":
        print(frequency_check())
    elif mode == "timebase":
        print(timebased_check())
    elif mode == "pattern":
        print(pattern_check())
    elif mode == "total":
        print(total_score())
    elif mode == "count":
        print(len(get_recent_failed_logins(filter_type="ssh")))
    elif mode == "local_anomaly":
        print(local_anomaly_check())
    elif mode == "debug":
        events = get_recent_failed_logins()
        print(f"Recent failed logins: {len(events)}")
        print(f"Frequency: {frequency_check()}")
        print(f"Time-based: {timebased_check()}")
        print(f"Pattern: {pattern_check()}")
        print(f"Total Score: {total_score()}")
