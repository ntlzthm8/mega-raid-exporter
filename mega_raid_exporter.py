#!/usr/bin/env python3
import json
import logging
import os
import re
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def env_int(name, default, minimum):
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise SystemExit(f"{name} must be at least {minimum}, got {value}")
    return value


LISTEN_ADDR = os.getenv("LISTEN_ADDR", "0.0.0.0")
LISTEN_PORT = env_int("LISTEN_PORT", 9634, 1)
STORCLI = os.getenv("STORCLI_PATH", "/opt/MegaRAID/storcli/storcli64")
HOST_DEV = os.getenv("HOST_DEV_PATH", "/host/dev")
HOST_PROC_DEVICES = os.getenv("HOST_PROC_DEVICES", "/host/proc/devices")
DEVICE_NODE = os.getenv("MEGARAID_IOCTL_NODE", "megaraid_sas_ioctl_node")
COMMAND_TIMEOUT = env_int("STORCLI_TIMEOUT_SECONDS", 20, 1)
SCRAPE_CACHE_SECONDS = env_int("SCRAPE_CACHE_SECONDS", 300, 0)
SCRAPE_FAILURE_CACHE_SECONDS = env_int("SCRAPE_FAILURE_CACHE_SECONDS", 15, 0)
AUTO_CREATE_IOCTL_NODE = os.getenv("MEGARAID_CREATE_IOCTL_NODE", "").lower() in ("1", "true", "yes")
SCRAPE_LOCK = threading.Lock()
METRICS_CACHE_LOCK = threading.Lock()
METRICS_CACHE_BODY = ""
METRICS_CACHE_TIME = 0.0
METRICS_CACHE_SUCCESS = False
LOGGER = logging.getLogger("mega_raid_exporter")


class ScrapeError(RuntimeError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def prom_escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def metric_line(name, labels, value):
    label_text = ""
    if labels:
        label_text = "{" + ",".join(f'{k}="{prom_escape(v)}"' for k, v in sorted(labels.items())) + "}"
    return f"{name}{label_text} {value}"


def read_megaraid_major():
    try:
        with open(HOST_PROC_DEVICES, "r", encoding="utf-8") as proc_devices:
            for line in proc_devices:
                parts = line.split()
                if len(parts) == 2 and parts[1] == "megaraid_sas_ioctl":
                    return int(parts[0])
    except FileNotFoundError:
        return None
    return None


def valid_device_node_name(name):
    return name and os.path.basename(name) == name and name not in (".", "..")


def ensure_ioctl_node():
    if not valid_device_node_name(DEVICE_NODE):
        return False, f"invalid device node name: {DEVICE_NODE!r}"

    major = read_megaraid_major()
    if major is None:
        return False, "megaraid_sas_ioctl major is not registered"

    host_node = os.path.join(HOST_DEV, DEVICE_NODE)
    try:
        st = os.stat(host_node)
        if not stat.S_ISCHR(st.st_mode):
            return False, f"{host_node} exists but is not a character device"
        if os.major(st.st_rdev) != major or os.minor(st.st_rdev) != 0:
            return False, f"{host_node} has major/minor {os.major(st.st_rdev)}:{os.minor(st.st_rdev)}, expected {major}:0"
    except FileNotFoundError:
        if not AUTO_CREATE_IOCTL_NODE:
            return False, f"{host_node} is missing; set MEGARAID_CREATE_IOCTL_NODE=true to create it"
        os.mknod(host_node, stat.S_IFCHR | 0o600, os.makedev(major, 0))
        os.chmod(host_node, 0o600)

    container_node = os.path.join("/dev", DEVICE_NODE)
    if not os.path.exists(container_node):
        if not AUTO_CREATE_IOCTL_NODE:
            return False, f"{container_node} is missing; mount the ioctl node into /dev or set MEGARAID_CREATE_IOCTL_NODE=true"
        try:
            os.symlink(host_node, container_node)
        except FileExistsError:
            pass

    try:
        st = os.stat(container_node)
    except FileNotFoundError:
        return False, f"{container_node} is missing"
    if not stat.S_ISCHR(st.st_mode):
        return False, f"{container_node} exists but is not a character device"
    if os.major(st.st_rdev) != major or os.minor(st.st_rdev) != 0:
        return False, f"{container_node} has major/minor {os.major(st.st_rdev)}:{os.minor(st.st_rdev)}, expected {major}:0"
    return True, ""


def run_storcli(args):
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [STORCLI, *args, "J"],
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScrapeError("timeout", f"StorCLI timed out after {COMMAND_TIMEOUT}s") from exc
    except OSError as exc:
        raise ScrapeError("execution_failed", f"cannot execute StorCLI: {exc}") from exc
    duration = time.monotonic() - started
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
        raise ScrapeError("command_failed", f"StorCLI command failed: {detail}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ScrapeError("invalid_json", f"failed to parse StorCLI JSON: {exc}") from exc
    validate_command_status(payload)
    return payload, duration


def response_blocks(payload):
    if not isinstance(payload, dict):
        return []
    blocks = []
    for controller in payload.get("Controllers", []):
        if isinstance(controller, dict):
            blocks.append(controller)
    return blocks


def first_response_data(payload):
    for block in response_blocks(payload):
        data = block.get("Response Data")
        if isinstance(data, dict):
            return data
    return {}


def validate_command_status(payload):
    blocks = response_blocks(payload)
    if not blocks:
        raise ScrapeError("invalid_response", "StorCLI response has no controller blocks")
    for block in blocks:
        status = block.get("Command Status")
        if not isinstance(status, dict):
            raise ScrapeError("invalid_response", "StorCLI response has no command status")
        value = str(status.get("Status", "Unknown"))
        if value.lower() != "success":
            description = status.get("Description", "")
            raise ScrapeError("storcli_status", f"StorCLI status is {value}: {description}")


def normalize_key(key):
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def controller_indexes(payload):
    data = first_response_data(payload)
    basics = data.get("System Overview", [])
    indexes = []
    if isinstance(basics, list):
        for row in basics:
            if isinstance(row, dict) and "Ctl" in row:
                indexes.append(str(row["Ctl"]))
    count = data.get("Number of Controllers")
    if not indexes and isinstance(count, int):
        indexes = [str(i) for i in range(count)]
    return indexes


def find_table(data, wanted):
    wanted_norm = normalize_key(wanted)
    for key, value in data.items():
        if normalize_key(key) == wanted_norm:
            return value if isinstance(value, list) else []
    return []


def collect_metrics():
    body, _ = collect_metrics_with_status()
    return body


def collect_metrics_with_status():
    global METRICS_CACHE_BODY, METRICS_CACHE_TIME, METRICS_CACHE_SUCCESS

    now = time.monotonic()
    with METRICS_CACHE_LOCK:
        ttl = SCRAPE_CACHE_SECONDS if METRICS_CACHE_SUCCESS else SCRAPE_FAILURE_CACHE_SECONDS
        if METRICS_CACHE_BODY and now - METRICS_CACHE_TIME < ttl:
            return METRICS_CACHE_BODY, METRICS_CACHE_SUCCESS

    with SCRAPE_LOCK:
        now = time.monotonic()
        with METRICS_CACHE_LOCK:
            ttl = SCRAPE_CACHE_SECONDS if METRICS_CACHE_SUCCESS else SCRAPE_FAILURE_CACHE_SECONDS
            if METRICS_CACHE_BODY and now - METRICS_CACHE_TIME < ttl:
                return METRICS_CACHE_BODY, METRICS_CACHE_SUCCESS

        body, success = collect_metrics_locked()
        with METRICS_CACHE_LOCK:
            METRICS_CACHE_BODY = body
            METRICS_CACHE_TIME = time.monotonic()
            METRICS_CACHE_SUCCESS = success
        return body, success


def collect_metrics_locked():
    lines = [
        "# HELP megaraid_exporter_up 1 if the exporter completed a StorCLI scrape.",
        "# TYPE megaraid_exporter_up gauge",
        "# HELP megaraid_exporter_scrape_error Last scrape error by stable reason.",
        "# TYPE megaraid_exporter_scrape_error gauge",
        "# HELP megaraid_controller_scrape_success 1 if controller details were collected.",
        "# TYPE megaraid_controller_scrape_success gauge",
        "# HELP megaraid_controller_info MegaRAID controller information.",
        "# TYPE megaraid_controller_info gauge",
        "# HELP megaraid_controller_health MegaRAID controller health state, 1 means optimal.",
        "# TYPE megaraid_controller_health gauge",
        "# HELP megaraid_virtual_drive_state MegaRAID virtual drive state, 1 means optimal.",
        "# TYPE megaraid_virtual_drive_state gauge",
        "# HELP megaraid_physical_drive_state MegaRAID physical drive state, 1 means online.",
        "# TYPE megaraid_physical_drive_state gauge",
        "# HELP megaraid_storcli_command_duration_seconds StorCLI command duration.",
        "# TYPE megaraid_storcli_command_duration_seconds gauge",
    ]

    node_ok, node_error = ensure_ioctl_node()
    if not node_ok:
        LOGGER.error("device validation failed: %s", node_error)
        lines.append(metric_line("megaraid_exporter_scrape_error", {"reason": "device_unavailable"}, 1))
        lines.append(metric_line("megaraid_exporter_up", {}, 0))
        return "\n".join(lines) + "\n", False

    try:
        overview, duration = run_storcli(["show"])
        lines.append(metric_line("megaraid_storcli_command_duration_seconds", {"command": "show"}, f"{duration:.6f}"))
        indexes = controller_indexes(overview)
        if not indexes:
            raise ScrapeError("no_controllers", "StorCLI returned no controller indexes")
    except Exception as exc:
        reason = exc.reason if isinstance(exc, ScrapeError) else "unexpected"
        if isinstance(exc, ScrapeError):
            LOGGER.error("overview scrape failed [%s]: %s", reason, exc)
        else:
            LOGGER.exception("overview scrape failed: %s", exc)
        lines.append(metric_line("megaraid_exporter_scrape_error", {"reason": reason}, 1))
        lines.append(metric_line("megaraid_exporter_up", {}, 0))
        return "\n".join(lines) + "\n", False

    all_success = True
    error_reasons = set()
    for controller in indexes:
        try:
            details, detail_duration = run_storcli([f"/c{controller}", "show", "all"])
            lines.append(metric_line("megaraid_storcli_command_duration_seconds", {"command": "controller_show_all", "controller": controller}, f"{detail_duration:.6f}"))
            data = first_response_data(details)
            if not data:
                raise ScrapeError("invalid_response", "controller response has no data")

            basics = data.get("Basics", {}) if isinstance(data.get("Basics"), dict) else {}
            version = data.get("Version", {}) if isinstance(data.get("Version"), dict) else {}
            controller_labels = {
                "controller": controller,
                "product": basics.get("Product Name") or basics.get("Model", ""),
                "serial": basics.get("Serial Number", ""),
                "pci_address": basics.get("PCI Address", ""),
                "firmware": version.get("FW Version") or version.get("Firmware Version", ""),
                "driver": version.get("Driver Version", ""),
            }
            lines.append(metric_line("megaraid_controller_info", controller_labels, 1))

            health = "Unknown"
            for row in find_table(first_response_data(overview), "System Overview"):
                if str(row.get("Ctl")) == controller:
                    health = row.get("Hlth", "Unknown")
                    break
            lines.append(metric_line("megaraid_controller_health", {"controller": controller, "state": health}, 1 if health == "Opt" else 0))

            for vd in find_table(data, "VD LIST"):
                labels = {
                    "controller": controller,
                    "vd": str(vd.get("DG/VD", "")),
                    "type": vd.get("TYPE", ""),
                    "state": vd.get("State", "Unknown"),
                    "name": vd.get("Name", ""),
                }
                lines.append(metric_line("megaraid_virtual_drive_state", labels, 1 if vd.get("State") == "Optl" else 0))

            for pd in find_table(data, "PD LIST"):
                labels = {
                    "controller": controller,
                    "slot": str(pd.get("EID:Slt", "")),
                    "did": str(pd.get("DID", "")),
                    "state": pd.get("State", "Unknown"),
                    "dg": str(pd.get("DG", "")),
                    "interface": pd.get("Intf", ""),
                    "media": pd.get("Med", ""),
                    "model": pd.get("Model", ""),
                }
                lines.append(metric_line("megaraid_physical_drive_state", labels, 1 if pd.get("State") == "Onln" else 0))
            lines.append(metric_line("megaraid_controller_scrape_success", {"controller": controller}, 1))
        except Exception as exc:
            all_success = False
            reason = exc.reason if isinstance(exc, ScrapeError) else "unexpected"
            if isinstance(exc, ScrapeError):
                LOGGER.error("controller %s scrape failed [%s]: %s", controller, reason, exc)
            else:
                LOGGER.exception("controller %s scrape failed: %s", controller, exc)
            lines.append(metric_line("megaraid_controller_scrape_success", {"controller": controller}, 0))
            error_reasons.add(reason)

    for reason in sorted(error_reasons):
        lines.append(metric_line("megaraid_exporter_scrape_error", {"reason": reason}, 1))
    lines.append(metric_line("megaraid_exporter_up", {}, 1 if all_success else 0))
    return "\n".join(lines) + "\n", all_success


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/readyz":
            _, success = collect_metrics_with_status()
            body = b"ready\n" if success else b"not ready\n"
            self.send_response(200 if success else 503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path not in ("/metrics", "/"):
            self.send_response(404)
            self.end_headers()
            return
        body = collect_metrics().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer((LISTEN_ADDR, LISTEN_PORT), Handler)
    print(f"listening on {LISTEN_ADDR}:{LISTEN_PORT}", flush=True)
    server.serve_forever()
