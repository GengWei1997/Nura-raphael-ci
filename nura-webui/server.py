#!/usr/bin/env python3
"""Web management panel for the Xiaomi Redmi K20 Pro (xiaomi-raphael) on Nura.

Standard library only. Read-only except same-origin POSTs that signal
processes owned by the account the service runs as.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

BATTERY = "battery"
GPU_DEVICE = "2c00000.gpu"
SOC_MODEL = "Snapdragon 855"
DEVICE_NAME = "Xiaomi Redmi K20 Pro"

# The panel runs as root so it can enable/disable units, but process killing is
# limited to this uid: the interactive user, never root and never the panel.
KILL_UID = 10000

# Clusters of the SM8150: 4x Cortex-A55, 3x Cortex-A76, 1x Cortex-A76 prime.
CLUSTER_NAMES = ["4x Cortex-A55", "3x Cortex-A76", "1x Cortex-A76 Prime"]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, UnicodeError):
        return None


def _int_value(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _first_int(value: str | None) -> int | None:
    """Pull the leading number out of strings such as '5220 MHz'."""
    match = re.match(r"\s*(-?\d+)", str(value or ""))
    return int(match.group(1)) if match else None


def _float_value(value: str | None) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _uevent(path: Path) -> dict[str, str]:
    raw = _read_text(path / "uevent") or ""
    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, separator, entry = line.partition("=")
        if separator:
            values[key.removeprefix("POWER_SUPPLY_")] = entry
    return values


def _power_supply(sys_root: Path, name: str) -> dict[str, str]:
    path = sys_root / "class" / "power_supply" / name
    values = _uevent(path)
    for key, entry in values.items():
        values[key] = entry
    text = _read_text(path / "type")
    if text:
        values.setdefault("TYPE", text)
    return values


def battery_info(sys_root: Path) -> dict[str, Any]:
    """PM8150B fuel gauge: microvolts and microamps, tenths of a degree."""
    gauge = _power_supply(sys_root, BATTERY)
    if not gauge:
        return {"available": False, "percent": None, "status": None, "temperature_c": None,
                "voltage_v": None, "current_ma": None, "present": None, "health": None,
                "plugged": False, "source": None, "technology": None}

    def number(key: str) -> int | None:
        value = _int_value(gauge.get(key))
        return value

    capacity = number("CAPACITY")
    temperature = number("TEMP")
    voltage = number("VOLTAGE_NOW")
    current = number("CURRENT_NOW")  # microamps
    online = [n for n in ("usb", "ac", "wireless")
              if (_read_text(sys_root / "class" / "power_supply" / n / "online") or "0") != "0"]
    return {
        "available": True,
        "percent": capacity,
        "status": gauge.get("STATUS"),
        "charge_type": gauge.get("CHARGE_TYPE"),
        "temperature_c": round(temperature / 10, 1) if temperature is not None else None,
        "voltage_v": round(voltage / 1_000_000, 3) if voltage is not None else None,
        "current_ma": round(current / 1000, 1) if current is not None else None,
        "present": (gauge.get("PRESENT") or "1") not in {"0", "no", "false"},
        "health": gauge.get("HEALTH"),
        "technology": gauge.get("TECHNOLOGY"),
        "plugged": bool(online),
        "source": online[0] if online else None,
    }


def gpu_info(sys_root: Path) -> dict[str, Any]:
    """Adreno 640 through devfreq; the vendor driver exposes no load counter."""
    path = sys_root / "class" / "devfreq" / GPU_DEVICE
    result: dict[str, Any] = {"available": path.is_dir(), "model": "Adreno 640",
                              "clock_mhz": None, "max_clock_mhz": None, "min_clock_mhz": None}
    if not result["available"]:
        return result

    def mhz(name: str) -> float | None:
        value = _int_value(_read_text(path / name))
        return round(value / 1_000_000, 0) if value else None

    # The governor and the step list are deliberately not exposed: this GPU's
    # clock-pinning node is read-only, so the panel cannot change either and
    # showing them would only invite the question.
    result.update(
        clock_mhz=mhz("cur_freq"),
        max_clock_mhz=mhz("max_freq"),
        min_clock_mhz=mhz("min_freq"),
    )
    return result


def cpu_clusters(sys_root: Path) -> list[dict[str, Any]]:
    base = sys_root / "devices" / "system" / "cpu" / "cpufreq"
    clusters: list[dict[str, Any]] = []
    policies = sorted(base.glob("policy[0-9]*"), key=lambda p: int(re.sub(r"\D", "", p.name) or 0))
    for index, policy in enumerate(policies):
        cpus = [int(c) for c in (_read_text(policy / "affected_cpus") or "").split() if c.isdigit()]

        def mhz(name: str) -> int | None:
            value = _int_value(_read_text(policy / name))
            return round(value / 1000) if value else None

        clusters.append({
            "name": CLUSTER_NAMES[index] if index < len(CLUSTER_NAMES) else f"cluster {index}",
            "cpus": cpus,
            "cur_mhz": mhz("scaling_cur_freq"),
            "min_mhz": mhz("cpuinfo_min_freq"),
            "max_mhz": mhz("cpuinfo_max_freq"),
            "governor": _read_text(policy / "scaling_governor"),
            "online": all((_read_text(sys_root / "devices" / "system" / "cpu" / f"cpu{c}" / "online") or "1") == "1"
                          for c in cpus),
        })
    return clusters


def thermals(sys_root: Path) -> list[dict[str, Any]]:
    zones: list[dict[str, Any]] = []
    for path in sorted((sys_root / "class" / "thermal").glob("thermal_zone*")):
        kind = _read_text(path / "type")
        raw = _int_value(_read_text(path / "temp"))
        if not kind or raw is None:
            continue
        limits = [_int_value(_read_text(p)) for p in sorted(path.glob("trip_point_*_temp"))]
        limits = [v for v in limits if v]
        zones.append({"name": kind, "temp_c": round(raw / 1000, 1),
                      "limit_c": round(min(limits) / 1000) if limits else None})
    zones.sort(key=lambda z: z["temp_c"], reverse=True)
    return zones


def cpu_percent(proc_root: Path, previous: tuple[int, int] | None) -> tuple[float | None, tuple[int, int] | None]:
    raw = _read_text(proc_root / "stat")
    if not raw:
        return None, previous
    for line in raw.splitlines():
        fields = line.split()
        if fields and fields[0] == "cpu":
            ticks = [int(f) for f in fields[1:9]]
            total = sum(ticks)
            idle = ticks[3] + ticks[4]
            value = None
            if previous and total > previous[0]:
                value = round(max(0.0, min(100.0, (total - previous[0] - (idle - previous[1])) * 100 / (total - previous[0]))), 1)
            return value, (total, idle)
    return None, previous


def core_percent(proc_root: Path, previous: dict[int, tuple[int, int]]) -> tuple[list[float | None], dict[int, tuple[int, int]]]:
    current: dict[int, tuple[int, int]] = {}
    for line in (_read_text(proc_root / "stat") or "").splitlines():
        fields = line.split()
        if fields and fields[0][:3] == "cpu" and fields[0][3:].isdigit():
            ticks = [int(f) for f in fields[1:9]]
            current[int(fields[0][3:])] = (sum(ticks), sum(ticks) - ticks[3] - ticks[4])
    values: list[float | None] = []
    for index in range(max(current) + 1 if current else 0):
        now, before = current.get(index), previous.get(index)
        if now and before and now[0] > before[0]:
            values.append(round(max(0.0, min(100.0, (now[1] - before[1]) * 100 / (now[0] - before[0]))), 1))
        elif now:
            # The core is online but there is no earlier sample to diff against
            # yet; 0 keeps it counted as a core instead of looking offline.
            values.append(0.0)
        else:
            values.append(None)
    return values, current


def loadavg(proc_root: Path) -> float | None:
    raw = (_read_text(proc_root / "loadavg") or "").split()
    return _float_value(raw[0]) if raw else None


def uptime_seconds(proc_root: Path) -> float | None:
    raw = _read_text(proc_root / "uptime")
    return _float_value(raw.split()[0]) if raw else None


def memory_info(proc_root: Path) -> dict[str, Any]:
    values: dict[str, int] = {}
    for line in (_read_text(proc_root / "meminfo") or "").splitlines():
        match = re.match(r"^(MemTotal|MemAvailable|MemFree|Cached):\s+(\d+)\s+kB", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable", values.get("MemFree"))
    used = max(0, total - available) if total and available is not None else None
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "free_bytes": values.get("MemFree"),
        "cached_bytes": values.get("Cached"),
        "used_percent": round(used * 100 / total, 1) if used is not None and total else None,
    }


def storage_info(proc_root: Path) -> list[dict[str, Any]]:
    """Local filesystems, one row per device. /var/tmp is a bind of / here."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in (_read_text(proc_root / "self" / "mounts") or "").splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mountpoint, fstype = fields[0], fields[1].replace("\\040", " "), fields[2]
        if not device.startswith("/dev/") or fstype in {"squashfs", "tmpfs", "devtmpfs"}:
            continue
        if device in seen:
            continue
        seen.add(device)
        try:
            usage = shutil.disk_usage(mountpoint)
        except OSError:
            continue
        rows.append({
            "device": Path(device).name,
            "mountpoint": mountpoint,
            "fstype": fstype,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": round(usage.used * 100 / usage.total, 1) if usage.total else None,
        })
    rows.sort(key=lambda r: r["total_bytes"], reverse=True)
    return rows


def _nmcli(args: list[str]) -> list[str]:
    try:
        proc = subprocess.run(["nmcli", "-t", *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return []
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def network_info(run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    result: dict[str, Any] = {
        "wifi": {"connected": False, "ssid": None, "signal": None, "ip": None, "interface": None,
                 "mac": None, "band": None, "channel": None, "frequency": None},
        "usb": {"ip": None},
        "interfaces": [],
    }
    for line in _nmcli(["-f", "GENERAL.CONNECTION,GENERAL.STATE,IP4.ADDRESS,GENERAL.HWADDR", "dev", "show", "wlan0"]):
        key, _, value = line.partition(":")
        if key == "GENERAL.STATE" and "connected" in value:
            result["wifi"]["connected"] = True
        elif key == "IP4.ADDRESS[1]" and value:
            result["wifi"]["ip"] = value.split("/")[0]
        elif key == "GENERAL.HWADDR" and value:
            result["wifi"]["mac"] = value
        elif key == "GENERAL.CONNECTION" and value:
            result["wifi"]["ssid"] = value
    result["wifi"]["interface"] = "wlan0" if result["wifi"]["connected"] or result["wifi"]["mac"] else None
    # IN-USE:SSID:SIGNAL:BAND:CHAN:FREQ; the SSID may contain colons, so split from the right.
    #
    # The RATE column is deliberately not read: NetworkManager fills it with the
    # interface's maximum PHY rate, not the negotiated one, so an idle link and a
    # busy one report the same number (and 2.4 GHz bands advertise 1170 Mbit/s
    # while the connected 5 GHz link says 270). Band, channel and frequency are
    # facts about the current association, so those are shown instead.
    for line in _nmcli(["-f", "IN-USE,SSID,SIGNAL,BAND,CHAN,FREQ", "dev", "wifi"]):
        if not line.startswith("*"):
            continue
        parts = line.rsplit(":", 4)
        if len(parts) == 5:
            head, signal, band, channel, freq = parts
            result["wifi"]["ssid"] = result["wifi"]["ssid"] or head.split(":", 1)[-1]
            result["wifi"]["signal"] = _int_value(signal)
            result["wifi"]["band"] = band.strip() or None
            result["wifi"]["channel"] = _int_value(channel)
            result["wifi"]["frequency"] = _first_int(freq)
        break
    for line in _nmcli(["-f", "IP4.ADDRESS", "dev", "show", "usb0"]):
        if line.startswith("IP4.ADDRESS") and ":" in line:
            result["usb"]["ip"] = line.split(":", 1)[1].split("/")[0]
    try:
        proc = run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=2)
        for line in proc.stdout.splitlines():
            match = re.match(r"^\d+:\s+(\S+)\s+inet\s+(\S+)", line)
            if match:
                result["interfaces"].append({"name": match.group(1), "address": match.group(2)})
    except (OSError, subprocess.SubprocessError):
        pass
    return result


CPU_MODES = ("performance", "balanced", "powersave")


def cpu_mode(run: Callable[..., Any]) -> str:
    """Map the live cpufreq state onto one of the three panel profiles.

    Balanced and powersave are the same governor with a different max clock, so
    the cap separates them; performance is a different governor entirely.
    """
    base = Path("/sys/devices/system/cpu/cpufreq")
    policies = sorted(base.glob("policy[0-9]*"))
    if not policies:
        return "balanced"
    governors = {(_read_text(p / "scaling_governor") or "") for p in policies}
    capped = False
    for policy in policies:
        maximum = _int_value(_read_text(policy / "scaling_max_freq"))
        hardware = _int_value(_read_text(policy / "cpuinfo_max_freq"))
        if maximum and hardware and maximum < hardware:
            capped = True
    if governors == {"performance"}:
        return "performance"
    return "powersave" if capped else "balanced"


def cpufreq_set(run: Callable[..., Any], mode: Any) -> tuple[bool, str]:
    """Switch the CPU profile through the privileged helper."""
    if not isinstance(mode, str) or mode not in CPU_MODES:
        return False, "bad request"
    try:
        proc = run(["sudo", "-n", CPUPREQ, "mode", mode],
                   capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)[:120]
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "failed").strip().splitlines()
        return False, (lines[0] if lines else "failed")[:120]
    return True, mode


def services_info(run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    result: dict[str, Any] = {"running": [], "failed": [], "total": 0, "sshd": None, "nftables": None}

    def listing(state: str) -> list[dict[str, str]]:
        try:
            proc = run(["systemctl", "list-units", "--type=service", f"--state={state}",
                        "--no-legend", "--plain", "--no-pager"], capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            return []
        units = []
        for line in proc.stdout.splitlines():
            fields = line.split(None, 4)
            if len(fields) >= 4 and fields[0].endswith(".service"):
                units.append({"name": fields[0], "state": fields[2], "sub": fields[3],
                              "description": fields[4] if len(fields) > 4 else ""})
        return units

    result["running"] = listing("running")
    result["failed"] = listing("failed")
    try:
        proc = run(["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain", "--no-pager"],
                   capture_output=True, text=True, timeout=4)
        result["total"] = sum(1 for line in proc.stdout.splitlines() if line.strip().endswith(".service") or ".service" in line.split()[0:1])
    except (OSError, subprocess.SubprocessError):
        pass
    names = {u["name"] for u in result["running"]}
    failed = {u["name"] for u in result["failed"]}

    def active(unit: str) -> dict[str, Any]:
        # nftables is a oneshot with RemainAfterExit=yes: active, but "exited"
        # rather than "running", so it is absent from the state=running listing.
        try:
            proc = run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return {"up": None, "failed": False}
        state = proc.stdout.strip()
        return {"up": state == "active", "failed": unit in failed, "state": state or None}

    result["sshd"] = {"up": "sshd.service" in names, "failed": "sshd.service" in failed}
    result["nftables"] = active("nftables.service")
    return result


def docker_info(run: Callable[..., Any] = subprocess.run, docker_cmd: str = "docker") -> dict[str, Any]:
    """Containers via `docker ps -a`; the panel joins the docker group at start."""
    try:
        proc = run([docker_cmd, "ps", "-a", "--format", "{{json .}}"],
                   capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "containers": [], "error": str(exc)[:120]}
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "docker failed").strip()
        return {"available": False, "containers": [], "error": message[:120]}
    containers = []
    for line in proc.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append({
            "name": item.get("Names"),
            "image": item.get("Image"),
            "state": item.get("State"),
            "status": item.get("Status"),
            "ports": item.get("Ports"),
        })
    return {"available": True, "containers": containers, "error": None}


def docker_version(run: Callable[..., Any] = subprocess.run, docker_cmd: str = "docker") -> str | None:
    try:
        proc = run([docker_cmd, "version", "--format", "{{.Server.Version}}"],
                   capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


NFT = "nft"
NFT_TABLE = "inet filter"
MANAGED_COMMENT = "nura webui"
SYSTEMCTL = "/usr/bin/systemctl"
CPUPREQ = "/usr/libexec/nura-cpufreq"


def _nft(run: Callable[..., Any], args: list[str], timeout: int = 5) -> tuple[bool, str]:
    try:
        proc = run([NFT, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)[:120]
    if proc.returncode != 0:
        # nft prints the offending line back; the first line is the actual error.
        message = (proc.stderr or proc.stdout or "nft failed").strip().splitlines()
        return False, (message[0] if message else "nft failed").removeprefix("Error: ")[:160]
    return True, proc.stdout


def _rule_summary(rule: dict[str, Any]) -> str:
    """Flatten an nft rule's expression list into something readable.

    A typical rule is a chain of matches followed by a verdict, so the parts
    are collected in source order and joined at the end.
    """
    protocol = ""
    parts: list[str] = []
    verdict = ""
    counters = ""

    for expr in rule.get("expr") or []:
        match = expr.get("match")
        if match:
            left = match.get("left") or {}
            payload = left.get("payload") or {}
            meta = left.get("meta") or {}
            field = payload.get("field") or meta.get("key") or ""
            right = match.get("right")
            if isinstance(right, dict):
                if "set" in right:
                    right = ", ".join(str(v) for v in right["set"])
                elif "prefix" in right:
                    right = f"{right['prefix'].get('addr')}/{right['prefix'].get('len')}"
                else:
                    right = next(iter(right.values()), right)
            if field == "protocol" and not parts:
                protocol = str(right)
                continue
            if field in ("dport", "sport") and payload.get("protocol") in ("tcp", "udp", "sctp"):
                protocol = payload["protocol"]
            if field == "dport":
                parts.append(f"port {right}")
            elif field == "sport":
                parts.append(f"sport {right}")
            elif field == "saddr":
                parts.append(f"from {right}")
            elif field == "type":
                # Long ICMP type sets are noise; the comment already says what it is.
                stripped = [str(v) for v in str(right).split(", ")]
                parts.append(f"type {right}" if len(stripped) <= 2 else "type …")
            elif field == "hoplimit":
                continue
            elif field in ("iifname", "oifname"):
                parts.append(f"{'in' if field == 'iifname' else 'out'} {right}")
            elif field:
                parts.append(f"{field} {right}")
        elif "accept" in expr:
            verdict = "accept"
        elif "drop" in expr:
            verdict = "drop"
        elif "reject" in expr:
            verdict = "reject"
        elif "counter" in expr:
            counter = expr["counter"]
            counters = f"{counter.get('packets', 0)} pkts"

    head = " ".join(x for x in (protocol, *parts) if x) or "all traffic"
    line = f"{head} → {verdict or 'continue'}"
    return f"{line} · {counters}" if counters else line


def _rule_fields(rule: dict[str, Any]) -> tuple[str | None, int | None, str | None]:
    """Pull protocol, port and source back out, so the form can be pre-filled."""
    protocol: str | None = None
    port: int | None = None
    source: str | None = None
    for expr in rule.get("expr") or []:
        match = expr.get("match")
        if not match:
            continue
        left = match.get("left") or {}
        payload = left.get("payload") or {}
        field = payload.get("field") or (left.get("meta") or {}).get("key") or ""
        right = match.get("right")
        if field == "protocol" and protocol is None:
            protocol = str(right)
        elif field in ("dport", "sport") and payload.get("protocol") in ("tcp", "udp"):
            protocol = payload["protocol"]
            if field == "dport" and isinstance(right, int):
                port = right
        elif field == "saddr":
            if isinstance(right, dict) and "prefix" in right:
                source = f"{right['prefix'].get('addr')}/{right['prefix'].get('len')}"
            elif isinstance(right, str):
                source = right
    if protocol not in ("tcp", "udp"):
        protocol = None
    return protocol, port, source


def firewall_info(run: Callable[..., Any] = subprocess.run, chain: str = "input") -> dict[str, Any]:
    """Rules of the nftables input chain, with the handles needed to delete them."""
    ok, output = _nft(run, ["-j", "list", "chain", NFT_TABLE, chain])
    if not ok:
        return {"available": False, "error": output, "policy": None, "rules": [], "port_16888": False}

    try:
        payload = json.loads(output)
    except ValueError:
        return {"available": False, "error": "nft returned malformed JSON", "policy": None, "rules": [],
                "port_16888": False}

    policy = None
    rules: list[dict[str, Any]] = []
    for item in payload.get("nftables") or []:
        if "chain" in item:
            policy = item["chain"].get("policy")
        rule = item.get("rule")
        if not rule or rule.get("chain") != chain:
            continue
        comment = rule.get("comment") or ""
        summary = _rule_summary(rule)
        protocol, port, source = _rule_fields(rule)
        rules.append({
            "handle": rule.get("handle"),
            "comment": comment,
            "summary": summary,
            "protocol": protocol,
            "port": port,
            "source": source,
            "managed": comment.startswith(MANAGED_COMMENT),
        })
    return {
        "available": True,
        "error": None,
        "policy": policy,
        "rules": rules,
        "port_16888": any("16888" in r["summary"] for r in rules),
    }


def firewall_add(run: Callable[..., Any], port: Any, protocol: Any, source: Any,
                 comment: str = "") -> tuple[bool, str]:
    """Insert an accept rule at the top of the input chain."""
    parsed, error = _parse_rule(port, protocol, source)
    if parsed is None:
        return False, error
    proto, port_number, origin = parsed
    label = _managed_label(comment)

    args = ["add", "rule", NFT_TABLE, "input"]
    args += _match_args(proto, port_number, origin)
    args += ["accept", "comment", f'"{label}"']
    ok, output = _nft(run, args)
    return (True, f"{proto}/{port_number} accepted") if ok else (False, output)


def firewall_update(run: Callable[..., Any], handle: Any, port: Any, protocol: Any, source: Any,
                    comment: str = "", managed: bool = False) -> tuple[bool, str]:
    """Rewrite a rule in place; `replace rule ... handle N` keeps the handle.

    A rule that was not created here keeps its plain comment, so it does not
    start looking like one of the panel's own after an edit.
    """
    if not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0:
        return False, "bad handle"
    parsed, error = _parse_rule(port, protocol, source)
    if parsed is None:
        return False, error
    proto, port_number, origin = parsed
    label = _managed_label(comment) if managed else (comment.strip()[:80] if isinstance(comment, str) else "")

    args = ["replace", "rule", NFT_TABLE, "input", "handle", str(handle)]
    args += _match_args(proto, port_number, origin)
    args += ["accept"]
    if label:
        args += ["comment", f'"{label}"']
    ok, output = _nft(run, args)
    return (True, f"rule {handle} updated") if ok else (False, output)


def _parse_rule(port: Any, protocol: Any, source: Any) -> tuple[tuple[str, int, str] | None, str]:
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        return None, "bad port"
    if protocol not in ("tcp", "udp"):
        return None, "bad protocol"
    origin = source.strip() if isinstance(source, str) else ""
    if origin and not re.fullmatch(r"[0-9a-fA-F:./]{1,49}", origin):
        return None, "bad source"
    return (protocol, port, origin), ""


def _managed_label(comment: str) -> str:
    extra = comment.strip()[:60] if isinstance(comment, str) else ""
    return MANAGED_COMMENT + (f": {extra}" if extra else "")


def _match_args(protocol: str, port: int, origin: str) -> list[str]:
    args: list[str] = []
    if origin:
        args += ["ip", "saddr", origin]
    args += [protocol, "dport", str(port)]
    return args


def firewall_delete(run: Callable[..., Any], handle: Any) -> tuple[bool, str]:
    if not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0:
        return False, "bad handle"
    ok, output = _nft(run, ["delete", "rule", NFT_TABLE, "input", "handle", str(handle)])
    return (True, f"rule {handle} deleted") if ok else (False, output)


PROTECTED_COMMS = {"nura-webui"}

# Feature toggles the panel may flip. The service is not root, so writes go
# through the two commands granted in /etc/sudoers.d/nura-webui; nothing else
# is permitted.
TOGGLES = {
    "blank-screen": {
        "unit": "blank_screen.service",
    },
}


def toggle_state(run: Callable[..., Any], name: str) -> dict[str, Any]:
    """Report whether a toggled unit is enabled and currently running."""
    spec = TOGGLES.get(name)
    if spec is None:
        return {"available": False, "enabled": None, "active": None, "error": "unknown toggle"}

    def query(args: list[str]) -> str | None:
        try:
            proc = run(args, capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            return None
        return (proc.stdout or "").strip() or None

    # Reading state needs no privileges, so these do not go through sudo.
    enabled = query(["systemctl", "is-enabled", spec["unit"]])
    active = query(["systemctl", "is-active", spec["unit"]])
    # `is-enabled` prints "not-found" for a unit that does not exist; that is
    # not a usable toggle, so treat it (and anything unknown) as absent.
    usable = enabled is not None and enabled not in ("not-found", "unknown", "")
    return {
        "available": usable,
        "enabled": usable and enabled in ("enabled", "enabled-runtime"),
        "state": enabled,
        "active": active == "active",
        "unit": spec["unit"],
        "error": None if usable else "unit not found",
    }


def toggle_set(run: Callable[..., Any], name: str, want: bool) -> tuple[bool, str]:
    spec = TOGGLES.get(name)
    if spec is None:
        return False, "unknown toggle"
    if not isinstance(want, bool):
        return False, "bad request"
    if want:
        # Enabling alone is enough: starting the unit would blank the screen
        # 15 s later, which nobody wants triggered by flipping a switch.
        command = [SYSTEMCTL, "enable", spec["unit"]]
    else:
        # --now also stops a sleep that is already counting down.
        command = [SYSTEMCTL, "disable", "--now", spec["unit"]]
    action = "enable" if want else "disable"
    try:
        proc = run(["sudo", "-n", *command],
                   capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)[:120]
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "failed").strip().splitlines()
        return False, (message[0] if message else "failed")[:160]
    return True, f"{spec['unit']} {action}d"
SECRET_RE = re.compile(r"(?i)((?:key|token|secret|passw\w*|auth\w*|credential\w*)[\"']?\s*[=:]\s*)\S+")
KILL_GRACE_S = 4
_users: dict[int, str] = {}


def _user_name(uid: int) -> str:
    if uid not in _users:
        try:
            _users[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _users[uid] = str(uid)
    return _users[uid]


def _cmdline(proc_root: Path, pid: int) -> str | None:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()[:400]
    except OSError:
        return None
    text = raw.replace(b"\0", b" ").decode(errors="replace").strip()
    return SECRET_RE.sub(r"\1***", text)[:80] or None


def process_snapshot(proc_root: Path) -> dict[int, tuple[int, int, str, int]]:
    snapshot: dict[int, tuple[int, int, str, int]] = {}
    try:
        entries = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return snapshot
    for entry in entries:
        stat = _read_text(entry / "stat")
        if not stat:
            continue
        close = stat.rfind(")")
        if close < 0:
            continue
        try:
            fields = stat[close + 1:].split()
            ticks = int(fields[11]) + int(fields[12])
            starttime = int(fields[19])
            rss_pages = int(fields[21]) if len(fields) > 21 else 0
            rss = rss_pages * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, IndexError, OSError, AttributeError):
            continue
        snapshot[int(entry.name)] = (starttime, ticks, stat[stat.find("(") + 1:close].strip(), rss)
    return snapshot


def process_protected(pid: int, comm: str) -> bool:
    return pid in (1, os.getpid()) or comm in PROTECTED_COMMS


class Collector:
    def __init__(self, proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys"),
                 run: Callable[..., Any] = subprocess.run) -> None:
        self.proc_root = Path(proc_root)
        self.sys_root = Path(sys_root)
        self.run = run
        self.cpu_count = os.cpu_count() or 8
        self._previous_cpu: tuple[int, int] | None = None
        self._previous_cores: dict[int, tuple[int, int]] = {}
        self._previous_processes: dict[int, tuple[int, int, str, int]] = {}
        self._lock = threading.Lock()
        self._termed: dict[tuple[int, int], float] = {}
        self._services_cache: tuple[float, dict[str, Any]] | None = None
        self._docker_cache: tuple[float, dict[str, Any]] | None = None
        self._firewall_cache: tuple[float, dict[str, Any]] | None = None
        self._settings_cache: tuple[float, dict[str, Any]] | None = None

    DOCKER_ACTIONS = {"pause": ("running",), "unpause": ("paused",), "stop": ("running", "paused"),
                      "start": ("exited", "created"), "restart": ("running", "paused", "exited")}

    def _services(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._services_cache and now - self._services_cache[0] < 10:
            return self._services_cache[1]
        value = services_info(self.run)
        self._services_cache = (now, value)
        return value

    def collect(self) -> dict[str, Any]:
        with self._lock:
            total_percent, previous_cpu = cpu_percent(self.proc_root, self._previous_cpu)
            cores, previous_cores = core_percent(self.proc_root, self._previous_cores)
            current = process_snapshot(self.proc_root)
            previous = self._previous_processes
            memory = memory_info(self.proc_root)
            rows: list[dict[str, Any]] = []
            total_delta = (previous_cpu[0] - self._previous_cpu[0]) if (previous_cpu and self._previous_cpu) else 0
            for pid, (start, ticks, comm, rss) in current.items():
                before = previous.get(pid)
                percent = None
                if total_delta > 0 and before and before[0] == start:
                    percent = round(max(0.0, ticks - before[1]) * (len(cores) or 1) * 100 / total_delta, 1)
                try:
                    uid = os.stat(self.proc_root / str(pid)).st_uid
                except OSError:
                    uid = -1
                rows.append({"pid": pid, "name": comm, "user": _user_name(uid) if uid >= 0 else "?",
                             "cpu_percent": percent, "rss_bytes": rss, "start": start,
                             "memory_percent": round(rss * 100 / memory["total_bytes"], 1)
                             if memory.get("total_bytes") else None,
                             "killable": uid == KILL_UID and not process_protected(pid, comm)})
            rows.sort(key=lambda r: (r["cpu_percent"] or 0, r["rss_bytes"]), reverse=True)
            for row in rows[:30]:
                row["cmd"] = _cmdline(self.proc_root, row["pid"])
            self._previous_cpu = previous_cpu
            self._previous_cores = previous_cores
            self._previous_processes = current

            storage = storage_info(self.proc_root)
            total_bytes = sum(r["total_bytes"] for r in storage)
            used_bytes = sum(r["used_bytes"] for r in storage)
            return {
                "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "device": {"name": DEVICE_NAME, "soc": SOC_MODEL, "hostname": os.uname().nodename,
                           "kernel": os.uname().release,
                           "login_user": _user_name(KILL_UID) if os.getuid() == 0 else pwd.getpwuid(os.getuid()).pw_name},
                "uptime_seconds": uptime_seconds(self.proc_root),
                "battery": battery_info(self.sys_root),
                "cpu": {"percent": total_percent, "logical_cores": self.cpu_count, "cores": cores,
                        "clusters": cpu_clusters(self.sys_root), "loadavg": loadavg(self.proc_root),
                        "mode": cpu_mode(self.run)},
                "gpu": gpu_info(self.sys_root),
                "thermals": thermals(self.sys_root),
                "memory": memory,
                "storage": {"partitions": storage, "total_bytes": total_bytes, "used_bytes": used_bytes,
                            "free_bytes": max(0, total_bytes - used_bytes),
                            "used_percent": round(used_bytes * 100 / total_bytes, 1) if total_bytes else None},
                "network": network_info(self.run),
                "services": self._services(),
                "containers": self._docker(),
                "firewall": self._firewall(),
                "settings": self._settings(),
                "processes": rows,
            }

    def _settings(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._settings_cache and now - self._settings_cache[0] < 5:
            return self._settings_cache[1]
        value = {"blank-screen": toggle_state(self.run, "blank-screen")}
        self._settings_cache = (now, value)
        return value

    def setting_set(self, name: Any, want: Any) -> tuple[bool, str]:
        if not isinstance(name, str):
            return False, "bad request"
        ok, message = toggle_set(self.run, name, want)
        self._settings_cache = None
        return ok, message

    def cpu_mode_set(self, mode: Any) -> tuple[bool, str]:
        return cpufreq_set(self.run, mode)

    def _firewall(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._firewall_cache and now - self._firewall_cache[0] < 10:
            return self._firewall_cache[1]
        value = firewall_info(self.run)
        self._firewall_cache = (now, value)
        return value

    def firewall_add(self, port: Any, protocol: Any, source: Any, comment: Any) -> tuple[bool, str]:
        ok, message = firewall_add(self.run, port, protocol, source, comment or "")
        self._firewall_cache = None
        return ok, message

    def firewall_delete(self, handle: Any) -> tuple[bool, str]:
        ok, message = firewall_delete(self.run, handle)
        self._firewall_cache = None
        return ok, message

    def firewall_update(self, handle: Any, port: Any, protocol: Any, source: Any,
                        comment: Any, managed: Any = False) -> tuple[bool, str]:
        ok, message = firewall_update(self.run, handle, port, protocol, source, comment or "", managed is True)
        self._firewall_cache = None
        return ok, message

    def _docker(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._docker_cache and now - self._docker_cache[0] < 10:
            return self._docker_cache[1]
        value = {**docker_info(self.run), "server_version": docker_version(self.run)}
        self._docker_cache = (now, value)
        return value

    def docker_action(self, action: str, name: Any) -> tuple[bool, str]:
        states = self.DOCKER_ACTIONS.get(action)
        if states is None or not isinstance(name, str):
            return False, "bad request"
        listing = docker_info(self.run)
        match = next((c for c in listing["containers"] if c["name"] == name), None)
        if match is None:
            return False, "unknown container"
        if (match["state"] or "").lower() not in states:
            return False, f"cannot {action} a {match['state']} container"
        try:
            proc = self.run(["docker", action, name], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)[:80]
        self._docker_cache = None
        if proc.returncode != 0:
            return False, (proc.stderr or "failed").strip()[:100]
        return True, f"{action} {name}: ok"

    def kill(self, pid: Any, start: Any, sig: Any) -> tuple[bool, str]:
        if not (isinstance(pid, int) and not isinstance(pid, bool) and isinstance(start, int)) or sig not in ("TERM", "KILL"):
            return False, "bad request"
        snapshot = process_snapshot(self.proc_root)
        entry = snapshot.get(pid)
        if not entry or entry[0] != start:
            return False, "process is gone"
        if process_protected(pid, entry[2]):
            return False, "protected process"
        try:
            if os.stat(self.proc_root / str(pid)).st_uid != KILL_UID:
                return False, "not your process"
        except OSError:
            return False, "process is gone"
        now = time.monotonic()
        if sig == "KILL" and now - self._termed.get((pid, start), now) < KILL_GRACE_S:
            return False, "send SIGTERM first and wait"
        try:
            os.kill(pid, signal.SIGTERM if sig == "TERM" else signal.SIGKILL)
        except OSError as exc:
            return False, str(exc)[:80]
        if sig == "TERM":
            self._termed[(pid, start)] = now
        return True, f"SIG{sig} sent to {entry[2]} ({pid})"


class Handler(BaseHTTPRequestHandler):
    collector: Collector
    html_path: Path

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/status":
            body = json.dumps(self.collector.collect(), separators=(",", ":")).encode()
            self._send(200, body, "application/json; charset=utf-8")
        elif path in ("/", "/index.html"):
            try:
                body = self.html_path.read_bytes()
            except OSError:
                self._send(500, b"panel page is unavailable", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/api/health":
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not origin or urlsplit(origin).netloc != self.headers.get("Host", "\0"):
            return self._json(403, False, "bad origin")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._json(415, False, "json only")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if 0 < length <= 4096 else None
        except (ValueError, OSError):
            body = None
        if not isinstance(body, dict):
            return self._json(400, False, "bad request")
        if urlsplit(self.path).path == "/api/process/kill":
            ok, message = self.collector.kill(body.get("pid"), body.get("start"), body.get("signal"))
        elif urlsplit(self.path).path.startswith("/api/docker/"):
            ok, message = self.collector.docker_action(urlsplit(self.path).path[len("/api/docker/"):], body.get("name"))
        elif urlsplit(self.path).path == "/api/firewall/add":
            ok, message = self.collector.firewall_add(
                body.get("port"), body.get("protocol"), body.get("source"), body.get("comment"))
        elif urlsplit(self.path).path == "/api/firewall/delete":
            ok, message = self.collector.firewall_delete(body.get("handle"))
        elif urlsplit(self.path).path == "/api/firewall/update":
            ok, message = self.collector.firewall_update(
                body.get("handle"), body.get("port"), body.get("protocol"),
                body.get("source"), body.get("comment"), body.get("managed"))
        elif urlsplit(self.path).path == "/api/settings/set":
            ok, message = self.collector.setting_set(body.get("name"), body.get("enabled"))
        elif urlsplit(self.path).path == "/api/cpufreq/set":
            ok, message = self.collector.cpu_mode_set(body.get("mode"))
        else:
            return self._json(404, False, "not found")
        self._json(200 if ok else 409, ok, message)

    def _json(self, status: int, ok: bool, message: str) -> None:
        self._send(status, json.dumps({"ok": ok, "message": message}).encode(), "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def main() -> None:
    try:
        Path("/proc/self/comm").write_text("nura-webui")
    except OSError:
        pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=16888)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
    parser.add_argument("--sys-root", default="/sys", help=argparse.SUPPRESS)
    args = parser.parse_args()

    handler = type("PanelHandler", (Handler,), {
        "collector": Collector(proc_root=Path(args.proc_root), sys_root=Path(args.sys_root)),
        "html_path": Path(__file__).with_name("index.html"),
    })
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    server.daemon_threads = True
    print(f"nura-webui listening on {args.bind}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
