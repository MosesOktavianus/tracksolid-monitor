"""
TrackSolidPro Device Monitor (Playwright version)
Login ke TrackSolidPro pakai browser asli (headless), ambil semua device,
deteksi yang offline > OFFLINE_THRESHOLD_HOURS jam.
Hasil disimpan ke devices.json (untuk web list) dan dikirim email kalau ada perubahan status.

Kenapa Playwright (bukan requests biasa)?
TrackSolidPro generate token JWT lewat JavaScript di browser (disimpan di localStorage)
sebelum request login dikirim. Token ini tidak bisa direplikasi gampang lewat HTTP request
biasa, jadi kita pakai browser asli (headless) supaya token itu otomatis ter-generate
sama seperti saat login manual.

Cara pakai:
- Set environment variables: TSP_ACCOUNT, TSP_PASSWORD, RESEND_API_KEY, EMAIL_FROM, EMAIL_TO
- playwright install chromium  (sekali saja, sudah otomatis di workflow)
- python check_devices.py
"""

import os
import json
import requests
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.tracksolidpro.com"
DEVICE_LIST_URL = f"{BASE_URL}/v3/new/newEquipment/queryEquipmentList"

OFFLINE_THRESHOLD_HOURS = 12

DATA_FILE = "devices.json"
PREVIOUS_FILE = "devices_previous.json"

ACCOUNT = os.environ.get("TSP_ACCOUNT")
PASSWORD = os.environ.get("TSP_PASSWORD")

if not ACCOUNT or not PASSWORD:
    raise SystemExit("ERROR: set environment variable TSP_ACCOUNT dan TSP_PASSWORD dulu.")


def login_and_get_session(playwright) -> tuple:
    """
    Buka browser headless, login manual lewat form, lalu ambil:
    - cookies (untuk dipakai di request requests.Session berikutnya)
    - token dari localStorage (untuk header Authorization)
    """
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()

    print("Membuka halaman login...")
    page.goto(f"{BASE_URL}/resource/dev/index.html#/login", wait_until="networkidle", timeout=60000)

    # Tunggu form login muncul. Selector ini best-effort -- kalau berubah,
    # perlu disesuaikan dengan inspect elemen form login asli.
    page.wait_for_selector("input[type='text'], input[type='email']", timeout=30000)

    # Isi form login. Asumsi: input pertama = username, input password = password.
    inputs = page.query_selector_all("input")
    username_filled = False
    for inp in inputs:
        input_type = inp.get_attribute("type")
        if input_type == "password":
            inp.fill(PASSWORD)
        elif not username_filled and input_type in ("text", "email", None):
            inp.fill(ACCOUNT)
            username_filled = True

    # Klik tombol login (cari tombol dengan teks "Login" / "Sign in" / "登录")
    page.click("button:has-text('Login'), button:has-text('Sign in'), button[type='submit']")

    # Tunggu sampai redirect ke halaman monitor (artinya login sukses)
    page.wait_for_url("**/monitorObject**", timeout=30000)
    page.wait_for_timeout(2000)  # beri waktu localStorage ke-set

    # Ambil token dari localStorage
    token = page.evaluate("() => localStorage.getItem('token')")
    cookies = page.context.cookies()

    browser.close()

    if not token:
        raise SystemExit("Gagal mengambil token dari localStorage setelah login.")

    print("Login berhasil, token & cookies didapat.")
    return token, cookies


def build_requests_session(token: str, cookies: list) -> requests.Session:
    session = requests.Session()
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain"))
    session.headers.update({
        "Authorization": token,
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "Must": "true",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    })
    return session


def fetch_all_devices(session: requests.Session) -> list:
    payload = {
        "imei": "",
        "startRow": "0",
        "userType": 8,
        "userId": "",
        "isNewMcType": "0",
        "orgId": "",
        "pageSize": 1000,
        "searchStatus": "",
        "siftType": "",
        "sortRule": "",
        "sortType": "",
        "type": "NORMAL",
        "videoEntry": "",
    }
    resp = session.post(DEVICE_LIST_URL, json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"Device list response status: {resp.status_code}")
        print(f"Device list response body: {resp.text[:500]}")
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok", False):
        raise SystemExit(f"Gagal ambil device list. Response: {data}")
    return data.get("data", [])


def parse_gps_time(gps_time_str: str):
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

    processed.sort(key=lambda x: (not x["isOffline"], -(x["hoursSinceUpdate"] or 0)))
    return processed


def load_previous() -> dict:
    if os.path.exists(PREVIOUS_FILE):
        with open(PREVIOUS_FILE, "r") as f:
            return {d["imei"]: d["isOffline"] for d in json.load(f).get("devices", [])}
    return {}


def detect_new_offline(processed: list, previous_status: dict) -> list:
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

    resend_api_key = os.environ.get("RESEND_API_KEY")
    from_email = os.environ.get("EMAIL_FROM", "onboarding@resend.dev")
    to_email = os.environ.get("EMAIL_TO")

    if not all([resend_api_key, to_email]):
        print("RESEND_API_KEY / EMAIL_TO belum diset, skip kirim email.")
        return

    rows = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'>{d['deviceName']}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{d['imei']}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{d['lastUpdate']}</td></tr>"
        for d in newly_offline
    )
    html_body = f"""
    <p>Device berikut baru terdeteksi <b>OFFLINE</b> (lebih dari {OFFLINE_THRESHOLD_HOURS} jam tidak update):</p>
    <table style="border-collapse:collapse;width:100%;font-family:sans-serif;font-size:14px">
      <tr style="background:#f5f5f5">
        <th style="padding:6px 10px;text-align:left">Device</th>
        <th style="padding:6px 10px;text-align:left">IMEI</th>
        <th style="padding:6px 10px;text-align:left">Terakhir Update</th>
      </tr>
      {rows}
    </table>
    """

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {resend_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": from_email,
            "to": [to_email],
            "subject": f"[TrackSolid Alert] {len(newly_offline)} device baru OFFLINE",
            "html": html_body,
        },
        timeout=30,
    )

    if resp.status_code >= 400:
        print(f"Gagal kirim email via Resend: {resp.status_code} {resp.text}")
    else:
        print(f"Email alert terkirim ke {to_email} untuk {len(newly_offline)} device.")


def main():
    with sync_playwright() as playwright:
        token, cookies = login_and_get_session(playwright)

    session = build_requests_session(token, cookies)
    raw_devices = fetch_all_devices(session)
    print(f"Total device diterima dari server: {len(raw_devices)}")

    previous_status = load_previous()
    processed = process_devices(raw_devices)
    newly_offline = detect_new_offline(processed, previous_status)

    save_results(processed)
    send_email_alert(newly_offline)


if __name__ == "__main__":
    main()
