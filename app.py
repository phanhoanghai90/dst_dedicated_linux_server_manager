"""
DST Web Panel - web quản lý nhiều Don't Starve Together Dedicated Server.
Ý tưởng quản lý shard (stdin console, dò log, chèn Lua lấy thống kê) tham khảo từ
https://github.com/diogo-webber/vox-launcher, viết lại cho Linux + web.
"""

import configparser
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unicodedata
import urllib.request
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

# ------------------------------------------------------------------------------------ #
# Đường dẫn

IS_WINDOWS    = os.name == "nt"
_DEFAULT_BASE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DSTLauncher" if IS_WINDOWS else Path("/root/dst")

BASE_DIR      = Path(os.environ.get("DST_BASE", _DEFAULT_BASE))
WEBUI_DIR     = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # PyInstaller giải nén file vào _MEIPASS
CONFIG_FILE   = WEBUI_DIR / "config.json"
SERVER_DIR    = BASE_DIR / "server"
STEAMCMD_DIR  = BASE_DIR / "steamcmd"
STORAGE_ROOT  = BASE_DIR / "data"
CONF_DIR      = "DoNotStarveTogether"
CLUSTERS_ROOT = STORAGE_ROOT / CONF_DIR
BACKUP_ROOT   = STORAGE_ROOT / "backups"
SERVER_EXE    = SERVER_DIR / "bin64" / ("dontstarve_dedicated_server_nullrenderer_x64" + (".exe" if IS_WINDOWS else ""))
STEAMCMD_EXE  = STEAMCMD_DIR / ("steamcmd.exe" if IS_WINDOWS else "steamcmd.sh")
MODS_DIR      = SERVER_DIR / "mods"
MODS_SETUP    = MODS_DIR / "dedicated_server_mods_setup.lua"
WORKSHOP_DIR  = BASE_DIR / "workshop"  # steamcmd tải mod về đây

DST_APP_ID    = "343050"
DST_GAME_ID   = "322330"  # app id của game, dùng cho Workshop
STEAMCMD_URL  = ("https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip" if IS_WINDOWS
                 else "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz")
# Windows: không bật cửa sổ console đen cho từng tiến trình con.
NO_WINDOW     = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
SHARDS        = ("Master", "Caves")
PANEL_FILE    = "webpanel.json"  # cấu hình riêng của web cho từng server (mods, caves, slot port)

CLUSTER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

# File cho phép sửa trực tiếp trên web (đường dẫn tương đối trong thư mục cluster).
EDITABLE_FILES = {
    "modoverrides":     "Master/modoverrides.lua",  # được copy sang Caves khi lưu
    "master_leveldata": "Master/worldgenoverride.lua",
    "caves_worldgen":   "Caves/worldgenoverride.lua",
    "adminlist":        "adminlist.txt",
    "blocklist":        "blocklist.txt",
    "whitelist":        "whitelist.txt",
}

# ------------------------------------------------------------------------------------ #
# Cấu hình của web panel (chỉ còn tài khoản đăng nhập)

_config_lock = threading.Lock()


def load_config():
    with _config_lock:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text())

        config = {"username": "admin", "password": secrets.token_urlsafe(12)}
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
        CONFIG_FILE.chmod(0o600)

        return config


def save_config(config):
    with _config_lock:
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
        CONFIG_FILE.chmod(0o600)


# ------------------------------------------------------------------------------------ #
# File ini

DEFAULT_CLUSTER_INI = {
    "GAMEPLAY": {
        "game_mode": "survival",
        "max_players": "6",
        "pvp": "false",
        "pause_when_empty": "true",
    },
    "NETWORK": {
        "cluster_name": "My DST Server",
        "cluster_description": "",
        "cluster_password": "",
        "cluster_intention": "cooperative",
        "cluster_language": "en",
        "lan_only_cluster": "false",
        "offline_cluster": "false",
    },
    "MISC": {
        "console_enabled": "true",
        "max_snapshots": "6",
    },
    "SHARD": {
        "shard_enabled": "true",
        "bind_ip": "127.0.0.1",
        "master_ip": "127.0.0.1",
        "master_port": "10888",
        "cluster_key": "",
    },
}

CAVES_WORLDGEN = 'return {\n  override_enabled = true,\n  preset = "DST_CAVE",\n}\n'
MASTER_LEVELDATA = 'return {\n  override_enabled = true,\n  preset = "SURVIVAL_TOGETHER",\n  overrides = {\n  },\n}\n'
MODOVERRIDES = "return {\n}\n"


def read_ini(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str

    if path.exists():
        parser.read(path, encoding="utf-8")

    return parser


def write_ini(path, parser):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        parser.write(f)


def slot_ports(slot):
    """Mỗi server một "slot", slot 0 dùng đúng port mặc định của DST."""

    return {
        "Master": {"server_port": 10999 + 2 * slot, "master_server_port": 27018 + 2 * slot, "authentication_port": 8768 + 2 * slot},
        "Caves":  {"server_port": 11000 + 2 * slot, "master_server_port": 27019 + 2 * slot, "authentication_port": 8769 + 2 * slot},
        "shard_master_port": 10888 + slot,
    }


def flat_ports(ports):
    return {ports["shard_master_port"], *ports["Master"].values(), *ports["Caves"].values()}


# ------------------------------------------------------------------------------------ #
# Shard process

STATS_PREFIX = "DSTWEB_STATS="

STATS_LUA = (
    "local p={} "
    "for _,c in ipairs(TheNet:GetClientTable() or {}) do "
    "if c.performance==nil then table.insert(p,c.name) end end "
    f'print("{STATS_PREFIX}"..json.encode({{players=p,max=TheNet:GetDefaultMaxPlayers(),'
    "day=TheWorld.state.cycles+1,season=TheWorld.state.season,phase=TheWorld.state.phase}))"
)

ROLLBACK_LUA = (
    "if TheNet:GetPlayerCount()<=0 then c_rollback({n}) "
    "else SetServerPaused(false) TheWorld:DoTaskInTime(3,function() c_rollback({n}) end) end"
)


class Shard:
    def __init__(self, cluster, name):
        self.cluster = cluster
        self.name = name
        self.process = None
        self.status = "offline"  # offline | starting | online | stopping
        self.error = None
        self.lines = deque(maxlen=3000)
        self.seq = 0
        self.lock = threading.Lock()

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def log(self, text):
        with self.lock:
            self.seq += 1
            self.lines.append((self.seq, text))

    def get_lines(self, after):
        with self.lock:
            return [{"seq": s, "text": t} for s, t in self.lines if s > after], self.seq

    def memory_mb(self):
        if not self.is_running():
            return None

        if IS_WINDOWS:
            try:
                import psutil
                return round(psutil.Process(self.process.pid).memory_info().rss / 1024 / 1024)
            except Exception:
                return None

        try:
            for line in Path(f"/proc/{self.process.pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024)
        except OSError:
            pass

        return None

    def start(self):
        if self.is_running():
            return

        args = [
            str(SERVER_EXE),
            "-console",
            "-persistent_storage_root", str(STORAGE_ROOT),
            "-conf_dir", CONF_DIR,
            "-cluster", self.cluster.id,
            "-shard", self.name,
            "-monitor_parent_process", str(os.getpid()),
        ]

        self.error = None
        self.status = "starting"
        self.log(f"[web] Khởi động shard {self.name}...")

        try:
            self.process = subprocess.Popen(
                args,
                cwd=SERVER_EXE.parent,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                   if IS_WINDOWS else {"start_new_session": True}),
            )
        except OSError as e:
            self.status = "offline"
            self.error = f"Không chạy được server: {e}"
            self.log(f"[web] {self.error}")
            return

        threading.Thread(target=self._read_output, args=(self.process,), daemon=True).start()

    def _read_output(self, process):
        for line in process.stdout:
            line = line.rstrip("\n")

            if STATS_PREFIX in line:
                if self.name == "Master":
                    self.cluster.update_stats(line.split(STATS_PREFIX, 1)[1])
                continue

            self.log(line)
            self._handle_keywords(line)

        process.wait()

        if process is self.process:
            self.status = "offline"
            self.log(f"[web] Shard {self.name} đã tắt (exit code {process.returncode}).")

            if self.name == "Master":
                self.cluster.stats = None

    def _handle_keywords(self, line):
        if "uploads added to server." in line:
            self.status = "online"
            if self.name == "Master":
                self.cluster.request_stats()

        elif "[Shard] Stopping" in line or "Shutting down" in line:
            self.status = "stopping"

        elif "E_INVALID_TOKEN" in line or "E_EXPIRED_TOKEN" in line:
            self.error = "Cluster token không hợp lệ hoặc đã hết hạn."
            self.cluster.stop_all()

        elif "SOCKET_PORT_ALREADY_IN_USE" in line:
            self.error = "Port đã bị chiếm dụng (có thể server khác đang dùng)."
            self.cluster.stop_all()

        elif "[Error] Server failed to start!" in line:
            self.error = "Server khởi động thất bại, xem log."

        elif "LUA ERROR stack traceback" in line:
            self.error = "Có lỗi Lua (thường do mod), xem log."

        elif "Received world rollback request" in line:
            self.status = "starting"

        elif self.name == "Master" and ("Sim paused" in line or "Sim unpaused" in line):
            self.cluster.request_stats()

    def send(self, command):
        if not self.is_running():
            return False

        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def stop(self, timeout=90, save=True):
        if not self.is_running():
            return

        process = self.process

        if self.status == "starting":
            process.terminate()
        else:
            self.status = "stopping"
            self.send(f"c_shutdown({str(save).lower()})")

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.log("[web] Shard không tắt kịp, buộc dừng.")
            process.kill()


# ------------------------------------------------------------------------------------ #
# Cluster (một server)

class Cluster:
    def __init__(self, cluster_id):
        self.id = cluster_id
        self.dir = CLUSTERS_ROOT / cluster_id
        self.backup_dir = BACKUP_ROOT / cluster_id
        self.shards = {name: Shard(self, name) for name in SHARDS}
        self.stats = None
        self.busy = False  # đang start/stop/restart/import
        self.master_sharded = False  # Master lúc khởi động có bật shard không
        self._settings_lock = threading.Lock()

    # --- Cấu hình riêng của web ---

    def load_settings(self):
        with self._settings_lock:
            path = self.dir / PANEL_FILE

            if path.exists():
                return json.loads(path.read_text())

            return {"slot": 0, "caves_enabled": True, "mods": []}

    def save_settings(self, settings):
        with self._settings_lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / PANEL_FILE).write_text(json.dumps(settings, indent=2))

    def update_settings(self, **values):
        settings = self.load_settings()
        settings.update(values)
        self.save_settings(settings)

    # --- File ---

    def path(self, relative):
        return self.dir / relative

    def editable(self, name):
        if name not in EDITABLE_FILES:
            raise HTTPException(404)

        return self.path(EDITABLE_FILES[name])

    @property
    def token_file(self):
        return self.dir / "cluster_token.txt"

    def name(self):
        return read_ini(self.dir / "cluster.ini").get("NETWORK", "cluster_name", fallback=self.id)

    def ports(self):
        cluster_ini = read_ini(self.dir / "cluster.ini")
        result = {"shard_master_port": cluster_ini.getint("SHARD", "master_port", fallback=0)}

        for shard in SHARDS:
            server_ini = read_ini(self.dir / shard / "server.ini")
            result[shard] = {
                key: server_ini.getint(section, key, fallback=0)
                for section, key in (("NETWORK", "server_port"), ("STEAM", "master_server_port"), ("STEAM", "authentication_port"))
            }

        return result

    def ensure_files(self, cluster_name=None):
        """Tạo file mặc định còn thiếu, không ghi đè thứ người dùng đã sửa."""

        self.dir.mkdir(parents=True, exist_ok=True)

        settings = self.load_settings()

        if not (self.dir / PANEL_FILE).exists():
            self.save_settings(settings)

        ports = slot_ports(settings["slot"])

        cluster_ini = self.dir / "cluster.ini"
        parser = read_ini(cluster_ini)

        for section, values in DEFAULT_CLUSTER_INI.items():
            if not parser.has_section(section):
                parser.add_section(section)

            for key, value in values.items():
                if not parser.has_option(section, key):
                    parser.set(section, key, value)

        if not cluster_ini.exists():
            parser.set("SHARD", "master_port", str(ports["shard_master_port"]))

        if cluster_name is not None:
            parser.set("NETWORK", "cluster_name", cluster_name)

        if not parser.get("SHARD", "cluster_key"):
            parser.set("SHARD", "cluster_key", secrets.token_hex(16))

        write_ini(cluster_ini, parser)

        for shard in SHARDS:
            server_ini = self.dir / shard / "server.ini"

            if server_ini.exists():
                continue

            shard_ports = ports[shard]
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read_dict({
                "NETWORK": {"server_port": str(shard_ports["server_port"])},
                "SHARD":   {"is_master": "true"} if shard == "Master" else {"is_master": "false", "name": "Caves", "id": str(secrets.randbelow(2**31 - 2) + 2)},
                "STEAM":   {"master_server_port": str(shard_ports["master_server_port"]), "authentication_port": str(shard_ports["authentication_port"])},
                "ACCOUNT": {"encode_user_path": "true"},
            })
            write_ini(server_ini, parser)

        for relative, content in (
            (EDITABLE_FILES["caves_worldgen"], CAVES_WORLDGEN),
            (EDITABLE_FILES["master_leveldata"], MASTER_LEVELDATA),
            (EDITABLE_FILES["modoverrides"], MODOVERRIDES),
        ):
            path = self.path(relative)

            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

    def sync_modoverrides(self):
        src = self.editable("modoverrides")
        dst = self.dir / "Caves" / "modoverrides.lua"

        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)

    # --- Tiến trình ---

    def update_stats(self, raw):
        try:
            self.stats = json.loads(raw)
        except ValueError:
            pass

    def request_stats(self):
        self.shards["Master"].send(STATS_LUA)

    def any_running(self):
        return any(s.is_running() for s in self.shards.values())

    def require_stopped(self):
        if self.any_running() or self.busy:
            raise HTTPException(409, "Hãy tắt server trước.")

    def check_ready(self):
        if not SERVER_EXE.exists():
            raise HTTPException(400, "Chưa cài game server. Vào trang chủ để cài đặt.")

        if not self.token_file.exists():
            raise HTTPException(400, "Chưa có cluster token. Vào tab Token để nhập.")

    def check_can_start(self):
        self.check_ready()

        own_ports = flat_ports(self.ports())

        for other in registry.all():
            if other is not self and other.any_running() and own_ports & flat_ports(other.ports()):
                raise HTTPException(409, f'Trùng port với server "{other.name()}" đang chạy. Đổi port trong tab Cấu hình.')

    def start_all(self):
        """Chạy trong run_background: tải mod có thể mất vài phút."""

        self.check_can_start()

        mod_ids = self.load_settings().get("mods", [])
        master = self.shards["Master"]
        master.status = "starting"

        try:
            update_game_if_needed(master.log)
            failed = sync_workshop_mods(mod_ids, master.log) if mod_ids else []
        finally:
            master.status = "offline"

        if failed:
            master.log(f"[web] Không tải được {len(failed)} mod qua steamcmd, để server tự tải: {', '.join(failed)}")

        self.ensure_files()
        self.sync_modoverrides()
        write_mods_setup()

        self.shards["Master"].start()
        self.master_sharded = read_ini(self.dir / "cluster.ini").getboolean("SHARD", "shard_enabled", fallback=False)

        if self.load_settings().get("caves_enabled", True):
            self.shards["Caves"].start()

    def set_caves(self, enabled):
        """Bật/tắt Caves; nếu server đang chạy thì áp dụng ngay."""

        cluster_ini = self.dir / "cluster.ini"
        parser = read_ini(cluster_ini)
        parser.set("SHARD", "shard_enabled", str(enabled).lower())
        write_ini(cluster_ini, parser)
        self.update_settings(caves_enabled=enabled)

        master, caves = self.shards["Master"], self.shards["Caves"]

        if not master.is_running():
            return

        if not enabled:
            master.log("[web] Tắt Caves...")
            caves.stop()
        elif self.master_sharded:
            master.log("[web] Bật Caves...")
            caves.start()
        else:
            # Master chạy ở chế độ không shard thì Caves không kết nối được, phải khởi động lại cả cụm
            master.log("[web] Bật Caves cần khởi động lại server...")
            self.stop_all_and_wait()
            self.start_all()

    def stop_all(self, save=True):
        threads = [threading.Thread(target=s.stop, kwargs={"save": save}) for s in self.shards.values() if s.is_running()]

        for t in threads:
            t.start()

        return threads

    def stop_all_and_wait(self, save=True):
        for t in self.stop_all(save):
            t.join()

    def run_background(self, fn):
        if self.busy:
            raise HTTPException(409, "Đang xử lý một thao tác khác.")

        def runner():
            self.busy = True
            try:
                fn()
            except Exception as e:
                detail = e.detail if isinstance(e, HTTPException) else e
                self.shards["Master"].log(f"[web] Lỗi: {detail}")
            finally:
                self.busy = False

        threading.Thread(target=runner, daemon=True).start()

    def summary(self):
        settings = self.load_settings()
        cluster_ini = read_ini(self.dir / "cluster.ini")
        ports = self.ports()

        return {
            "id": self.id,
            "name": self.name(),
            "game_mode": cluster_ini.get("GAMEPLAY", "game_mode", fallback=""),
            "max_players": cluster_ini.get("GAMEPLAY", "max_players", fallback=""),
            "has_password": bool(cluster_ini.get("NETWORK", "cluster_password", fallback="")),
            "caves_enabled": settings.get("caves_enabled", True),
            "mods_count": len(settings.get("mods", [])),
            "token_set": self.token_file.exists(),
            "busy": self.busy,
            "stats": self.stats,
            "ports": {"Master": ports["Master"]["server_port"], "Caves": ports["Caves"]["server_port"]},
            "shards": {
                name: {"status": s.status, "error": s.error, "memory_mb": s.memory_mb()}
                for name, s in self.shards.items()
            },
        }


class Registry:
    def __init__(self):
        self.clusters = {}
        self.lock = threading.Lock()

    def load(self):
        CLUSTERS_ROOT.mkdir(parents=True, exist_ok=True)

        for path in sorted(CLUSTERS_ROOT.iterdir()):
            if path.is_dir() and (path / "cluster.ini").exists() and CLUSTER_ID_RE.match(path.name):
                cluster = self.clusters.get(path.name) or Cluster(path.name)
                cluster.ensure_files()
                self.clusters[path.name] = cluster

        self._migrate_old_config()

    def _migrate_old_config(self):
        """Bản cũ chỉ có 1 server, lưu mods/caves trong config.json."""

        config = load_config()

        if "mods" not in config and "caves_enabled" not in config:
            return

        old = self.clusters.get("MyCluster")

        if old:
            old.update_settings(mods=config.get("mods", []), caves_enabled=config.get("caves_enabled", True))

        config.pop("mods", None)
        config.pop("caves_enabled", None)
        save_config(config)

    def all(self):
        return list(self.clusters.values())

    def get(self, cluster_id):
        cluster = self.clusters.get(cluster_id)

        if cluster is None:
            raise HTTPException(404, "Không tìm thấy server.")

        return cluster

    def any_running(self):
        return any(c.any_running() or c.busy for c in self.clusters.values())

    def used_ports(self):
        used = set()

        for cluster in self.all():
            used |= flat_ports(cluster.ports())

        return used

    def free_slot(self):
        used_slots = {c.load_settings().get("slot") for c in self.all()}
        used_ports = self.used_ports()
        slot = 0

        while slot in used_slots or flat_ports(slot_ports(slot)) & used_ports:
            slot += 1

        return slot

    def make_id(self, name):
        decomposed = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D"))
        ascii_name = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
        base = re.sub(r"[^A-Za-z0-9]+", "_", ascii_name).strip("_")[:30] or "Server"
        cluster_id, n = base, 2

        while cluster_id in self.clusters or (CLUSTERS_ROOT / cluster_id).exists():
            cluster_id = f"{base}_{n}"
            n += 1

        return cluster_id

    def create(self, name, copy_token, cluster_id=None):
        with self.lock:
            if cluster_id is not None and (not CLUSTER_ID_RE.match(cluster_id) or cluster_id in self.clusters):
                raise HTTPException(400, f"ID server không hợp lệ hoặc đã có: {cluster_id}")

            cluster = Cluster(cluster_id or self.make_id(name))
            cluster.save_settings({"slot": self.free_slot(), "caves_enabled": True, "mods": []})
            cluster.ensure_files(cluster_name=name)

            if copy_token:
                donor = next((c for c in self.all() if c.token_file.exists()), None)

                if donor:
                    shutil.copyfile(donor.token_file, cluster.token_file)
                    cluster.token_file.chmod(0o600)

            self.clusters[cluster.id] = cluster

        return cluster

    def delete(self, cluster):
        cluster.require_stopped()
        backup = make_backup(cluster, "truoc-khi-xoa")

        with self.lock:
            shutil.rmtree(cluster.dir)
            self.clusters.pop(cluster.id, None)
            sync_state_path(cluster.id).unlink(missing_ok=True)

        write_mods_setup()

        return backup


registry = Registry()


def write_mods_setup():
    """
    dedicated_server_mods_setup.lua nằm trong thư mục game, dùng chung cho mọi server.
    Chỉ ghi những mod chưa được web cài sẵn vào mods/workshop-<id> (để server tự tải).
    Mod có chạy hay không do modoverrides.lua của từng server quyết định.
    File bị ghi đè mỗi lần update game nên luôn ghi lại trước khi chạy.
    """

    if not MODS_SETUP.parent.exists():
        return

    mod_ids = []

    for cluster in registry.all():
        for mod_id in cluster.load_settings().get("mods", []):
            if mod_id not in mod_ids and not (MODS_DIR / f"workshop-{mod_id}" / "modinfo.lua").exists():
                mod_ids.append(mod_id)

    lines = ["-- Được tạo bởi DST Web Panel, đừng sửa tay.\n"]
    lines += [f'ServerModSetup("{mod_id}")\n' for mod_id in mod_ids]

    MODS_SETUP.write_text("".join(lines))


# ------------------------------------------------------------------------------------ #
# Đọc modinfo.lua / modoverrides.lua bằng Lua thật (lupa) để cấu hình mod như trong game

# Chạy file Lua trong môi trường hạn chế (không có os/io/require), giới hạn số lệnh.
LUA_SANDBOX = """
local loadstring, setfenv, pcall, sethook = loadstring, setfenv, pcall, debug.sethook

local function copy(t)
  local r = {}
  for k, v in pairs(t) do r[k] = v end
  return r
end

local safe_string = copy(string)
safe_string.dump = nil

return function(code, chunkname, extra)
  local env = {
    assert = assert, error = error, ipairs = ipairs, next = next, pairs = pairs, pcall = pcall,
    select = select, tonumber = tonumber, tostring = tostring, type = type, unpack = unpack,
    xpcall = xpcall, rawget = rawget, rawset = rawset, rawequal = rawequal,
    setmetatable = setmetatable, getmetatable = getmetatable,
    string = safe_string, table = copy(table), math = copy(math),
    print = function() end,
  }
  env._G = env

  for k, v in pairs(extra or {}) do env[k] = v end

  env.ChooseTranslationTable = function(tbl)
    return tbl[env.locale] or tbl[1]
  end

  local fn, err = loadstring(code, chunkname)
  if not fn then return nil, err end

  setfenv(fn, env)
  sethook(function() error("chạy quá lâu") end, "", 5000000)
  local ok, result = pcall(fn)
  sethook()

  if not ok then return nil, tostring(result) end
  return env, result
end
"""

LUA_KEYWORDS = {
    "and", "break", "do", "else", "elseif", "end", "false", "for", "function", "if", "in",
    "local", "nil", "not", "or", "repeat", "return", "then", "true", "until", "while",
}
LUA_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def run_lua(code, chunkname, extra=None):
    """Trả về (env, giá trị return) của file Lua, ném ValueError nếu lỗi."""

    import lupa.lua51 as lupa

    lua = lupa.LuaRuntime(register_eval=False, register_builtins=False, max_memory=64 * 1024 * 1024)
    sandbox = lua.execute(LUA_SANDBOX)

    try:
        env, result = sandbox(code.lstrip("﻿"), "=" + chunkname, lua.table_from(extra or {}))
    except lupa.LuaError as e:
        raise ValueError(str(e))

    if env is None:
        raise ValueError(str(result))

    return env, result


def lua_to_py(value, depth=0):
    import lupa.lua51 as lupa

    if lupa.lua_type(value) == "function" or depth > 20:
        return None

    if lupa.lua_type(value) != "table":
        return value

    items = dict(value.items())

    if items and all(type(k) is int for k in items) and sorted(items) == list(range(1, len(items) + 1)):
        return [lua_to_py(items[k], depth + 1) for k in sorted(items)]

    return {k: lua_to_py(v, depth + 1) for k, v in items.items() if isinstance(k, (str, int, float))}


def lua_string(s):
    out = []

    for ch in s:
        if ch in '\\"':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\{ord(ch):03d}")
        else:
            out.append(ch)

    return '"' + "".join(out) + '"'


def to_lua(value, indent=0):
    """Ghi giá trị Python thành Lua, cùng kiểu trình bày với file game tự tạo."""

    pad = "  " * indent

    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() and abs(value) < 2**53 else repr(float(value))
    if isinstance(value, str):
        return lua_string(value)

    if isinstance(value, list):
        entries = [to_lua(v, indent + 1) for v in value]
    else:
        entries = []

        for key in sorted(value, key=lambda k: (not isinstance(k, str), str(k))):
            if isinstance(key, str) and LUA_IDENT_RE.match(key) and key not in LUA_KEYWORDS:
                lua_key = key
            else:
                lua_key = f"[{lua_string(key) if isinstance(key, str) else to_lua(key)}]"
            entries.append(f"{lua_key}={to_lua(value[key], indent + 1)}")

    if not entries:
        return "{  }"

    return "{\n" + ",\n".join(pad + "  " + e for e in entries) + "\n" + pad + "}"


_modinfo_cache = {}


MOD_FOLDER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def read_modinfo(folder):
    """Tên, mô tả và configuration_options của mod (vd. folder "workshop-123"), None nếu chưa có trên máy."""

    if not MOD_FOLDER_RE.match(folder) or folder.startswith("."):
        return None

    path = MODS_DIR / folder / "modinfo.lua"

    if not path.exists():
        return None

    mtime = path.stat().st_mtime
    cached = _modinfo_cache.get(folder)

    if cached and cached[0] == mtime:
        return cached[1]

    try:
        env, _ = run_lua(
            path.read_text(encoding="utf-8", errors="replace"), f"{folder}/modinfo.lua",
            {"locale": "en", "folder_name": folder, "IsInFrontEnd": True},
        )
        info = {key: lua_to_py(env[key]) for key in (
            "name", "description", "author", "version", "client_only_mod", "all_clients_require_mod", "configuration_options",
        )}
        error = None
    except ValueError as e:
        info, error = {}, str(e)

    options = []

    for opt in info.get("configuration_options") or []:
        if not isinstance(opt, dict):
            continue

        choices = [
            {"description": str(c.get("description", "")), "data": c.get("data"), "hover": c.get("hover") or ""}
            for c in opt.get("options") or [] if isinstance(c, dict)
        ]
        name = opt.get("name")
        label = opt.get("label") or name or ""

        options.append({
            "name": name if isinstance(name, str) else "",
            "label": str(label),
            "hover": str(opt.get("hover") or ""),
            "default": opt.get("default"),
            "options": choices,
            "client": bool(opt.get("client")),
            # Dòng tiêu đề nhóm: chỉ có 1 lựa chọn rỗng, giống cách game hiển thị.
            "header": len(choices) <= 1 and all(c["description"] == "" for c in choices),
        })

    result = {
        "name": str(info.get("name") or folder),
        "description": str(info.get("description") or ""),
        "author": str(info.get("author") or ""),
        "version": str(info.get("version") or ""),
        "client_only": bool(info.get("client_only_mod")),
        "all_clients_require": bool(info.get("all_clients_require_mod")),
        "options": options,
        "error": error,
    }

    _modinfo_cache[folder] = (mtime, result)
    return result


def read_modoverrides(path):
    """{"workshop-ID": {"enabled": bool, "configuration_options": {...}}, ...}"""

    if not path.exists():
        return {}

    _, result = run_lua(path.read_text(encoding="utf-8", errors="replace"), "modoverrides.lua")
    result = lua_to_py(result)

    if result in (None, []):
        return {}

    if not isinstance(result, dict):
        raise ValueError("modoverrides.lua phải trả về một bảng (return { ... }).")

    return result


def stats_loop():
    while True:
        time.sleep(30)

        for cluster in registry.all():
            if cluster.shards["Master"].status == "online":
                cluster.request_stats()


# ------------------------------------------------------------------------------------ #
# Cài đặt / cập nhật qua steamcmd

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# steamcmd không chạy song song được (cài game và tải mod dùng chung).
steamcmd_lock = threading.Lock()

MOD_SIGNATURE_FILE = ".webpanel_source"


def workshop_signature(src):
    return "\n".join(
        f"{p.relative_to(src)}:{p.stat().st_size}:{int(p.stat().st_mtime)}"
        for p in sorted(src.rglob("*")) if p.is_file()
    )


def extract_legacy_mod(archive, dest):
    """Mod legacy là file zip, đường dẫn bên trong dùng dấu \\ kiểu Windows."""

    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            target = (dest / name).resolve()

            if not target.is_relative_to(dest.resolve()):
                raise ValueError(f"Đường dẫn không hợp lệ trong mod: {info.filename}")

            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)

            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def install_workshop_mod(mod_id):
    """Chép mod steamcmd đã tải vào mods/workshop-<id>, bỏ qua nếu không có gì thay đổi."""

    src = WORKSHOP_DIR / "steamapps" / "workshop" / "content" / DST_GAME_ID / mod_id
    dst = MODS_DIR / f"workshop-{mod_id}"
    signature = workshop_signature(src)

    if (dst / MOD_SIGNATURE_FILE).exists() and (dst / MOD_SIGNATURE_FILE).read_text() == signature:
        return False

    tmp = dst.with_name(dst.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)

    legacy = list(src.glob("*_legacy.bin"))

    if legacy:
        tmp.mkdir()
        extract_legacy_mod(legacy[0], tmp)
    else:
        shutil.copytree(src, tmp)

    (tmp / MOD_SIGNATURE_FILE).write_text(signature)

    shutil.rmtree(dst, ignore_errors=True)
    tmp.rename(dst)

    # Bản server tự tải trước đây (nếu có) có thể được ưu tiên hơn, xoá đi cho chắc.
    for old in (SERVER_DIR / "ugc_mods").glob(f"*/*/content/{DST_GAME_ID}/{mod_id}"):
        shutil.rmtree(old, ignore_errors=True)

    return True


def sync_workshop_mods(mod_ids, log):
    """
    Tải/cập nhật mod bằng steamcmd rồi cài vào mods/ của game.
    Server tự tải mod (ServerModSetup) hay bị timeout với mod legacy, steamcmd thì không.
    Trả về danh sách mod không tải được.
    """

    steamcmd = STEAMCMD_EXE

    if not steamcmd.exists():
        return list(mod_ids)

    args = [str(steamcmd), "+force_install_dir", str(WORKSHOP_DIR), "+login", "anonymous"]

    for mod_id in mod_ids:
        args += ["+workshop_download_item", DST_GAME_ID, mod_id]

    args.append("+quit")

    log(f"[web] Đang tải/cập nhật {len(mod_ids)} mod bằng steamcmd...")
    WORKSHOP_DIR.mkdir(parents=True, exist_ok=True)

    with steamcmd_lock:
        try:
            process = subprocess.run(
                args, cwd=STEAMCMD_DIR, capture_output=True, text=True, errors="replace", timeout=1800, **NO_WINDOW,
            )
            output = ANSI_RE.sub("", process.stdout)
        except subprocess.TimeoutExpired:
            log("[web] steamcmd chạy quá lâu, bỏ qua.")
            output = ""

    failed, updated = [], 0

    for mod_id in mod_ids:
        error = re.search(rf"ERROR! Download item {mod_id} failed \(([^)]*)\)", output)

        if f"Downloaded item {mod_id} " not in output:
            log(f"[web] Mod {mod_id}: lỗi tải ({error.group(1) if error else 'không rõ'})")

            if not (MODS_DIR / f"workshop-{mod_id}" / "modinfo.lua").exists():
                failed.append(mod_id)

            continue

        try:
            updated += install_workshop_mod(mod_id)
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            log(f"[web] Mod {mod_id}: lỗi cài đặt ({e})")
            failed.append(mod_id)

    log(f"[web] Mod: {len(mod_ids) - len(failed)}/{len(mod_ids)} sẵn sàng, {updated} mod mới/cập nhật.")

    return failed


def run_app_update(log):
    """Cài/cập nhật game bằng steamcmd, trả về exit code. Người gọi phải giữ steamcmd_lock."""

    args = [
        str(STEAMCMD_EXE),
        "+force_install_dir", str(SERVER_DIR),
        "+login", "anonymous",
        "+app_update", DST_APP_ID, "validate",
        "+quit",
    ]

    # steamcmd hay lỗi "Missing configuration" ở lần chạy đầu, thử lại là được.
    for attempt in range(1, 4):
        log(f"$ {' '.join(args)}  (lần {attempt})")

        process = subprocess.Popen(
            args, cwd=STEAMCMD_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1, **NO_WINDOW,
        )

        for line in process.stdout:
            log(ANSI_RE.sub("", line).rstrip())

        process.wait()

        if process.returncode == 0:
            break

    return process.returncode


def latest_build():
    """Build mới nhất của nhánh public trên Steam, None nếu không kiểm tra được."""

    steamcmd = STEAMCMD_EXE

    if not steamcmd.exists():
        return None

    args = [str(steamcmd), "+login", "anonymous", "+app_info_update", "1", "+app_info_print", DST_APP_ID, "+quit"]

    try:
        with steamcmd_lock:
            output = subprocess.run(
                args, cwd=STEAMCMD_DIR, capture_output=True, text=True, errors="replace", timeout=300, **NO_WINDOW,
            ).stdout
    except subprocess.TimeoutExpired:
        return None

    match = re.search(r'"branches"\s*\{\s*"public"\s*\{\s*"buildid"\s+"(\d+)"', output)
    return match.group(1) if match else None


def update_game_if_needed(log):
    """
    Klei cập nhật game thì client không còn thấy server bản cũ trong danh sách,
    nên trước mỗi lần start kiểm tra và cập nhật nếu Steam có bản mới.
    """

    if install_job.running:
        return

    if registry.any_running():
        log("[web] Có server khác đang chạy, bỏ qua kiểm tra cập nhật game.")
        return

    latest, current = latest_build(), installed_build()

    if latest is None:
        log("[web] Không kiểm tra được bản game mới nhất, chạy với bản hiện tại.")
        return

    if latest == current:
        return

    log(f"[web] Có bản game mới (build {current} → {latest}), đang cập nhật...")

    with steamcmd_lock:
        returncode = run_app_update(log)

    if returncode == 0:
        log(f"[web] Đã cập nhật game lên build {installed_build()}.")
    else:
        log(f"[web] Cập nhật game lỗi (exit code {returncode}), chạy với bản hiện tại.")


AUTO_UPDATE_INTERVAL = 30 * 60


def auto_update_loop():
    """Server đang chạy mà có bản game mới thì tự restart (để start_all cập nhật) khi không còn ai chơi."""

    while True:
        time.sleep(AUTO_UPDATE_INTERVAL)

        try:
            running = [c for c in registry.all() if c.any_running()]

            if not running or install_job.running or any(c.busy for c in running):
                continue

            latest = latest_build()

            if latest is None or latest == installed_build():
                continue

            if any((c.stats or {}).get("players") for c in running):
                for c in running:
                    c.shards["Master"].log(f"[web] Có bản game mới (build {latest}), sẽ tự cập nhật khi không còn ai chơi.")
                continue

            for c in running:
                c.busy = True

            try:
                for c in running:
                    c.shards["Master"].log(f"[web] Có bản game mới (build {latest}), tự restart để cập nhật...")
                    c.stop_all_and_wait()

                for c in running:
                    try:
                        c.start_all()
                    except Exception as e:
                        detail = e.detail if isinstance(e, HTTPException) else e
                        c.shards["Master"].log(f"[web] Lỗi: {detail}")
            finally:
                for c in running:
                    c.busy = False

        except Exception as e:
            print(f"auto_update_loop: {e}", flush=True)


class InstallJob:
    def __init__(self):
        self.running = False
        self.lines = deque(maxlen=2000)
        self.seq = 0
        self.result = None

    def log(self, text):
        self.seq += 1
        self.lines.append((self.seq, text))

    def start(self):
        if self.running:
            raise HTTPException(409, "Đang cài đặt.")

        if registry.any_running():
            raise HTTPException(409, "Hãy tắt tất cả server trước khi cập nhật.")

        self.running = True
        self.result = None
        self.lines.clear()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        with steamcmd_lock:
            self._install()

    def _install(self):
        try:
            steamcmd = STEAMCMD_EXE

            if not steamcmd.exists():
                self.log("Đang tải steamcmd...")
                STEAMCMD_DIR.mkdir(parents=True, exist_ok=True)
                archive = STEAMCMD_DIR / STEAMCMD_URL.rsplit("/", 1)[1]
                urllib.request.urlretrieve(STEAMCMD_URL, archive)

                if IS_WINDOWS:
                    with zipfile.ZipFile(archive) as zf:
                        zf.extractall(STEAMCMD_DIR)
                else:
                    with tarfile.open(archive) as tar:
                        tar.extractall(STEAMCMD_DIR, filter="data")

                archive.unlink()

            returncode = run_app_update(self.log)
            ok = returncode == 0 and SERVER_EXE.exists()
            self.result = "ok" if ok else f"lỗi (exit code {returncode})"
            self.log(f"Hoàn tất: {self.result}")

            if ok:
                write_mods_setup()

        except Exception as e:
            self.result = f"lỗi: {e}"
            self.log(self.result)

        finally:
            self.running = False


install_job = InstallJob()


def installed_build():
    manifest = SERVER_DIR / "steamapps" / f"appmanifest_{DST_APP_ID}.acf"

    if manifest.exists():
        match = re.search(r'"buildid"\s+"(\d+)"', manifest.read_text())
        if match:
            return match.group(1)

    return None


def memory_info():
    if IS_WINDOWS:
        try:
            import psutil
            vm = psutil.virtual_memory()
            return {"total_mb": vm.total // 1024 // 1024, "available_mb": vm.available // 1024 // 1024}
        except Exception:
            return {"total_mb": None, "available_mb": None}

    info = {}

    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        info[key] = int(value.split()[0]) // 1024

    return {"total_mb": info.get("MemTotal"), "available_mb": info.get("MemAvailable")}


# ------------------------------------------------------------------------------------ #
# Sao lưu / import thế giới

MAX_UPLOAD_BYTES  = 1024 * 1024 * 1024      # 1 GB file zip
MAX_EXTRACT_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB sau khi giải nén
BACKUP_KEEP       = 20

# Những file luôn giữ của server, không lấy từ file upload / không đưa vào bản sao lưu.
KEEP_FROM_SERVER = {"cluster_token.txt", "server.ini", PANEL_FILE}


def make_backup(cluster, label="backup"):
    cluster.backup_dir.mkdir(parents=True, exist_ok=True)

    name = f"{cluster.id}_{datetime.now():%Y%m%d-%H%M%S}_{label}.zip"
    path = cluster.backup_dir / name

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in cluster.dir.rglob("*"):
            if file.is_file() and file.name not in ("cluster_token.txt", PANEL_FILE):
                zf.write(file, Path(cluster.id) / file.relative_to(cluster.dir))

    for old in sorted(cluster.backup_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime)[:-BACKUP_KEEP]:
        old.unlink()

    return name


def safe_extract(zip_path, dest):
    with zipfile.ZipFile(zip_path) as zf:
        if sum(info.file_size for info in zf.infolist()) > MAX_EXTRACT_BYTES:
            raise HTTPException(400, "File giải nén quá lớn.")

        for info in zf.infolist():
            target = (dest / info.filename).resolve()

            if not target.is_relative_to(dest.resolve()):
                raise HTTPException(400, f"Đường dẫn không hợp lệ trong zip: {info.filename}")

        zf.extractall(dest)


def find_cluster_root(root):
    """Thư mục nông nhất chứa cluster.ini hoặc thư mục Master/."""

    candidates = [p.parent for p in root.rglob("cluster.ini")]
    candidates += [p.parent for p in root.rglob("Master") if p.is_dir()]

    if not candidates:
        return None

    return min(candidates, key=lambda p: len(p.parts))


def import_cluster(cluster, zip_path, use_cluster_ini):
    """Thay thế thế giới của server bằng nội dung file zip. Server phải đang tắt."""

    with tempfile.TemporaryDirectory(dir=STORAGE_ROOT) as tmp:
        tmp = Path(tmp)

        try:
            safe_extract(zip_path, tmp)
        except zipfile.BadZipFile:
            raise HTTPException(400, "File không phải zip hợp lệ.")

        src = find_cluster_root(tmp)

        if src is None or not (src / "Master").is_dir():
            raise HTTPException(400, "Không tìm thấy thư mục Master trong file zip. Hãy nén cả thư mục Cluster_N.")

        has_caves = (src / "Caves").is_dir()
        backup = make_backup(cluster, "truoc-upload")

        for shard in SHARDS:
            dst_shard = cluster.dir / shard

            # Xoá mọi thứ của shard cũ, trừ server.ini (port).
            if dst_shard.exists():
                for item in dst_shard.iterdir():
                    if item.name in KEEP_FROM_SERVER:
                        continue
                    shutil.rmtree(item) if item.is_dir() else item.unlink()

            if (src / shard).is_dir():
                shutil.copytree(
                    src / shard, dst_shard, dirs_exist_ok=True,
                    ignore=lambda d, names: [n for n in names if n in KEEP_FROM_SERVER],
                )

        if use_cluster_ini and (src / "cluster.ini").exists():
            # Giữ phần [SHARD] của server để port giao tiếp giữa các shard không bị trùng.
            own = read_ini(cluster.dir / "cluster.ini")
            parser = read_ini(src / "cluster.ini")

            if own.has_section("SHARD"):
                if not parser.has_section("SHARD"):
                    parser.add_section("SHARD")
                for key, value in own.items("SHARD"):
                    parser.set("SHARD", key, value)

            write_ini(cluster.dir / "cluster.ini", parser)

        for name in ("adminlist.txt", "blocklist.txt", "whitelist.txt"):
            if (src / name).exists():
                shutil.copyfile(src / name, cluster.dir / name)

    mod_ids = finish_world_import(cluster, has_caves)

    return {"backup": backup, "caves": has_caves, "mods": mod_ids}


def finish_world_import(cluster, has_caves):
    """Sau khi thay file thế giới: tạo file mặc định còn thiếu, bật/tắt Caves, cập nhật danh sách mod."""

    # Tạo lại file mặc định còn thiếu (vd. Caves/worldgenoverride.lua khi thế giới không có hang).
    cluster.ensure_files()

    parser = read_ini(cluster.dir / "cluster.ini")
    parser.set("SHARD", "shard_enabled", str(has_caves).lower())
    write_ini(cluster.dir / "cluster.ini", parser)

    # Lấy danh sách mod từ modoverrides để server tự tải.
    modoverrides = cluster.editable("modoverrides")
    mod_ids = re.findall(r"workshop-(\d+)", modoverrides.read_text()) if modoverrides.exists() else []

    settings = cluster.load_settings()
    cluster.update_settings(caves_enabled=has_caves, mods=list(dict.fromkeys(settings.get("mods", []) + mod_ids)))
    write_mods_setup()
    cluster.sync_modoverrides()

    return mod_ids


# ------------------------------------------------------------------------------------ #
# App + auth

security = HTTPBasic()


def auth(credentials: HTTPBasicCredentials = Depends(security)):
    config = load_config()

    user_ok = secrets.compare_digest(credentials.username.encode(), config["username"].encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), config["password"].encode())

    if not (user_ok and pass_ok):
        raise HTTPException(401, "Sai tài khoản", headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(app):
    load_config()
    registry.load()
    threading.Thread(target=stats_loop, daemon=True).start()
    threading.Thread(target=auto_update_loop, daemon=True).start()
    yield

    threads = [t for c in registry.all() for t in c.stop_all()]

    for t in threads:
        t.join()


app = FastAPI(title="DST Web Panel", lifespan=lifespan, dependencies=[Depends(auth)])


def get_cluster(cid: str) -> Cluster:
    return registry.get(cid)


@app.get("/")
def index():
    return FileResponse(WEBUI_DIR / "static" / "index.html")


# --- Trang chủ ----------------------------------------------------------------------- #

@app.get("/api/status")
def status():
    return {
        "installed": SERVER_EXE.exists(),
        "build": installed_build(),
        "installing": install_job.running,
        "memory": memory_info(),
        "clusters": [c.summary() for c in registry.all()],
    }


class NewCluster(BaseModel):
    name: str
    copy_token: bool = True


@app.post("/api/clusters")
def create_cluster(body: NewCluster):
    name = body.name.strip()

    if not name or "\n" in name:
        raise HTTPException(400, "Tên server không hợp lệ.")

    return registry.create(name, body.copy_token).summary()


@app.delete("/api/clusters/{cid}")
def delete_cluster(cluster: Cluster = Depends(get_cluster)):
    return {"backup": registry.delete(cluster)}


@app.post("/api/install")
def install():
    install_job.start()
    return {"ok": True}


@app.get("/api/install/log")
def install_log(after: int = 0):
    return {"lines": [{"seq": s, "text": t} for s, t in install_job.lines if s > after], "seq": install_job.seq}


# --- Điều khiển từng server ---------------------------------------------------------- #

@app.get("/api/clusters/{cid}")
def cluster_status(cluster: Cluster = Depends(get_cluster)):
    return cluster.summary()


@app.post("/api/clusters/{cid}/start")
def server_start(cluster: Cluster = Depends(get_cluster)):
    if cluster.busy:
        raise HTTPException(409, "Đang xử lý một thao tác khác.")

    cluster.check_can_start()
    cluster.run_background(cluster.start_all)
    return {"ok": True}


@app.post("/api/clusters/{cid}/switch-on")
def server_switch_on(cluster: Cluster = Depends(get_cluster)):
    """Bật server này và tắt mọi server khác (chỉ một server chạy tại một thời điểm)."""

    if cluster.busy:
        raise HTTPException(409, "Đang xử lý một thao tác khác.")

    others = [c for c in registry.all() if c is not cluster]

    for other in others:
        if other.busy:
            raise HTTPException(409, f'Server "{other.name()}" đang xử lý thao tác khác, thử lại sau.')

    cluster.check_ready()

    def switch():
        running = [c for c in others if c.any_running()]

        for other in running:
            other.busy = True
            cluster.shards["Master"].log(f'[web] Tắt server "{other.name()}" để bật server này...')

        try:
            for t in [t for other in running for t in other.stop_all()]:
                t.join()
        finally:
            for other in running:
                other.busy = False

        cluster.start_all()

    cluster.run_background(switch)
    return {"ok": True}


@app.post("/api/clusters/{cid}/stop")
def server_stop(cluster: Cluster = Depends(get_cluster)):
    cluster.run_background(cluster.stop_all_and_wait)
    return {"ok": True}


@app.post("/api/clusters/{cid}/restart")
def server_restart(cluster: Cluster = Depends(get_cluster)):
    def restart():
        cluster.stop_all_and_wait()
        cluster.start_all()

    cluster.run_background(restart)
    return {"ok": True}


class CavesToggle(BaseModel):
    enabled: bool


@app.post("/api/clusters/{cid}/caves")
def caves_toggle(body: CavesToggle, cluster: Cluster = Depends(get_cluster)):
    cluster.ensure_files()
    cluster.run_background(lambda: cluster.set_caves(body.enabled))
    return {"ok": True}


class ConsoleCommand(BaseModel):
    shard: str = "Master"
    command: str


@app.post("/api/clusters/{cid}/console")
def console(body: ConsoleCommand, cluster: Cluster = Depends(get_cluster)):
    shard = cluster.shards.get(body.shard)

    if not shard or not shard.send(body.command):
        raise HTTPException(400, "Shard không chạy.")

    shard.log(f"> {body.command}")
    return {"ok": True}


class Action(BaseModel):
    action: str
    value: str | int | None = None


@app.post("/api/clusters/{cid}/action")
def action(body: Action, cluster: Cluster = Depends(get_cluster)):
    master = cluster.shards["Master"]

    if body.action == "save":
        command = "c_save()"
    elif body.action == "rollback":
        command = ROLLBACK_LUA.format(n=max(1, min(int(body.value or 1), 10)))
    elif body.action == "regenerate":
        command = "c_regenerateworld()"
    elif body.action == "announce":
        command = f"c_announce({json.dumps(str(body.value or ''))})"
    else:
        raise HTTPException(400, "Thao tác không hợp lệ.")

    if not master.send(command):
        raise HTTPException(400, "Master shard không chạy.")

    master.log(f"> {body.action} {body.value or ''}".rstrip())
    return {"ok": True}


@app.get("/api/clusters/{cid}/logs/{shard}")
def logs(shard: str, after: int = 0, cluster: Cluster = Depends(get_cluster)):
    if shard not in cluster.shards:
        raise HTTPException(404)

    lines, seq = cluster.shards[shard].get_lines(after)
    return {"lines": lines, "seq": seq}


# --- Cấu hình server ----------------------------------------------------------------- #

class ClusterSettings(BaseModel):
    cluster_name: str
    cluster_description: str = ""
    cluster_password: str = ""
    cluster_intention: str = "cooperative"
    cluster_language: str = "en"
    game_mode: str = "survival"
    max_players: int = 6
    pvp: bool = False
    pause_when_empty: bool = True
    max_snapshots: int = 6
    caves_enabled: bool = True
    master_port: int = 10999
    caves_port: int = 11000


CLUSTER_FIELDS = {
    "cluster_name": "NETWORK", "cluster_description": "NETWORK", "cluster_password": "NETWORK",
    "cluster_intention": "NETWORK", "cluster_language": "NETWORK",
    "game_mode": "GAMEPLAY", "max_players": "GAMEPLAY", "pvp": "GAMEPLAY", "pause_when_empty": "GAMEPLAY",
    "max_snapshots": "MISC",
}


@app.get("/api/clusters/{cid}/settings")
def get_settings(cluster: Cluster = Depends(get_cluster)):
    parser = read_ini(cluster.dir / "cluster.ini")
    result = {}

    for key, section in CLUSTER_FIELDS.items():
        value = parser.get(section, key, fallback="")
        result[key] = {"true": True, "false": False}.get(value, value)

    ports = cluster.ports()
    result["caves_enabled"] = cluster.load_settings().get("caves_enabled", True)
    result["master_port"] = ports["Master"]["server_port"]
    result["caves_port"] = ports["Caves"]["server_port"]

    return result


@app.put("/api/clusters/{cid}/settings")
def put_settings(body: ClusterSettings, cluster: Cluster = Depends(get_cluster)):
    if any("\n" in str(v) for v in body.model_dump().values()):
        raise HTTPException(400, "Giá trị không được chứa xuống dòng.")

    if body.master_port == body.caves_port:
        raise HTTPException(400, "Port của Master và Caves phải khác nhau.")

    other_ports = set()

    for other in registry.all():
        if other is not cluster:
            other_ports |= flat_ports(other.ports())

    if {body.master_port, body.caves_port} & other_ports:
        raise HTTPException(400, "Port game đang được server khác dùng.")

    cluster.ensure_files()

    cluster_ini = cluster.dir / "cluster.ini"
    parser = read_ini(cluster_ini)
    data = body.model_dump()

    for key, section in CLUSTER_FIELDS.items():
        value = data[key]
        parser.set(section, key, str(value).lower() if isinstance(value, bool) else str(value))

    parser.set("SHARD", "shard_enabled", str(body.caves_enabled).lower())
    write_ini(cluster_ini, parser)

    for shard, port in (("Master", body.master_port), ("Caves", body.caves_port)):
        server_ini = cluster.dir / shard / "server.ini"
        parser = read_ini(server_ini)
        parser.set("NETWORK", "server_port", str(port))
        write_ini(server_ini, parser)

    cluster.update_settings(caves_enabled=body.caves_enabled)

    return {"ok": True}


# --- Token --------------------------------------------------------------------------- #

class Token(BaseModel):
    token: str


@app.get("/api/clusters/{cid}/token")
def get_token(cluster: Cluster = Depends(get_cluster)):
    if not cluster.token_file.exists():
        return {"set": False, "preview": ""}

    token = cluster.token_file.read_text().strip()
    return {"set": True, "preview": token[:10] + "…" + token[-4:] if len(token) > 16 else "***"}


@app.put("/api/clusters/{cid}/token")
def put_token(body: Token, cluster: Cluster = Depends(get_cluster)):
    token = body.token.strip()

    if not token or "\n" in token:
        raise HTTPException(400, "Token không hợp lệ.")

    cluster.ensure_files()
    cluster.token_file.write_text(token)
    cluster.token_file.chmod(0o600)

    for shard in cluster.shards.values():
        shard.error = None

    return {"ok": True}


# --- Mods ---------------------------------------------------------------------------- #

class Mods(BaseModel):
    mods: list[str]


@app.get("/api/clusters/{cid}/mods")
def get_mods(cluster: Cluster = Depends(get_cluster)):
    return {"mods": cluster.load_settings().get("mods", [])}


@app.put("/api/clusters/{cid}/mods")
def put_mods(body: Mods, cluster: Cluster = Depends(get_cluster)):
    ids = []

    for item in body.mods:
        match = re.search(r"(\d{6,})", item)  # chấp nhận cả link workshop
        if match and match.group(1) not in ids:
            ids.append(match.group(1))

    cluster.update_settings(mods=ids)
    write_mods_setup()

    return {"mods": ids}


@app.post("/api/clusters/{cid}/mods/download")
def download_mods(cluster: Cluster = Depends(get_cluster)):
    """Tải mod về ngay (không cần khởi động server) để đọc được tuỳ chọn cấu hình."""

    mod_ids = cluster.load_settings().get("mods", [])

    if not mod_ids:
        raise HTTPException(400, "Danh sách mod đang trống.")

    def download():
        master = cluster.shards["Master"]
        failed = sync_workshop_mods(mod_ids, master.log)

        if failed:
            master.log(f"[web] Không tải được: {', '.join(failed)}")

    cluster.run_background(download)
    return {"ok": True}


@app.get("/api/clusters/{cid}/modconfig")
def get_modconfig(cluster: Cluster = Depends(get_cluster)):
    """Danh sách mod kèm tuỳ chọn trong modinfo.lua và giá trị đang đặt trong modoverrides.lua."""

    error = None

    try:
        overrides = read_modoverrides(cluster.editable("modoverrides"))
    except ValueError as e:
        overrides, error = {}, f"Không đọc được modoverrides.lua: {e}"

    folders = [f"workshop-{mod_id}" for mod_id in cluster.load_settings().get("mods", [])]
    folders += [key for key in overrides if isinstance(key, str) and key not in folders]

    mods = []

    for folder in folders:
        entry = overrides.get(folder)
        entry = entry if isinstance(entry, dict) else {}
        config = entry.get("configuration_options")
        info = read_modinfo(folder)

        mods.append({
            "folder": folder,
            "workshop_id": folder.removeprefix("workshop-") if re.fullmatch(r"workshop-\d+", folder) else None,
            "installed": info is not None,
            "in_overrides": folder in overrides,
            "enabled": bool(entry.get("enabled")),
            "config": config if isinstance(config, dict) else {},
            **(info or {"name": folder, "description": "", "author": "", "version": "", "client_only": False,
                        "all_clients_require": False, "options": [], "error": None}),
        })

    return {"mods": mods, "error": error}


class ModConfig(BaseModel):
    enabled: bool
    config: dict = {}


class ModConfigs(BaseModel):
    mods: dict[str, ModConfig]


@app.put("/api/clusters/{cid}/modconfig")
def put_modconfig(body: ModConfigs, cluster: Cluster = Depends(get_cluster)):
    path = cluster.editable("modoverrides")

    try:
        overrides = read_modoverrides(path)
    except ValueError as e:
        raise HTTPException(400, f"modoverrides.lua đang lỗi, sửa tay ở mục Nâng cao trước: {e}")

    for folder, mod in body.mods.items():
        if not MOD_FOLDER_RE.match(folder):
            raise HTTPException(400, f"Tên mod không hợp lệ: {folder}")

        entry = overrides.get(folder)
        entry = entry if isinstance(entry, dict) else {}
        config = entry.get("configuration_options")

        # Giữ lại các khoá không có trong modinfo (mod tự ghi thêm), chỉ ghi đè cái được sửa.
        entry["configuration_options"] = {**(config if isinstance(config, dict) else {}), **mod.config}
        entry["enabled"] = mod.enabled
        overrides[folder] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("return " + to_lua(overrides) + "\n")
    cluster.sync_modoverrides()

    return {"ok": True}


# --- Upload / sao lưu thế giới ------------------------------------------------------- #

@app.post("/api/clusters/{cid}/upload")
def world_upload(file: UploadFile, use_cluster_ini: bool = Form(False), cluster: Cluster = Depends(get_cluster)):
    cluster.require_stopped()

    with tempfile.NamedTemporaryFile(dir=STORAGE_ROOT, suffix=".zip") as tmp:
        size = 0

        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)

            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(400, "File quá lớn (tối đa 1 GB).")

            tmp.write(chunk)

        tmp.flush()

        return import_cluster(cluster, Path(tmp.name), use_cluster_ini)


@app.get("/api/clusters/{cid}/backups")
def list_backups(cluster: Cluster = Depends(get_cluster)):
    if not cluster.backup_dir.exists():
        return {"backups": []}

    files = sorted(cluster.backup_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)

    return {"backups": [
        {"name": p.name, "size_mb": round(p.stat().st_size / 1024 / 1024, 1),
         "time": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
        for p in files
    ]}


def backup_path(cluster, name):
    path = cluster.backup_dir / Path(name).name

    if not path.exists() or path.suffix != ".zip":
        raise HTTPException(404, "Không tìm thấy bản sao lưu.")

    return path


@app.post("/api/clusters/{cid}/backups")
def create_backup(cluster: Cluster = Depends(get_cluster)):
    return {"name": make_backup(cluster)}


@app.get("/api/clusters/{cid}/backups/{name}")
def download_backup(name: str, cluster: Cluster = Depends(get_cluster)):
    return FileResponse(backup_path(cluster, name), filename=Path(name).name, media_type="application/zip")


@app.post("/api/clusters/{cid}/backups/{name}/restore")
def restore_backup(name: str, cluster: Cluster = Depends(get_cluster)):
    cluster.require_stopped()
    return import_cluster(cluster, backup_path(cluster, name), use_cluster_ini=True)


@app.delete("/api/clusters/{cid}/backups/{name}")
def delete_backup(name: str, cluster: Cluster = Depends(get_cluster)):
    backup_path(cluster, name).unlink()
    return {"ok": True}


# --- Tải về chơi local ---------------------------------------------------------------- #

# Không cần khi chạy ở máy khác: log, bản backup tự động của game, cấu hình riêng của web
EXPORT_SKIP_DIRS  = {"backup"}
EXPORT_SKIP_FILES = {"cluster_token.txt", PANEL_FILE, "server_log.txt", "server_chat_log.txt"}

EXPORT_README = """DST - goi chay local: {name}
==================================================

Thu muc trong goi:
  {cid}/
                                     thu muc the gioi (cluster)
  mods/                              file mod (chi co khi chon "kem mod")
  dedicated_server_mods_setup.lua    danh sach mod de server tu tai tu Workshop
  start_server.bat                   script chay dedicated server tren Windows
  mods.txt                           link Workshop cua tung mod

CACH 1 - Choi bang game (Host Game), don gian nhat
--------------------------------------------------
1. Mo thu muc save cua DST:
     Windows: Documents\\Klei\\DoNotStarveTogether\\<day so>\\
     macOS:   ~/Documents/Klei/DoNotStarveTogether/<day so>/
2. Chep thu muc "{cid}" vao do va DOI TEN thanh Cluster_N, voi N la mot o the gioi
   con trong trong game (vi du Cluster_5 neu dang co 4 the gioi).
3. Subscribe cac mod trong mods.txt tren Steam Workshop (hoac chep cac thu muc
   trong mods/ vao <thu muc cai game>\\mods\\).
4. Vao game > Host Game, chon o the gioi vua chep, kiem tra tab Mods roi Resume.
{caves_note}
CACH 2 - Chay dedicated server tren may (giong server that)
-----------------------------------------------------------
1. Cai "Don't Starve Together Dedicated Server" trong Steam (muc Tools).
2. Chep thu muc "{cid}" vao Documents\\Klei\\DoNotStarveTogether\\ (giu nguyen ten).
3. Tao file {cid}\\cluster_token.txt chua token (lay o tab Token cua web, hoac
   trong game: Account > Generate Server Token).
4. Mod: chep dedicated_server_mods_setup.lua (va thu muc mods/ neu co) vao
   <thu muc Dedicated Server>\\mods\\ - server se tu tai mod con thieu.
5. Mo start_server.bat, sua dong DST_DIR neu cai o cho khac, roi chay.
   Ket noi trong game: Browse Games > tab LAN, hoac console: c_connect("127.0.0.1", {port})
"""

EXPORT_BAT = r"""@echo off
rem Chay the gioi "{cid}" bang DST Dedicated Server tren Windows.
set DST_DIR=C:\Program Files (x86)\Steam\steamapps\common\Don't Starve Together Dedicated Server
set CLUSTER={cid}

if not exist "%USERPROFILE%\Documents\Klei\DoNotStarveTogether\%CLUSTER%\cluster_token.txt" (
  echo Chua co cluster_token.txt trong thu muc %CLUSTER%. Xem README.txt.
  pause
  exit /b 1
)

cd /d "%DST_DIR%\bin64"
start "DST Master" dontstarve_dedicated_server_nullrenderer_x64.exe -console -cluster %CLUSTER% -shard Master
{caves_line}"""


def build_local_package(cluster, include_mods):
    settings = cluster.load_settings()
    mod_ids = settings.get("mods", [])
    caves = settings.get("caves_enabled", True)

    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=STORAGE_ROOT)
    os.close(fd)

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for file in cluster.dir.rglob("*"):
            rel = file.relative_to(cluster.dir)

            if not file.is_file() or file.name in EXPORT_SKIP_FILES or EXPORT_SKIP_DIRS & set(rel.parts[:-1]):
                continue
            if not caves and rel.parts[0] == "Caves":
                continue

            zf.write(file, Path(cluster.id) / rel)

        if include_mods:
            for mod_id in mod_ids:
                folder = MODS_DIR / f"workshop-{mod_id}"

                for file in folder.rglob("*"):
                    if file.is_file() and file.name != MOD_SIGNATURE_FILE:
                        zf.write(file, Path("mods") / file.relative_to(MODS_DIR))

        zf.writestr("dedicated_server_mods_setup.lua", "".join(f'ServerModSetup("{m}")\n' for m in mod_ids))
        zf.writestr("mods.txt", "".join(
            f"https://steamcommunity.com/sharedfiles/filedetails/?id={m}\n" for m in mod_ids))

        bat = EXPORT_BAT.format(
            cid=cluster.id,
            caves_line='start "DST Caves" dontstarve_dedicated_server_nullrenderer_x64.exe -console -cluster %CLUSTER% -shard Caves\n' if caves else "",
        )
        zf.writestr("start_server.bat", bat.replace("\n", "\r\n"))

        readme = EXPORT_README.format(
            name=cluster.name(), cid=cluster.id, port=cluster.ports()["Master"]["server_port"],
            caves_note="" if caves else "   (Server dang tat Caves nen goi khong co thu muc Caves.)\n",
        )
        zf.writestr("README.txt", readme.replace("\n", "\r\n"))

    return tmp


@app.get("/api/clusters/{cid}/export-local")
def export_local(mods: bool = False, cluster: Cluster = Depends(get_cluster)):
    # Server đang chạy thì lưu trước để gói có trạng thái mới nhất
    if cluster.shards["Master"].send("c_save()"):
        time.sleep(5)

    tmp = build_local_package(cluster, mods)
    name = f"{cluster.id}_local_{datetime.now():%Y%m%d-%H%M}{'_mods' if mods else ''}.zip"

    return FileResponse(tmp, filename=name, media_type="application/zip", background=BackgroundTask(os.unlink, tmp))


# --- Đồng bộ 2 chiều với một thư mục trên máy Windows ------------------------------- #
#
# Máy local thường nằm sau NAT nên server không gọi vào được. Trên máy local chạy một agent
# PowerShell (sync_agent/dstsync.ps1), cứ SYNC_POLL_SECONDS hỏi server qua /sync-api một lần.
# Server quyết định chiều đồng bộ dựa trên "baseline" = mã băm của lần đồng bộ thành công trước:
#   - chỉ server đổi  -> lấy về local (pull)
#   - chỉ local đổi   -> đẩy lên server (push)
#   - cả hai cùng đổi -> bên có file save mới hơn thắng, bên kia được sao lưu trước khi ghi đè.

SYNC_DIR           = STORAGE_ROOT / "sync"
SYNC_AGENT_DIR     = WEBUI_DIR / "sync_agent"
SYNC_SKIP_FILES    = {"cluster_token.txt", PANEL_FILE, "server.ini", "server_log.txt", "server_chat_log.txt",
                      "client_log.txt", "client_chat_log.txt"}
SYNC_SKIP_DIRS     = {"backup"}
SYNC_MANIFEST_NAME = "__dstsync_manifest.json"
SYNC_RETRY_SECONDS = 30 * 60   # đồng bộ hỏng/bị hoãn thì thử lại sau chừng này
SYNC_ONLINE_SECONDS = 120      # agent không hỏi quá chừng này thì coi là mất kết nối
SYNC_HISTORY_KEEP  = 20
SYNC_MODES         = ("auto", "pull", "push")

_sync_state_lock = threading.Lock()


def sync_state_path(cid):
    return SYNC_DIR / f"{cid}.json"


def sync_load(cid):
    path = sync_state_path(cid)
    state = json.loads(path.read_text()) if path.exists() else {}

    for key, value in {
        "token": None, "interval_hours": 24, "baseline": None, "pending": None, "requested": None,
        "last_attempt": 0, "last_success": 0, "running_since": 0, "agent": None, "history": [],
    }.items():
        state.setdefault(key, value)

    return state


def sync_update(cid, fn):
    """Đọc - sửa - ghi trạng thái sync dưới khoá. fn(state) sửa state tại chỗ."""

    with _sync_state_lock:
        state = sync_load(cid)
        result = fn(state)
        SYNC_DIR.mkdir(parents=True, exist_ok=True)
        path = sync_state_path(cid)
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        path.chmod(0o600)
        return result


def sync_skipped(rel):
    parts = rel.split("/")
    return parts[-1] in SYNC_SKIP_FILES or bool(SYNC_SKIP_DIRS & set(parts[:-1]))


def sync_clean_path(rel):
    """Đường dẫn tương đối kiểu a/b/c do agent gửi lên; chặn .., đường dẫn tuyệt đối..."""

    if not isinstance(rel, str) or not rel or "\\" in rel or rel.startswith("/") or "\0" in rel:
        raise HTTPException(400, f"Đường dẫn không hợp lệ: {rel!r}")

    parts = rel.split("/")

    if any(p in ("", ".", "..") for p in parts) or ":" in parts[0]:
        raise HTTPException(400, f"Đường dẫn không hợp lệ: {rel!r}")

    return rel


def file_sha256(path):
    h = hashlib.sha256()

    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)

    return h.hexdigest()


def server_manifest(cluster):
    manifest = {}

    for file in cluster.dir.rglob("*"):
        if not file.is_file():
            continue

        rel = file.relative_to(cluster.dir).as_posix()

        if not sync_skipped(rel):
            manifest[rel] = {"sha": file_sha256(file), "mtime": int(file.stat().st_mtime)}

    return manifest


def manifest_hash(manifest):
    h = hashlib.sha256()

    for rel in sorted(manifest):
        h.update(f"{rel}\0{manifest[rel]['sha'].lower()}\n".encode())

    return h.hexdigest()


def manifest_has_world(manifest):
    return any(len(rel.split("/")) > 2 and rel.split("/")[1] == "save" for rel in manifest)


def manifest_newest(manifest):
    saves = [m.get("mtime", 0) for rel, m in manifest.items() if "/save/" in rel]
    return max(saves or [m.get("mtime", 0) for m in manifest.values()] or [0])


def sync_decide(server, local, baseline, mode):
    """Trả về (action, lý do, có xung đột không). action: none | pull | push."""

    hs, hl = manifest_hash(server), manifest_hash(local)

    if hs == hl:
        return "none", "Hai bên đã giống nhau.", False
    if mode == "pull":
        return "pull", "Lấy bản từ server theo yêu cầu (bản ở máy local được sao lưu trước).", False
    if mode == "push":
        return "push", "Đẩy bản ở máy local lên server theo yêu cầu (bản trên server được sao lưu trước).", False
    if not manifest_has_world(local):
        return "pull", "Máy local chưa có thế giới, lấy từ server về.", False
    if not manifest_has_world(server):
        return "push", "Server chưa có thế giới, đẩy bản ở máy local lên.", False
    if baseline is None:
        return "pull", "Đồng bộ lần đầu: lấy thế giới từ server (bản ở máy local được sao lưu trước).", False
    if hl == baseline:
        return "pull", "Server có thay đổi mới.", False
    if hs == baseline:
        return "push", "Máy local có thay đổi mới.", False

    if manifest_newest(local) > manifest_newest(server):
        return "push", "Cả hai bên đều thay đổi; bản ở máy local mới hơn nên được giữ (bản trên server đã được sao lưu).", True

    return "pull", "Cả hai bên đều thay đổi; bản trên server mới hơn nên được giữ (bản ở máy local đã được sao lưu).", True


def sync_players(cluster):
    if not cluster.shards["Master"].is_running():
        return []

    return (cluster.stats or {}).get("players") or []


def sync_add_history(state, ok, action, message):
    state["history"].insert(0, {"time": int(time.time()), "ok": ok, "action": action, "message": message})
    del state["history"][SYNC_HISTORY_KEEP:]


def sync_status(cid):
    state = sync_load(cid)
    now = time.time()
    agent = state["agent"]
    next_due = None

    if state["token"]:
        next_due = max(state["last_success"] + state["interval_hours"] * 3600,
                       state["last_attempt"] + SYNC_RETRY_SECONDS if state["last_attempt"] > state["last_success"] else 0)

    return {
        "configured": bool(state["token"]),
        "agent": agent,
        "agent_online": bool(agent and now - agent.get("seen", 0) < SYNC_ONLINE_SECONDS),
        "interval_hours": state["interval_hours"],
        "requested": state["requested"],
        "running": bool(state["running_since"] and now - state["running_since"] < 15 * 60),
        "last_success": state["last_success"] or None,
        "next_due": next_due,
        "history": state["history"],
        "now": int(now),
    }


# ---- API cho agent (xác thực bằng token riêng của từng server, không dùng tài khoản web) ----

sync_api = FastAPI(title="DST Sync API", docs_url=None, redoc_url=None, openapi_url=None)


def sync_cluster(cid: str, x_sync_token: str = Header(default="")) -> Cluster:
    token = sync_load(cid)["token"] if CLUSTER_ID_RE.match(cid) else None

    if not token or not secrets.compare_digest(x_sync_token.encode(), token.encode()):
        raise HTTPException(401, "Token đồng bộ không đúng (có thể đã bị đổi trên web). Tải lại bộ cài agent.")

    return registry.get(cid)


class SyncPoll(BaseModel):
    version: int = 0
    folder: str = ""
    host: str = ""
    game_running: bool = False


@sync_api.post("/{cid}/poll")
def sync_poll(body: SyncPoll, cluster: Cluster = Depends(sync_cluster)):
    now = time.time()

    def apply(state):
        state["agent"] = {"seen": int(now), "folder": body.folder[:300], "host": body.host[:100],
                          "version": body.version, "game_running": body.game_running}

        if state["requested"]:
            return "manual"

        if now - state["last_success"] >= state["interval_hours"] * 3600 and now - state["last_attempt"] >= SYNC_RETRY_SECONDS:
            return "schedule"

        return None

    reason = sync_update(cluster.id, apply)

    return {
        "due": reason is not None,
        "reason": reason,
        "rules": {"skip_files": sorted(SYNC_SKIP_FILES), "skip_dirs": sorted(SYNC_SKIP_DIRS)},
    }


class SyncPlan(BaseModel):
    manifest: dict[str, dict]


def clean_manifest(manifest):
    result = {}

    for rel, info in manifest.items():
        rel = sync_clean_path(rel)

        if sync_skipped(rel):
            continue

        sha = str(info.get("sha", "")).lower()

        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise HTTPException(400, f"Mã băm không hợp lệ cho {rel}")

        result[rel] = {"sha": sha, "mtime": int(info.get("mtime") or 0)}

    return result


@sync_api.post("/{cid}/plan")
def sync_plan(body: SyncPlan, cluster: Cluster = Depends(sync_cluster)):
    local = clean_manifest(body.manifest)
    state = sync_load(cluster.id)
    mode = state["requested"] if state["requested"] in ("pull", "push") else "auto"

    sync_update(cluster.id, lambda s: s.update(last_attempt=int(time.time()), running_since=int(time.time())))

    if cluster.busy:
        return {"action": "wait", "reason": "Server đang bận (đang bật/tắt/cập nhật), sẽ thử lại sau.", "conflict": False}

    server = server_manifest(cluster)
    action, reason, conflict = sync_decide(server, local, state["baseline"], mode)
    players = sync_players(cluster)

    if action == "push" and players:
        return {"action": "wait", "conflict": conflict,
                "reason": f"Máy local có thay đổi nhưng đang có {len(players)} người chơi trên server, hoãn đẩy lên."}

    if action == "pull" and players and cluster.shards["Master"].send("c_save()"):
        # Có người đang chơi: lưu trước để lấy được trạng thái mới nhất.
        time.sleep(5)
        server = server_manifest(cluster)
        action, reason, conflict = sync_decide(server, local, state["baseline"], mode)

    if action == "push" and cluster.any_running():
        reason += " Server sẽ được khởi động lại."

    sync_update(cluster.id, lambda s: s.update(pending=manifest_hash(server)))

    return {"action": action, "reason": reason, "conflict": conflict, "server": server}


class SyncDownload(BaseModel):
    paths: list[str]


@sync_api.post("/{cid}/download")
def sync_download(body: SyncDownload, cluster: Cluster = Depends(sync_cluster)):
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=STORAGE_ROOT)
    os.close(fd)

    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for rel in dict.fromkeys(body.paths):
                rel = sync_clean_path(rel)

                # server.ini chỉ gửi khi máy local còn thiếu (agent tự xin), còn lại phải nằm trong tập đồng bộ.
                if sync_skipped(rel) and rel.split("/")[-1] != "server.ini":
                    raise HTTPException(400, f"Không được tải file này: {rel}")

                file = cluster.dir / rel

                if file.is_file() and file.resolve().is_relative_to(cluster.dir.resolve()):
                    zf.write(file, rel)
    except BaseException:
        os.unlink(tmp)
        raise

    return FileResponse(tmp, media_type="application/zip", background=BackgroundTask(os.unlink, tmp))


def sync_apply_push(cluster, zip_path):
    with tempfile.TemporaryDirectory(dir=STORAGE_ROOT) as tmp:
        tmp = Path(tmp)

        try:
            safe_extract(zip_path, tmp)
        except zipfile.BadZipFile:
            raise HTTPException(400, "Dữ liệu gửi lên không phải zip hợp lệ.")

        try:
            local = clean_manifest(json.loads((tmp / SYNC_MANIFEST_NAME).read_text(encoding="utf-8")))
        except (OSError, ValueError, AttributeError):
            raise HTTPException(400, "Thiếu danh sách file trong gói gửi lên.")

        if not manifest_has_world(local):
            raise HTTPException(400, "Thư mục ở máy local không có thế giới (thiếu Master/save), không đẩy lên.")

        incoming = {}

        for file in tmp.rglob("*"):
            rel = file.relative_to(tmp).as_posix()

            if not file.is_file() or rel == SYNC_MANIFEST_NAME:
                continue
            if rel not in local or file_sha256(file) != local[rel]["sha"]:
                raise HTTPException(400, f"File {rel} gửi lên không khớp danh sách, thử lại.")

            incoming[rel] = file

        if cluster.busy:
            raise HTTPException(409, "Server đang bận, thử lại sau.")

        players = sync_players(cluster)

        if players:
            raise HTTPException(409, f"Đang có {len(players)} người chơi trên server, hoãn đẩy lên.")

        was_running = cluster.any_running()
        master = cluster.shards["Master"]
        cluster.busy = True

        try:
            if was_running:
                # Không lưu khi tắt: thế giới sắp bị thay bằng bản ở máy local, lưu thêm chỉ làm lệch dữ liệu.
                master.log("[web] Đồng bộ: tắt server để nhận thế giới từ máy local...")
                cluster.stop_all_and_wait(save=False)

            current = server_manifest(cluster)

            for rel, info in local.items():
                if rel not in incoming and current.get(rel, {}).get("sha") != info["sha"]:
                    raise HTTPException(409, "Server vừa thay đổi trong lúc đồng bộ, sẽ thử lại sau.")

            backup = make_backup(cluster, "truoc-sync")
            own = read_ini(cluster.dir / "cluster.ini")

            for rel in current:
                if rel not in local:
                    (cluster.dir / rel).unlink()

            for rel, file in incoming.items():
                dest = cluster.dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, dest)

            for d in sorted((p for p in cluster.dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                if d.parent != cluster.dir and not any(d.iterdir()):
                    d.rmdir()

            # Giữ phần [SHARD] của server (port giữa các shard, cluster_key) như khi upload thế giới.
            if own.has_section("SHARD"):
                parser = read_ini(cluster.dir / "cluster.ini")

                if not parser.has_section("SHARD"):
                    parser.add_section("SHARD")
                for key, value in own.items("SHARD"):
                    parser.set("SHARD", key, value)

                write_ini(cluster.dir / "cluster.ini", parser)

            # Giữ lựa chọn bật/tắt Caves của server; chỉ tắt khi thế giới gửi lên không có hang.
            has_caves = cluster.load_settings().get("caves_enabled", True) and any(rel.startswith("Caves/") for rel in local)
            finish_world_import(cluster, has_caves)
            master.log(f"[web] Đồng bộ: đã nhận thế giới từ máy local (bản cũ sao lưu: {backup}).")
        finally:
            cluster.busy = False

            if was_running:
                cluster.run_background(cluster.start_all)

    server = server_manifest(cluster)
    sync_update(cluster.id, lambda s: s.update(pending=manifest_hash(server)))

    return {"ok": True, "backup": backup, "restarted": was_running, "server": server}


@sync_api.post("/{cid}/push")
async def sync_push(request: Request, cluster: Cluster = Depends(sync_cluster)):
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=STORAGE_ROOT)

    try:
        size = 0

        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)

                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(400, "Dữ liệu gửi lên quá lớn (tối đa 1 GB).")

                f.write(chunk)

        return await run_in_threadpool(sync_apply_push, cluster, Path(tmp))
    finally:
        os.unlink(tmp)


class SyncDone(BaseModel):
    ok: bool
    action: str = ""
    message: str = ""
    manifest: dict[str, dict] | None = None


@sync_api.post("/{cid}/done")
def sync_done(body: SyncDone, cluster: Cluster = Depends(sync_cluster)):
    local_hash = manifest_hash(clean_manifest(body.manifest)) if body.manifest is not None else None
    now = int(time.time())

    def apply(state):
        state["running_since"] = 0
        state["requested"] = None
        ok, message = body.ok, body.message[:500]

        if ok and local_hash != state["pending"]:
            ok, message = False, "Sau khi đồng bộ, dữ liệu hai bên vẫn lệch (server có thể vừa lưu). Sẽ thử lại sau."

        if ok:
            state["baseline"] = state["pending"]
            state["last_success"] = now

        sync_add_history(state, ok, body.action[:20], message)
        return ok

    return {"ok": sync_update(cluster.id, apply)}


app.mount("/sync-api", sync_api)


# ---- API cho web ----

@app.get("/api/clusters/{cid}/sync")
def get_sync(cluster: Cluster = Depends(get_cluster)):
    return sync_status(cluster.id)


class SyncRequest(BaseModel):
    mode: str = "auto"


@app.post("/api/clusters/{cid}/sync/request")
def request_sync(body: SyncRequest, cluster: Cluster = Depends(get_cluster)):
    if body.mode not in SYNC_MODES:
        raise HTTPException(400, "Chế độ không hợp lệ.")

    if not sync_load(cluster.id)["token"]:
        raise HTTPException(400, "Chưa cài agent ở máy local.")

    sync_update(cluster.id, lambda s: s.update(requested=body.mode))
    return sync_status(cluster.id)


class SyncSettings(BaseModel):
    interval_hours: int


@app.put("/api/clusters/{cid}/sync/settings")
def put_sync_settings(body: SyncSettings, cluster: Cluster = Depends(get_cluster)):
    if not 1 <= body.interval_hours <= 24 * 7:
        raise HTTPException(400, "Chu kỳ phải từ 1 giờ đến 7 ngày.")

    sync_update(cluster.id, lambda s: s.update(interval_hours=body.interval_hours))
    return sync_status(cluster.id)


@app.post("/api/clusters/{cid}/sync/reset")
def reset_sync(cluster: Cluster = Depends(get_cluster)):
    """Đổi token (agent cũ ngừng hoạt động) và quên baseline: lần sau coi như đồng bộ lần đầu."""

    sync_update(cluster.id, lambda s: s.update(token=None, baseline=None, pending=None, requested=None,
                                              agent=None, last_success=0, last_attempt=0, running_since=0))
    return sync_status(cluster.id)


@app.get("/api/clusters/{cid}/desktop-export")
def desktop_export(cluster: Cluster = Depends(get_cluster)):
    """App DST Launcher trên Windows lấy cấu hình để tạo server giống hệt ở máy local."""

    def ensure_token(state):
        state["token"] = state["token"] or secrets.token_urlsafe(32)
        return state["token"]

    settings = cluster.load_settings()

    return {
        "id": cluster.id,
        "name": cluster.name(),
        "cluster_token": cluster.token_file.read_text().strip() if cluster.token_file.exists() else "",
        "sync_token": sync_update(cluster.id, ensure_token),
        "mods": settings.get("mods", []),
        "caves_enabled": settings.get("caves_enabled", True),
    }


SYNC_INSTALL_BAT = r"""@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
"""


@app.get("/api/clusters/{cid}/sync/agent")
def download_sync_agent(request: Request, cluster: Cluster = Depends(get_cluster)):
    def ensure_token(state):
        state["token"] = state["token"] or secrets.token_urlsafe(32)
        return state["token"]

    token = sync_update(cluster.id, ensure_token)
    config = {"server": str(request.base_url).rstrip("/"), "cluster": cluster.id, "name": cluster.name(), "token": token}
    bom = b"\xef\xbb\xbf"  # PowerShell 5 cần BOM để đọc đúng tiếng Việt

    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=STORAGE_ROOT)
    os.close(fd)

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("dstsync.ps1", "install.ps1", "uninstall.ps1"):
            zf.writestr(name, bom + (SYNC_AGENT_DIR / name).read_bytes())

        zf.writestr("HUONG_DAN.txt", bom + (SYNC_AGENT_DIR / "HUONG_DAN.txt").read_text().replace("\n", "\r\n").encode())
        zf.writestr("config.json", json.dumps(config, indent=2, ensure_ascii=False))
        zf.writestr("CAI_DAT.bat", SYNC_INSTALL_BAT.replace("\n", "\r\n"))

    return FileResponse(tmp, filename=f"dstsync_{cluster.id}.zip", media_type="application/zip",
                        background=BackgroundTask(os.unlink, tmp))


# --- File editor --------------------------------------------------------------------- #

class FileContent(BaseModel):
    content: str


@app.get("/api/clusters/{cid}/files/{name}")
def get_file(name: str, cluster: Cluster = Depends(get_cluster)):
    path = cluster.editable(name)
    return {"content": path.read_text() if path.exists() else "", "path": str(path)}


@app.put("/api/clusters/{cid}/files/{name}")
def put_file(name: str, body: FileContent, cluster: Cluster = Depends(get_cluster)):
    path = cluster.editable(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.content)

    if name == "modoverrides":
        cluster.sync_modoverrides()

    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))
