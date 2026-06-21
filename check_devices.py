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

OFFLINE_THRESHOLD_HOURS = 8  # threshold terendah (dipakai untuk hitungan "totalOffline" dasar)
OFFLINE_LEVELS = [
    (36, "offline-36"),
    (24, "offline-24"),
    (12, "offline-12"),
    (8, "offline-8"),
]  # urutan dari paling parah ke paling ringan -- dicek dari atas


def get_offline_level(hours_since: float):
    """
    Kembalikan level offline berdasarkan berapa jam device tidak update.
    None artinya device masih online (di bawah 8 jam).
    """
    if hours_since is None:
        return "offline-36"
    for threshold_hours, level in OFFLINE_LEVELS:
        if hours_since >= threshold_hours:
            return level
    return None

DATA_FILE = "devices.json"
PREVIOUS_FILE = "devices_previous.json"

ACCOUNT = os.environ.get("TSP_ACCOUNT")
PASSWORD = os.environ.get("TSP_PASSWORD")

# Dukung sampai 5 akun TrackSolid sekaligus, digabung jadi satu dashboard.
# Akun pertama pakai TSP_ACCOUNT/TSP_PASSWORD (kompatibel dengan setup lama).
# Akun ke-2 sampai 5 pakai TSP_ACCOUNT_2/TSP_PASSWORD_2, dst.
# Label akun (opsional, untuk tampilan di web) lewat TSP_LABEL_1, TSP_LABEL_2, dst.
def load_accounts():
    accounts = []
    if ACCOUNT and PASSWORD:
        accounts.append({
            "account": ACCOUNT,
            "password": PASSWORD,
            "label": os.environ.get("TSP_LABEL_1", ACCOUNT),
        })
    for i in range(2, 6):
        acc = os.environ.get(f"TSP_ACCOUNT_{i}")
        pwd = os.environ.get(f"TSP_PASSWORD_{i}")
        if acc and pwd:
            accounts.append({
                "account": acc,
                "password": pwd,
                "label": os.environ.get(f"TSP_LABEL_{i}", acc),
            })
    return accounts


ACCOUNTS = load_accounts()

if not ACCOUNTS:
    raise SystemExit("ERROR: set minimal TSP_ACCOUNT dan TSP_PASSWORD (akun pertama).")


def login_and_fetch_devices(playwright, account: str, password: str, label: str) -> list:
    """
    Buka browser headless, login manual lewat form, lalu panggil endpoint device list
    LANGSUNG DARI DALAM BROWSER CONTEXT (pakai fetch JS), supaya semua header/cookie/token
    persis seperti request asli dari browser -- tidak perlu rakit ulang manual di requests.
    """
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()

    print(f"[{label}] Membuka halaman login...")
    page.goto(f"{BASE_URL}/resource/dev/index.html#/login", wait_until="networkidle", timeout=60000)

    page.wait_for_selector("input", timeout=30000)

    inputs = page.query_selector_all("input")
    username_filled = False
    for inp in inputs:
        input_type = inp.get_attribute("type")
        if input_type == "password":
            inp.fill(password)
        elif not username_filled and input_type in ("text", "email", None):
            inp.fill(account)
            username_filled = True

    if not username_filled:
        page.screenshot(path=f"debug_login_page_{label}.png")
        browser.close()
        raise SystemExit(f"[{label}] Tidak ketemu input username. Screenshot disimpan.")

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
        page.screenshot(path=f"debug_before_click_{label}.png")
        browser.close()
        raise SystemExit(f"[{label}] Tidak bisa klik tombol login dengan selector manapun. Error terakhir: {last_error}")

    try:
        page.wait_for_url("**/monitorObject**", timeout=30000)
    except Exception:
        page.screenshot(path=f"debug_after_click_{label}.png")
        print(f"[{label}] URL saat ini: {page.url}")
        browser.close()
        raise

    # Tunggu network idle supaya semua request awal halaman monitor
    # (getUserGroup, queryEquipmentList versi UI, dll) selesai dulu.
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass  # kalau timeout, lanjut saja -- network idle tidak selalu tercapai di SPA

    page.wait_for_timeout(3000)

    # PENTING: klik node teratas di Account List (induk organisasi) secara eksplisit.
    # Server TrackSolid sepertinya menyimpan "grup aktif" berdasarkan klik UI terakhir,
    # bukan murni dari payload orgId -- tanpa klik ini, device list yang didapat
    # cuma mencakup sub-grup pertama (108 device), bukan semua (363 device).
    try:
        account_list_item = page.locator("text=/Stock\\d+\\/Total\\d+/").first
        account_list_item.scroll_into_view_if_needed(timeout=5000)
        try:
            account_list_item.click(timeout=8000)
        except Exception:
            # Fallback: paksa klik meski Playwright menganggap elemen "tidak terlihat"
            # (bisa terjadi karena overlay/animasi/struktur DOM yang sedikit beda antar akun).
            account_list_item.click(timeout=5000, force=True)
        page.wait_for_timeout(3000)
        print(f"[{label}] Berhasil klik node induk organisasi di Account List.")
    except Exception as e:
        print(f"[{label}] PERINGATAN: gagal klik node Account List ({e}), lanjut tanpa klik eksplisit.")

    page.wait_for_timeout(5000)
    print(f"[{label}] Login berhasil.")

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
    print(f"[{label}] userId terdeteksi: {user_id}")

    # Coba ambil orgId dari localStorage juga -- field ini wajib diisi
    # (bukan string kosong) supaya server tidak menolak dengan IllegalParameter.
    # Coba beberapa kemungkinan lokasi: userInfo.orgId, atau key terpisah.
    org_id = page.evaluate("""() => {
        try {
            const userInfo = localStorage.getItem('userInfo');
            if (userInfo) {
                const parsed = JSON.parse(userInfo);
                if (parsed.orgId) return parsed.orgId;
            }
        } catch (e) {}
        // Coba cari di semua key localStorage yang namanya mengandung 'org'
        try {
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                if (key && key.toLowerCase().includes('org')) {
                    const val = localStorage.getItem(key);
                    if (val && val.length > 10 && val.length < 50) return val;
                }
            }
        } catch (e) {}
        return null;
    }""")
    print(f"[{label}] orgId terdeteksi dari localStorage: {org_id}")

    def call_device_list_api(use_org_id):
        payload = {
            "imei": "",
            "startRow": "0",
            "userType": 8,
            "userId": int(user_id) if user_id else "",
            "orgId": use_org_id or "",
            "siftType": "",
            "sortType": "",
            "sortRule": "",
            "isNewMcType": "0",
            "videoEntry": "",
            "type": "NORMAL",
            "searchStatus": "ALL",
        }
        return page.evaluate(
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

    result = call_device_list_api(org_id)

    # Kalau gagal IllegalParameter dan kita belum coba orgId dari localStorage,
    # atau orgId kosong, coba sekali lagi dengan menunggu lebih lama (kadang
    # localStorage org butuh waktu lebih untuk ke-set setelah klik UI).
    if result["status"] == 200:
        try:
            check_data = json.loads(result["text"])
            if check_data.get("code") == 10004:  # IllegalParameter
                print(f"[{label}] Percobaan pertama IllegalParameter, coba ulang setelah delay tambahan...")
                page.wait_for_timeout(4000)
                org_id_retry = page.evaluate("""() => {
                    try {
                        const userInfo = localStorage.getItem('userInfo');
                        if (userInfo) {
                            const parsed = JSON.parse(userInfo);
                            if (parsed.orgId) return parsed.orgId;
                        }
                    } catch (e) {}
                    return null;
                }""")
                print(f"[{label}] orgId percobaan ke-2: {org_id_retry}")
                result = call_device_list_api(org_id_retry or org_id)
        except (json.JSONDecodeError, KeyError):
            pass

    browser.close()

    if result["status"] != 200:
        raise SystemExit(f"[{label}] Gagal ambil device list. Status: {result['status']}, Body: {result['text'][:500]}")

    print(f"[{label}] Device list raw response (500 char pertama): {result['text'][:500]}")

    data = json.loads(result["text"])
    if not data.get("ok", False):
        raise SystemExit(f"[{label}] Gagal ambil device list. Response: {data}")

    all_devices = data.get("data", [])
    for d in all_devices:
        d["_accountLabel"] = label  # tag asal akun, dipakai nanti untuk tampilan di web

    print(f"[{label}] Total device terkumpul: {len(all_devices)}")
    return all_devices


def parse_gps_time(gps_time_str: str):
    if not gps_time_str:
        return None
    try:
        return datetime.strptime(gps_time_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def process_devices(raw_devices: list) -> list:
    now = datetime.now()
    processed = []

    for d in raw_devices:
        name = d.get("deviceName") or d.get("imei") or "Unknown"
        imei = d.get("imei", "")
        gps_time_str = d.get("gpsTime") or d.get("hbTime")
        last_update = parse_gps_time(gps_time_str)
        status_raw = d.get("status", "")

        if last_update is None:
            hours_since = None
            offline_level = "offline-36"
            is_offline = True
            is_recently_online = False
        else:
            delta = now - last_update
            hours_since = round(delta.total_seconds() / 3600, 1)
            offline_level = get_offline_level(hours_since)
            is_offline = offline_level is not None
            is_recently_online = hours_since <= 1.0

        processed.append({
            "deviceName": name,
            "imei": imei,
            "groupName": d.get("orgName", ""),
            "accountLabel": d.get("_accountLabel", ""),
            "statusRaw": status_raw,
            "lastUpdate": gps_time_str,
            "hoursSinceUpdate": hours_since,
            "isOffline": is_offline,
            "offlineLevel": offline_level,
            "isRecentlyOnline": is_recently_online,  # update dalam 1 jam terakhir
        })

    processed.sort(key=lambda x: (not x["isOffline"], -(x["hoursSinceUpdate"] or 0)))
    return processed


def load_previous() -> dict:
    if os.path.exists(PREVIOUS_FILE):
        with open(PREVIOUS_FILE, "r") as f:
            return {d["imei"]: d["isOffline"] for d in json.load(f).get("devices", [])}
    return {}


def detect_new_offline(processed: list, previous_status: dict) -> list:
    """
    Device yang baru OFFLINE di level >= 12 jam (bukan 8 jam) -- supaya email
    tidak terlalu sering terkirim untuk device yang baru sebentar offline.
    Level 8 jam tetap ditampilkan di web (badge), tapi tidak memicu email.
    """
    EMAIL_ALERT_LEVELS = {"offline-12", "offline-24", "offline-36"}
    newly_offline = []
    for d in processed:
        prev_offline = previous_status.get(d["imei"])
        currently_alertable = d["offlineLevel"] in EMAIL_ALERT_LEVELS
        if currently_alertable and prev_offline is False:
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
    print(f"Total akun yang akan dicek: {len(ACCOUNTS)}")
    all_raw_devices = []

    with sync_playwright() as playwright:
        for acc in ACCOUNTS:
            try:
                devices = login_and_fetch_devices(
                    playwright, acc["account"], acc["password"], acc["label"]
                )
                all_raw_devices.extend(devices)
            except Exception as e:
                # Satu akun gagal tidak boleh menggagalkan semuanya --
                # log error-nya, lanjut ke akun berikutnya.
                print(f"[{acc['label']}] GAGAL: {e}")
                continue

    print(f"Total device diterima dari SEMUA akun: {len(all_raw_devices)}")

    if not all_raw_devices:
        raise SystemExit("Tidak ada device yang berhasil diambil dari akun manapun. Cek log di atas untuk detail error per akun.")

    previous_status = load_previous()
    processed = process_devices(all_raw_devices)
    newly_offline = detect_new_offline(processed, previous_status)

    save_results(processed)
    send_email_alert(newly_offline)


if __name__ == "__main__":
    main()
