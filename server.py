#!/usr/bin/env python3
import http.server
import json
import os
import shlex
import shutil
import subprocess
import time
import signal
import threading
import urllib.request
from urllib.parse import urlparse, parse_qs

PORT = 3361
VERSION = "0.0.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/hirogura/diskmanager/main/install.sh"
SERVICE_NAME = "diskmanager"

running_process = None
current_log_file = None

# ---- ディスク完全消去ジョブ管理 ----
import re
wipe_job = None
wipe_lock = threading.Lock()
# 消去対象として許可するデバイス名（ホールディスクのみ。パーティションは不可）
WIPE_PATH_RE = re.compile(r"^/dev/(sd[a-z]+|hd[a-z]+|vd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")
WIPE_CHUNK = 4 * 1024 * 1024  # 4MB ずつ書き込む

def get_block_devices():
    devices = []
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MODEL,SERIAL,MOUNTPOINT,TRAN,FSTYPE"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            for dev in data.get("blockdevices", []):
                if dev.get("type") == "disk":
                    name = dev.get("name", "")
                    size = dev.get("size", "")
                    model = (dev.get("model") or "").strip()
                    serial = (dev.get("serial") or "").strip()
                    tran = (dev.get("tran") or "").strip()
                    mount = dev.get("mountpoint") or ""
                    fstype = (dev.get("fstype") or "").strip()
                    label = f"/dev/{name} - {size}"
                    if model: label += f" ({model})"
                    if serial: label += f" [{serial}]"
                    if tran: label += f" ({tran})"
                    if fstype: label += f" [{fstype}]"
                    if mount: label += f" mounted:{mount}"
                    devices.append({"path": f"/dev/{name}", "name": name, "size": size,
                        "model": model, "serial": serial, "tran": tran,
                        "mountpoint": mount, "fstype": fstype, "label": label})
    except Exception:
        pass
    return devices

def get_device_info(path):
    info = {}
    try:
        r = subprocess.run(["lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL", path],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            info["lsblk"] = r.stdout.strip()
    except Exception:
        pass
    try:
        r = subprocess.run(["blkid", path], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            info["blkid"] = r.stdout.strip()
    except Exception:
        pass
    try:
        r = subprocess.run(["file", "-s", path], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            info["file_type"] = r.stdout.strip()
    except Exception:
        pass
    return info

def get_file_info(path, for_dest=False):
    info = {}
    if not os.path.exists(path):
        if for_dest:
            info["status"] = "新規作成"
            dir_path = os.path.dirname(path)
            if dir_path and os.path.isdir(dir_path):
                info["target_dir"] = f"ディレクトリ: {dir_path}"
                try:
                    st = os.statvfs(dir_path)
                    free = st.f_bavail * st.f_frsize
                    info["free_space"] = f"空き容量: {free / (1024**3):.2f} GB"
                except Exception:
                    pass
            else:
                info["target_dir"] = f"ディレクトリが見つかりません: {dir_path}"
        else:
            info["error"] = "ファイルが見つかりません"
        return info
    try:
        stat = os.stat(path)
        info["size"] = stat.st_size
        info["size_human"] = f"{stat.st_size / (1024**3):.2f} GB"
        info["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
    except Exception:
        pass
    try:
        r = subprocess.run(["file", path], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            info["file_type"] = r.stdout.strip()
    except Exception:
        pass
    return info

def get_log_files():
    logs = []
    if os.path.isdir(LOG_DIR):
        for f in sorted(os.listdir(LOG_DIR), reverse=True):
            if f.endswith(".log"):
                fpath = os.path.join(LOG_DIR, f)
                logs.append({"name": f, "size": os.path.getsize(fpath), "mtime": os.path.getmtime(fpath)})
    return logs

# ---- ディスク完全消去用ヘルパー ----

def get_wipe_devices():
    """消去ページ用：パーティション情報・SSDヒント付きデバイス一覧"""
    devices = []
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MODEL,SERIAL,TRAN,FSTYPE,MOUNTPOINT"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0:
            return get_block_devices()
        data = json.loads(r.stdout)
        for dev in data.get("blockdevices", []):
            if dev.get("type") != "disk":
                continue
            name = dev.get("name", "")
            # 仮想デバイス（zram/loop/dm/md等）は対象外。実ディスクのみ表示
            if not WIPE_PATH_RE.match(f"/dev/{name}"):
                continue
            size_bytes = int(dev.get("size", 0) or 0)
            model = (dev.get("model") or "").strip()
            serial = (dev.get("serial") or "").strip()
            tran = (dev.get("tran") or "").strip()
            # 人間可読サイズ
            if size_bytes >= 1024**3:
                size = f"{size_bytes / (1024**3):.1f} GB"
            elif size_bytes >= 1024**2:
                size = f"{size_bytes / (1024**2):.1f} MB"
            else:
                size = f"{size_bytes} B"
            # 回転ディスク判定（0=SSD/NVMe、1=HDD、不明はNone）
            rotational = None
            ssd_hint = False
            try:
                with open(f"/sys/block/{name}/queue/rotational", "r") as f:
                    rotational = int(f.read().strip())
                    ssd_hint = (rotational == 0)
            except Exception:
                pass
            # パーティション一覧
            partitions = []
            has_mount = bool(dev.get("mountpoint"))
            for child in dev.get("children") or []:
                cname = child.get("name", "")
                csize = int(child.get("size", 0) or 0)
                if csize >= 1024**3:
                    csize_h = f"{csize / (1024**3):.1f} GB"
                elif csize >= 1024**2:
                    csize_h = f"{csize / (1024**2):.1f} MB"
                else:
                    csize_h = f"{csize} B"
                cmount = child.get("mountpoint") or ""
                if cmount:
                    has_mount = True
                partitions.append({
                    "name": cname, "path": f"/dev/{cname}",
                    "size": csize_h, "size_bytes": csize,
                    "fstype": (child.get("fstype") or "").strip(),
                    "mountpoint": cmount,
                })
            label = f"/dev/{name} - {size}"
            if model: label += f" ({model})"
            if serial: label += f" [{serial}]"
            if tran: label += f" ({tran})"
            devices.append({"path": f"/dev/{name}", "name": name, "size": size,
                "size_bytes": size_bytes, "model": model, "serial": serial,
                "tran": tran, "label": label, "partitions": partitions,
                "has_mount": has_mount, "rotational": rotational,
                "ssd_hint": ssd_hint})
    except Exception:
        pass
    return devices


def get_device_size_bytes(path):
    """blockdev でデバイスのバイト数を取得"""
    try:
        r = subprocess.run(["blockdev", "--getsize64", path],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return int(r.stdout.strip())
    except Exception:
        pass
    return 0


# ---- S.M.A.R.T.情報取得用ヘルパー ----

def get_smart_devices():
    """S.M.A.R.T.ページ用：接続ディスク一覧（消去ページと同等の実ディスク一覧）"""
    devs = get_wipe_devices()
    if devs:
        return devs
    # フォールバック：lsblk の disk 一覧から実デバイスのみ返す
    out = []
    for d in get_block_devices():
        if WIPE_PATH_RE.match(d.get("path", "")):
            d.setdefault("partitions", [])
            d.setdefault("has_mount", bool(d.get("mountpoint")))
            d.setdefault("ssd_hint", False)
            out.append(d)
    return out


def get_smart_info(path):
    """指定ディスクの S.M.A.R.T.情報を取得。smartctl の JSON + テキストを返す"""
    if not WIPE_PATH_RE.match(path or ""):
        return {"path": path, "available": False,
                "error": f"不正なデバイス指定です: {path}"}
    if not os.path.exists(path):
        return {"path": path, "available": False,
                "error": f"デバイスが見つかりません: {path}"}
    # smartctl 本体の存在確認
    try:
        r_ver = subprocess.run(["smartctl", "--version"],
            capture_output=True, text=True, timeout=5)
        if r_ver.returncode != 0 and not (r_ver.stdout or ""):
            return {"path": path, "available": False,
                    "error": "smartctl が利用できません（smartmontools を導入してください）"}
    except FileNotFoundError:
        return {"path": path, "available": False,
                "error": "smartctl が見つかりません（smartmontools を導入してください）"}
    except Exception as e:
        return {"path": path, "available": False, "error": f"smartctl 確認エラー: {e}"}

    # JSON 形式で全情報を取得（終了コードはビットマスクのため成否判定に使わない）
    smart_json = None
    try:
        r = subprocess.run(["smartctl", "-a", "-j", path],
            capture_output=True, text=True, timeout=20)
        raw = (r.stdout or "").strip()
        if raw:
            try:
                smart_json = json.loads(raw)
            except Exception:
                smart_json = None
    except subprocess.TimeoutExpired:
        return {"path": path, "available": False, "error": "smartctl がタイムアウトしました"}
    except Exception as e:
        return {"path": path, "available": False, "error": f"smartctl 実行エラー: {e}"}

    # テキスト形式も併せて取得（画面の「詳細」表示用）
    text_out = ""
    try:
        r2 = subprocess.run(["smartctl", "-a", path],
            capture_output=True, text=True, timeout=20)
        text_out = (r2.stdout or "") + (r2.stderr or "")
        text_out = text_out.strip()
    except Exception:
        pass

    if smart_json is None and not text_out:
        return {"path": path, "available": False,
                "error": "S.M.A.R.T.情報を取得できませんでした"}

    # 利用可否・ヘルス判定
    available = True
    health = "不明"
    health_ok = None
    support_msg = ""
    if smart_json is not None:
        try:
            status = smart_json.get("smart_status") or {}
            if "passed" in status:
                health_ok = bool(status.get("passed"))
                health = "正常" if health_ok else "異常あり"
            # NVMe でも smart_status.passed が入る。無い場合は全体ステータスで補完
            if health_ok is None:
                # exit_status 等から推測できないため不明のまま
                pass
            sup = smart_json.get("smart_support") or {}
            # smart_support.available が false の場合は S.M.A.R.T. 非対応
            if sup.get("available") is False:
                available = False
                support_msg = "このディスクは S.M.A.R.T. に対応していません"
            # デバイス open エラー時は利用不可
            msgs = smart_json.get("messages") or []
            for m in msgs:
                s = (m.get("string") or "") if isinstance(m, dict) else str(m)
                if "unable to" in s.lower() or "failed" in s.lower() or "error" in s.lower():
                    pass
        except Exception:
            pass
    # JSON が取れずテキストのみの場合、テキストから簡易判定
    if smart_json is None and text_out:
        low = text_out.lower()
        if "smart support is: unavailable" in low or "device does not support smart" in low:
            available = False
            support_msg = "このディスクは S.M.A.R.T. に対応していません"
        elif "smart overall-health self-assessment test result: passed" in low:
            health, health_ok = "正常", True
        elif "smart overall-health self-assessment test result: failed" in low:
            health, health_ok = "異常あり", False

    if not available:
        return {"path": path, "available": False,
                "health": health, "health_ok": health_ok,
                "error": support_msg or "S.M.A.R.T. に対応していません",
                "output": text_out, "data": smart_json}

    return {"path": path, "available": True,
            "health": health, "health_ok": health_ok,
            "output": text_out, "data": smart_json}


# ---- Clonezilla 高速クローン用ヘルパー ----
clone_install_running = False
clone_install_lock = threading.Lock()
# 実行中クローンのメタ情報（進捗表示用。サービス再起動に備えてファイルにも保存）
clone_job = None
clone_lock = threading.Lock()
CLONE_JOB_FILE = os.path.join(LOG_DIR, ".clone_job.json")


def _save_clone_job(job):
    """クローンジョブ情報をメモリ＋ファイルに保存する"""
    global clone_job
    with clone_lock:
        clone_job = job
    try:
        with open(CLONE_JOB_FILE, "w") as f:
            json.dump(job, f)
    except Exception:
        pass


def _load_clone_job():
    """メモリ優先、無ければファイルからクローンジョブ情報を復元する"""
    with clone_lock:
        if clone_job is not None:
            return dict(clone_job)
    try:
        if os.path.exists(CLONE_JOB_FILE):
            with open(CLONE_JOB_FILE, "r") as f:
                job = json.load(f)
            if isinstance(job, dict) and job.get("log_path"):
                return job
    except Exception:
        pass
    return None


def _pid_is_clone(pid):
    """指定 pid がクローン関係プロセス（ocs/partclone/stdbuf 経由）か確認する。PID 再利用の誤認防止用"""
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except Exception:
        return False
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            cmd = f.read().decode(errors="replace").lower()
        return ("ocs-" in cmd) or ("partclone" in cmd) or ("stdbuf" in cmd)
    except Exception:
        # cmdline が読めない＝権限等の例外時は生存のみで判断する
        return True


def get_system_disk():
    """システムドライブ（/ が載っている物理ディスク）を /dev/xxx 形式で返す"""
    try:
        r = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "/"],
            capture_output=True, text=True, timeout=5)
        src = (r.stdout or "").strip().split("\n")[0].strip() if r.returncode == 0 else ""
        # 「/dev/sda2[/@]」のような btrfs サブボリューム表記からデバイス部を抽出
        if "[" in src:
            src = src.split("[")[0]
        if not src.startswith("/dev/"):
            return ""
        # パーティション → 親ディスク名を解決
        try:
            r2 = subprocess.run(["lsblk", "-n", "-o", "PKNAME", src],
                capture_output=True, text=True, timeout=5)
            parent = (r2.stdout or "").strip().split("\n")[0].strip()
            if parent:
                return f"/dev/{parent}"
        except Exception:
            pass
        return src
    except Exception:
        return ""


def get_clone_status():
    """Clonezilla 導入状態とシステムディスクを返す"""
    has_onthefly = shutil.which("ocs-onthefly") is not None
    has_sr = shutil.which("ocs-sr") is not None
    has_partclone = any(shutil.which(f"partclone.{n}") for n in
        ("extfs", "btrfs", "xfs", "ntfs", "vfat", "exfat", "dd")) or shutil.which("partclone.dd") is not None
    installed = has_onthefly and has_sr
    with clone_install_lock:
        installing = clone_install_running
    return {"installed": installed, "installing": installing,
        "has_onthefly": has_onthefly, "has_sr": has_sr,
        "has_partclone": has_partclone,
        "system_disk": get_system_disk()}


def get_clone_devices():
    """クローンページ用：システムドライブを除外した実ディスク一覧"""
    sys_disk = get_system_disk()
    devs = get_wipe_devices()
    if not devs:
        # フォールバック：block デバイス一覧から実デバイス＋システム除外のみ適用
        out = []
        for d in get_block_devices():
            if not WIPE_PATH_RE.match(d.get("path", "")):
                continue
            if d.get("path") == sys_disk:
                continue
            d.setdefault("partitions", [])
            d.setdefault("has_mount", bool(d.get("mountpoint")))
            d.setdefault("size_bytes", 0)
            out.append(d)
        return out
    return [d for d in devs if d.get("path") != sys_disk]


def get_whole_disk_fstype(path):
    """ディスク全体（/dev/sdX 自体）に載っているファイルシステム種別を返す。無ければ空文字。
    Clonezilla はディスク全体に FS がある媒体を「パーティション」と判定して
    ディスク間クローンを拒否するため、事前チェック用（LVM2_member は対象外）"""
    try:
        r = subprocess.run(["blkid", "-o", "value", "-s", "TYPE", path],
            capture_output=True, text=True, timeout=5)
        fstype = (r.stdout or "").strip().split("\n")[0].strip()
        if fstype == "LVM2_member":
            return ""
        return fstype
    except Exception:
        return ""


def get_disk_mountpoints(path):
    """指定ディスク配下のマウントポイント・swap 使用状況を返す [(dev, mp)]。mpが [SWAP] のものは swap"""
    out = []
    try:
        r = subprocess.run(["lsblk", "-J", "-o", "NAME,MOUNTPOINT", path],
            capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return out
        data = json.loads(r.stdout or "{}")

        def walk(nodes):
            for n in nodes or []:
                name = n.get("name", "")
                mp = n.get("mountpoint") or ""
                if name and mp:
                    out.append((f"/dev/{name}", mp))
                walk(n.get("children") or [])
        walk(data.get("blockdevices", []))
    except Exception:
        pass
    return out


def _fmt_eta(sec):
    """残り秒数をおおよその日本語表記にする"""
    if sec is None or sec < 0:
        return "残り時間: 計算中"
    sec = int(sec)
    if sec < 10:
        return "まもなく完了"
    if sec < 60:
        return f"残り約 {sec} 秒"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"残り約 {m} 分 {s} 秒"
    h, m = divmod(m, 60)
    return f"残り約 {h} 時間 {m} 分"


def get_clone_progress():
    """実行中クローンの進捗をログ解析で推定する。partclone の出力形式を利用"""
    job = _load_clone_job()
    adopted = False
    if running_process is not None and running_process.poll() is None:
        # 自管理プロセスが別ジョブ（レスキュー等）の場合は実行中としない
        if job and job.get("pid") == running_process.pid:
            running, rc = True, None
        else:
            running, rc = False, None
    elif job and job.get("pid") and _pid_is_clone(job.get("pid")):
        # サービス再起動後に取り残されたプロセスを引き継いで追跡する
        running, rc, adopted = True, None, True
    else:
        running = False
        rc = None if running_process is None else running_process.poll()
    if not job:
        return {"running": running, "job": None}
    log_file = job.get("log_path", "")
    text = ""
    try:
        if log_file and os.path.exists(log_file):
            with open(log_file, "r", errors="replace") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 131072))
                text = f.read()
    except Exception:
        pass
    # 制御文字・ANSIエスケープを除去し、\r を改行扱いにする
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text).replace("\r", "\n")
    # 完了した partclone 処理数（保存・復元とも "Total Time: ..., 100.00% completed!" で1件）
    done_ops = len(re.findall(r"Total Time:.*?100\.00% completed!", text))
    failed = ("Partclone fail" in text) or ("プログラム中断" in text)
    # 現在の処理の進捗（Current block 優先、なければ Complete/Completed %）
    cur_frac = 0.0
    blocks = re.findall(r"Current block:\s*(\d+),\s*Total block:\s*(\d+)", text)
    if blocks:
        cur, tot = blocks[-1]
        try:
            cur_frac = min(1.0, max(0.0, int(cur) / int(tot))) if int(tot) > 0 else 0.0
        except Exception:
            cur_frac = 0.0
    else:
        pcts = re.findall(r"Complete:\s*([\d.]+)\s*%", text)
        if pcts:
            try:
                cur_frac = min(1.0, max(0.0, float(pcts[-1]) / 100.0))
            except Exception:
                cur_frac = 0.0
    # partclone 自身の残り時間・速度表示
    rems = re.findall(r"Remaining:\s*([0-9:]+)", text)
    op_remaining = rems[-1] if rems else ""
    rates = re.findall(r"Rate:\s*([0-9.]+\s*[KMGT]?B/min)", text)
    rate_text = rates[-1] if rates else ""
    # 現在処理中のデバイス（直近の開始行から）
    # ocs-onthefly のディスク間クローンでは "Starting to back up device (src) to device (dst)"
    # 形式になるため、旧形式と併せて最終出現位置で判定する
    cur_dev = ""
    saves = re.findall(r"Starting to clone device \(([^)]+)\)", text)
    restores = re.findall(r"Starting to restore image \([^)]*\) to device \(([^)]+)\)", text)
    backups = re.findall(r"Starting to back up device \(([^)]+)\) to device \(([^)]+)\)", text)
    runs = re.findall(r"Running:\s+partclone\.\S+.*?-s\s+(\S+)\s+.*?-O\s+(\S+)", text)
    # 時系列順は取れないため、テキスト上の最終出現位置で判定
    cands = []
    last_save = text.rfind("Starting to clone device (")
    if last_save >= 0 and saves:
        cands.append((last_save, saves[-1]))
    last_restore = text.rfind("Starting to restore image (")
    if last_restore >= 0 and restores:
        cands.append((last_restore, restores[-1]))
    last_backup = text.rfind("Starting to back up device (")
    if last_backup >= 0 and backups:
        cands.append((last_backup, f"{backups[-1][0]} → {backups[-1][1]}"))
    last_run = text.rfind("Running: partclone.")
    if last_run >= 0 and runs:
        cands.append((last_run, f"{runs[-1][0]} → {runs[-1][1]}"))
    if cands:
        cur_dev = sorted(cands, key=lambda x: x[0])[-1][1]
    total_ops = job.get("total_ops") or (done_ops + 1)
    if total_ops <= 0:
        total_ops = done_ops + 1
    # 現在のフェーズ（ocs の節目行から直近のものを抜粋。partclone 無出力の序盤対策）
    phase = ""
    try:
        noise_re = re.compile(
            r"Complete:\s*[\d.]+%|Current block:|records (in|out)|copied,|^\s*$|"
            r"^\*+$|TERM as linux|color|^\[[0-9;]+m?")
        cands = [ln.strip()[:110] for ln in text.split("\n") if ln.strip() and not noise_re.search(ln)]
        if cands:
            phase = cands[-1]
    except Exception:
        pass
    if not running:
        # 終了時：正常終了なら 100%、異常なら最終推定値で止める
        if (rc == 0 or (rc is None and done_ops >= total_ops)) and not failed:
            overall = 100.0
        else:
            overall = round(min(99.9, (done_ops + cur_frac) / total_ops * 100.0), 1)
    else:
        overall = round(min(99.9, (done_ops + cur_frac) / total_ops * 100.0), 1)
    # 状態判定（フロント表示用）
    if running:
        status = "running"
    elif failed:
        status = "error"
    elif rc == 0 or (rc is None and done_ops >= total_ops and overall >= 99.9):
        status = "done"
    elif rc is None:
        status = "unknown"
    else:
        status = "error"
    elapsed = int(time.time() - job.get("started_at", time.time()))
    eta_text = "残り時間: 計算中"
    if running and overall >= 1.0:
        try:
            eta_text = _fmt_eta(elapsed * (100.0 - overall) / overall)
        except Exception:
            pass
    if status == "done":
        eta_text = "完了"
    return {"running": running, "returncode": rc, "failed": failed,
        "status": status, "adopted": adopted,
        "job": job,
        "overall_percent": overall, "current_percent": round(cur_frac * 100.0, 1),
        "done_ops": done_ops, "total_ops": total_ops,
        "current_device": cur_dev, "op_remaining": op_remaining,
        "rate": rate_text, "phase": phase,
        "eta_text": eta_text, "elapsed_sec": elapsed,
        "parts": job.get("parts", []), "log_file": job.get("log_file", "")}


# ---- レスキュー（ddrescue）進捗表示用ヘルパー ----
# クローン／消去ページと同じ方式：ログ末尾のパース＋ジョブ情報の返却
rescue_job = None
rescue_lock = threading.Lock()
RESCUE_JOB_FILE = os.path.join(LOG_DIR, ".rescue_job.json")


def _save_rescue_job(job):
    """レスキュージョブ情報をメモリ＋ファイルに保存する"""
    global rescue_job
    with rescue_lock:
        rescue_job = job
    try:
        with open(RESCUE_JOB_FILE, "w") as f:
            json.dump(job, f)
    except Exception:
        pass


def _load_rescue_job():
    """メモリ優先、無ければファイルからレスキュージョブ情報を復元する"""
    with rescue_lock:
        if rescue_job is not None:
            return dict(rescue_job)
    try:
        if os.path.exists(RESCUE_JOB_FILE):
            with open(RESCUE_JOB_FILE, "r") as f:
                job = json.load(f)
            if isinstance(job, dict) and job.get("log_path"):
                return job
    except Exception:
        pass
    return None


def _pid_is_rescue(pid):
    """指定 pid が ddrescue プロセスか確認する。PID 再利用の誤認防止用"""
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except Exception:
        return False
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            cmd = f.read().decode(errors="replace").lower()
        return "ddrescue" in cmd
    except Exception:
        # cmdline が読めない＝権限等の例外時は生存のみで判断する
        return True


def get_rescue_progress():
    """実行中レスキューの進捗をログ解析で推定する。ddrescue の出力形式を利用（参考値）"""
    job = _load_rescue_job()
    adopted = False
    if running_process is not None and running_process.poll() is None:
        # 自管理プロセスが別ジョブ（クローン等）の場合は実行中としない
        if job and job.get("pid") == running_process.pid:
            running, rc = True, None
        else:
            running, rc = False, None
    elif job and job.get("pid") and _pid_is_rescue(job.get("pid")):
        # サービス再起動後に取り残されたプロセスを引き継いで追跡する
        running, rc, adopted = True, None, True
    else:
        running = False
        rc = None if running_process is None else running_process.poll()
    if not job:
        return {"running": running, "job": None}
    log_file = job.get("log_path", "")
    text = ""
    try:
        if log_file and os.path.exists(log_file):
            with open(log_file, "r", errors="replace") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 131072))
                text = f.read()
    except Exception:
        pass
    # 制御文字・ANSIエスケープを除去し、\r を改行扱いにする
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).replace("\r", "\n")
    # 救出率（%）。直近の値を採用
    pcts = re.findall(r"pct rescued:\s*([\d.]+)\s*%", text)
    percent = 0.0
    if pcts:
        try:
            percent = min(100.0, max(0.0, float(pcts[-1])))
        except Exception:
            percent = 0.0
    # 救出量・速度・残り時間等の参考情報（直近の値を採用）
    rescued = re.findall(r"^\s*rescued:\s*([0-9.]+\s*[KMGT]?B)", text, re.M)
    rescued_text = rescued[-1].strip() if rescued else ""
    cur_rates = re.findall(r"current rate:\s*([0-9.]+\s*\S+/s)", text)
    cur_rate = cur_rates[-1].strip() if cur_rates else ""
    avg_rates = re.findall(r"average rate:\s*([0-9.]+\s*\S+/s)", text)
    avg_rate = avg_rates[-1].strip() if avg_rates else ""
    rems = re.findall(r"remaining time:\s*([^\s,]+)", text)
    remaining = rems[-1].strip() if rems else ""
    runs = re.findall(r"run time:\s*([^\s,]+)", text)
    run_time = runs[-1].strip() if runs else ""
    errs = re.findall(r"read errors:\s*(\d+)", text)
    read_errors = errs[-1] if errs else "0"
    bads = re.findall(r"bad areas:\s*(\d+)", text)
    bad_areas = bads[-1] if bads else "0"
    # 現在のフェーズ（序盤の無出力対策で直近の作業行を抜粋）
    phases = re.findall(
        r"((?:Copying|Scraping|Trimming|Retrying|Filling|Generating|Verifying)[^\n]*|Finished[^\n]*)",
        text)
    phase = phases[-1].strip()[:110] if phases else ""
    # 状態判定（フロント表示用。中断は SIGTERM/SIGKILL 系の負の終了コードで区別）
    if running:
        status = "running"
    elif rc == 0:
        status = "done"
        percent = 100.0
    elif rc is not None and rc < 0:
        status = "stopped"
    elif rc is None:
        status = "unknown"
    else:
        status = "error"
    elapsed = int(time.time() - job.get("started_at", time.time()))
    return {"running": running, "returncode": rc,
        "status": status, "adopted": adopted,
        "job": job,
        "percent": round(percent, 2),
        "rescued": rescued_text, "current_rate": cur_rate,
        "average_rate": avg_rate, "remaining": remaining,
        "run_time": run_time, "read_errors": read_errors,
        "bad_areas": bad_areas, "phase": phase,
        "elapsed_sec": elapsed, "log_file": job.get("log_file", "")}


# ---- rsync ファイルコピー用ヘルパー ----
# 物理パーティションまたは ddrescue の .img イメージからマウントし、
# 選択したフォルダ・ファイルを rsync でコピーする
rsync_job = None
rsync_lock = threading.Lock()
RSYNC_JOB_FILE = os.path.join(LOG_DIR, ".rsync_job.json")
RSYNC_MOUNT_FILE = os.path.join(LOG_DIR, ".rsync_mounts.json")
RSYNC_MOUNT_BASE = "/mnt/diskmanager-rsync"
# マウント対象として許可するデバイス名（ホールディスク＋パーティション。loop/dm等は不可）
RSYNC_DEV_RE = re.compile(r"^/dev/(sd[a-z]+\d*|hd[a-z]+\d*|vd[a-z]+\d*|nvme\d+n\d+p?\d*|mmcblk\d+p?\d*)$")
rsync_install_running = False
rsync_install_lock = threading.Lock()
rsync_mounts = {"src": None, "dst": None}
rsync_mounts_lock = threading.Lock()


def _save_rsync_job(job):
    """rsyncジョブ情報をメモリ＋ファイルに保存する"""
    global rsync_job
    with rsync_lock:
        rsync_job = job
    try:
        with open(RSYNC_JOB_FILE, "w") as f:
            json.dump(job, f)
    except Exception:
        pass


def _load_rsync_job():
    """メモリ優先、無ければファイルからrsyncジョブ情報を復元する"""
    with rsync_lock:
        if rsync_job is not None:
            return dict(rsync_job)
    try:
        if os.path.exists(RSYNC_JOB_FILE):
            with open(RSYNC_JOB_FILE, "r") as f:
                job = json.load(f)
            if isinstance(job, dict) and job.get("log_path"):
                return job
    except Exception:
        pass
    return None


def _pid_is_rsync(pid):
    """指定 pid が rsync 関係プロセスか確認する。PID 再利用の誤認防止用"""
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except Exception:
        return False
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            cmd = f.read().decode(errors="replace").lower()
        return ("rsync" in cmd) or ("stdbuf" in cmd)
    except Exception:
        return True


def _load_rsync_mounts():
    """マウント追跡情報をメモリ優先、無ければファイルから復元する"""
    with rsync_mounts_lock:
        if rsync_mounts.get("src") is not None or rsync_mounts.get("dst") is not None:
            return {"src": rsync_mounts.get("src"), "dst": rsync_mounts.get("dst")}
    try:
        if os.path.exists(RSYNC_MOUNT_FILE):
            with open(RSYNC_MOUNT_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {"src": data.get("src"), "dst": data.get("dst")}
    except Exception:
        pass
    return {"src": None, "dst": None}


def _save_rsync_mounts(data):
    """マウント追跡情報をメモリ＋ファイルに保存する"""
    with rsync_mounts_lock:
        rsync_mounts["src"] = data.get("src")
        rsync_mounts["dst"] = data.get("dst")
    try:
        with open(RSYNC_MOUNT_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _rsync_side_dir(side):
    """自前マウント用のベースディレクトリ"""
    suffix = "src" if side == "src" else "dst"
    return os.path.join(RSYNC_MOUNT_BASE, suffix)


def _rsync_img_dir(side):
    """イメージ用マウントのベースディレクトリ（パーティション毎に pN を作る）"""
    suffix = "src" if side == "src" else "dst"
    return os.path.join(RSYNC_MOUNT_BASE, suffix + "-img")


def get_rsync_partitions():
    """rsyncページ用：マウント可能なパーティション（＋単一FSのホールディスク）一覧"""
    out = []
    sys_disk = get_system_disk()
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL,TRAN,LABEL,UUID"],
            capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return {"system_disk": sys_disk, "partitions": []}
        data = json.loads(r.stdout or "{}")
        disks = data.get("blockdevices", [])

        def fmt_size(n):
            try:
                n = int(n or 0)
            except Exception:
                return ""
            if n >= 1024**3:
                return f"{n / (1024**3):.1f} GB"
            if n >= 1024**2:
                return f"{n / (1024**2):.1f} MB"
            return f"{n} B"

        for dev in disks:
            dname = dev.get("name", "")
            if not RSYNC_DEV_RE.match(f"/dev/{dname}") or dev.get("type") != "disk":
                # 先頭が実ディスクでないもの（zram/loop/dm等）は除外
                if dev.get("type") != "disk":
                    continue
                if not WIPE_PATH_RE.match(f"/dev/{dname}"):
                    continue
            dmodel = (dev.get("model") or "").strip()
            dserial = (dev.get("serial") or "").strip()
            dtran = (dev.get("tran") or "").strip()
            disk_label = f"/dev/{dname}"
            if dmodel:
                disk_label += f" ({dmodel})"
            children = dev.get("children") or []
            # ホールディスク自体にFSがある場合（単一FS媒体）も候補にする
            if (dev.get("fstype") or "").strip() and RSYNC_DEV_RE.match(f"/dev/{dname}"):
                fstype = (dev.get("fstype") or "").strip()
                mp = dev.get("mountpoint") or ""
                label = f"/dev/{dname} - {fmt_size(dev.get('size', 0))} [{fstype}]"
                if dmodel:
                    label += f" ({dmodel})"
                if mp:
                    label += f" mounted:{mp}"
                out.append({"path": f"/dev/{dname}", "disk": f"/dev/{dname}",
                    "disk_label": disk_label, "size": fmt_size(dev.get("size", 0)),
                    "size_bytes": int(dev.get("size", 0) or 0), "fstype": fstype,
                    "mountpoint": mp, "label": label, "model": dmodel,
                    "serial": dserial, "tran": dtran,
                    "is_system": (f"/dev/{dname}" == sys_disk),
                    "part_label": "", "part_uuid": (dev.get("uuid") or "").strip()})
            for ch in children:
                cname = ch.get("name", "")
                cpath = f"/dev/{cname}"
                if not RSYNC_DEV_RE.match(cpath):
                    continue
                fstype = (ch.get("fstype") or "").strip()
                mp = ch.get("mountpoint") or ""
                plabel = (ch.get("label") or "").strip()
                puuid = (ch.get("uuid") or "").strip()
                label = f"{cpath} - {fmt_size(ch.get('size', 0))}"
                if fstype:
                    label += f" [{fstype}]"
                if plabel:
                    label += f" \"{plabel}\""
                label += f" ({disk_label})"
                if mp:
                    label += f" mounted:{mp}"
                out.append({"path": cpath, "disk": f"/dev/{dname}",
                    "disk_label": disk_label, "size": fmt_size(ch.get("size", 0)),
                    "size_bytes": int(ch.get("size", 0) or 0), "fstype": fstype,
                    "mountpoint": mp, "label": label, "model": dmodel,
                    "serial": dserial, "tran": dtran,
                    "is_system": (f"/dev/{dname}" == sys_disk),
                    "part_label": plabel, "part_uuid": puuid})
    except Exception:
        pass
    return {"system_disk": sys_disk, "partitions": out}


def get_dev_mountpoint(path):
    """指定デバイスの現在のマウントポイント（無ければ空文字）"""
    try:
        r = subprocess.run(["lsblk", "-n", "-o", "MOUNTPOINT", path],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            for line in (r.stdout or "").split("\n"):
                if line.strip():
                    return line.strip()
    except Exception:
        pass
    return ""


def _get_blk_fstype(path):
    """blkid でファイルシステム種別を取得する（取得失敗時は空文字）"""
    try:
        r = subprocess.run(["blkid", "-o", "value", "-s", "TYPE", path],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return ((r.stdout or "").strip().split("\n")[0] or "").strip()
    except Exception:
        pass
    return ""


def _is_ntfs_dirty(path):
    """NTFSダーティ（要chkdsk）かを判定する。理由文も返す"""
    short = os.path.basename((path or "").strip())
    # 直近の dmesg に dirty + force の記録があるか確認する
    try:
        r = subprocess.run(["dmesg", "--ctime"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            tail = (r.stdout or "")[-8192:].lower()
            if ("dirty" in tail and "force" in tail) or "scheduled for check" in tail:
                # デバイス名が含まれていれば確度が高い。含まれなくても NTFS なら疑う
                if not short or short.lower() in tail or "ntfs" in tail:
                    return True, "dmesg に NTFSダーティ（volume is dirty）の記録があります"
    except Exception:
        pass
    # ntfsresize --info はダーティ時に「Volume is scheduled for check」で失敗する
    try:
        r = subprocess.run(["ntfsresize", "--info", path],
            capture_output=True, text=True, timeout=30)
        out = ((r.stdout or "") + (r.stderr or "")).lower()
        if "scheduled for check" in out or "volume is dirty" in out:
            return True, "NTFSボリュームはチェック待ち（要 chkdsk）の状態です"
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return False, ""


def mount_rsync_device(side, path, force=False):
    """パーティション等をマウント（済みなら再利用）し、マウントポイントを返す"""
    if side not in ("src", "dst"):
        return {"ok": False, "error": "side が不正です"}
    path = (path or "").strip()
    if not RSYNC_DEV_RE.match(path):
        return {"ok": False, "error": f"不正なデバイス指定です: {path}"}
    if not os.path.exists(path):
        return {"ok": False, "error": f"デバイスが見つかりません: {path}"}
    sys_disk = get_system_disk()
    if side == "dst" and sys_disk:
        try:
            r = subprocess.run(["lsblk", "-n", "-o", "PKNAME", path],
                capture_output=True, text=True, timeout=5)
            parent = (r.stdout or "").strip().split("\n")[0].strip()
            if path == sys_disk or (parent and f"/dev/{parent}" == sys_disk):
                return {"ok": False, "error": f"{path} はシステムドライブのためコピー先に指定できません"}
        except Exception:
            pass
    mp = get_dev_mountpoint(path)
    mounts = _load_rsync_mounts()
    if mp:
        info = {"kind": "device", "dev": path, "mountpoint": mp,
            "own_mount": False, "mounted_at": time.time()}
        mounts[side] = info
        _save_rsync_mounts(mounts)
        return {"ok": True, "mountpoint": mp, "already_mounted": True, "info": info}
    # 未マウント → 自前でマウントする
    base = _rsync_side_dir(side)
    try:
        os.makedirs(base, exist_ok=True)
    except Exception as e:
        return {"ok": False, "error": f"マウント先を作成できません: {e}"}
    # 他デバイスで使用中の場合は解除してから使う
    try:
        r = subprocess.run(["mountpoint", "-q", base])
        if r.returncode == 0:
            subprocess.run(["umount", base], capture_output=True, timeout=30)
    except Exception:
        pass
    if side == "src":
        cmd = ["mount", "-o", "ro", path, base]
    else:
        cmd = ["mount", path, base]
    # 強制マウント指定時は NTFS のみ対象とし、ntfsfix でダーティをクリアしてから force 付きで載せる
    ntfsfix_out = ""
    if force:
        fstype = _get_blk_fstype(path).lower()
        if "ntfs" not in fstype:
            return {"ok": False, "error": f"強制マウントは NTFS のみ対応です（現在: {fstype or '不明'}）"}
        try:
            rf = subprocess.run(["ntfsfix", path],
                capture_output=True, text=True, timeout=60)
            ntfsfix_out = ((rf.stdout or "") + (rf.stderr or "")).strip()[:500]
        except FileNotFoundError:
            return {"ok": False, "error": "ntfsfix が見つかりません（ntfs-3g を導入してください）"}
        except Exception as e:
            return {"ok": False, "error": f"ntfsfix 実行エラー: {e}"}
        # コピー元は読み取り専用を維持し、コピー先は書き込み可で載せる
        if side == "src":
            cmd = ["mount", "-o", "ro,force", path, base]
        else:
            cmd = ["mount", "-o", "rw,force", path, base]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:
        return {"ok": False, "error": f"マウント実行エラー: {e}"}
    if r.returncode != 0:
        err = ((r.stderr or r.stdout) or "").strip().split("\n")[0][:300]
        # NTFSダーティ時は強制マウントの選択肢を返す（フロントで確認表示用）
        if not force:
            fstype = _get_blk_fstype(path).lower()
            if "ntfs" in fstype:
                dirty, reason = _is_ntfs_dirty(path)
                if dirty or ("ntfs" in err.lower() or "dirty" in err.lower()):
                    detail = reason or "NTFSボリュームがダーティ（要 chkdsk）の可能性があります"
                    mode_note = "コピー元は読み取り専用を維持" if side == "src" else "コピー先は書き込み可"
                    return {"ok": False, "error": f"マウント失敗: {err}",
                        "need_force": True, "fstype": "ntfs", "detail": detail,
                        "force_note": f"ntfsfix でダーティフラグをクリアして強制マウントできます（{mode_note}）。本来は Windows で chkdsk /f が推奨です"}
        # コピー元 ro 失敗時は NTFS のダーティ等が多いためヒント付きで返す
        hint = ""
        if side == "src" and ("ntfs" in err.lower() or "dirty" in err.lower() or "windows" in err.lower()):
            hint = "（NTFS が異常終了している可能性があります。Windows で正常に終了したディスクをご利用ください）"
        return {"ok": False, "error": f"マウント失敗: {err}{hint}"}
    info = {"kind": "device", "dev": path, "mountpoint": base,
        "own_mount": True, "mounted_at": time.time()}
    mounts[side] = info
    _save_rsync_mounts(mounts)
    res = {"ok": True, "mountpoint": base, "already_mounted": False, "info": info}
    if force:
        res["forced"] = True
        if ntfsfix_out:
            res["ntfsfix"] = ntfsfix_out
    return res


def _losetup_detach(loopdev):
    try:
        subprocess.run(["losetup", "-d", loopdev],
            capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def mount_rsync_image(side, image):
    """ddrescue等の .img イメージファイルを loop＋kpartx相当でマウントする（コピー元専用）"""
    if side != "src":
        return {"ok": False, "error": "イメージファイルはコピー元のみ指定できます"}
    image = (image or "").strip()
    if not image.startswith("/"):
        return {"ok": False, "error": "イメージは絶対パスで指定してください"}
    if not os.path.isfile(image):
        return {"ok": False, "error": f"イメージファイルが見つかりません: {image}"}
    try:
        if os.path.getsize(image) <= 0:
            return {"ok": False, "error": "イメージファイルのサイズが 0 です"}
    except Exception as e:
        return {"ok": False, "error": f"イメージファイルを確認できません: {e}"}
    mounts = _load_rsync_mounts()
    prev = mounts.get("src") or {}
    # 同一イメージの再マウント要求で loop が生存していれば再利用する
    if prev.get("kind") == "image" and prev.get("image") == image and prev.get("loop"):
        loopdev = prev.get("loop")
        alive = os.path.exists(loopdev)
        ok_mounts = [m for m in (prev.get("mounts") or []) if os.path.ismount(m.get("mountpoint", ""))]
        if alive and ok_mounts:
            return {"ok": True, "mountpoint": ok_mounts[0]["mountpoint"],
                "already_mounted": True, "loop": loopdev, "mounts": ok_mounts,
                "info": prev}
    # 前回の自前マウントを掃除する（他人のマウントは触らない）
    unmount_rsync_side("src")
    mounts = _load_rsync_mounts()
    try:
        r = subprocess.run(["losetup", "-f", "--show", "-P", image],
            capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return {"ok": False, "error": "losetup が見つかりません（util-linux を導入してください）"}
    except Exception as e:
        return {"ok": False, "error": f"loop デバイスの確保に失敗: {e}"}
    if r.returncode != 0:
        err = ((r.stderr or r.stdout) or "").strip().split("\n")[0][:300]
        return {"ok": False, "error": f"loop デバイスの確保に失敗: {err}"}
    loopdev = (r.stdout or "").strip().split("\n")[0].strip()
    if not loopdev.startswith("/dev/loop"):
        return {"ok": False, "error": f"loop デバイスの取得に失敗しました: {loopdev}"}
    try:
        subprocess.run(["udevadm", "settle"], capture_output=True, timeout=15)
    except Exception:
        pass
    time.sleep(1)
    # パーティション構成を調べる
    cands = []
    try:
        r2 = subprocess.run(["lsblk", "-J", "-o", "NAME,TYPE,FSTYPE,SIZE", loopdev],
            capture_output=True, text=True, timeout=10)
        if r2.returncode == 0:
            data = json.loads(r2.stdout or "{}")
            devs = data.get("blockdevices", [])
            if devs:
                top = devs[0]
                for ch in (top.get("children") or []):
                    cands.append(f"/dev/{ch.get('name', '')}")
                if not cands and (top.get("fstype") or "").strip():
                    cands.append(loopdev)
    except Exception:
        pass
    if not cands:
        # lsblk で取れない場合は単一FSとして直接マウントを試す
        cands = [loopdev]
    base = _rsync_img_dir("src")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception as e:
        _losetup_detach(loopdev)
        return {"ok": False, "error": f"マウント先を作成できません: {e}"}
    ok_mounts = []
    for idx, part in enumerate(cands):
        if not part or not os.path.exists(part):
            continue
        # パーティション毎のマウント先（単一の場合は base 直下ではなく p0 を使う）
        mp = os.path.join(base, f"p{idx}")
        try:
            os.makedirs(mp, exist_ok=True)
        except Exception:
            continue
        try:
            rr = subprocess.run(["mount", "-o", "ro", part, mp],
                capture_output=True, text=True, timeout=60)
        except Exception:
            continue
        if rr.returncode == 0:
            fstype = ""
            try:
                rb = subprocess.run(["blkid", "-o", "value", "-s", "TYPE", part],
                    capture_output=True, text=True, timeout=10)
                fstype = (rb.stdout or "").strip().split("\n")[0].strip()
            except Exception:
                pass
            ok_mounts.append({"dev": part, "mountpoint": mp, "fstype": fstype})
    if not ok_mounts:
        _losetup_detach(loopdev)
        return {"ok": False, "error": "イメージ内にマウント可能なファイルシステムが見つかりませんでした"
            "（パーティションテーブル破損・未対応FSの可能性があります）"}
    info = {"kind": "image", "image": image, "loop": loopdev,
        "mountpoint": ok_mounts[0]["mountpoint"], "mounts": ok_mounts,
        "own_mount": True, "mounted_at": time.time()}
    mounts["src"] = info
    _save_rsync_mounts(mounts)
    return {"ok": True, "mountpoint": ok_mounts[0]["mountpoint"],
        "already_mounted": False, "loop": loopdev, "mounts": ok_mounts,
        "info": info}


def unmount_rsync_side(side):
    """指定側の自前マウントを解除する（元からあるマウントは解除しない）"""
    if side not in ("src", "dst"):
        return {"ok": False, "error": "side が不正です"}
    mounts = _load_rsync_mounts()
    info = mounts.get(side)
    if not info:
        return {"ok": True, "message": "マウント情報がありません"}
    errors = []
    if (info.get("kind") == "image"):
        for m in (info.get("mounts") or []):
            mp = m.get("mountpoint", "")
            if mp and os.path.ismount(mp):
                try:
                    r = subprocess.run(["umount", mp], capture_output=True, text=True, timeout=30)
                    if r.returncode != 0:
                        errors.append(f"{mp}: {((r.stderr or r.stdout) or '').strip().split(chr(10))[0][:150]}")
                except Exception as e:
                    errors.append(f"{mp}: {e}")
        loopdev = info.get("loop", "")
        if loopdev and os.path.exists(loopdev):
            _losetup_detach(loopdev)
    else:
        if info.get("own_mount") and info.get("mountpoint"):
            mp = info["mountpoint"]
            if os.path.ismount(mp):
                try:
                    r = subprocess.run(["umount", mp], capture_output=True, text=True, timeout=30)
                    if r.returncode != 0:
                        errors.append(((r.stderr or r.stdout) or "").strip().split("\n")[0][:200])
                except Exception as e:
                    errors.append(str(e)[:200])
        # own_mount でない（元からあるマウントの再利用）は解除しない
    mounts[side] = None
    _save_rsync_mounts(mounts)
    if errors:
        return {"ok": False, "error": "; ".join(errors)}
    return {"ok": True, "message": "アンマウントしました"}


def _rsync_allowed_roots(side):
    """指定側でフォルダ指定を許可するルート（一覧・コピー時の脱出防止用）"""
    mounts = _load_rsync_mounts()
    info = mounts.get(side) or {}
    roots = []
    if info.get("kind") == "image":
        for m in (info.get("mounts") or []):
            mp = m.get("mountpoint", "")
            if mp:
                roots.append(os.path.realpath(mp))
        imgbase = os.path.realpath(_rsync_img_dir("src" if side == "src" else "dst"))
        roots.append(imgbase)
    elif info.get("mountpoint"):
        roots.append(os.path.realpath(info["mountpoint"]))
    # 実マウント点の再確認（追跡情報が古い場合の補正）
    if info.get("kind") == "device" and info.get("dev"):
        mp = get_dev_mountpoint(info["dev"])
        if mp and os.path.realpath(mp) not in roots:
            roots.append(os.path.realpath(mp))
    return [r for r in roots if r]


def _rsync_resolve_base(side, base, must_exist=True):
    """フォルダ指定を検証し、実パスを返す。NG時は (None, エラー文)"""
    base = (base or "").strip()
    if not base:
        return None, "フォルダを指定してください"
    if not base.startswith("/"):
        return None, "フォルダは絶対パスで指定してください"
    roots = _rsync_allowed_roots(side)
    if not roots:
        return None, "先にドライブをマウントしてください"
    real = os.path.realpath(base)
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        return None, f"マウント外のパスは指定できません（マウント点: {', '.join(roots)}）"
    if must_exist:
        if not os.path.exists(real):
            return None, f"フォルダが見つかりません: {base}"
        if not os.path.isdir(real):
            return None, f"フォルダではありません: {base}"
    return real, ""


def list_rsync_dir(side, base):
    """コピー元フォルダ直下のフォルダ・ファイル一覧を返す"""
    real, err = _rsync_resolve_base(side, base, must_exist=True)
    if err:
        return {"ok": False, "error": err}
    entries = []
    truncated = False
    try:
        names = sorted(os.listdir(real))
    except Exception as e:
        return {"ok": False, "error": f"一覧を取得できません: {e}"}
    for name in names:
        if len(entries) >= 2000:
            truncated = True
            break
        p = os.path.join(real, name)
        try:
            if os.path.islink(p):
                kind = "link"
            elif os.path.isdir(p):
                kind = "dir"
            elif os.path.isfile(p):
                kind = "file"
            else:
                kind = "other"
            st = os.lstat(p)
            entries.append({"name": name, "type": kind,
                "size": st.st_size, "size_h": _fmt_bytes(st.st_size),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
        except Exception:
            entries.append({"name": name, "type": "unknown",
                "size": 0, "size_h": "-", "mtime": ""})
    # フォルダ優先＋名前順
    entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))
    return {"ok": True, "base": real, "entries": entries, "truncated": truncated,
        "count": len(entries)}


def _fmt_bytes(n):
    try:
        n = int(n)
    except Exception:
        return "-"
    if n >= 1024**3:
        return f"{n / (1024**3):.2f} GB"
    if n >= 1024**2:
        return f"{n / (1024**2):.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _parse_rsync_size(s):
    """rsync --info=progress2（-hあり/なし）の転送量表記をバイト数に変換する（参考値）"""
    try:
        t = (s or "").strip().replace(",", "").upper()
        if not t:
            return 0
        mult = 1
        if t.endswith("B"):
            t = t[:-1].strip()
        if t and t[-1] in ("K", "M", "G", "T"):
            mult = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[t[-1]]
            t = t[:-1].strip()
        return int(float(t) * mult)
    except Exception:
        return 0


def _estimate_rsync_total(src_base, items, recursive=True):
    """コピー開始前に転送対象の合計バイト数・ファイル数を概算する（参考値）"""
    total_bytes = 0
    total_files = 0
    try:
        for name in items or []:
            full = os.path.join(src_base, name)
            try:
                if os.path.islink(full) or os.path.isfile(full):
                    try:
                        total_bytes += os.lstat(full).st_size
                    except Exception:
                        pass
                    total_files += 1
                elif os.path.isdir(full) and not os.path.islink(full):
                    if not recursive:
                        total_files += 1
                        continue
                    for root, _dirs, files in os.walk(full, followlinks=False):
                        for fn in files:
                            fp = os.path.join(root, fn)
                            try:
                                if os.path.islink(fp):
                                    total_files += 1
                                    continue
                                total_bytes += os.lstat(fp).st_size
                                total_files += 1
                            except Exception:
                                continue
                            # 巨大なツリーで開始が遅くならないよう上限を設ける
                            if total_files > 200000:
                                return total_bytes, total_files
                else:
                    total_files += 1
            except Exception:
                continue
    except Exception:
        pass
    return total_bytes, total_files


def get_rsync_status():
    """rsync コマンドの導入状態を返す"""
    has_rsync = shutil.which("rsync") is not None
    with rsync_install_lock:
        installing = rsync_install_running
    return {"installed": has_rsync, "installing": installing}


def _run_rsync_install():
    """rsync をバックグラウンドで導入する"""
    global rsync_install_running
    log_path = os.path.join(LOG_DIR, "rsync-install.log")
    try:
        with open(log_path, "w") as f:
            f.write(f"=== rsync install started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            f.flush()
            os_id, os_like = "", ""
            try:
                with open("/etc/os-release") as of:
                    for line in of:
                        if line.startswith("ID="):
                            os_id = line.split("=", 1)[1].strip().strip('"').lower()
                        elif line.startswith("ID_LIKE="):
                            os_like = line.split("=", 1)[1].strip().strip('"').lower()
            except Exception:
                pass
            is_arch = ("arch" in os_like) or os_id in ("arch", "cachyos") or \
                (shutil.which("pacman") and not shutil.which("apt-get"))
            if is_arch:
                cmd = ["pacman", "-Sy", "--noconfirm", "--needed", "rsync"]
            else:
                f.write("$ apt-get update\n")
                f.flush()
                r0 = subprocess.run(["apt-get", "update"], stdout=f, stderr=subprocess.STDOUT, timeout=600)
                if r0.returncode != 0:
                    f.write(f"apt-get update failed (code={r0.returncode})\n")
                cmd = ["apt-get", "install", "-y", "rsync"]
            f.write(f"$ {' '.join(cmd)}\n")
            f.flush()
            r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=3600)
            f.write(f"\n=== finished code={r.returncode} ===\n")
    except Exception as e:
        try:
            with open(log_path, "a") as f:
                f.write(f"install error: {e}\n")
        except Exception:
            pass
    finally:
        with rsync_install_lock:
            rsync_install_running = False


def get_rsync_progress():
    """実行中 rsync の進捗をログ解析＋事前概算で推定する（--info=progress2 の出力利用。参考値）"""
    job = _load_rsync_job()
    adopted = False
    if running_process is not None and running_process.poll() is None:
        if job and job.get("pid") == running_process.pid:
            running, rc = True, None
        else:
            running, rc = False, None
    elif job and job.get("pid") and _pid_is_rsync(job.get("pid")):
        running, rc, adopted = True, None, True
    else:
        running = False
        rc = None if running_process is None else running_process.poll()
    if not job:
        return {"running": running, "job": None}
    log_file = job.get("log_path", "")
    text = ""
    try:
        if log_file and os.path.exists(log_file):
            with open(log_file, "r", errors="replace") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 131072))
                text = f.read()
    except Exception:
        pass
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).replace("\r", "\n")
    # --info=progress2 の「5.24M  33%  621.09MB/s (xfr#1, to-chk=2/4)」形式から直近値を採用。
    # -h 付きのため転送量は「32.77K」「12,345」の両形式を取り得る
    prog_rows = re.findall(
        r"([0-9][\d,]*\.?\d*\s*[KMGT]?B?)\s+(\d{1,3})%\s+([\d.]+\s*[KMGT]?B/s)",
        text, re.I)
    percent = 0.0
    speed = ""
    transferred_str = ""
    transferred_bytes = 0
    if prog_rows:
        try:
            transferred_str = prog_rows[-1][0].strip()
            percent = min(100.0, max(0.0, float(prog_rows[-1][1])))
            speed = prog_rows[-1][2].strip()
            transferred_bytes = _parse_rsync_size(transferred_str)
        except Exception:
            percent = 0.0
    else:
        # 旧形式・桁区切りなし等のフォールバック（%単独行も拾う）
        bare = re.findall(r"(?:^|\n)\s*[0-9][\d,]*\.?\d*\s*[KMGT]?B?\s+(\d{1,3})%", text, re.I)
        if bare:
            try:
                percent = min(100.0, max(0.0, float(bare[-1])))
            except Exception:
                pass
    speeds = re.findall(r"(\d[\d.]*\s*[KMGT]?B/s)", text, re.I)
    if speeds:
        speed = speeds[-1].strip()
    xfrs = re.findall(r"xfr#(\d+)", text)
    xfr = xfrs[-1] if xfrs else ""
    tochs = re.findall(r"to-chk=(\d+)/(\d+)", text)
    toch = f"{tochs[-1][0]}/{tochs[-1][1]}" if tochs else ""
    # ファイル数ベースの参考進捗（to-chk=残り/全体。残りが0に近づくほど完了）
    file_percent = None
    try:
        if tochs:
            remain, whole = int(tochs[-1][0]), int(tochs[-1][1])
            if whole > 0 and 0 <= remain <= whole:
                file_percent = (whole - remain) / whole * 100.0
    except Exception:
        file_percent = None
    # 容量ベースの参考進捗（事前概算の合計に対する転送量の割合）
    bytes_percent = None
    total_bytes = int(job.get("total_bytes") or 0)
    try:
        if total_bytes > 0 and transferred_bytes > 0:
            bytes_percent = min(100.0, transferred_bytes / total_bytes * 100.0)
    except Exception:
        bytes_percent = None
    # 進捗率の決定：progress2 の % を優先し、未出力の序盤はファイル数・容量ベースで補完する。
    # progress2 の % は全体容量基準のため、0% のまま停滞しがちな序盤のみ補完する
    basis = "progress2"
    if percent <= 0:
        if file_percent is not None and file_percent > 0:
            percent = file_percent
            basis = "file-count"
        elif bytes_percent is not None and bytes_percent > 0:
            percent = bytes_percent
            basis = "bytes"
    failed = ("rsync error" in text) or ("rsync: " in text and "failed" in text.lower())
    # 現在のフェーズ（直近のファイル行。進捗行・空行は除外）
    phase = ""
    try:
        noise_re = re.compile(r"^\s*[\d,]+\s+\d{1,3}%|^\s*[0-9][\d,.]*\s*[KMGT]?B?\s+\d{1,3}%|xfr#|to-chk=|^sending|^sent |^total size|^receiving|^\s*$", re.I)
        cands = [ln.strip()[:110] for ln in text.split("\n")
            if ln.strip() and not noise_re.search(ln.strip())]
        if cands:
            phase = cands[-1]
    except Exception:
        pass
    if running:
        status = "running"
    elif rc == 0 and not failed:
        status = "done"
        percent = 100.0
    elif rc is not None and rc < 0:
        status = "stopped"
    elif rc is None:
        status = "unknown"
    else:
        status = "error"
    elapsed = int(time.time() - job.get("started_at", time.time()))
    # 残り時間の目安（参考値）。進捗1%以上で経過時間から線形推定する
    eta_text = "残り時間: 計算中"
    try:
        if running and percent >= 1.0 and percent < 100.0:
            eta_text = _fmt_eta(elapsed * (100.0 - percent) / percent)
        elif running and basis == "file-count" and percent >= 1.0:
            eta_text = _fmt_eta(elapsed * (100.0 - percent) / percent) + "（ファイル数から推定）"
    except Exception:
        pass
    if status == "done":
        eta_text = "完了"
    total_h = _fmt_bytes(total_bytes) if total_bytes > 0 else ""
    transferred_h = _fmt_bytes(transferred_bytes) if transferred_bytes > 0 else (transferred_str or "")
    return {"running": running, "returncode": rc,
        "status": status, "adopted": adopted,
        "job": job,
        "percent": round(percent, 1),
        "percent_basis": basis,
        "speed": speed, "transferred_files": xfr, "to_check": toch,
        "transferred_bytes": transferred_bytes, "transferred_h": transferred_h,
        "total_bytes": total_bytes, "total_h": total_h,
        "total_files": int(job.get("total_files") or 0),
        "phase": phase, "failed": failed,
        "eta_text": eta_text,
        "elapsed_sec": elapsed, "log_file": job.get("log_file", "")}


# ---- パーティション操作用ヘルパー ----
# KDEパーティションマネージャ相当の表示＋拡縮小/作成/削除を提供する。
# parted を中核に使い、ファイルシステム側のリサイズは対応FSのみ行う。
PART_DEV_RE = re.compile(r"^/dev/(sd[a-z]+\d*|hd[a-z]+\d*|vd[a-z]+\d*|nvme\d+n\d+p?\d*|mmcblk\d+p?\d*)$")
part_install_running = False
part_install_lock = threading.Lock()
MIB = 1024 * 1024


def get_part_tools():
    """パーティション操作に必要なツールの導入状態を返す"""
    tools = {}
    for name in ("parted", "sfdisk", "wipefs", "partprobe",
                 "e2fsck", "resize2fs", "ntfsresize", "fatresize",
                 "xfs_growfs", "btrfs",
                 "mkfs.ext4", "mkfs.vfat", "mkfs.ntfs", "mkfs.xfs",
                 "mkfs.exfat", "mkfs.btrfs"):
        tools[name] = shutil.which(name) is not None
    with part_install_lock:
        installing = part_install_running
    # 必須は parted のみ。FS系は対応FSを使う場合に個別チェックする
    return {"installed": bool(tools.get("parted")), "installing": installing,
        "tools": tools, "system_disk": get_system_disk()}


def _run_cmd(cmd, timeout=120, input_text=None):
    """コマンドを実行し (rc, 出力) を返す。input_text 指定時は標準入力に渡す"""
    try:
        r = subprocess.run(cmd, input=input_text,
            capture_output=True, text=True, timeout=timeout)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except FileNotFoundError:
        return 127, f"コマンドが見つかりません: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "コマンドがタイムアウトしました"


def _parted_resizepart(disk, num, end_bytes):
    """parted resizepart を実行する。縮小時は確認プロンプト
    （「それでも実行しますか？」）が -s 指定でも出て失敗するため、
    ---pretend-input-tty＋Yes応答で無人化する"""
    cur_end = None
    try:
        _, bounds, _ = _parted_parse_free(disk)
        if num in bounds:
            cur_end = bounds[num][1]
    except Exception:
        pass
    end_s = f"{int(end_bytes)}B"
    if cur_end is not None and int(end_bytes) < cur_end:
        # 縮小：プロンプトに Yes を自動応答させる（有限回。不足時は EOF で安全に中断）
        return _run_cmd(["parted", "---pretend-input-tty", disk,
            "resizepart", str(num), end_s],
            timeout=300, input_text="Yes\n" * 8)
    return _run_cmd(["parted", "-s", disk, "resizepart", str(num), end_s],
        timeout=300)


def _fs_volume_size(part, fstype):
    """ファイルシステム自体の現在のサイズ（バイト）を返す。不明時はNone"""
    fstype = (fstype or "").lower()
    if fstype in ("ext4", "ext3", "ext2"):
        if shutil.which("dumpe2fs") is None:
            return None
        rc, out = _run_cmd(["dumpe2fs", "-h", part], timeout=60)
        if rc != 0:
            return None
        blocks = bsize = None
        for line in out.split("\n"):
            s = line.strip()
            if s.startswith("Block count:"):
                try:
                    blocks = int(s.split(":")[1].strip().split()[0])
                except Exception:
                    pass
            elif s.startswith("Block size:"):
                try:
                    bsize = int(s.split(":")[1].strip().split()[0])
                except Exception:
                    pass
        if blocks and bsize:
            return blocks * bsize
        return None
    if fstype == "ntfs":
        if shutil.which("ntfsresize") is None:
            return None
        rc, out = _run_cmd(["ntfsresize", "-f", "--info", part], timeout=300)
        if rc != 0:
            return None
        m = re.search(r"Current volume size:\s*(\d+)\s*bytes", out)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return None
        return None
    return None


def _part_parent_disk(part):
    """パーティション → 親ディスク名（/dev/xxx）を返す。失敗時は空文字"""
    try:
        r = subprocess.run(["lsblk", "-n", "-o", "PKNAME", part],
            capture_output=True, text=True, timeout=5)
        parent = (r.stdout or "").strip().split("\n")[0].strip()
        if parent:
            return f"/dev/{parent}"
    except Exception:
        pass
    return ""


def _part_number(disk, part):
    """パーティション番号を返す（/sys優先、parted表示で補完）。失敗時はNone"""
    disk_base = os.path.basename(disk)
    part_base = os.path.basename(part)
    try:
        with open(f"/sys/block/{disk_base}/{part_base}/partition", "r") as f:
            return int(f.read().strip())
    except Exception:
        pass
    # フォールバック：nvme0n1p2 / mmcblk0p1 / sda12 の末尾数字
    try:
        base = os.path.basename(disk)
        suffix = part_base[len(base):] if part_base.startswith(base) else part_base
        suffix = suffix.lstrip("p")
        if suffix.isdigit():
            return int(suffix)
    except Exception:
        pass
    return None


def _parted_parse_free(disk):
    """parted の print free を解析し (テーブル種別, パーティション番号→(start,end), 空き一覧) を返す
    パーティションテーブルが無い場合（未初期化）は table に "unknown" を返す"""
    table = ""
    bounds = {}
    frees = []
    rc, out = _run_cmd(["parted", "-s", disk, "unit", "B", "print", "free"], timeout=30)
    if rc != 0:
        # 未初期化ディスクは parted が非ゼロ終了＋「ディスクラベルが認識できません」等を出す
        low = (out or "").lower()
        if ("unknown" in low or "認識できません" in (out or "")
                or "unrecognised" in low or "unrecognized" in low):
            return "unknown", bounds, frees
        return table, bounds, frees
    for line in out.split("\n"):
        s = line.strip()
        low = s.lower()
        # パーティションテーブル種別（日英両対応）
        if "パーティションテーブル" in s or "partition table" in low:
            if "gpt" in low:
                table = "gpt"
            elif "msdos" in low or "mbr" in low:
                table = "msdos"
            else:
                table = s.split(":")[-1].strip()
            continue
        # 空き領域行（番号なし・先頭がバイト数）
        m_free = re.match(r"^(\d+)B\s+(\d+)B\s+(\d+)B\s+.*(?:空き|free)", s, re.I)
        if m_free and not re.match(r"^\d+\s", s):
            try:
                frees.append({"start_bytes": int(m_free.group(1)),
                    "end_bytes": int(m_free.group(2)),
                    "size_bytes": int(m_free.group(3))})
            except Exception:
                pass
            continue
        # パーティション行（先頭が番号）
        m_part = re.match(r"^(\d+)\s+(\d+)B\s+(\d+)B\s+(\d+)B", s)
        if m_part:
            try:
                bounds[int(m_part.group(1))] = (int(m_part.group(2)), int(m_part.group(3)))
            except Exception:
                pass
    return table, bounds, frees


def get_part_devices():
    """パーティション操作ページ用：ディスク＋パーティション（使用量）＋空き領域の一覧"""
    devices = []
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,LABEL,UUID,PARTLABEL,PARTTYPE,PARTUUID,"
             "FSUSED,FSAVAIL,FSUSE%,MODEL,SERIAL,TRAN,PARTTYPE"],
            capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        data = json.loads(r.stdout or "{}")
    except Exception:
        return []
    sys_disk = get_system_disk()
    for dev in data.get("blockdevices", []) or []:
        if dev.get("type") != "disk":
            continue
        name = dev.get("name", "")
        disk = f"/dev/{name}"
        if not WIPE_PATH_RE.match(disk):
            continue
        size_bytes = int(dev.get("size", 0) or 0)
        model = (dev.get("model") or "").strip()
        serial = (dev.get("serial") or "").strip()
        tran = (dev.get("tran") or "").strip()
        table, bounds, frees = _parted_parse_free(disk)
        partitions = []
        has_mount = bool(dev.get("mountpoint"))
        for child in dev.get("children") or []:
            if (child.get("type") or "") not in ("part", "raid", "lvm"):
                # ディスク直下の part 以外（暗号化マッパー等）は表示のみ対象外
                if (child.get("type") or "") != "part":
                    continue
            cname = child.get("name", "")
            cpath = f"/dev/{cname}"
            if not PART_DEV_RE.match(cpath):
                continue
            csize = int(child.get("size", 0) or 0)
            cmount = child.get("mountpoint") or ""
            if cmount:
                has_mount = True
            try:
                used = int(child.get("fsused", 0) or 0)
            except Exception:
                used = 0
            try:
                avail = int(child.get("fsavail", 0) or 0)
            except Exception:
                avail = 0
            use_pct = (child.get("fsuse%") or "").strip()
            num = _part_number(disk, cpath)
            start_b, end_b = bounds.get(num, (0, 0)) if num else (0, 0)
            partitions.append({
                "name": cname, "path": cpath, "number": num,
                "start_bytes": start_b, "end_bytes": end_b,
                "size_bytes": csize, "size": _fmt_bytes(csize),
                "fstype": (child.get("fstype") or "").strip(),
                "label": (child.get("label") or "").strip(),
                "uuid": (child.get("uuid") or "").strip(),
                "partlabel": (child.get("partlabel") or "").strip(),
                "parttype": (child.get("parttype") or "").strip(),
                "mountpoint": cmount,
                "used_bytes": used, "used": _fmt_bytes(used) if used else "",
                "avail_bytes": avail, "avail": _fmt_bytes(avail) if avail else "",
                "use_percent": use_pct,
            })
        # 空き領域に人間可読サイズを付与（1MiB未満の微小ギャップは表示対象外）
        free_list = []
        for f in frees:
            if int(f.get("size_bytes", 0) or 0) < MIB:
                continue
            free_list.append({**f, "size": _fmt_bytes(f["size_bytes"])})
        # パーティションテーブルが無い未初期化ディスク（/dev/sdc等の空ドライブ）は
        # 初期化→作成フローで扱えるようフラグを立てる
        needs_init = (table in ("", "unknown")) and not partitions and not free_list
        label = f"/dev/{name} - {_fmt_bytes(size_bytes)}"
        if model:
            label += f" ({model})"
        if serial:
            label += f" [{serial}]"
        if tran:
            label += f" ({tran})"
        devices.append({"path": disk, "name": name,
            "size": _fmt_bytes(size_bytes), "size_bytes": size_bytes,
            "model": model, "serial": serial, "tran": tran, "label": label,
            "table": table or "不明",
            "needs_init": needs_init,
            "partitions": partitions, "free_spaces": free_list,
            "has_mount": has_mount, "is_system": (disk == sys_disk)})
    return devices


def _part_refresh(disk):
    """パーティション変更後のカーネル再読み込み"""
    _run_cmd(["partprobe", disk], timeout=60)
    _run_cmd(["udevadm", "settle"], timeout=30)
    time.sleep(1)


def _part_guard(path, for_create_disk=False):
    """共通ガード：形式・存在・システムドライブ・マウントを検証。NG時はエラー文、OK時は participles(disk)"""
    disk = path if for_create_disk else _part_parent_disk(path)
    if not disk:
        # ディスク指定（作成時）または親解決失敗
        if for_create_disk:
            return None, f"デバイスが見つかりません: {path}"
        return None, f"親ディスクを特定できません: {path}"
    if not WIPE_PATH_RE.match(disk):
        return None, f"不正なデバイス指定です: {path}"
    if not os.path.exists(path if not for_create_disk else disk):
        return None, f"デバイスが見つかりません: {path}"
    if disk == get_system_disk():
        return None, f"{disk} はシステムドライブのため操作できません"
    if not shutil.which("parted"):
        return None, "parted が利用できません（先にツールをインストールしてください）"
    return disk, ""


def part_mklabel(disk, table_type):
    """未初期化ディスクにパーティションテーブルを作成する（gpt/msdosのみ）"""
    disk = (disk or "").strip()
    table_type = (table_type or "").strip().lower()
    if not WIPE_PATH_RE.match(disk):
        return {"ok": False, "error": f"不正なデバイス指定です: {disk}"}
    if table_type not in ("gpt", "msdos"):
        return {"ok": False, "error": f"テーブル種別は gpt/msdos を指定してください: {table_type}"}
    d, err = _part_guard(disk, for_create_disk=True)
    if err:
        return {"ok": False, "error": err}
    # ディスク全体にFSがある媒体や既存パーティションがある場合は初期化を拒否
    try:
        r = subprocess.run(["lsblk", "-J", "-o", "NAME,TYPE,FSTYPE", disk],
            capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout or "{}")
        devs = data.get("blockdevices", [])
        if devs:
            top = devs[0]
            if (top.get("fstype") or "").strip():
                return {"ok": False, "error": f"{disk} 全体にファイルシステムがあるため初期化できません（データを退避してから消去してください）"}
            if top.get("children"):
                return {"ok": False, "error": f"{disk} には既にパーティションがあるため初期化できません"}
    except Exception:
        pass
    rc, out = _run_cmd(["parted", "-s", disk, "mklabel", table_type], timeout=120)
    if rc != 0:
        return {"ok": False, "error": f"パーティションテーブルの作成に失敗しました: {out[:300]}"}
    _part_refresh(disk)
    name = "GPT" if table_type == "gpt" else "MBR(msdos)"
    return {"ok": True, "message": f"{disk} を {name} で初期化しました"}


def part_dellabel(disk):
    """パーティションテーブルだけのドライブからテーブルを削除する（未初期化に戻す）
    パーティションが1つでもある場合や全体FSがある場合は拒否する"""
    disk = (disk or "").strip()
    if not WIPE_PATH_RE.match(disk):
        return {"ok": False, "error": f"不正なデバイス指定です: {disk}"}
    d, err = _part_guard(disk, for_create_disk=True)
    if err:
        return {"ok": False, "error": err}
    # マウント中（ディスク配下のいずれか）・swap使用中は拒否
    mounts = get_disk_mountpoints(disk)
    if mounts:
        dev, mp = mounts[0]
        return {"ok": False, "error": f"{dev} はマウント中（{mp}）のため削除できません（先にアンマウントしてください）"}
    try:
        r = subprocess.run(["lsblk", "-J", "-o", "NAME,TYPE,FSTYPE", disk],
            capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout or "{}")
        devs = data.get("blockdevices", [])
        if devs:
            top = devs[0]
            if (top.get("fstype") or "").strip():
                return {"ok": False, "error": f"{disk} 全体にファイルシステムがあるため削除できません"}
            if top.get("children"):
                return {"ok": False, "error": f"{disk} にはパーティションがあるためテーブルを削除できません（先にパーティションを削除してください）"}
    except Exception as e:
        return {"ok": False, "error": f"ディスク状態の確認に失敗しました: {e}"}
    # テーブル種別の確認（未初期化なら削除対象なし）
    table, _, _ = _parted_parse_free(disk)
    if table in ("", "unknown"):
        return {"ok": False, "error": f"{disk} にパーティションテーブルがありません"}
    # 署名を除去し、先頭・末尾（GPTバックアップ対策）をゼロクリアする
    rc, out = _run_cmd(["wipefs", "-a", disk], timeout=60)
    if rc != 0:
        return {"ok": False, "error": f"署名の削除に失敗しました: {out[:300]}"}
    size_bytes = get_device_size_bytes(disk)
    # 先頭 2MiB をゼロクリア（MBR/GPTヘッダ）
    rc, out = _run_cmd(["dd", "if=/dev/zero", f"of={disk}",
        "bs=1M", "count=2", "conv=fsync"], timeout=300)
    if rc != 0:
        return {"ok": False, "error": f"テーブルの削除に失敗しました: {out[:300]}"}
    # GPTバックアップ（末尾）対策：ディスク末尾 2MiB をゼロクリア
    if size_bytes and size_bytes > 4 * MIB:
        try:
            skip_mb = size_bytes // MIB - 2
            rc, out = _run_cmd(["dd", "if=/dev/zero", f"of={disk}",
                "bs=1M", f"seek={skip_mb}", "count=2", "conv=fsync"], timeout=300)
            if rc != 0:
                return {"ok": False, "error": f"テーブルの削除に失敗しました（末尾）: {out[:300]}"}
        except Exception as e:
            return {"ok": False, "error": f"末尾のクリアに失敗しました: {e}"}
    _part_refresh(disk)
    return {"ok": True, "message": f"{disk} のパーティションテーブルを削除しました"}


def part_delete(part):
    """パーティション削除。マウント中・システムは拒否"""
    part = (part or "").strip()
    if not PART_DEV_RE.match(part):
        return {"ok": False, "error": f"不正なデバイス指定です: {part}"}
    if not os.path.exists(part):
        return {"ok": False, "error": f"デバイスが見つかりません: {part}"}
    disk, err = _part_guard(part)
    if err:
        return {"ok": False, "error": err}
    if get_dev_mountpoint(part):
        return {"ok": False, "error": f"{part} はマウント中のため削除できません（先にアンマウントしてください）"}
    # swap 有効なパーティションは拒否
    try:
        r = subprocess.run(["swapon", "--show=NAME", "--noheadings"],
            capture_output=True, text=True, timeout=10)
        if part in (r.stdout or ""):
            return {"ok": False, "error": f"{part} は swap として使用中のため削除できません（swapoff 後に再試行）"}
    except Exception:
        pass
    num = _part_number(disk, part)
    if not num:
        return {"ok": False, "error": f"パーティション番号を特定できません: {part}"}
    rc, out = _run_cmd(["parted", "-s", disk, "rm", str(num)], timeout=120)
    if rc != 0:
        return {"ok": False, "error": f"削除に失敗しました: {out[:300]}"}
    _run_cmd(["wipefs", "-a", part], timeout=30)
    _part_refresh(disk)
    return {"ok": True, "message": f"{part} を削除しました"}


# 作成に対応するファイルシステムと mkfs コマンド
PART_MKFS = {
    "ext4": ["mkfs.ext4", "-F"],
    "ntfs": ["mkfs.ntfs", "-f", "-F"],
    "vfat": ["mkfs.vfat", "-F", "32"],
    "exfat": ["mkfs.exfat"],
    "xfs": ["mkfs.xfs", "-f"],
    "btrfs": ["mkfs.btrfs", "-f"],
}


def part_create(disk, fstype, size_bytes, label="", start_bytes=None, table_type=""):
    """空き領域にパーティションを作成し、ファイルシステムを初期化する
    未初期化ディスクでは table_type（gpt/msdos、既定gpt）で先に初期化してから作成する"""
    disk = (disk or "").strip()
    fstype = (fstype or "").strip().lower()
    try:
        size_bytes = int(size_bytes or 0)
    except Exception:
        return {"ok": False, "error": "サイズが不正です"}
    if not WIPE_PATH_RE.match(disk):
        return {"ok": False, "error": f"不正なデバイス指定です: {disk}"}
    d, err = _part_guard(disk, for_create_disk=True)
    if err:
        return {"ok": False, "error": err}
    if fstype not in PART_MKFS:
        return {"ok": False, "error": f"未対応のファイルシステムです: {fstype}"}
    mkfs_bin = PART_MKFS[fstype][0]
    if shutil.which(mkfs_bin) is None:
        return {"ok": False, "error": f"{mkfs_bin} が利用できません（ツールをインストールしてください）"}
    label = (label or "").strip()
    if fstype == "vfat" and len(label) > 11:
        return {"ok": False, "error": "vfat のラベルは11文字までです"}
    if size_bytes < 16 * MIB:
        return {"ok": False, "error": "サイズは 16MiB 以上を指定してください"}
    # 未初期化ディスクは先にパーティションテーブルを作成する
    table, _, frees = _parted_parse_free(disk)
    if table in ("", "unknown") and not frees:
        want = (table_type or "gpt").strip().lower()
        if want not in ("gpt", "msdos"):
            return {"ok": False, "error": f"テーブル種別は gpt/msdos を指定してください: {table_type}"}
        # 既存データの誤消去を防ぐため全体FSがあれば拒否する
        try:
            r = subprocess.run(["lsblk", "-J", "-o", "NAME,TYPE,FSTYPE", disk],
                capture_output=True, text=True, timeout=10)
            data = json.loads(r.stdout or "{}")
            devs = data.get("blockdevices", [])
            if devs:
                top = devs[0]
                if (top.get("fstype") or "").strip():
                    return {"ok": False, "error": f"{disk} 全体にファイルシステムがあるため作成できません"}
                if not top.get("children"):
                    rc, out = _run_cmd(["parted", "-s", disk, "mklabel", want], timeout=120)
                    if rc != 0:
                        return {"ok": False, "error": f"パーティションテーブルの作成に失敗しました: {out[:300]}"}
                    _part_refresh(disk)
        except Exception as e:
            return {"ok": False, "error": f"ディスク状態の確認に失敗しました: {e}"}
    # 空き領域の選定（指定開始位置が無ければ収まる最初の領域）
    _, _, frees = _parted_parse_free(disk)
    cands = [f for f in frees if int(f.get("size_bytes", 0) or 0) >= size_bytes + MIB]
    if not cands:
        return {"ok": False, "error": "指定サイズが収まる空き領域が見つかりません"}
    chosen = None
    if start_bytes is not None:
        try:
            start_bytes = int(start_bytes)
        except Exception:
            return {"ok": False, "error": "開始位置が不正です"}
        for f in cands:
            if f["start_bytes"] <= start_bytes and start_bytes + size_bytes <= f["end_bytes"]:
                chosen = f
                break
        if chosen is None:
            return {"ok": False, "error": "指定の開始位置に十分な空き領域がありません"}
    else:
        chosen = cands[0]
        start_bytes = int(chosen["start_bytes"])
    # MiB アライメント（開始は切り上げ・終了は切り捨て）
    start_al = ((start_bytes + MIB - 1) // MIB) * MIB
    end_al = start_al + size_bytes
    end_al = (end_al // MIB) * MIB
    if end_al - start_al < 16 * MIB or end_al > int(chosen["end_bytes"]):
        return {"ok": False, "error": "アライメント調整後に有効な領域が残りません（サイズを調整してください）"}
    # GPT ではデータ用パーティション名が必要な場合があるため付与
    _, bounds_before, _ = _parted_parse_free(disk)
    rc, out = _run_cmd(["parted", "-s", disk, "mkpart", "primary",
        f"{start_al}B", f"{end_al}B"], timeout=120)
    if rc != 0:
        return {"ok": False, "error": f"パーティション作成に失敗しました: {out[:300]}"}
    _part_refresh(disk)
    # 新規パーティションの特定（番号が最大のもの）
    _, bounds_after, _ = _parted_parse_free(disk)
    new_nums = set(bounds_after) - set(bounds_before)
    if not new_nums:
        # parted が番号を再利用した場合：parted print から末尾を採用
        new_num = max(bounds_after) if bounds_after else None
    else:
        new_num = max(new_nums)
    if not new_num:
        return {"ok": False, "error": "作成後のパーティションを特定できませんでした"}
    new_part = None
    # lsblk で番号→デバイス名を解決
    try:
        r = subprocess.run(["lsblk", "-J", "-o", "NAME,TYPE", disk],
            capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout or "{}")
        for dev in data.get("blockdevices", []):
            for ch in dev.get("children") or []:
                if _part_number(disk, f"/dev/{ch.get('name', '')}") == new_num:
                    new_part = f"/dev/{ch.get('name', '')}"
    except Exception:
        pass
    if not new_part:
        return {"ok": False, "error": "作成後のデバイス名を特定できませんでした"}
    # ファイルシステム初期化
    cmd = list(PART_MKFS[fstype])
    if label:
        if fstype in ("ext4", "ntfs", "xfs", "exfat", "btrfs"):
            cmd += ["-L", label]
        elif fstype == "vfat":
            cmd += ["-n", label.upper()]
    cmd.append(new_part)
    rc, out = _run_cmd(cmd, timeout=600)
    if rc != 0:
        return {"ok": False, "error": f"{new_part} のフォーマットに失敗しました: {out[:300]}",
            "part": new_part}
    _part_refresh(disk)
    return {"ok": True, "message": f"{new_part} ({fstype}) を作成しました", "part": new_part}


def part_resize(part, new_size_bytes):
    """パーティションの拡縮小。ext4/ntfs はFS連動、xfs/btrfs は拡大のみ（FS拡張は別途案内）"""
    part = (part or "").strip()
    try:
        new_size_bytes = int(new_size_bytes or 0)
    except Exception:
        return {"ok": False, "error": "サイズが不正です"}
    if not PART_DEV_RE.match(part):
        return {"ok": False, "error": f"不正なデバイス指定です: {part}"}
    disk, err = _part_guard(part)
    if err:
        return {"ok": False, "error": err}
    if get_dev_mountpoint(part):
        return {"ok": False, "error": f"{part} はマウント中のため変更できません（先にアンマウントしてください）"}
    num = _part_number(disk, part)
    if not num:
        return {"ok": False, "error": f"パーティション番号を特定できません: {part}"}
    # 現在の境界・FS種別・使用量を取得
    _, bounds, frees = _parted_parse_free(disk)
    if num not in bounds:
        return {"ok": False, "error": f"パーティション情報を取得できません: {part}"}
    start_b, end_b = bounds[num]
    cur_size = end_b - start_b
    if new_size_bytes < 16 * MIB:
        return {"ok": False, "error": "サイズは 16MiB 以上を指定してください"}
    if abs(new_size_bytes - cur_size) < MIB:
        return {"ok": False, "error": "サイズに変化がありません（1MiB以上変更してください）"}
    fstype = ""
    used_bytes = 0
    try:
        r = subprocess.run(["lsblk", "-J", "-b", "-o", "NAME,FSTYPE,FSUSED", part],
            capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout or "{}")
        devs = data.get("blockdevices", [])
        if devs:
            fstype = ((devs[0].get("fstype") or "").strip().lower())
            used_bytes = int(devs[0].get("fsused", 0) or 0)
    except Exception:
        pass
    if new_size_bytes > cur_size:
        # --- 拡大：後続の空きが連続している必要がある ---
        new_end = start_b + new_size_bytes
        ok_gap = any(f["start_bytes"] <= end_b + 1 and new_end <= f["end_bytes"] + 1
            for f in frees)
        if not ok_gap:
            return {"ok": False, "error": "パーティション直後に十分な空き領域がありません（拡大には隣接する空きが必要です）"}
        new_end_al = (new_end // MIB) * MIB
        if new_end_al - start_b < cur_size + MIB:
            return {"ok": False, "error": "アライメント調整後に拡大幅が残りません（サイズを調整してください）"}
        if fstype in ("", "swap"):
            rc, out = _parted_resizepart(disk, num, new_end_al)
            if rc != 0:
                return {"ok": False, "error": f"拡大に失敗しました: {out[:300]}"}
            _part_refresh(disk)
            return {"ok": True, "message": f"{part} を {_fmt_bytes(new_end_al - start_b)} に拡大しました"}
        if fstype in ("ext4",):
            if shutil.which("resize2fs") is None or shutil.which("e2fsck") is None:
                return {"ok": False, "error": "e2fsck/resize2fs が利用できません"}
            rc, out = _parted_resizepart(disk, num, new_end_al)
            if rc != 0:
                return {"ok": False, "error": f"パーティション拡大に失敗しました: {out[:300]}"}
            _part_refresh(disk)
            rc, out = _run_cmd(["e2fsck", "-f", "-y", part], timeout=600)
            # e2fsck は修正ありで rc=1 を返すことがあるため 0/1 は許容
            if rc not in (0, 1):
                return {"ok": False, "error": f"ファイルシステム検査に失敗しました: {out[:300]}"}
            rc, out = _run_cmd(["resize2fs", part], timeout=1800)
            if rc != 0:
                return {"ok": False, "error": f"ファイルシステム拡大に失敗しました: {out[:300]}"}
            _part_refresh(disk)
            return {"ok": True, "message": f"{part} を拡大しました（ext4連動）"}
        if fstype in ("ntfs",):
            if shutil.which("ntfsresize") is None:
                return {"ok": False, "error": "ntfsresize が利用できません"}
            rc, out = _parted_resizepart(disk, num, new_end_al)
            if rc != 0:
                return {"ok": False, "error": f"パーティション拡大に失敗しました: {out[:300]}"}
            _part_refresh(disk)
            rc, out = _run_cmd(["ntfsresize", "-f", part], timeout=1800)
            if rc != 0:
                return {"ok": False, "error": f"NTFS拡大に失敗しました: {out[:300]}"}
            _part_refresh(disk)
            return {"ok": True, "message": f"{part} を拡大しました（NTFS連動）"}
        if fstype in ("xfs", "btrfs"):
            rc, out = _parted_resizepart(disk, num, new_end_al)
            if rc != 0:
                return {"ok": False, "error": f"拡大に失敗しました: {out[:300]}"}
            _part_refresh(disk)
            grow = "xfs_growfs（マウント後に実行）" if fstype == "xfs" else "btrfs filesystem resize（マウント後に実行）"
            return {"ok": True, "message": f"{part} のパーティションを拡大しました。FS拡張は別途 {grow} が必要です",
                "need_fs_grow": True}
        if fstype in ("vfat", "exfat"):
            if fstype == "vfat" and shutil.which("fatresize") is None:
                return {"ok": False, "error": "vfat のリサイズには fatresize が必要です（未導入のため未対応）"}
            rc, out = _parted_resizepart(disk, num, new_end_al)
            if rc != 0:
                return {"ok": False, "error": f"拡大に失敗しました: {out[:300]}"}
            _part_refresh(disk)
            if fstype == "vfat":
                rc, out = _run_cmd(["fatresize", "-s", f"{(new_end_al - start_b) // MIB}M", part], timeout=1800)
                if rc != 0:
                    return {"ok": False, "error": f"FAT拡大に失敗しました: {out[:300]}"}
            return {"ok": True, "message": f"{part} を拡大しました（{fstype}連動）"}
        return {"ok": False, "error": f"未対応のファイルシステムです: {fstype or '不明'}"}
    else:
        # --- 縮小 ---
        if used_bytes and new_size_bytes <= int(used_bytes * 1.05) + 64 * MIB:
            return {"ok": False, "error": f"使用中 ({_fmt_bytes(used_bytes)}) のため指定サイズに縮小できません"}
        new_end = start_b + new_size_bytes
        new_end_al = (new_end // MIB) * MIB
        if new_end_al <= start_b + 16 * MIB:
            return {"ok": False, "error": "縮小後のサイズが小さすぎます"}
        if fstype in ("xfs", "btrfs"):
            return {"ok": False, "error": f"{fstype} の縮小は未対応です（拡大のみ対応）"}
        if fstype in ("ext4",):
            if shutil.which("resize2fs") is None or shutil.which("e2fsck") is None:
                return {"ok": False, "error": "e2fsck/resize2fs が利用できません"}
            # FSが既に目標サイズ以下なら（前回FSのみ成功等の再試行）FS縮小をスキップする
            target_size = new_end_al - start_b
            vol = _fs_volume_size(part, fstype)
            if vol is None or vol > target_size:
                rc, out = _run_cmd(["e2fsck", "-f", "-y", part], timeout=600)
                if rc not in (0, 1):
                    return {"ok": False, "error": f"ファイルシステム検査に失敗しました: {out[:300]}"}
                # FSを先に縮小（ブロック単位の切り上げ誤差に備え1MiB余裕を見る）
                shrink_arg = f"{(new_end_al - start_b) // MIB}M"
                rc, out = _run_cmd(["resize2fs", part, shrink_arg], timeout=1800)
                if rc != 0:
                    return {"ok": False, "error": f"ファイルシステム縮小に失敗しました: {out[:300]}"}
            rc, out = _parted_resizepart(disk, num, new_end_al)
            if rc != 0:
                return {"ok": False, "error": f"FSは縮小済みですがパーティション縮小に失敗しました: {out[:300]}"}
            _part_refresh(disk)
            return {"ok": True, "message": f"{part} を縮小しました（ext4連動）"}
        if fstype in ("ntfs",):
            if shutil.which("ntfsresize") is None:
                return {"ok": False, "error": "ntfsresize が利用できません"}
            # FSが既に目標サイズ以下なら（前回FSのみ成功等の再試行）FS縮小をスキップする
            target_size = new_end_al - start_b
            vol = _fs_volume_size(part, fstype)
            if vol is None or vol > target_size:
                # バイト指定で端数なく縮小（ntfsresize側でクラスタ境界に切り捨て）
                shrink_arg = f"{target_size}"
                rc, out = _run_cmd(["ntfsresize", "-f", "-s", shrink_arg, part], timeout=1800)
                if rc != 0:
                    return {"ok": False, "error": f"NTFS縮小に失敗しました: {out[:300]}"}
            rc, out = _parted_resizepart(disk, num, new_end_al)
            if rc != 0:
                return {"ok": False, "error": f"FSは縮小済みですがパーティション縮小に失敗しました: {out[:300]}"}
            _part_refresh(disk)
            return {"ok": True, "message": f"{part} を縮小しました（NTFS連動）"}
        if fstype in ("vfat",):
            if shutil.which("fatresize") is None:
                return {"ok": False, "error": "vfat のリサイズには fatresize が必要です（未導入のため未対応）"}
            rc, out = _run_cmd(["fatresize", "-s", f"{(new_end_al - start_b) // MIB}M", part], timeout=1800)
            if rc != 0:
                return {"ok": False, "error": f"FAT縮小に失敗しました: {out[:300]}"}
            rc, out = _parted_resizepart(disk, num, new_end_al)
            if rc != 0:
                return {"ok": False, "error": f"FSは縮小済みですがパーティション縮小に失敗しました: {out[:300]}"}
            _part_refresh(disk)
            return {"ok": True, "message": f"{part} を縮小しました（vfat連動）"}
        if fstype in ("", "swap"):
            if not fstype:
                rc, out = _parted_resizepart(disk, num, new_end_al)
                if rc != 0:
                    return {"ok": False, "error": f"縮小に失敗しました: {out[:300]}"}
                _part_refresh(disk)
                return {"ok": True, "message": f"{part} を縮小しました"}
            return {"ok": False, "error": "swap の縮小は未対応です（削除後に再作成してください）"}
        return {"ok": False, "error": f"未対応のファイルシステムです: {fstype or '不明'}"}


def _run_part_install():
    """parted＋FS操作ツールをバックグラウンドで導入する"""
    global part_install_running
    log_path = os.path.join(LOG_DIR, "part-install.log")
    try:
        with open(log_path, "w") as f:
            f.write(f"=== part tools install started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            f.flush()
            os_id, os_like = "", ""
            try:
                with open("/etc/os-release") as of:
                    for line in of:
                        if line.startswith("ID="):
                            os_id = line.split("=", 1)[1].strip().strip('"').lower()
                        elif line.startswith("ID_LIKE="):
                            os_like = line.split("=", 1)[1].strip().strip('"').lower()
            except Exception:
                pass
            is_arch = ("arch" in os_like) or os_id in ("arch", "cachyos") or \
                (shutil.which("pacman") and not shutil.which("apt-get"))
            if is_arch:
                cmd = ["pacman", "-Sy", "--noconfirm", "--needed",
                    "parted", "dosfstools", "ntfs-3g", "exfatprogs",
                    "xfsprogs", "btrfs-progs", "e2fsprogs"]
            else:
                f.write("$ apt-get update\n")
                f.flush()
                r0 = subprocess.run(["apt-get", "update"], stdout=f, stderr=subprocess.STDOUT, timeout=600)
                if r0.returncode != 0:
                    f.write(f"apt-get update failed (code={r0.returncode})\n")
                cmd = ["apt-get", "install", "-y",
                    "parted", "dosfstools", "ntfs-3g", "exfatprogs", "exfat-fuse",
                    "xfsprogs", "btrfs-progs", "e2fsprogs"]
            f.write(f"$ {' '.join(cmd)}\n")
            f.flush()
            r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=3600)
            f.write(f"\n=== finished code={r.returncode} ===\n")
    except Exception as e:
        try:
            with open(log_path, "a") as f:
                f.write(f"install error: {e}\n")
        except Exception:
            pass
    finally:
        with part_install_lock:
            part_install_running = False


def _split_ocs_image(path):
    """Clonezilla イメージ指定「/dir/NAME」を (ocsroot_dir, image_name) に分割"""
    p = (path or "").strip()
    if not p or "/" not in p:
        return None, None
    img_dir = os.path.dirname(p)
    img_name = os.path.basename(p)
    if not img_dir or not img_name:
        return None, None
    # イメージ名は Clonezilla の制約上ディレクトリ名として使える文字のみ
    if not re.match(r"^[A-Za-z0-9._-]+$", img_name):
        return None, None
    return img_dir, img_name


def _run_clone_install():
    """Clonezilla / partclone をバックグラウンドで導入する"""
    global clone_install_running
    log_path = os.path.join(LOG_DIR, "clone-install.log")
    try:
        with open(log_path, "w") as f:
            f.write(f"=== clone tools install started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            f.flush()
            # OS 判定（install.sh と同じ基準）
            os_id, os_like = "", ""
            try:
                with open("/etc/os-release") as of:
                    for line in of:
                        if line.startswith("ID="):
                            os_id = line.split("=", 1)[1].strip().strip('"').lower()
                        elif line.startswith("ID_LIKE="):
                            os_like = line.split("=", 1)[1].strip().strip('"').lower()
            except Exception:
                pass
            is_arch = ("arch" in os_like) or os_id in ("arch", "cachyos") or \
                (shutil.which("pacman") and not shutil.which("apt-get"))
            if is_arch:
                cmd = ["pacman", "-Sy", "--noconfirm", "--needed", "clonezilla", "partclone"]
            else:
                # Debian/Ubuntu 系は事前に apt-get update してから導入
                f.write("$ apt-get update\n")
                f.flush()
                r0 = subprocess.run(["apt-get", "update"], stdout=f, stderr=subprocess.STDOUT, timeout=600)
                if r0.returncode != 0:
                    f.write(f"apt-get update failed (code={r0.returncode})\n")
                cmd = ["apt-get", "install", "-y", "clonezilla", "partclone"]
            f.write(f"$ {' '.join(cmd)}\n")
            f.flush()
            r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=3600)
            f.write(f"\n=== finished code={r.returncode} ===\n")
    except Exception as e:
        try:
            with open(log_path, "a") as f:
                f.write(f"install error: {e}\n")
        except Exception:
            pass
    finally:
        with clone_install_lock:
            clone_install_running = False


def _wipe_log(job, msg):
    """消去ジョブのログファイルに追記"""
    try:
        lf = job.get("log_file")
        if lf:
            with open(lf, "a") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _wipe_write_pass(job, target, fill, pass_no, pass_total, log_prefix):
    """1パス分の上書き。fill='zero' または 'random'。停止要求時は False を返す"""
    size = target["size_bytes"]
    chunk = WIPE_CHUNK
    zero_block = b"\x00" * chunk
    written = target.get("pass_written_base", 0)
    t0 = time.time()
    try:
        fd = os.open(target["path"], os.O_WRONLY)
    except Exception as e:
        target["status"] = "error"
        target["message"] = f"オープン失敗: {e}"
        return False
    try:
        # 先頭にシーク（O_WRONLY では offset 0 から開始されるが明示）
        os.lseek(fd, 0, os.SEEK_SET)
        # 既に書き込み済みバイトがある場合（再開ではないが念のため）スキップ
        remaining = size
        # パス開始時点の基準値を記録
        base = 0
        while remaining > 0:
            if job.get("stop"):
                target["status"] = "stopped"
                target["message"] = "ユーザーにより中断"
                return False
            n = chunk if remaining >= chunk else remaining
            if fill == "zero":
                buf = zero_block[:n] if n != chunk else zero_block
            else:
                buf = os.urandom(n)
            try:
                w = os.write(fd, buf)
            except Exception as e:
                target["status"] = "error"
                target["message"] = f"書き込み失敗: {e}"
                return False
            if w == 0:
                target["status"] = "error"
                target["message"] = "書き込みが 0 バイトで終了"
                return False
            base += w
            remaining -= w
            # 進捗更新（このパスの進捗＋全体パス換算）
            target["bytes_written"] = target.get("bytes_written", 0) + 0  # 全体は下で再計算
            elapsed = time.time() - t0
            # このパス内の割合
            pass_frac = base / size if size else 1.0
            # 全体割合 = (完了パス + 当パス進捗) / 全パス
            overall = ((pass_no - 1) + pass_frac) / pass_total if pass_total else 1.0
            target["percent"] = round(overall * 100, 1)
            target["pass_current"] = pass_no
            target["pass_total"] = pass_total
            if elapsed > 0 and base > 0:
                speed = base / elapsed
                target["speed_bps"] = speed
                left_in_pass = size - base
                passes_left = (pass_total - pass_no) * size + left_in_pass
                target["eta_sec"] = int(passes_left / speed) if speed > 0 else -1
            target["message"] = f"{log_prefix} パス {pass_no}/{pass_total} 書き込み中"
        os.fsync(fd)
        target["bytes_written"] = (pass_no * size)
        return True
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


def _wipe_ssd(job, target, tran):
    """SSD 消去。接続方式でコマンドを自動選択。進捗は取れないため開始/完了のみ"""
    path = target["path"]
    tran_low = (tran or "").lower()
    if tran_low == "nvme":
        target["method_detail"] = "NVMe Format (Sanitize相当: nvme format --ses=1)"
        cmd = ["nvme", "format", path, "--ses=1", "--force"]
    elif tran_low in ("sata", "ata", "sas"):
        target["method_detail"] = "Secure Erase (hdparm)"
        cmd = None  # hdparm は複数ステップのため後述
    else:
        # USB 接続等：Secure Erase が使えないため blkdiscard（TRIM/Sanitize相当）
        target["method_detail"] = "blkdiscard による破棄（USB経由等のためSanitize相当）"
        cmd = ["blkdiscard", "-f", path]
    _wipe_log(job, f"{path}: {target['method_detail']} 開始")
    try:
        if cmd is not None:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            out = (r.stdout or "") + (r.stderr or "")
            _wipe_log(job, f"{path}: 終了 code={r.returncode}\n{out}")
            if r.returncode == 0:
                target["percent"] = 100.0
                target["status"] = "done"
                target["message"] = "消去完了"
                return True
            # blkdiscard が未対応の場合はゼロ1パスにフォールバック
            if "blkdiscard" in target["method_detail"]:
                _wipe_log(job, f"{path}: blkdiscard失敗、ゼロ1パスにフォールバック")
                target["method_detail"] += " → blkdiscard非対応のためゼロ消去に切替"
                target["pass_total"] = 1
                ok = _wipe_write_pass(job, target, "zero", 1, 1, "SSDフォールバック(ゼロ)")
                if ok:
                    target["percent"] = 100.0
                    target["status"] = "done"
                    target["message"] = "消去完了（ゼロ1パス）"
                    return True
                return False
            target["status"] = "error"
            target["message"] = f"消去コマンド失敗 (code={r.returncode}): {out[:500]}"
            return False
        else:
            # hdparm Secure Erase（2ステップ）
            # Frozen チェック
            r = subprocess.run(["hdparm", "-I", path],
                capture_output=True, text=True, timeout=30)
            out = (r.stdout or "") + (r.stderr or "")
            if "frozen" in out.lower():
                # frozen と not frozen の両方を含む場合があるため "not frozen" を優先判定
                if "not frozen" not in out.lower():
                    target["status"] = "error"
                    target["message"] = "Frozen状態のためSecure Eraseできません（電源再投入で解除される場合があります）"
                    _wipe_log(job, f"{path}: frozen のため中止")
                    return False
            passwd = "diskmanager"
            r1 = subprocess.run(
                ["hdparm", "--user-master", "u", "--security-set-pass", passwd, path],
                capture_output=True, text=True, timeout=120)
            _wipe_log(job, f"{path}: security-set-pass code={r1.returncode} {(r1.stdout or '') + (r1.stderr or '')}")
            if r1.returncode != 0:
                target["status"] = "error"
                target["message"] = f"セキュリティパスワード設定失敗: {(r1.stderr or r1.stdout or '')[:300]}"
                return False
            if job.get("stop"):
                target["status"] = "stopped"
                return False
            r2 = subprocess.run(
                ["hdparm", "--user-master", "u", "--security-erase", passwd, path],
                capture_output=True, text=True, timeout=3600)
            _wipe_log(job, f"{path}: security-erase code={r2.returncode} {(r2.stdout or '') + (r2.stderr or '')}")
            if r2.returncode == 0:
                target["percent"] = 100.0
                target["status"] = "done"
                target["message"] = "消去完了（Secure Erase）"
                return True
            target["status"] = "error"
            target["message"] = f"Secure Erase失敗: {(r2.stderr or r2.stdout or '')[:300]}"
            return False
    except FileNotFoundError as e:
        target["status"] = "error"
        target["message"] = f"消去コマンドが見つかりません: {e}（nvme-cli / hdparm / util-linux を導入してください）"
        return False
    except subprocess.TimeoutExpired:
        target["status"] = "error"
        target["message"] = "消去コマンドがタイムアウト"
        return False
    except Exception as e:
        target["status"] = "error"
        target["message"] = f"消去エラー: {e}"
        return False


def _run_wipe_job(job):
    """消去ジョブのバックグラウンド実行（対象ディスクを順次処理）"""
    global wipe_job
    _wipe_log(job, f"消去ジョブ開始 method={job['method']} passes={job['passes']}")
    for idx, target in enumerate(job["targets"]):
        if job.get("stop"):
            if target["status"] == "waiting":
                target["status"] = "stopped"
                target["message"] = "中断"
            continue
        job["current_index"] = idx
        target["status"] = "running"
        target["percent"] = 0.0
        target["started_at"] = time.time()
        _wipe_log(job, f"{target['path']} 開始 (SSD={target['is_ssd']})")
        if target["is_ssd"]:
            ok = _wipe_ssd(job, target, target.get("tran", ""))
        else:
            if job["method"] == "random":
                passes = job["passes"]
                target["method_detail"] = f"乱数 {passes} 回上書き"
                ok = True
                for p in range(1, passes + 1):
                    if job.get("stop"):
                        target["status"] = "stopped"
                        target["message"] = "ユーザーにより中断"
                        ok = False
                        break
                    target["message"] = f"乱数 パス {p}/{passes} 書き込み中"
                    if not _wipe_write_pass(job, target, "random", p, passes, "乱数"):
                        ok = False
                        break
                if ok:
                    target["percent"] = 100.0
                    target["status"] = "done"
                    target["message"] = f"消去完了（乱数{passes}回）"
            else:
                target["method_detail"] = "ゼロ 1 回上書き"
                if _wipe_write_pass(job, target, "zero", 1, 1, "ゼロ"):
                    target["percent"] = 100.0
                    target["status"] = "done"
                    target["message"] = "消去完了（ゼロ1回）"
                    ok = True
                else:
                    ok = False
        target["finished_at"] = time.time()
        _wipe_log(job, f"{target['path']} 終了 status={target['status']} {target.get('message','')}")
    job["finished_at"] = time.time()
    job["running"] = False
    _wipe_log(job, "消去ジョブ終了")

def read_log_tail(filepath, lines=100):
    try:
        with open(filepath, "r", errors="replace") as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:])
    except Exception:
        return ""

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(BASE_DIR, "public"), **kw)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/api/devices":
            self._json(get_block_devices())
        elif p.path == "/api/device-info":
            q = parse_qs(p.query)
            path = q.get("path", [""])[0]
            self._json(get_device_info(path) if path else {"error": "path required"}, 400 if not path else 200)
        elif p.path == "/api/file-info":
            q = parse_qs(p.query)
            path = q.get("path", [""])[0]
            for_dest = q.get("for_dest", ["0"])[0] == "1"
            self._json(get_file_info(path, for_dest) if path else {"error": "path required"}, 400 if not path else 200)
        elif p.path == "/api/status":
            with wipe_lock:
                wrun = wipe_job is not None and bool(wipe_job.get("running"))
            self._json({"running": running_process is not None and running_process.poll() is None,
                        "log_file": current_log_file, "version": VERSION,
                        "wipe_running": wrun,
                        "clone_running": self._any_running()})
        elif p.path == "/api/logs":
            self._json(get_log_files())
        elif p.path == "/api/log-content":
            q = parse_qs(p.query)
            name = os.path.basename(q.get("name", [""])[0])
            lines = int(q.get("lines", ["100"])[0])
            if name:
                fpath = os.path.join(LOG_DIR, name)
                self._json({"content": read_log_tail(fpath, lines)} if os.path.exists(fpath) else {"error": "not found"}, 404 if not os.path.exists(fpath) else 200)
            else:
                self._json({"error": "name required"}, 400)
        elif p.path == "/api/log-stream":
            q = parse_qs(p.query)
            name = q.get("name", [""])[0]
            if name: self._stream_log(os.path.join(LOG_DIR, name))
            else: self._json({"error": "name required"}, 400)
        elif p.path == "/api/wipe-devices":
            self._json(get_wipe_devices())
        elif p.path == "/api/smart-devices":
            self._json(get_smart_devices())
        elif p.path == "/api/smart":
            q = parse_qs(p.query)
            path = q.get("path", [""])[0]
            if not path:
                self._json({"available": False, "error": "path required"}, 400)
            else:
                self._json(get_smart_info(path))
        elif p.path == "/api/wipe/status":
            with wipe_lock:
                if wipe_job is None:
                    self._json({"running": False, "job": None})
                else:
                    # 進捗スナップショットを返す
                    self._json({"running": bool(wipe_job.get("running")),
                        "job": wipe_job})
        elif p.path == "/api/clone/check":
            self._json(get_clone_status())
        elif p.path == "/api/clone-devices":
            sys_disk = get_system_disk()
            self._json({"system_disk": sys_disk, "devices": get_clone_devices()})
        elif p.path == "/api/clone/progress":
            self._json(get_clone_progress())
        elif p.path == "/api/rescue/progress":
            self._json(get_rescue_progress())
        elif p.path == "/api/rsync/check":
            self._json(get_rsync_status())
        elif p.path == "/api/rsync/partitions":
            self._json(get_rsync_partitions())
        elif p.path == "/api/rsync/list":
            q = parse_qs(p.query)
            side = q.get("side", ["src"])[0]
            base = q.get("base", [""])[0]
            if side not in ("src", "dst"):
                self._json({"ok": False, "error": "side が不正です"}, 400)
            elif not base:
                self._json({"ok": False, "error": "base が必要です"}, 400)
            else:
                res = list_rsync_dir(side, base)
                self._json(res, 200 if res.get("ok") else 400)
        elif p.path == "/api/rsync/mounts":
            self._json(_load_rsync_mounts())
        elif p.path == "/api/rsync/progress":
            self._json(get_rsync_progress())
        elif p.path == "/api/part/check":
            self._json(get_part_tools())
        elif p.path == "/api/part/devices":
            self._json(get_part_devices())
        else:
            super().do_GET()

    def do_POST(self):
        p = urlparse(self.path)
        cl = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(cl) if cl else b""
        try: data = json.loads(body) if body else {}
        except: data = {}
        if p.path == "/api/start": self._handle_start(data)
        elif p.path == "/api/stop": self._handle_stop()
        elif p.path == "/api/force-stop": self._handle_force_stop()
        elif p.path == "/api/update": self._handle_update()
        elif p.path == "/api/restart": self._handle_restart()
        elif p.path == "/api/wipe/start": self._handle_wipe_start(data)
        elif p.path == "/api/wipe/stop": self._handle_wipe_stop()
        elif p.path == "/api/clone/install": self._handle_clone_install()
        elif p.path == "/api/clone/start": self._handle_clone_start(data)
        elif p.path == "/api/clone/unmount": self._handle_clone_unmount(data)
        elif p.path == "/api/clone/stop": self._handle_stop()
        elif p.path == "/api/rsync/install": self._handle_rsync_install()
        elif p.path == "/api/rsync/mount": self._handle_rsync_mount(data)
        elif p.path == "/api/rsync/mount-image": self._handle_rsync_mount_image(data)
        elif p.path == "/api/rsync/unmount": self._handle_rsync_unmount(data)
        elif p.path == "/api/rsync/mkdir": self._handle_rsync_mkdir(data)
        elif p.path == "/api/rsync/start": self._handle_rsync_start(data)
        elif p.path == "/api/rsync/stop": self._handle_stop()
        elif p.path == "/api/part/install": self._handle_part_install()
        elif p.path == "/api/part/unmount": self._handle_part_unmount(data)
        elif p.path == "/api/part/delete": self._handle_part_delete(data)
        elif p.path == "/api/part/create": self._handle_part_create(data)
        elif p.path == "/api/part/mklabel": self._handle_part_mklabel(data)
        elif p.path == "/api/part/dellabel": self._handle_part_dellabel(data)
        elif p.path == "/api/part/resize": self._handle_part_resize(data)
        else: self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _wipe_running(self):
        with wipe_lock:
            return wipe_job is not None and bool(wipe_job.get("running"))

    def _handle_start(self, data):
        global running_process, current_log_file
        if self._any_running():
            self._json({"error": "既に実行中です"}); return
        if self._wipe_running():
            self._json({"error": "ディスク消去実行中はレスキューできません"}); return

        source = data.get("source", "")
        dest = data.get("dest", "")
        options = data.get("options", {})
        resume_log = data.get("resume_log", "")

        if not source: self._json({"error": "コピー元を指定してください"}); return
        if not dest: self._json({"error": "コピー先を指定してください"}); return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_name = resume_log if resume_log else f"ddrescue_{os.path.basename(source).replace('/', '_')[:20]}_{timestamp}.run.log"
        current_log_file = os.path.join(LOG_DIR, log_name)
        mapfile_path = current_log_file[:-len(".run.log")] + ".map" if current_log_file.endswith(".run.log") else current_log_file + ".map"

        cmd = ["stdbuf", "-o0", "-e0", "ddrescue"]
        if options.get("direct"): cmd.append("-d")
        if options.get("force"): cmd.append("-f")
        if options.get("no_scrape"): cmd.append("-n")
        if options.get("no_sweep"): cmd.append("-N")
        if options.get("sparse"): cmd.append("-S")
        if options.get("odirect"): cmd.append("-D")
        if options.get("reverse"): cmd.append("-R")
        if options.get("unidirectional"): cmd.append("-u")

        for opt, flag in [
            ("retry_passes", "-r"), ("input_pos", "-i"),
            ("size_limit", "-s"), ("sector_size", "-b"), ("cluster_size", "-c"),
            ("min_read_rate", "-a"), ("max_bad_areas", "-e"),
            ("max_error_rate", "-E"), ("timeout", "-T"),
        ]:
            val = options.get(opt, "")
            if val and str(val).strip():
                cmd.extend([flag, str(val).strip()])

        cmd.extend([source, dest, mapfile_path])

        try:
            log_f = open(current_log_file, "a")
            log_f.write(f"=== ddrescue started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            log_f.write(f"Command: {' '.join(cmd)}\n\n")
            log_f.flush()
            running_process = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
            log_f.close()
            _save_rescue_job({"source": source, "dest": dest,
                "started_at": time.time(), "log_file": log_name, "log_path": current_log_file,
                "mapfile": mapfile_path, "pid": running_process.pid})
            self._json({"ok": True, "log_file": log_name, "pid": running_process.pid})
        except Exception as e:
            self._json({"error": str(e)})

    def _adopted_clone_pid(self):
        """サービス再起動後に取り残されたクローン／rsyncプロセスの pid を返す（無ければ None）"""
        if running_process is not None and running_process.poll() is None:
            return None  # 自プロセスで管理中
        job = _load_clone_job()
        if job and job.get("pid") and _pid_is_clone(job.get("pid")):
            return int(job.get("pid"))
        rjob = _load_rsync_job()
        if rjob and rjob.get("pid") and _pid_is_rsync(rjob.get("pid")):
            return int(rjob.get("pid"))
        return None

    def _any_running(self):
        """自管理プロセスまたは引き継ぎクローン／rsyncのいずれかが実行中か"""
        if running_process is not None and running_process.poll() is None:
            return True
        return self._adopted_clone_pid() is not None

    def _handle_stop(self):
        global running_process
        if running_process and running_process.poll() is None:
            proc = running_process
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception: pass

            def escalate():
                try:
                    if proc.poll() is None:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception: pass
            threading.Timer(3.0, escalate).start()
            self._json({"ok": True})
            return
        pid = self._adopted_clone_pid()
        if pid:
            # 再起動後に引き継いだプロセスはプロセスグループごと停止する
            try:
                os.killpg(pid, signal.SIGTERM)
            except Exception: pass

            def escalate_adopted():
                try:
                    os.killpg(pid, signal.SIGKILL)
                except Exception: pass
            threading.Timer(3.0, escalate_adopted).start()
            self._json({"ok": True, "adopted": True})
            return
        self._json({"error": "実行中のプロセスがありません"})

    def _handle_force_stop(self):
        global running_process
        if running_process and running_process.poll() is None:
            try:
                os.killpg(os.getpgid(running_process.pid), signal.SIGKILL)
            except Exception: pass
            self._json({"ok": True})
            return
        pid = self._adopted_clone_pid()
        if pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception: pass
            self._json({"ok": True, "adopted": True})
            return
        self._json({"error": "実行中のプロセスがありません"})

    def _handle_update(self):
        if self._any_running():
            self._json({"error": "レスキュー実行中はアップデートできません"}); return
        if self._wipe_running():
            self._json({"error": "ディスク消去実行中はアップデートできません"}); return
        script_path = "/tmp/diskmanager-install.sh"
        try:
            urllib.request.urlretrieve(INSTALL_SCRIPT_URL, script_path)
            os.chmod(script_path, 0o755)
        except Exception as e:
            self._json({"error": f"インストーラのダウンロードに失敗しました: {e}"}); return
        try:
            log_path = os.path.join(LOG_DIR, "update.log")
            with open(log_path, "w") as log_f: log_f.close()
            # systemd 管理下で別ユニット起動（サービス再起動時に道連れkillされるのを防ぐ）
            if os.path.exists("/usr/bin/systemd-run"):
                cmd = ["systemd-run", "--collect", "--unit=diskmanager-update",
                       "--description=Disk Manager update",
                       "bash", "-c", f"exec bash '{script_path}' > '{log_path}' 2>&1"]
                out = subprocess.DEVNULL
            else:
                cmd = ["bash", script_path]
                out = open(log_path, "w")
            subprocess.Popen(cmd, stdout=out,
                stderr=subprocess.STDOUT, start_new_session=True)
            if out is not subprocess.DEVNULL: out.close()
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": str(e)})

    def _handle_restart(self):
        if self._any_running():
            self._json({"error": "レスキュー実行中は再起動できません"}); return
        if self._wipe_running():
            self._json({"error": "ディスク消去実行中は再起動できません"}); return

        def do_restart():
            try:
                subprocess.run(["systemctl", "restart", SERVICE_NAME], timeout=30)
            except Exception: pass
        threading.Timer(0.5, do_restart).start()
        self._json({"ok": True})

    def _handle_wipe_start(self, data):
        global wipe_job
        if self._any_running():
            self._json({"error": "レスキュー実行中は消去できません"}); return
        with wipe_lock:
            if wipe_job is not None and bool(wipe_job.get("running")):
                self._json({"error": "既に消去実行中です"}); return
        targets_in = data.get("targets", [])
        method = data.get("method", "zero")
        try:
            passes = int(data.get("passes", 3))
        except Exception:
            passes = 3
        if method not in ("zero", "random"):
            self._json({"error": "消去方式が不正です"}); return
        passes = max(1, min(7, passes))
        if not targets_in:
            self._json({"error": "対象ディスクを選択してください"}); return
        # 最新デバイス一覧で検証（存在確認・マウント確認・サイズ取得）
        devs = {d["path"]: d for d in get_wipe_devices()}
        targets = []
        for t in targets_in:
            path = (t.get("path") or "").strip()
            is_ssd = bool(t.get("is_ssd"))
            if not WIPE_PATH_RE.match(path):
                self._json({"error": f"不正なデバイス指定です: {path}"}); return
            if path not in devs:
                self._json({"error": f"デバイスが見つかりません: {path}"}); return
            info = devs[path]
            if info.get("has_mount"):
                self._json({"error": f"{path} はマウント中のパーティションを含むため消去できません。アンマウントしてから実行してください"}); return
            size_bytes = info.get("size_bytes") or get_device_size_bytes(path)
            if not size_bytes or size_bytes <= 0:
                self._json({"error": f"{path} のサイズを取得できません"}); return
            # 二重指定を除去
            if any(x["path"] == path for x in targets):
                continue
            targets.append({"path": path, "name": info.get("name", ""),
                "size": info.get("size", ""), "size_bytes": size_bytes,
                "model": info.get("model", ""), "serial": info.get("serial", ""),
                "tran": info.get("tran", ""), "is_ssd": is_ssd,
                "status": "waiting", "percent": 0.0,
                "pass_current": 0, "pass_total": 1,
                "bytes_written": 0, "speed_bps": 0, "eta_sec": -1,
                "message": "待機中", "method_detail": ""})
        if not targets:
            self._json({"error": "対象ディスクを選択してください"}); return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_name = f"wipe_{timestamp}.log"
        log_file = os.path.join(LOG_DIR, log_name)
        job = {"id": timestamp, "running": True, "stop": False,
            "method": method, "passes": passes, "targets": targets,
            "current_index": 0, "started_at": time.time(),
            "finished_at": None, "log_file": log_name}
        try:
            with open(log_file, "w") as f:
                f.write(f"=== wipe started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                f.write(f"method={method} passes={passes} targets={[t['path'] for t in targets]}\n\n")
        except Exception:
            pass
        job["log_file"] = log_file
        with wipe_lock:
            wipe_job = job
        th = threading.Thread(target=_run_wipe_job, args=(job,), daemon=True)
        th.start()
        self._json({"ok": True, "job_id": timestamp, "log_file": log_name})

    def _handle_wipe_stop(self):
        with wipe_lock:
            if wipe_job is None or not bool(wipe_job.get("running")):
                self._json({"error": "実行中の消去ジョブがありません"}); return
            wipe_job["stop"] = True
        self._json({"ok": True})

    def _handle_clone_install(self):
        global clone_install_running
        st = get_clone_status()
        if st["installed"]:
            self._json({"ok": True, "already": True}); return
        with clone_install_lock:
            if clone_install_running:
                self._json({"ok": True, "installing": True}); return
            clone_install_running = True
        if self._any_running():
            with clone_install_lock:
                clone_install_running = False
            self._json({"error": "レスキュー／クローン実行中はインストールできません"}); return
        if self._wipe_running():
            with clone_install_lock:
                clone_install_running = False
            self._json({"error": "ディスク消去実行中はインストールできません"}); return
        th = threading.Thread(target=_run_clone_install, daemon=True)
        th.start()
        self._json({"ok": True, "installing": True})

    def _handle_clone_start(self, data):
        global running_process, current_log_file, clone_job
        if self._any_running():
            self._json({"error": "既に実行中です"}); return
        if self._wipe_running():
            self._json({"error": "ディスク消去実行中はクローンできません"}); return
        st = get_clone_status()
        if not st["installed"]:
            self._json({"error": "Clonezilla がインストールされていません。先にインストールしてください"}); return
        source = (data.get("source") or "").strip()
        dest = (data.get("dest") or "").strip()
        source_type = data.get("source_type", "disk")
        dest_type = data.get("dest_type", "disk")
        # 旧フロントは 'disk'/'file' を送るため 'file' は 'image' とみなす
        if source_type not in ("disk", "image"):
            source_type = "image" if source_type == "file" else "disk"
        if dest_type not in ("disk", "image"):
            dest_type = "image" if dest_type == "file" else "disk"
        resize = bool(data.get("resize", True))
        if not source:
            self._json({"error": "コピー元を指定してください"}); return
        if not dest:
            self._json({"error": "コピー先を指定してください"}); return
        if source_type == "disk" and dest_type == "disk" and source == dest:
            self._json({"error": "コピー元とコピー先が同じです"}); return
        sys_disk = get_system_disk()
        # 最新デバイス一覧で検証
        devs = {d["path"]: d for d in get_clone_devices()}
        full_devs = {d["path"]: d for d in get_wipe_devices()}
        if source_type == "disk":
            if not WIPE_PATH_RE.match(source):
                self._json({"error": f"不正なデバイス指定です: {source}"}); return
            if source == sys_disk:
                self._json({"error": f"{source} はシステムドライブのため対象外です"}); return
            if source not in devs:
                if source in full_devs:
                    self._json({"error": f"{source} は使用中のため選択できません（マウント解除後に再試行）"}); return
                self._json({"error": f"デバイスが見つかりません: {source}"}); return
            if full_devs.get(source, {}).get("has_mount"):
                self._json({"error": f"{source} はマウント中のため実行できません", "unmountable": [source]}); return
            # ディスク全体にファイルシステムがある媒体（Live USB のハイブリッド ISO 等）は
            # Clonezilla が「パーティション」と判定してディスク間クローンを拒否するため事前に案内する
            src_fs = get_whole_disk_fstype(source)
            if src_fs:
                self._json({"error": f"{source} のディスク全体にファイルシステム ({src_fs}) があるため Clonezilla では複製できません"
                    "（Live USB 等の特殊形式）。このような媒体は「レスキュー」ページの ddrescue でセクタコピーしてください"}); return
        if dest_type == "disk":
            if not WIPE_PATH_RE.match(dest):
                self._json({"error": f"不正なデバイス指定です: {dest}"}); return
            if dest == sys_disk:
                self._json({"error": f"{dest} はシステムドライブのため対象外です"}); return
            if dest not in devs:
                if dest in full_devs:
                    self._json({"error": f"{dest} は使用中のため選択できません（マウント解除後に再試行）"}); return
                self._json({"error": f"デバイスが見つかりません: {dest}"}); return
            if full_devs.get(dest, {}).get("has_mount"):
                self._json({"error": f"{dest} はマウント中のため実行できません", "unmountable": [dest]}); return
        if source_type == "image" and dest_type == "image":
            self._json({"error": "イメージ→イメージの変換は未対応です"}); return
        # コピー先サイズの事前チェック（disk→disk のみ）
        if source_type == "disk" and dest_type == "disk":
            s_size = (devs.get(source) or {}).get("size_bytes") or get_device_size_bytes(source)
            d_size = (devs.get(dest) or {}).get("size_bytes") or get_device_size_bytes(dest)
            if s_size and d_size and d_size < s_size:
                self._json({"error": f"コピー先 ({dest}) がコピー元より小さいため実行できません"}); return
        # イメージ指定の検証
        src_img = dst_img = None
        if source_type == "image":
            src_img = _split_ocs_image(source)
            if not src_img[0] or not src_img[1]:
                self._json({"error": "イメージ指定が不正です（例: /backup/myimage）"}); return
            if not os.path.isdir(os.path.join(src_img[0], src_img[1])):
                self._json({"error": f"イメージが見つかりません: {source}"}); return
        if dest_type == "image":
            dst_img = _split_ocs_image(dest)
            if not dst_img[0] or not dst_img[1]:
                self._json({"error": "イメージ指定が不正です（例: /backup/myimage）"}); return
            if not os.path.isdir(dst_img[0]):
                self._json({"error": f"保存先ディレクトリが見つかりません: {dst_img[0]}"}); return
        # コマンド組み立て（使用中セクタのみ＝partclone ベースの Clonezilla 方式）
        if source_type == "disk" and dest_type == "disk":
            src_base = os.path.basename(source)
            dst_base = os.path.basename(dest)
            cmd = ["stdbuf", "-o0", "-e0", "ocs-onthefly",
                "--batch", "--nogui", "-e1", "auto", "-e2", "-j2", "-sfsck",
                "-k1" if resize else "-k0"]
            if resize:
                cmd.append("-r")
            cmd += ["-p", "true", "-f", src_base, "-d", dst_base]
            tag = f"{src_base}_to_{dst_base}"
        elif source_type == "disk" and dest_type == "image":
            src_base = os.path.basename(source)
            cmd = ["stdbuf", "-o0", "-e0", "ocs-sr",
                "--batch", "--nogui", "-q2", "-c", "-j2", "-z0", "-sfsck",
                "-p", "true", "-or", dst_img[0], "savedisk", dst_img[1], src_base]
            tag = f"{src_base}_to_img"
        else:  # image -> disk
            dst_base = os.path.basename(dest)
            cmd = ["stdbuf", "-o0", "-e0", "ocs-sr",
                "--batch", "--nogui", "-e1", "auto", "-e2", "-j2", "-sfsck",
                "-g", "auto", "-p", "true"]
            if resize:
                cmd += ["-r", "-k1"]
            else:
                cmd += ["-k0"]
            cmd += ["-or", src_img[0], "restoredisk", src_img[1], dst_base]
            tag = f"img_to_{dst_base}"
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_name = f"clone_{tag}_{timestamp}.log"
        current_log_file = os.path.join(LOG_DIR, log_name)
        # 進捗表示用メタ情報（disk→disk は保存＋復元の2工程/パーティション）
        if source_type == "disk" and dest_type == "disk":
            mode, parts = "disk2disk", [p.get("path", "") for p in (devs.get(source) or {}).get("partitions", [])]
            total_ops = 2 * max(1, len(parts))
        elif source_type == "disk":
            mode, parts = "disk2img", [p.get("path", "") for p in (devs.get(source) or {}).get("partitions", [])]
            total_ops = max(1, len(parts))
        else:
            mode, parts, total_ops = "img2disk", [], 0  # 復元のみ。総工程数は進行に応じて適応
        try:
            log_f = open(current_log_file, "w")
            log_f.write(f"=== clone started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            log_f.write(f"Command: {' '.join(cmd)}\n")
            log_f.write(f"source={source} ({source_type}) dest={dest} ({dest_type}) resize={resize}\n\n")
            log_f.flush()
            running_process = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
            log_f.close()
            _save_clone_job({"mode": mode, "parts": parts, "total_ops": total_ops,
                "started_at": time.time(), "log_file": log_name, "log_path": current_log_file,
                "pid": running_process.pid})
            self._json({"ok": True, "log_file": log_name, "pid": running_process.pid})
        except FileNotFoundError as e:
            self._json({"error": f"Clonezilla コマンドが見つかりません: {e}"})
        except Exception as e:
            self._json({"error": str(e)})

    def _handle_clone_unmount(self, data):
        """指定ディスク配下のマウントをすべて解除する（システムドライブは保護）"""
        if self._any_running():
            self._json({"error": "実行中はアンマウントできません"}); return
        if self._wipe_running():
            self._json({"error": "ディスク消去実行中はアンマウントできません"}); return
        targets_in = data.get("devices", [])
        if not targets_in:
            self._json({"error": "対象ディスクを指定してください"}); return
        sys_disk = get_system_disk()
        devs = {d["path"]: d for d in get_wipe_devices()}
        results = []
        all_ok = True
        for raw in targets_in:
            path = (raw or "").strip()
            if not WIPE_PATH_RE.match(path):
                results.append({"path": path, "ok": False, "error": f"不正なデバイス指定です: {path}"})
                all_ok = False
                continue
            if path == sys_disk:
                results.append({"path": path, "ok": False, "error": f"{path} はシステムドライブのため対象外です"})
                all_ok = False
                continue
            if path not in devs:
                results.append({"path": path, "ok": False, "error": f"デバイスが見つかりません: {path}"})
                all_ok = False
                continue
            mounts = get_disk_mountpoints(path)
            if not mounts:
                results.append({"path": path, "ok": True, "unmounted": [], "message": "マウントされていません"})
                continue
            unmounted = []
            err = ""
            # 深いマウントから順に解除（swap は swapoff）
            for dev, mp in sorted(mounts, key=lambda x: len(x[1]), reverse=True):
                try:
                    if mp.startswith("["):
                        r = subprocess.run(["swapoff", dev],
                            capture_output=True, text=True, timeout=30)
                    else:
                        r = subprocess.run(["umount", mp],
                            capture_output=True, text=True, timeout=30)
                    if r.returncode == 0:
                        unmounted.append(mp if not mp.startswith("[") else f"{dev}(swap)")
                    else:
                        err = ((r.stderr or r.stdout) or "").strip().split("\n")[0][:200]
                        break
                except Exception as e:
                    err = str(e)[:200]
                    break
            # 残存チェック
            remain = [mp for _, mp in get_disk_mountpoints(path)]
            if remain and not err:
                err = f"解除できませんでした: {', '.join(remain)}"
            if err:
                results.append({"path": path, "ok": False, "error": err, "unmounted": unmounted})
                all_ok = False
            else:
                results.append({"path": path, "ok": True, "unmounted": unmounted})
        self._json({"ok": all_ok, "results": results},
            200 if all_ok else 400)

    def _handle_part_install(self):
        global part_install_running
        st = get_part_tools()
        if st["installed"]:
            self._json({"ok": True, "already": True}); return
        with part_install_lock:
            if part_install_running:
                self._json({"ok": True, "installing": True}); return
            part_install_running = True
        if self._any_running():
            with part_install_lock:
                part_install_running = False
            self._json({"error": "レスキュー／クローン／コピー実行中はインストールできません"}); return
        if self._wipe_running():
            with part_install_lock:
                part_install_running = False
            self._json({"error": "ディスク消去実行中はインストールできません"}); return
        th = threading.Thread(target=_run_part_install, daemon=True)
        th.start()
        self._json({"ok": True, "installing": True})

    def _handle_part_unmount(self, data):
        """パーティション単体のマウント解除（swap は swapoff）。システムドライブは保護"""
        if self._any_running():
            self._json({"error": "実行中はアンマウントできません"}); return
        if self._wipe_running():
            self._json({"error": "ディスク消去実行中はアンマウントできません"}); return
        part = ((data.get("part") or data.get("path") or "")).strip()
        if not PART_DEV_RE.match(part):
            self._json({"error": f"不正なデバイス指定です: {part}"}); return
        disk = _part_parent_disk(part)
        if disk == get_system_disk():
            self._json({"error": f"{part} はシステムドライブのため対象外です"}); return
        mp = get_dev_mountpoint(part)
        if not mp:
            # swap の可能性を確認
            try:
                r = subprocess.run(["swapon", "--show=NAME", "--noheadings"],
                    capture_output=True, text=True, timeout=10)
                if part in (r.stdout or ""):
                    r2 = subprocess.run(["swapoff", part],
                        capture_output=True, text=True, timeout=30)
                    if r2.returncode == 0:
                        self._json({"ok": True, "message": f"{part} の swap を無効化しました"}); return
                    self._json({"error": f"swapoff 失敗: {((r2.stderr or r2.stdout) or '').strip().split(chr(10))[0][:200]}"}); return
            except Exception:
                pass
            self._json({"ok": True, "message": "マウントされていません"}); return
        if mp.startswith("["):
            r = subprocess.run(["swapoff", part], capture_output=True, text=True, timeout=30)
        else:
            r = subprocess.run(["umount", mp], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            self._json({"ok": True, "message": f"{mp} をアンマウントしました"}); return
        self._json({"error": f"アンマウント失敗: {((r.stderr or r.stdout) or '').strip().split(chr(10))[0][:200]}"})

    def _part_busy_guard(self):
        if self._any_running():
            return "レスキュー／クローン／コピー実行中はパーティション操作できません"
        if self._wipe_running():
            return "ディスク消去実行中はパーティション操作できません"
        return ""

    def _handle_part_delete(self, data):
        err = self._part_busy_guard()
        if err:
            self._json({"error": err}); return
        part = ((data.get("part") or data.get("path") or "")).strip()
        if not part:
            self._json({"error": "パーティションを指定してください"}); return
        res = part_delete(part)
        self._json(res, 200 if res.get("ok") else 400)

    def _handle_part_create(self, data):
        err = self._part_busy_guard()
        if err:
            self._json({"error": err}); return
        disk = (data.get("disk") or "").strip()
        fstype = (data.get("fstype") or "").strip()
        label = (data.get("label") or "").strip()
        table_type = (data.get("table") or data.get("table_type") or "").strip()
        if not disk:
            self._json({"error": "対象ディスクを指定してください"}); return
        if not fstype:
            self._json({"error": "ファイルシステムを指定してください"}); return
        # サイズは MiB または バイトのいずれかで受け付ける
        size_bytes = data.get("size_bytes")
        if size_bytes is None and data.get("size_mib") is not None:
            try:
                size_bytes = int(float(data.get("size_mib")) * MIB)
            except Exception:
                size_bytes = 0
        start_bytes = data.get("start_bytes")
        res = part_create(disk, fstype, size_bytes, label, start_bytes, table_type)
        self._json(res, 200 if res.get("ok") else 400)

    def _handle_part_mklabel(self, data):
        err = self._part_busy_guard()
        if err:
            self._json({"error": err}); return
        disk = (data.get("disk") or "").strip()
        table_type = (data.get("table") or data.get("table_type") or "").strip()
        if not disk:
            self._json({"error": "対象ディスクを指定してください"}); return
        res = part_mklabel(disk, table_type)
        self._json(res, 200 if res.get("ok") else 400)

    def _handle_part_dellabel(self, data):
        err = self._part_busy_guard()
        if err:
            self._json({"error": err}); return
        disk = (data.get("disk") or "").strip()
        if not disk:
            self._json({"error": "対象ディスクを指定してください"}); return
        res = part_dellabel(disk)
        self._json(res, 200 if res.get("ok") else 400)

    def _handle_part_resize(self, data):
        err = self._part_busy_guard()
        if err:
            self._json({"error": err}); return
        part = ((data.get("part") or data.get("path") or "")).strip()
        if not part:
            self._json({"error": "パーティションを指定してください"}); return
        new_size = data.get("new_size_bytes")
        if new_size is None and data.get("new_size_mib") is not None:
            try:
                new_size = int(float(data.get("new_size_mib")) * MIB)
            except Exception:
                new_size = 0
        res = part_resize(part, new_size)
        self._json(res, 200 if res.get("ok") else 400)

    def _handle_rsync_install(self):
        global rsync_install_running
        st = get_rsync_status()
        if st["installed"]:
            self._json({"ok": True, "already": True}); return
        with rsync_install_lock:
            if rsync_install_running:
                self._json({"ok": True, "installing": True}); return
            rsync_install_running = True
        if self._any_running():
            with rsync_install_lock:
                rsync_install_running = False
            self._json({"error": "レスキュー／クローン／コピー実行中はインストールできません"}); return
        if self._wipe_running():
            with rsync_install_lock:
                rsync_install_running = False
            self._json({"error": "ディスク消去実行中はインストールできません"}); return
        th = threading.Thread(target=_run_rsync_install, daemon=True)
        th.start()
        self._json({"ok": True, "installing": True})

    def _handle_rsync_mount(self, data):
        if self._any_running():
            self._json({"error": "実行中はマウント操作できません"}); return
        side = (data.get("side") or "src").strip()
        path = (data.get("path") or "").strip()
        if side not in ("src", "dst"):
            self._json({"error": "side が不正です"}); return
        if not path:
            self._json({"error": "デバイスを指定してください"}); return
        force = bool(data.get("force"))
        res = mount_rsync_device(side, path, force=force)
        self._json(res, 200 if res.get("ok") else 400)

    def _handle_rsync_mount_image(self, data):
        if self._any_running():
            self._json({"error": "実行中はマウント操作できません"}); return
        image = (data.get("image") or data.get("path") or "").strip()
        if not image:
            self._json({"error": "イメージファイルを指定してください"}); return
        res = mount_rsync_image("src", image)
        self._json(res, 200 if res.get("ok") else 400)

    def _handle_rsync_unmount(self, data):
        if self._any_running():
            self._json({"error": "実行中はアンマウントできません"}); return
        side = (data.get("side") or "src").strip()
        if side not in ("src", "dst"):
            self._json({"error": "side が不正です"}); return
        res = unmount_rsync_side(side)
        self._json(res, 200 if res.get("ok") else 400)

    def _handle_rsync_mkdir(self, data):
        if self._any_running():
            self._json({"error": "実行中は作成できません"}); return
        side = (data.get("side") or "dst").strip()
        if side != "dst":
            self._json({"error": "作成はコピー先のみ対応しています"}); return
        path = (data.get("path") or "").strip()
        if not path or not path.startswith("/"):
            self._json({"error": "コピー先フォルダは絶対パスで指定してください"}); return
        roots = _rsync_allowed_roots("dst")
        if not roots:
            self._json({"error": "先にコピー先ドライブをマウントしてください"}); return
        real = os.path.realpath(path)
        if not any(real == r or real.startswith(r + os.sep) for r in roots):
            self._json({"error": f"マウント外のパスは作成できません（マウント点: {', '.join(roots)}）"}); return
        try:
            os.makedirs(real, exist_ok=True)
        except Exception as e:
            self._json({"error": f"作成失敗: {e}"}); return
        self._json({"ok": True, "path": real})

    def _handle_rsync_start(self, data):
        global running_process, current_log_file
        if self._any_running():
            self._json({"error": "既に実行中です"}); return
        if self._wipe_running():
            self._json({"error": "ディスク消去実行中はコピーできません"}); return
        if shutil.which("rsync") is None:
            self._json({"error": "rsync がインストールされていません。先にインストールしてください"}); return
        src_base_in = (data.get("src_base") or "").strip()
        dst_base_in = (data.get("dst_base") or "").strip()
        items = data.get("items") or []
        opts = data.get("options") or {}
        if not src_base_in:
            self._json({"error": "コピー元フォルダを指定してください"}); return
        if not dst_base_in:
            self._json({"error": "コピー先フォルダを指定してください"}); return
        if not isinstance(items, list) or not items:
            self._json({"error": "コピーするフォルダ・ファイルを1つ以上選択してください"}); return
        src_base, err = _rsync_resolve_base("src", src_base_in, must_exist=True)
        if err:
            self._json({"error": f"コピー元フォルダ: {err}"}); return
        # コピー先は無ければ作成する
        dst_roots = _rsync_allowed_roots("dst")
        if not dst_roots:
            self._json({"error": "先にコピー先ドライブをマウントしてください"}); return
        dst_real = os.path.realpath(dst_base_in)
        if not dst_base_in.startswith("/"):
            self._json({"error": "コピー先フォルダは絶対パスで指定してください"}); return
        if not any(dst_real == r or dst_real.startswith(r + os.sep) for r in dst_roots):
            self._json({"error": f"コピー先はマウント内を指定してください（マウント点: {', '.join(dst_roots)}）"}); return
        # コピー元とコピー先が同一フォルダの場合は拒否
        if os.path.realpath(src_base) == dst_real:
            self._json({"error": "コピー元とコピー先が同じフォルダです"}); return
        # コピー先がコピー元配下／逆の包含関係は誤コピー防止のため拒否
        if dst_real.startswith(os.path.realpath(src_base) + os.sep):
            # 同一マウント内の別フォルダは通常あり得るが、無限再帰の恐れがあるため確認済み扱いでも拒否しない。
            # ここでは許可する（rsync の典型的な使い方のため）
            pass
        # 選択項目の検証
        clean_items = []
        seen = set()
        for raw in items:
            name = (raw or "").strip()
            if not name or name in seen:
                continue
            if os.path.isabs(name) or ".." in name.split("/"):
                self._json({"error": f"不正な選択項目です: {name}"}); return
            full = os.path.realpath(os.path.join(src_base, name))
            allowed_root = os.path.realpath(src_base)
            if not (full == allowed_root or full.startswith(allowed_root + os.sep)):
                self._json({"error": f"不正な選択項目です: {name}"}); return
            if not os.path.exists(full):
                self._json({"error": f"見つかりません: {name}"}); return
            seen.add(name)
            clean_items.append(name)
        if not clean_items:
            self._json({"error": "コピーするフォルダ・ファイルを1つ以上選択してください"}); return
        if len(clean_items) > 2000:
            self._json({"error": "選択項目が多すぎます（2000件まで）"}); return
        # コピー先フォルダは無ければ作成
        created = False
        try:
            if not os.path.exists(dst_real):
                os.makedirs(dst_real, exist_ok=True)
                created = True
            if not os.path.isdir(dst_real):
                self._json({"error": f"コピー先がフォルダではありません: {dst_base_in}"}); return
        except Exception as e:
            self._json({"error": f"コピー先フォルダを作成できません: {e}"}); return
        # 進捗推定の基準となる合計容量・ファイル数を概算する（参考値。失敗してもコピーは続行）
        recursive = bool(opts.get("recursive", True))
        try:
            total_bytes, total_files = _estimate_rsync_total(src_base, clean_items, recursive)
        except Exception:
            total_bytes, total_files = 0, 0
        # コマンド組み立て（進捗表示・詳細出力は常に有効。-r/-t/-u のみ切替）
        cmd = ["stdbuf", "-o0", "-e0", "rsync", "-v", "-h", "--info=progress2", "--stats"]
        if opts.get("recursive", True):
            cmd.append("-r")
        if opts.get("timestamps", True):
            cmd.append("-t")
        if opts.get("update", True):
            cmd.append("-u")
        srcs = [os.path.join(src_base, n) for n in clean_items]
        cmd += srcs + [dst_real]
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_name = f"rsync_{timestamp}.log"
        current_log_file = os.path.join(LOG_DIR, log_name)
        try:
            log_f = open(current_log_file, "w")
            log_f.write(f"=== rsync started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            log_f.write(f"Command: {' '.join(shlex.quote(c) for c in cmd)}\n")
            log_f.write(f"src_base={src_base} dst_base={dst_real} items={len(clean_items)} "
                f"recursive={recursive} "
                f"timestamps={bool(opts.get('timestamps', True))} "
                f"update={bool(opts.get('update', True))} created_dst={created} "
                f"total_bytes={total_bytes} ({_fmt_bytes(total_bytes)}) total_files={total_files}\n\n")
            log_f.flush()
            running_process = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
            log_f.close()
            _save_rsync_job({"src_base": src_base, "dst_base": dst_real,
                "items": clean_items, "options": {
                    "recursive": recursive,
                    "timestamps": bool(opts.get("timestamps", True)),
                    "update": bool(opts.get("update", True))},
                "total_bytes": total_bytes, "total_files": total_files,
                "started_at": time.time(), "log_file": log_name, "log_path": current_log_file,
                "pid": running_process.pid})
            self._json({"ok": True, "log_file": log_name, "pid": running_process.pid,
                "created_dst": created})
        except FileNotFoundError:
            self._json({"error": "rsync コマンドが見つかりません"})
        except Exception as e:
            self._json({"error": str(e)})

    def _sse_send(self, text):
        escaped = text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
        self.wfile.write(f"data: {escaped}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _stream_log(self, filepath):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        interval = 0.5
        try:
            with open(filepath, "r", errors="replace") as f:
                f.seek(0, 2)
                size = f.tell()
                start = max(0, size - 4096)
                f.seek(start)
                pending = ""
                last_send = 0.0
                initial = f.read()
                if start > 0:
                    nl = initial.find("\n")
                    initial = initial[nl + 1:] if nl != -1 else ""
                if initial:
                    self._sse_send(initial)
                    last_send = time.time()
                while True:
                    chunk = f.read(8192)
                    if chunk:
                        pending += chunk
                    alive = running_process is not None and running_process.poll() is None
                    now = time.time()
                    if pending and (not alive or now - last_send >= interval):
                        self._sse_send(pending)
                        pending = ""
                        last_send = now
                    if not alive:
                        break
                    time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError): pass

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *a): pass

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print(f"Disk Manager running on port {PORT}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        if running_process and running_process.poll() is None:
            os.killpg(os.getpgid(running_process.pid), signal.SIGTERM)
        server.server_close()
