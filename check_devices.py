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


def login_and_fetch_devices(playwright) -> list:
    """
    Buka browser headless, login manual lewat form, lalu panggil endpoint device list
    LANGSUNG DARI DALAM BROWSER CONTEXT (pakai fetch JS), supaya semua header/cookie/token
    persis seperti request asli dari browser -- tidak perlu rakit ulang manual di requests.
    """
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()

    print("Membuka halaman login...")
    page.goto(f"{BASE_URL}/resource/dev/index.html#/login", wait_until="networkidle", timeout=60000)

    page.wait_for_selector("input", timeout=30000)

    inputs = page.query_selector_all("input")
    username_filled = False
    for inp in inputs:
        input_type = inp.get_attribute("type")
        if input_type == "password":
            inp.fill(PASSWORD)
        elif not username_filled and input_type in ("text", "email", None):
            inp.fill(ACCOUNT)
            username_filled = True

    if not username_filled:
        page.screenshot(path="debug_login_page.png")
        raise SystemExit("Tidak ketemu input username. Screenshot disimpan ke debug_login_page.png")

    clicked = False
    selectors_to_try = [
        "button:has-text('Sign in')",
        "button.login-button",
        "text=Sign in",
    ]
    last_error = None
    for selector in selectors_to_try:
        try:
            page.click(selector, timeout=10000)
            clicked = True
            break
        except Exception as e:
            last_error = e
            continue

    if not clicked:
        page.screenshot(path="debug_before_click.png")
        raise SystemExit(f"Tidak bisa klik tombol login dengan selector manapun. Error terakhir: {last_error}")

    try:
        page.wait_for_url("**/monitorObject**", timeout=30000)
    except Exception:
        page.screenshot(path="debug_after_click.png")
        print(f"URL saat ini: {page.url}")
        raise

    # Tunggu network idle supaya semua request awal halaman monitor
    # (getUserGroup, queryEquipmentList versi UI, dll) selesai dulu.
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass  # kalau timeout, lanjut saja -- network idle tidak selalu tercapai di SPA

    page.wait_for_timeout(5000)
    print("Login berhasil.")

    token_check = page.evaluate("() => localStorage.getItem('token')")
    if token_check:
        print(f"Token terdeteksi (panjang: {len(token_check)} karakter, awalan: {token_check[:15]}...)")
    else:
        print("PERINGATAN: token di localStorage kosong/null!")

    # Coba ambil userId dari localStorage (biasanya tersimpan setelah login,
    # dipakai UI untuk semua request berikutnya termasuk queryEquipmentList).
    user_id = page.evaluate("""() => {
        try {
            const userInfo = localStorage.getItem('userInfo');
            if (userInfo) {
                const parsed = JSON.parse(userInfo);
                return parsed.id || parsed.userId || null;
            }
        } catch (e) {}
        return null;
    }""")
    print(f"userId terdeteksi: {user_id}")

    # Panggil endpoint device list LANGSUNG dari dalam browser (pakai fetch),
    # supaya semua header/auth/cookie otomatis persis seperti request asli.
    payload = {
        "imei": "",
        "startRow": "0",
        "userType": 8,
        "userId": user_id or "",
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

    result = page.evaluate(
        """async (payload) => {
            const token = localStorage.getItem('token');
            const resp = await fetch('/v3/new/newEquipment/queryEquipmentList', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json;charset=UTF-8',
                    'Accept': 'application/json, text/plain, */*',
                    'Authorization': token,
                    'Must': 'true',
                },
                body: JSON.stringify(payload),
            });
            const status = resp.status;
            const text = await resp.text();
            return { status, text };
        }""",
        payload,
    )

    browser.close()

    if result["status"] != 200:
        raise SystemExit(f"Gagal ambil device list. Status: {result['status']}, Body: {result['text'][:500]}")

    print(f"Device list raw response (500 char pertama): {result['text'][:500]}")

    data = json.loads(result["text"])
    if not data.get("ok", False):
        raise SystemExit(f"Gagal ambil device list. Response: {data}")

    device_count = len(data.get("data", []))
    print(f"Jumlah device dalam response: {device_count}")

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
        raw_devices = login_and_fetch_devices(playwright)

    print(f"Total device diterima dari server: {len(raw_devices)}")

    previous_status = load_previous()
    processed = process_devices(raw_devices)
    newly_offline = detect_new_offline(processed, previous_status)

    save_results(processed)
    send_email_alert(newly_offline)


if __name__ == "__main__":
    main()
