from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import re
import runpy
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import builtins
from builtins import open as builtin_open
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from functools import lru_cache
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import zstandard as zstd


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
HOST = "127.0.0.1"
PORT = 8876
IDLE_EXIT_SECONDS = 120
LOCAL_VENDOR = ROOT / "vendor" / "wechat-decrypt"
SHARED_VENDOR = ROOT.parent / "vendor" / "wechat-decrypt"
VENDOR_ROOT = LOCAL_VENDOR if LOCAL_VENDOR.exists() else SHARED_VENDOR
VENDOR_CONFIG = VENDOR_ROOT / "config.json"
DECRYPTED_DIR = VENDOR_ROOT / "decrypted"
LAST_ACTIVITY_AT = time.time()
HAS_CLIENT_ACTIVITY = False
MEMORY_KEYS_RESULT: dict[str, Any] | None = None


def mark_activity() -> None:
    global LAST_ACTIVITY_AT, HAS_CLIENT_ACTIVITY
    LAST_ACTIVITY_AT = time.time()
    HAS_CLIENT_ACTIVITY = True


def start_idle_shutdown_watch(server: ThreadingHTTPServer) -> None:
    def watch() -> None:
        while True:
            time.sleep(5)
            if not HAS_CLIENT_ACTIVITY:
                continue
            if time.time() - LAST_ACTIVITY_AT < IDLE_EXIT_SECONDS:
                continue
            print(f"No browser activity for {IDLE_EXIT_SECONDS} seconds, shutting down.")
            server.shutdown()
            return

    threading.Thread(target=watch, name="idle-shutdown-watch", daemon=True).start()


def get_home() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home())))


def find_xwechat_db_dirs() -> list[Path]:
    appdata = os.environ.get("APPDATA", "")
    config_dir = Path(appdata) / "Tencent" / "xwechat" / "config"
    if not config_dir.exists():
        return []

    roots: list[Path] = []
    seen: set[str] = set()
    for ini_file in glob.glob(str(config_dir / "*.ini")):
        try:
            content = Path(ini_file).read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            try:
                content = Path(ini_file).read_text(encoding="gbk").strip()
            except Exception:
                continue
        except Exception:
            continue

        if not content:
            continue

        root = Path(content) / "xwechat_files"
        if not root.exists():
            continue

        for child in sorted(root.iterdir()):
            db_dir = child / "db_storage"
            if db_dir.exists():
                normalized = str(db_dir).lower()
                if normalized not in seen:
                    seen.add(normalized)
                    roots.append(db_dir)
    return roots


def find_wechat_msg_dirs() -> list[Path]:
    base = get_home() / "Documents" / "WeChat Files"
    if not base.exists():
        return []

    ignored = {"All Users", "Applet", "WMPF"}
    candidates: list[Path] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name in ignored:
            continue
        msg_dir = child / "Msg"
        if msg_dir.exists():
            candidates.append(msg_dir)
    return candidates


def describe_accounts() -> list[dict[str, Any]]:
    results = []
    all_candidates: list[tuple[str, Path, str]] = []
    all_candidates.extend(("xwechat_db", path, path.parent.name) for path in find_xwechat_db_dirs())
    all_candidates.extend(("legacy_msg", path, path.parent.name) for path in find_wechat_msg_dirs())

    for index, (kind, target_dir, label) in enumerate(all_candidates, start=1):
        try:
            modified_at = target_dir.stat().st_mtime
        except OSError:
            modified_at = 0
        results.append(
            {
                "index": index,
                "label": label,
                "kind": kind,
                "target_dir": str(target_dir),
                "modified_at": modified_at,
            }
        )
    results.sort(key=lambda item: item["modified_at"], reverse=True)
    for index, item in enumerate(results, start=1):
        item["index"] = index
        item["display_name"] = f"微信账号 {index}"
        item["display_hint"] = "当前登录微信" if index == 1 else "可选账号"
        item["recommended"] = index == 1
        item["type_label"] = "新版微信目录" if item["kind"] == "xwechat_db" else "旧版微信目录"
    return results


def pick_account(index: int) -> Path:
    accounts = describe_accounts()
    if not accounts:
        raise RuntimeError("没有检测到微信账号目录")
    if not 1 <= index <= len(accounts):
        raise RuntimeError(f"账号序号超出范围，可选 1-{len(accounts)}")
    return Path(accounts[index - 1]["target_dir"])


def pick_best_account() -> Path:
    accounts = describe_accounts()
    if not accounts:
        raise RuntimeError("没有自动找到微信聊天记录目录。你可以手动输入目录，或者先登录微信再试一次。")
    return Path(accounts[0]["target_dir"])


def normalize_manual_dir(raw_path: str) -> Path:
    path = Path(str(raw_path or "").strip().strip('"')).expanduser()
    if not str(path):
        raise RuntimeError("请先输入微信聊天记录目录")
    if not path.exists():
        raise RuntimeError(f"目录不存在：{path}")
    return path


def resolve_target_dir(index: int | None = None, manual_dir: str = "") -> tuple[Path, str]:
    if manual_dir.strip():
        return normalize_manual_dir(manual_dir), "manual"
    if index is not None:
        return pick_account(index), "selected"
    return pick_best_account(), "auto"


def ensure_vendor_config(msg_dir: Path) -> dict[str, Any]:
    config = {
        "db_dir": str(msg_dir),
        "keys_file": "all_keys.json",
        "decrypted_dir": "decrypted",
        "decoded_image_dir": "decoded_images",
        "wechat_process": "Weixin.exe",
    }
    VENDOR_CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return config


def cleanup_sensitive_artifacts(clear_memory_keys: bool = True) -> None:
    try:
        if DECRYPTED_DIR.exists():
            shutil.rmtree(DECRYPTED_DIR, ignore_errors=True)
    except Exception:
        pass
    for filename in ("all_keys.json", "config.json"):
        try:
            file_path = VENDOR_ROOT / filename
            if file_path.exists():
                file_path.unlink()
        except Exception:
            pass
    if clear_memory_keys:
        global MEMORY_KEYS_RESULT
        MEMORY_KEYS_RESULT = None


def vendor_ready() -> list[str]:
    problems: list[str] = []
    if not VENDOR_ROOT.exists():
        problems.append("缺少 wechat-decrypt 目录")
    try:
        import Crypto  # noqa: F401
    except Exception:
        problems.append("缺少 pycryptodome")
    return problems


def run_vendor_script(script_name: str) -> dict[str, Any]:
    if not VENDOR_ROOT.exists():
        return {"ok": False, "stdout": "", "stderr": "缺少解密器目录"}
    if script_name in {"find_all_keys.py", "decrypt_db.py"}:
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
            returncode = run_vendor_script_inline(script_name)
        if script_name == "decrypt_db.py":
            get_message_table_index.cache_clear()
        return {
            "ok": returncode == 0,
            "stdout": stdout_buffer.getvalue(),
            "stderr": stderr_buffer.getvalue(),
            "returncode": returncode,
        }
    command = [os.environ.get("PYTHON", "python"), script_name]
    completed = subprocess.run(
        command,
        cwd=VENDOR_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if script_name == "decrypt_db.py":
        get_message_table_index.cache_clear()
    return {
        "ok": completed.returncode == 0,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "returncode": completed.returncode,
    }


def run_vendor_script_inline(script_name: str) -> int:
    global MEMORY_KEYS_RESULT
    script_path = VENDOR_ROOT / script_name
    if not script_path.exists():
        print(f"[ERROR] 缺少脚本: {script_name}")
        return 1

    previous_cwd = Path.cwd()
    previous_path = list(sys.path)
    previous_env_path = os.environ.get("PATH", "")
    try:
        os.chdir(VENDOR_ROOT)
        if str(VENDOR_ROOT) not in sys.path:
            sys.path.insert(0, str(VENDOR_ROOT))
        system32 = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32")
        if system32.lower() not in previous_env_path.lower():
            os.environ["PATH"] = system32 + os.pathsep + previous_env_path
        try:
            if script_name == "find_all_keys.py":
                cleanup_sensitive_artifacts(clear_memory_keys=False)
                import importlib

                key_scan_common = importlib.import_module("key_scan_common")
                original_save_results = key_scan_common.save_results

                def save_results_in_memory(db_files, salt_to_dbs, key_map, db_dir, out_file, print_fn):
                    global MEMORY_KEYS_RESULT
                    result = {}
                    print_fn(f"\n{'=' * 60}")
                    print_fn(f"结果: {len(key_map)}/{len(salt_to_dbs)} salts 找到密钥")
                    for rel, path, sz, salt_hex, page1 in db_files:
                        if salt_hex in key_map:
                            result[rel] = {
                                "enc_key": key_map[salt_hex],
                                "salt": salt_hex,
                                "size_mb": round(sz / 1024 / 1024, 1),
                            }
                            print_fn(f"  OK: {rel} ({sz / 1024 / 1024:.1f}MB)")
                        else:
                            print_fn(f"  MISSING: {rel} (salt={salt_hex})")
                    if not result:
                        print_fn(f"\n[!] 未提取到任何密钥")
                        raise RuntimeError("未能从任何微信进程中提取到密钥")
                    result["_db_dir"] = db_dir
                    MEMORY_KEYS_RESULT = result
                    print_fn("\n密钥已暂存在程序内存中")

                key_scan_common.save_results = save_results_in_memory
                try:
                    runpy.run_path(str(script_path), run_name="__main__")
                finally:
                    key_scan_common.save_results = original_save_results
                return 0

            if script_name == "decrypt_db.py":
                if not MEMORY_KEYS_RESULT:
                    print("[ERROR] 内存中没有可用的密钥，请先运行解析")
                    return 1

                original_exists = os.path.exists
                keys_filename = "all_keys.json"
                keys_payload = json.dumps(MEMORY_KEYS_RESULT, ensure_ascii=False)

                def fake_exists(path):
                    path_str = os.fspath(path)
                    if path_str == keys_filename or Path(path_str).name == keys_filename:
                        return True
                    return original_exists(path)

                def fake_open(path, *args, **kwargs):
                    path_str = os.fspath(path)
                    if path_str == keys_filename or Path(path_str).name == keys_filename:
                        return io.StringIO(keys_payload)
                    return builtin_open(path, *args, **kwargs)

                os.path.exists = fake_exists
                builtins.open = fake_open
                try:
                    runpy.run_path(str(script_path), run_name="__main__")
                finally:
                    os.path.exists = original_exists
                    builtins.open = builtin_open
                return 0

            runpy.run_path(str(script_path), run_name="__main__")
            return 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
            return code
        except Exception:
            traceback.print_exc()
            return 1
    finally:
        os.chdir(previous_cwd)
        sys.path[:] = previous_path
        os.environ["PATH"] = previous_env_path


def runtime_status() -> dict[str, Any]:
    problems = vendor_ready()
    cfg = {}
    if VENDOR_CONFIG.exists():
        cfg = json.loads(VENDOR_CONFIG.read_text(encoding="utf-8"))
    accounts = describe_accounts()
    return {
        "vendor_root": str(VENDOR_ROOT),
        "vendor_exists": VENDOR_ROOT.exists(),
        "decrypted_dir": str(DECRYPTED_DIR),
        "config": cfg,
        "problems": problems,
        "decrypted_ready": get_wechat_decrypted_status()["ready"],
        "account_count": len(accounts),
    }


def read_vendor_config() -> dict[str, Any]:
    if not VENDOR_CONFIG.exists():
        return {}
    return json.loads(VENDOR_CONFIG.read_text(encoding="utf-8"))


def get_self_username() -> str:
    cfg = read_vendor_config()
    db_dir = cfg.get("db_dir", "")
    if not db_dir:
        return ""
    account_dir = Path(db_dir).parent.name
    match = re.fullmatch(r"(.+)_([0-9a-fA-F]{4,})", account_dir)
    return match.group(1) if match else account_dir


def get_decrypted_contact_db() -> Path | None:
    path = DECRYPTED_DIR / "contact" / "contact.db"
    return path if path.exists() else None


def get_decrypted_message_dbs() -> list[Path]:
    message_dir = DECRYPTED_DIR / "message"
    if not message_dir.exists():
        return []
    return sorted(message_dir.glob("message_*.db"))


@lru_cache(maxsize=1)
def get_message_table_index() -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for db_path in get_decrypted_message_dbs():
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "select name from sqlite_master where type='table' and name like 'Msg_%'"
            ).fetchall()
            for (table_name,) in rows:
                index.setdefault(table_name, []).append(str(db_path))
        finally:
            conn.close()
    return index


def get_wechat_decrypted_status() -> dict[str, Any]:
    contact_db = get_decrypted_contact_db()
    message_dbs = get_decrypted_message_dbs()
    return {
        "ready": bool(contact_db and message_dbs),
        "contact_db": str(contact_db) if contact_db else "",
        "message_db_count": len(message_dbs),
        "self_username": get_self_username(),
    }


def load_contacts() -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    contact_db = get_decrypted_contact_db()
    if not contact_db:
        return {}, {}

    conn = sqlite3.connect(str(contact_db))
    try:
        rows = conn.execute(
            """
            select username, alias, remark, nick_name
            from contact
            where username is not null and username <> ''
            """
        ).fetchall()
    finally:
        conn.close()

    names: dict[str, str] = {}
    details: dict[str, dict[str, Any]] = {}
    for username, alias, remark, nick_name in rows:
        display = remark or nick_name or alias or username
        names[username] = display
        details[username] = {
            "username": username,
            "display_name": display,
            "remark": remark or "",
            "nick_name": nick_name or "",
        }
    return names, details


def find_msg_dbs_for_user(username: str) -> tuple[list[Path], str | None]:
    table_name = f"Msg_{hashlib.md5(username.encode('utf-8')).hexdigest()}"
    db_paths = [Path(item) for item in (get_message_table_index().get(table_name) or [])]
    return db_paths, table_name if db_paths else None


def load_name2id_map(conn: sqlite3.Connection) -> dict[int, str]:
    try:
        rows = conn.execute("select rowid, user_name from Name2Id").fetchall()
    except sqlite3.Error:
        return {}
    return {rowid: username for rowid, username in rows if username}


_zstd_dctx = zstd.ZstdDecompressor()


def decompress_content(content: Any, compression_type: Any) -> str | bytes | None:
    if compression_type == 4 and isinstance(content, bytes):
        try:
            return _zstd_dctx.decompress(content).decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(content, bytes):
        try:
            return content.decode("utf-8", errors="replace")
        except Exception:
            return content
    return content


def parse_message_content(content: Any, is_group: bool) -> tuple[str, str]:
    if content is None:
        return "", ""
    if isinstance(content, bytes):
        return "", "(二进制内容)"
    sender = ""
    text = content
    if is_group and ":\n" in content:
        sender, text = content.split(":\n", 1)
    return sender, text


def resolve_sender_role(real_sender_id: int, sender_from_content: str, is_group: bool, chat_username: str, id_to_username: dict[int, str]) -> str:
    sender_username = id_to_username.get(real_sender_id, "")
    self_username = get_self_username()
    if is_group:
        if sender_username == self_username or sender_from_content == self_username:
            return "self"
        return "other"
    if sender_username == self_username:
        return "self"
    if sender_username == chat_username:
        return "other"
    return "unknown"


def format_sender_label(real_sender_id: int, sender_from_content: str, is_group: bool, chat_username: str, chat_display_name: str, names: dict[str, str], id_to_username: dict[int, str]) -> str:
    sender_username = id_to_username.get(real_sender_id, "")
    self_username = get_self_username()
    if is_group:
        if sender_username and sender_username != chat_username:
            return "我" if sender_username == self_username else names.get(sender_username, sender_username)
        if sender_from_content:
            return "我" if sender_from_content == self_username else names.get(sender_from_content, sender_from_content)
        return ""
    if sender_username == chat_username:
        return chat_display_name
    if sender_username == self_username:
        return "我"
    return names.get(sender_username, sender_username) if sender_username else ""


def load_wechat_messages(username: str) -> list[dict[str, Any]]:
    names, _ = load_contacts()
    chat_display_name = names.get(username, username)
    is_group = "@chatroom" in username
    db_paths, table_name = find_msg_dbs_for_user(username)
    if not db_paths or not table_name:
        return []

    messages: list[dict[str, Any]] = []
    seen_keys: set[tuple[int, int, int]] = set()

    for db_path in db_paths:
        conn = sqlite3.connect(str(db_path))
        try:
            id_to_username = load_name2id_map(conn)
            rows = conn.execute(
                f"""
                select local_id, local_type, create_time, real_sender_id, message_content, WCDB_CT_message_content
                from [{table_name}]
                order by create_time asc
                """
            ).fetchall()
        finally:
            conn.close()

        for local_id, local_type, create_time, real_sender_id, content, compression_type in rows:
            dedupe_key = (int(create_time or 0), int(real_sender_id or 0), int(local_id or 0))
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            content = decompress_content(content, compression_type)
            sender_from_content, text = parse_message_content(content, is_group)
            sender_role = resolve_sender_role(real_sender_id, sender_from_content, is_group, username, id_to_username)
            if sender_role not in {"self", "other"}:
                continue
            messages.append(
                {
                    "local_id": int(local_id or 0),
                    "timestamp": int(create_time or 0),
                    "sender_role": sender_role,
                    "sender_label": format_sender_label(
                        real_sender_id, sender_from_content, is_group, username, chat_display_name, names, id_to_username
                    ) or ("我" if sender_role == "self" else chat_display_name),
                    "text": (text or "").strip() if isinstance(text, str) else "(二进制内容)",
                    "local_type": int(local_type or 0),
                }
            )

    messages.sort(key=lambda item: (item["timestamp"], item["local_id"]))
    return messages


def is_ignorable_message(text: str) -> bool:
    text = str(text or "").strip()
    if not text:
        return True
    return any(rule in text for rule in ["撤回了一条消息", "你撤回了一条消息", "系统消息"])


DEFAULT_GAP_HOURS = 6.0


def analyze_contact(username: str, gap_hours: float = DEFAULT_GAP_HOURS) -> dict[str, Any]:
    names, _ = load_contacts()
    display_name = names.get(username, username)
    messages = [item for item in load_wechat_messages(username) if not is_ignorable_message(item["text"])]
    if not messages:
        return {"ready": False, "display_name": display_name, "sessions": []}

    gap_seconds = gap_hours * 3600
    sessions: list[dict[str, Any]] = []
    current_session: dict[str, Any] | None = None

    for message in messages:
        ts = message["timestamp"]
        dt = datetime.fromtimestamp(ts)
        if current_session is None:
            current_session = {
                "start_timestamp": ts,
                "first_sender_role": message["sender_role"],
                "first_sender_label": message["sender_label"],
                "first_text": message["text"][:120],
                "messages": [{
                    "timestamp": ts,
                    "sender_role": message["sender_role"],
                    "sender_label": message["sender_label"],
                    "text": message["text"],
                }],
                "last_timestamp": ts,
                "last_date": dt.date().isoformat(),
            }
            continue

        prev_ts = current_session["last_timestamp"]
        gap_break = ts - prev_ts > gap_seconds
        if gap_break:
            sessions.append(current_session)
            current_session = {
                "start_timestamp": ts,
                "first_sender_role": message["sender_role"],
                "first_sender_label": message["sender_label"],
                "first_text": message["text"][:120],
                "messages": [{
                    "timestamp": ts,
                    "sender_role": message["sender_role"],
                    "sender_label": message["sender_label"],
                    "text": message["text"],
                }],
                "last_timestamp": ts,
                "last_date": dt.date().isoformat(),
            }
            continue

        current_session["messages"].append({
            "timestamp": ts,
            "sender_role": message["sender_role"],
            "sender_label": message["sender_label"],
            "text": message["text"],
        })
        current_session["last_timestamp"] = ts
        current_session["last_date"] = dt.date().isoformat()

    if current_session:
        sessions.append(current_session)

    self_starts = sum(1 for item in sessions if item["first_sender_role"] == "self")
    other_starts = sum(1 for item in sessions if item["first_sender_role"] == "other")
    return {
        "ready": True,
        "display_name": display_name,
        "message_count": len(messages),
        "session_count": len(sessions),
        "self_starts": self_starts,
        "other_starts": other_starts,
        "range": {
            "from": datetime.fromtimestamp(messages[0]["timestamp"]).isoformat(timespec="seconds"),
            "to": datetime.fromtimestamp(messages[-1]["timestamp"]).isoformat(timespec="seconds"),
        },
        "sessions": sessions,
    }


def list_contacts() -> list[dict[str, Any]]:
    names, details = load_contacts()
    contacts = []
    for username, display_name in names.items():
        if "@chatroom" in username:
            continue
        db_paths, table_name = find_msg_dbs_for_user(username)
        if not db_paths or not table_name:
            continue
        contacts.append(
            {
                "username": username,
                "display_name": display_name,
                "remark": details[username]["remark"],
                "nick_name": details[username]["nick_name"],
            }
        )
    contacts.sort(key=lambda item: item["display_name"].lower())
    return contacts


def auto_parse(index: int | None = None, manual_dir: str = "") -> dict[str, Any]:
    problems = vendor_ready()
    if problems:
        return {
            "ok": False,
            "error": "程序运行环境还没准备好",
            "tips": ["先运行 install_deps.bat，把需要的依赖装好。"],
        }

    try:
        target_dir, source = resolve_target_dir(index=index, manual_dir=manual_dir)
        config = ensure_vendor_config(target_dir)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "tips": [
                "先打开并登录微信。",
                "再点一次“一键解析”。",
                "如果还是找不到，就手动输入微信聊天记录目录。"
            ],
        }

    logs: list[str] = [
        "已找到可用的微信聊天记录目录。",
        "正在读取当前登录微信的数据。",
    ]
    if source == "manual":
        logs.insert(0, "这次使用的是你手动提供的目录。")

    key_result = run_vendor_script("find_all_keys.py")
    if not key_result.get("ok"):
        return {
            "ok": False,
            "error": "解析失败",
            "log": "\n".join(item for item in logs if item),
            "tips": [
                "请先打开并登录微信后再试。",
                "如果刚登录微信，等几秒再点一次。",
                "必要时重新打开微信后重试。"
            ],
            "config": config,
        }

    decrypt_result = run_vendor_script("decrypt_db.py")
    if not decrypt_result.get("ok"):
        return {
            "ok": False,
            "error": "解析失败",
            "log": "\n".join(item for item in logs if item),
            "tips": [
                "请先确认微信已经登录。",
                "如果微信刚同步了聊天记录，再点一次试试。",
                "必要时重新打开微信后重试。"
            ],
            "config": config,
        }

    contacts = list_contacts()
    logs.extend([
        "聊天记录已经解析完成。",
        f"已准备好 {len(contacts)} 个联系人。",
    ])
    return {
        "ok": True,
        "log": "\n".join(item for item in logs if item),
        "config": config,
        "contact_count": len(contacts),
        "contacts_ready": bool(contacts),
        "tips": ["解析完成，下面已经可以直接选择联系人了。"],
    }


class AppHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        mark_activity()
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/health":
            return self.send_json({"ok": True, "host": HOST, "port": PORT})
        if parsed.path == "/api/wechat/accounts":
            return self.send_json({"items": describe_accounts()})
        if parsed.path == "/api/wechat/runtime":
            return self.send_json(runtime_status())
        if parsed.path == "/api/wechat/contacts":
            return self.send_json({"items": list_contacts()})
        if parsed.path == "/api/wechat/analyze":
            username = (query.get("username") or [""])[0]
            return self.send_json(analyze_contact(username, DEFAULT_GAP_HOURS))
        return super().do_GET()

    def do_POST(self) -> None:
        mark_activity()
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        index_raw = (query.get("index") or [""])[0].strip()
        manual_dir = (query.get("manual_dir") or [""])[0]
        if parsed.path == "/api/wechat/configure":
            index = int((query.get("index") or ["1"])[0])
            try:
                msg_dir = pick_account(index)
                return self.send_json({"ok": True, "config": ensure_vendor_config(msg_dir)})
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/wechat/keys":
            index = int((query.get("index") or ["1"])[0])
            try:
                ensure_vendor_config(pick_account(index))
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return self.send_json(run_vendor_script("find_all_keys.py"))
        if parsed.path == "/api/wechat/decrypt":
            index = int((query.get("index") or ["1"])[0])
            try:
                ensure_vendor_config(pick_account(index))
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return self.send_json(run_vendor_script("decrypt_db.py"))
        if parsed.path == "/api/wechat/parse":
            index = int(index_raw) if index_raw.isdigit() else None
            return self.send_json(auto_parse(index=index, manual_dir=manual_dir))
        self.send_json({"ok": False, "error": "unknown route"}, HTTPStatus.NOT_FOUND)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    os.chdir(ROOT)
    cleanup_sensitive_artifacts(clear_memory_keys=True)
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    start_idle_shutdown_watch(server)
    print(f"WeChat easy analyzer running at http://{HOST}:{PORT}")
    threading.Timer(0.8, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    try:
        server.serve_forever()
    finally:
        cleanup_sensitive_artifacts(clear_memory_keys=True)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-vendor-script":
        sys.exit(run_vendor_script_inline(sys.argv[2]))
    main()
