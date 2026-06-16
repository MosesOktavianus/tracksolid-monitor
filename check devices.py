"""
TrackSolidPro Device Monitor
Login ke TrackSolidPro, ambil semua device, deteksi yang offline > OFFLINE_THRESHOLD_HOURS jam.
Hasil disimpan ke devices.json (untuk web list) dan dikirim email kalau ada perubahan status.

Cara pakai:
- Set environment variables: TSP_ACCOUNT, TSP_PASSWORD, dan (kalau pakai email) EMAIL_* vars
- python check_devices.py
"""

import os
import json
import hashlib
import requests
from datetime import datetime, timezone, timedelta

BASE_URL = "https://www.tracksolidpro.com"
LOGIN_URL = f"{BASE_URL}/v3/new/newHomepage/login"
DEVICE_LIST_URL = f"{BASE_URL}/v3/new/newEquipment/queryEquipmentList"

OFFLINE_THRESHOLD_HOURS = 12

DATA_FILE = "devices.json"
PREVIOUS_FILE = "devices_previous.json"

# ---------- Konfigurasi dari environment variables ----------
ACCOUNT = os.environ.get("TSP_ACCOUNT")
PASSWORD = os.environ.get("TSP_PASSWORD")

if not ACCOUNT or not PASSWORD:
    raise SystemExit("ERROR: set environment variable TSP_ACCOUNT dan TSP_PASSWORD dulu.")


def md5_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def login(session: requests.Session) -> None:
    """Login dan biarkan session menyimpan cookie JSESSIONID otomatis."""
    payload = {
        "account": ACCOUNT,
        "language": "en",
        "nodeId": "",
        "password": md5_hash(PASSWORD),
        "validCode": "",
    }
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
    }
    resp = session.post(LOGIN_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok", False) and not data.get("success", False):
        raise SystemExit(f"Login gagal. Response: {data}")
    print("Login berhasil.")


def fetch_all_devices(session: requests.Session) -> list:
    """Ambil semua device. pageSize besar supaya 1x request cukup untuk ~362 device."""
    payload = {
        "imei": "",
        "startRow": "0",
        "userType": 8,
        "userId": "",
        "isNewMcType": "0",
        "orgId": "",
        "pageSize": 1000,  # lebih dari jumlah device supaya 1x ambil semua
        "searchStatus": "",  # kosong = semua status (online + offline)
        "siftType": "",
        "sortRule": "",
        "sortType": "",
        "type": "NORMAL",
        "videoEntry": "",
    }
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
    }
    resp = session.post(DEVICE_LIST_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok", False):
        raise SystemExit(f"Gagal ambil device list. Response: {data}")
    return data.get("data", [])


def parse_gps_time(gps_time_str: str):
    """gpsTime formatnya '2026-06-16 17:15:21' (asumsi WIB/lokal server)."""
    if not gps_time_str:
        return None
    try:
        return datetime.strptime(gps_time_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def process_devices(raw_devices: list) -> list:
    now = datetime.now()
    threshold = timedelta(hours=OFFLINE_THRESHOLD_HOURS)
    processed = []

    for d in raw_devices:
        name = d.get("deviceName") or d.get("imei") or "Unknown"
        imei = d.get("imei", "")
        gps_time_str = d.get("gpsTime") or d.get("hbTime")
        last_update = parse_gps_time(gps_time_str)
        status_raw = d.get("status", "")

        if last_update is None:
            is_offline = True
            hours_since = None
        else:
            delta = now - last_update
            hours_since = round(delta.total_seconds() / 3600, 1)
            is_offline = delta > threshold

        processed.append({
            "deviceName": name,
            "imei": imei,
            "groupName": d.get("orgName", ""),
            "statusRaw": status_raw,
            "lastUpdate": gps_time_str,
            "hoursSinceUpdate": hours_since,
            "isOffline": is_offline,
        })

    # urutkan: offline duluan, lalu yang paling lama tidak update
    processed.sort(key=lambda x: (not x["isOffline"], -(x["hoursSinceUpdate"] or 0)))
    return processed


def load_previous() -> dict:
    if os.path.exists(PREVIOUS_FILE):
        with open(PREVIOUS_FILE, "r") as f:
            return {d["imei"]: d["isOffline"] for d in json.load(f).get("devices", [])}
    return {}


def detect_new_offline(processed: list, previous_status: dict) -> list:
    """Device yang BARU jadi offline (sebelumnya online, sekarang offline)."""
    newly_offline = []
    for d in processed:
        prev_offline = previous_status.get(d["imei"])
        if d["isOffline"] and prev_offline is False:
            newly_offline.append(d)
    return newly_offline


def save_results(processed: list):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output = {
        "lastChecked": now_str,
        "totalDevices": len(processed),
        "totalOffline": sum(1 for d in processed if d["isOffline"]),
        "totalOnline": sum(1 for d in processed if not d["isOffline"]),
        "devices": processed,
    }
    # simpan snapshot lama sebagai "previous" sebelum overwrite
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            old = f.read()
        with open(PREVIOUS_FILE, "w") as f:
            f.write(old)

    with open(DATA_FILE, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Disimpan: {output['totalOnline']} online, {output['totalOffline']} offline dari {output['totalDevices']} device.")
    return output


def send_email_alert(newly_offline: list):
    if not newly_offline:
        print("Tidak ada device baru offline. Email tidak dikirim.")
        return

    smtp_host = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", "465"))
    smtp_user = os.environ.get("EMAIL_USER")
    smtp_pass = os.environ.get("EMAIL_PASSWORD")
    to_email = os.environ.get("EMAIL_TO")

    if not all([smtp_user, smtp_pass, to_email]):
        print("EMAIL_USER / EMAIL_PASSWORD / EMAIL_TO belum diset, skip kirim email.")
        return

    import smtplib
    from email.mime.text import MIMEText

    lines = [f"- {d['deviceName']} (IMEI: {d['imei']}), terakhir update: {d['lastUpdate']}" for d in newly_offline]
    body = "Device berikut baru terdeteksi OFFLINE (lebih dari {} jam tidak update):\n\n{}".format(
        OFFLINE_THRESHOLD_HOURS, "\n".join(lines)
    )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[TrackSolid Alert] {len(newly_offline)} device baru OFFLINE"
    msg["From"] = smtp_user
    msg["To"] = to_email

    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_email], msg.as_string())

    print(f"Email alert terkirim ke {to_email} untuk {len(newly_offline)} device.")


def main():
    session = requests.Session()
    login(session)
    raw_devices = fetch_all_devices(session)
    print(f"Total device diterima dari server: {len(raw_devices)}")

    previous_status = load_previous()
    processed = process_devices(raw_devices)
    newly_offline = detect_new_offline(processed, previous_status)

    save_results(processed)
    send_email_alert(newly_offline)


if __name__ == "__main__":
    main()
