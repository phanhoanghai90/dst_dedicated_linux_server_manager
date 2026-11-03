"""
Liên kết app desktop (DST Launcher trên Windows) với DST Web Panel trên VPS.

- "Đồng bộ" (pull_configs): lấy danh sách server trên web, tạo server giống hệt ở máy local
  (cùng ID, cluster token, mod, bật/tắt Caves) rồi đồng bộ thế giới.
- Khi app mở: dừng các server đó trên VPS (nếu không có người chơi), rồi cứ interval_min phút
  đồng bộ thế giới 2 chiều một lần (cùng giao thức /sync-api với agent PowerShell).
- Khi app đóng: tắt server local (có lưu), đồng bộ lần cuối, bật lại server trên VPS mà app đã dừng.
"""

import base64
import io
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import deque
from datetime import datetime

from fastapi import HTTPException

import app as core

LINK_FILE = core.BASE_DIR / "remote.json"
POLL_SECONDS = 30
DEFAULTS = {
    "url": "", "username": "", "password": "",
    "interval_min": 10, "stop_remote": True, "restart_remote_on_exit": True,
    "clusters": {},          # cid -> {"sync_token": ...}
    "stopped_by_app": [],    # server trên VPS do app dừng, bật lại khi thoát
}

_file_lock = threading.Lock()
_job_lock = threading.Lock()      # mỗi lúc chỉ một việc đồng bộ
_log = deque(maxlen=300)
_seq = 0
_status = {}                       # cid -> {"time", "ok", "message"}
_state = {"job": None, "next_sync": None, "shutting_down": False}


class RemoteError(Exception):
    pass


def log(text):
    global _seq
    _seq += 1
    _log.append((_seq, f"{datetime.now():%H:%M:%S}  {text}"))
    print(text, flush=True)


def load_link():
    with _file_lock:
        data = json.loads(LINK_FILE.read_text(encoding="utf-8")) if LINK_FILE.exists() else {}

    return {**DEFAULTS, **data}


def save_link(data):
    with _file_lock:
        LINK_FILE.parent.mkdir(parents=True, exist_ok=True)
        LINK_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def update_link(**values):
    data = load_link()
    data.update(values)
    save_link(data)
    return data


def configured(link=None):
    link = link or load_link()
    return bool(link["url"] and link["username"] and link["password"])


# ------------------------------------------------------------------------------------ #
# HTTP

def request(path, body=None, raw=None, method=None, sync_token=None, binary=False, timeout=900, link=None):
    link = link or load_link()
    url = link["url"].rstrip("/") + path
    headers = {}

    if sync_token:
        headers["X-Sync-Token"] = sync_token
    else:
        cred = base64.b64encode(f"{link['username']}:{link['password']}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"

    data = None

    if raw is not None:
        data, headers["Content-Type"] = raw, "application/zip"
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method or ("POST" if data is not None else "GET"))

    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            content = res.read()
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")

        try:
            detail = json.loads(text).get("detail", text)
        except ValueError:
            detail = text[:200]

        if e.code == 401 and not sync_token:
            detail = "Sai tài khoản/mật khẩu web panel."
        if e.code == 404 and "Not Found" in str(detail):
            detail = "Web panel trên VPS chưa có tính năng này, hãy cập nhật panel."

        raise RemoteError(f"VPS báo lỗi ({e.code}): {detail}") from None
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise RemoteError(f"Không kết nối được VPS: {getattr(e, 'reason', e)}") from None

    return content if binary else json.loads(content or b"{}")


# ------------------------------------------------------------------------------------ #
# Đồng bộ thế giới của một server (cùng thuật toán với agent PowerShell)

def _remove_empty_dirs(root):
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if d.parent != root and not any(d.iterdir()):
            d.rmdir()


def _pull(cluster, token, server, local):
    need = [p for p in server if p not in local or local[p]["sha"] != server[p]["sha"]]
    extra = [p for p in local if p not in server]

    if need:
        data = request(f"/sync-api/{cluster.id}/download", {"paths": need}, sync_token=token, binary=True)

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                rel = core.sync_clean_path(name)
                dest = cluster.dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(name))

    for rel in extra:
        (cluster.dir / rel).unlink(missing_ok=True)

    _remove_empty_dirs(cluster.dir)

    # Cấu hình riêng của app (không nằm trong file đồng bộ) theo cluster.ini / modoverrides vừa lấy về.
    caves = core.read_ini(cluster.dir / "cluster.ini").getboolean("SHARD", "shard_enabled", fallback=True)
    modoverrides = cluster.editable("modoverrides")
    mod_ids = re.findall(r"workshop-(\d+)", modoverrides.read_text()) if modoverrides.exists() else []
    settings = cluster.load_settings()
    cluster.update_settings(caves_enabled=caves, mods=list(dict.fromkeys(settings.get("mods", []) + mod_ids)))
    core.write_mods_setup()

    return len(need), len(extra)


def _push(cluster, token, server, local):
    changed = [p for p in local if p not in server or server[p]["sha"] != local[p]["sha"]]
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(core.SYNC_MANIFEST_NAME, json.dumps(local))
        for rel in changed:
            zf.write(cluster.dir / rel, rel)

    res = request(f"/sync-api/{cluster.id}/push", raw=buf.getvalue(), sync_token=token)
    return res["server"], len(changed)


def sync_world(cid, final=False):
    link = load_link()
    token = link["clusters"].get(cid, {}).get("sync_token")
    cluster = core.registry.clusters.get(cid)

    if not token or cluster is None:
        return

    def done(ok, action, message, manifest=None):
        body = {"ok": ok, "action": action, "message": message}
        if manifest is not None:
            body["manifest"] = manifest
        try:
            ok = request(f"/sync-api/{cid}/done", body, sync_token=token)["ok"] and ok
        except RemoteError as e:
            ok, message = False, str(e)
        _status[cid] = {"time": int(time.time()), "ok": ok, "message": message}
        log(f"[{cid}] {'✓' if ok else '✗'} {message}")
        return ok

    try:
        request(f"/sync-api/{cid}/poll", {"version": 1, "folder": f"DST Launcher: {cluster.dir}",
                                          "host": socket.gethostname()}, sync_token=token)

        master = cluster.shards["Master"]
        local_running = cluster.any_running()

        # Đang có người chơi ở máy local: lưu trước để bản đẩy lên có tiến độ mới nhất.
        if local_running and (cluster.stats or {}).get("players") and master.send("c_save()"):
            time.sleep(5)

        local = core.server_manifest(cluster)
        plan = request(f"/sync-api/{cid}/plan", {"manifest": local}, sync_token=token)
        action = plan["action"]

        if action == "wait":
            return done(False, "wait", plan["reason"])

        if action == "pull" and cluster.any_running():
            return done(False, "wait", "Server trên VPS có thay đổi mới nhưng server ở máy local đang chạy, "
                                       "chưa ghi đè. Tắt server local rồi đồng bộ lại.")

        if action == "pull":
            if local and not final:
                core.make_backup(cluster, "truoc-sync")
            n, d = _pull(cluster, token, plan["server"], local)
            detail = f" ({n} file mới/đổi, xoá {d})"
        elif action == "push":
            server, n = _push(cluster, token, plan["server"], local)
            _pull(cluster, token, server, core.server_manifest(cluster))  # lấy lại file VPS tự chỉnh (cluster.ini...)
            detail = f" ({n} file)"
        else:
            detail = ""

        verb = {"pull": "Lấy từ VPS", "push": "Đẩy lên VPS", "none": "Không đổi"}[action]
        return done(True, action, f"{verb}: {plan['reason']}{detail}", core.server_manifest(cluster))

    except (RemoteError, HTTPException, OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
        message = e.detail if isinstance(e, HTTPException) else str(e)
        _status[cid] = {"time": int(time.time()), "ok": False, "message": message}
        log(f"[{cid}] ✗ {message}")

        try:
            request(f"/sync-api/{cid}/done", {"ok": False, "action": "error", "message": message}, sync_token=token)
        except RemoteError:
            pass

        return False


def sync_all(final=False):
    for cid in list(load_link()["clusters"]):
        sync_world(cid, final=final)

    link = load_link()
    _state["next_sync"] = int(time.time()) + link["interval_min"] * 60


# ------------------------------------------------------------------------------------ #
# Lấy cấu hình server từ web, dừng / bật server trên VPS

def pull_configs():
    status = request("/api/status")
    remote = status.get("clusters", [])
    log(f"Lấy cấu hình {len(remote)} server từ VPS...")
    link = load_link()

    for item in remote:
        cid = item["id"]
        info = request(f"/api/clusters/{cid}/desktop-export")
        cluster = core.registry.clusters.get(cid)

        if cluster is None:
            cluster = core.registry.create(info["name"], copy_token=False, cluster_id=cid)
            log(f"[{cid}] Đã tạo server local \"{info['name']}\".")

        if info["cluster_token"]:
            cluster.token_file.write_text(info["cluster_token"])

        cluster.update_settings(mods=info["mods"], caves_enabled=info["caves_enabled"])
        link["clusters"][cid] = {"sync_token": info["sync_token"]}

    save_link(link)
    core.write_mods_setup()
    log("Đã lấy cấu hình, đồng bộ thế giới...")
    sync_all()


def remote_cluster(cid):
    return request(f"/api/clusters/{cid}")


def stop_remote_servers():
    link = load_link()
    stopped = list(link["stopped_by_app"])

    for cid in link["clusters"]:
        try:
            st = remote_cluster(cid)
        except RemoteError as e:
            log(f"[{cid}] {e}")
            continue

        if all(s["status"] == "offline" for s in st["shards"].values()):
            continue

        players = (st.get("stats") or {}).get("players") or []

        if players:
            log(f"[{cid}] Đang có {len(players)} người chơi trên VPS ({', '.join(players)}), không dừng server.")
            continue

        log(f"[{cid}] Dừng server trên VPS (sẽ bật lại khi thoát app)...")
        request(f"/api/clusters/{cid}/stop", method="POST")

        if cid not in stopped:
            stopped.append(cid)
        update_link(stopped_by_app=stopped)

        for _ in range(60):  # chờ tối đa 3 phút
            time.sleep(3)
            st = remote_cluster(cid)
            if all(s["status"] == "offline" for s in st["shards"].values()) and not st.get("busy"):
                log(f"[{cid}] Server trên VPS đã dừng.")
                break


def restart_remote_servers():
    link = load_link()
    remaining = []

    for cid in link["stopped_by_app"]:
        try:
            request(f"/api/clusters/{cid}/start", method="POST")
            log(f"[{cid}] Đã bật lại server trên VPS.")
        except RemoteError as e:
            log(f"[{cid}] Không bật lại được server trên VPS: {e}")
            remaining.append(cid)

    update_link(stopped_by_app=remaining)


# ------------------------------------------------------------------------------------ #
# Chạy nền

def run_job(name, fn):
    if not _job_lock.acquire(blocking=False):
        raise HTTPException(409, f"Đang chạy: {_state['job']}")

    def runner():
        _state["job"] = name
        try:
            fn()
        except (RemoteError, HTTPException) as e:
            log(f"Lỗi: {e.detail if isinstance(e, HTTPException) else e}")
        except Exception as e:
            log(f"Lỗi không mong đợi: {e!r}")
        finally:
            _state["job"] = None
            _job_lock.release()

    threading.Thread(target=runner, daemon=True).start()


def startup():
    link = load_link()

    if link["stop_remote"]:
        stop_remote_servers()

    sync_all()


def background_loop():
    if configured() and load_link()["clusters"]:
        try:
            run_job("Khởi động: dừng server VPS và đồng bộ", startup)
        except HTTPException:
            pass

    while not _state["shutting_down"]:
        time.sleep(POLL_SECONDS)

        if _state["shutting_down"] or not configured():
            continue

        link = load_link()
        manual = []

        # Hỏi VPS: hiện trạng thái trên web, và nhận yêu cầu "Đồng bộ ngay" bấm từ web.
        for cid, info in link["clusters"].items():
            try:
                res = request(f"/sync-api/{cid}/poll", {"version": 1, "folder": "DST Launcher", "host": socket.gethostname()},
                              sync_token=info.get("sync_token"), timeout=30)
                if res.get("reason") == "manual":
                    manual.append(cid)
            except RemoteError:
                pass

        due = _state["next_sync"] is None or time.time() >= _state["next_sync"]

        if (due or manual) and not _job_lock.locked():
            try:
                if due:
                    run_job("Đồng bộ định kỳ", sync_all)
                else:
                    run_job("Đồng bộ theo yêu cầu từ web", lambda: [sync_world(cid) for cid in manual])
            except HTTPException:
                pass


def start():
    threading.Thread(target=background_loop, daemon=True).start()


def shutdown(progress=lambda text: None):
    """Gọi khi đóng app: tắt server local (có lưu), đồng bộ lần cuối, bật lại server VPS."""

    _state["shutting_down"] = True
    got_lock = _job_lock.acquire(timeout=600)  # chờ việc đang chạy xong

    try:
        running = [c for c in core.registry.all() if c.any_running()]

        if running:
            progress("Đang lưu và tắt server ở máy local...")
            for t in [t for c in running for t in c.stop_all()]:
                t.join()

        link = load_link()

        if configured(link) and link["clusters"]:
            progress("Đang đồng bộ lần cuối lên VPS...")
            sync_all(final=True)

            if link["restart_remote_on_exit"] and load_link()["stopped_by_app"]:
                progress("Đang bật lại server trên VPS...")
                restart_remote_servers()
    finally:
        if got_lock:
            _job_lock.release()


# ------------------------------------------------------------------------------------ #
# API cho giao diện

def apply_settings(url, username, password, interval_min, stop_remote, restart_remote_on_exit):
    """Lưu cấu hình liên kết sau khi kiểm tra kết nối. Trả về số server trên VPS. Mật khẩu trống = giữ cũ."""

    url = url.strip().rstrip("/")

    if not url.startswith(("http://", "https://")):
        raise RemoteError("Địa chỉ web panel phải bắt đầu bằng http:// hoặc https://")
    if not 1 <= interval_min <= 24 * 60:
        raise RemoteError("Chu kỳ đồng bộ phải từ 1 phút đến 1 ngày.")

    link = load_link()
    new = {**link, "url": url, "username": username.strip(), "password": password or link["password"],
           "interval_min": interval_min, "stop_remote": stop_remote, "restart_remote_on_exit": restart_remote_on_exit}

    count = len(request("/api/status", link=new, timeout=20).get("clusters", []))

    if url != link["url"]:
        new["clusters"], new["stopped_by_app"] = {}, []

    save_link(new)
    _state["next_sync"] = int(time.time()) + interval_min * 60
    return count


def status_payload(after=0):
    link = load_link()
    names = {c.id: c.name() for c in core.registry.all()}

    return {
        "configured": configured(link),
        "url": link["url"], "username": link["username"], "has_password": bool(link["password"]),
        "interval_min": link["interval_min"], "stop_remote": link["stop_remote"],
        "restart_remote_on_exit": link["restart_remote_on_exit"],
        "stopped_by_app": link["stopped_by_app"],
        "clusters": [{"id": cid, "name": names.get(cid, cid), **(_status.get(cid) or {})} for cid in link["clusters"]],
        "job": _state["job"], "next_sync": _state["next_sync"],
        "log": [{"seq": seq, "text": text} for seq, text in list(_log) if seq > after], "seq": _seq,
    }
