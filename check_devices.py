#!/usr/bin/env python3
"""
TrackSolid Device Monitor — versi network-interception.

Perbaikan utama dibanding versi lama:
1. TIDAK memanggil API device-list secara manual (itu sumber error
   TS.Common.IllegalParameter — parameter/signature yang kita rakit sendiri
   tidak pernah cocok dengan yang diharapkan server).
   Sebagai gantinya, script MENYADAP (intercept) respons device-list yang
   dibuat oleh browser/web TrackSolid sendiri. Parameternya pasti valid
   karena yang membuat adalah aplikasi web resmi mereka.
2. Ekspansi account tree yang benar: klik ikon expand dulu, lalu klik node.
   Kalau node tidak visible, pakai scroll + force-click, bukan sekadar
   "lanjut tanpa klik" (yang membuat data tidak pernah termuat).
3. Screenshot + dump HTML otomatis per akun saat gagal, untuk debugging.
4. Output ke docs/devices.json agar langsung dibaca dashboard GitHub Pages.

ENV yang dibutuhkan (set sebagai GitHub Secrets):
  TS_ACCOUNTS  : JSON array, contoh:
                 [{"email":"a@mail.com","password":"xxx"},
                  {"email":"b@mail.com","password":"yyy"}]
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

LOGIN_URL = "https://www.tracksolidpro.com/main/login"  # sesuaikan jika beda
OUT_DIR = Path("docs")
DEBUG_DIR = Path("debug")
PAGE_TIMEOUT = 45_000

# Kata kunci untuk mengenali respons API yang berisi daftar device.
DEVICE_URL_HINTS = ("device", "Device", "vehicle", "tracker")
DEVICE_FIELD_HINTS = ("imei", "deviceName", "device_name", "vehicleName")


def log(msg: str) -> None:
    print(f"[***] {msg}", flush=True)


def load_accounts() -> list[dict]:
    raw = os.environ.get("TS_ACCOUNTS", "").strip()
    if not raw:
        log("ENV TS_ACCOUNTS kosong. Set secret berisi JSON array akun.")
        sys.exit(2)
    try:
        accounts = json.loads(raw)
        assert isinstance(accounts, list) and accounts
        return accounts
    except Exception as e:
        log(f"TS_ACCOUNTS bukan JSON valid: {e}")
        sys.exit(2)


def extract_devices_from_json(data) -> list[dict]:
    """Cari array of dict yang terlihat seperti daftar device di dalam
    struktur JSON apa pun (robust terhadap perubahan bentuk respons)."""
    found: list[dict] = []

    def walk(node):
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node):
                sample = node[0]
                if any(k in sample for k in DEVICE_FIELD_HINTS):
                    found.extend(node)
                    return
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)

    walk(data)
    return found


def normalize(d: dict, account: str) -> dict:
    """Seragamkan field antar kemungkinan format respons TrackSolid."""
    imei = str(d.get("imei") or d.get("deviceImei") or d.get("id") or "")
    name = (d.get("deviceName") or d.get("device_name")
            or d.get("vehicleName") or d.get("name") or imei or "Tanpa nama")
    # status: TrackSolid umumnya 1/online, 0/offline; bisa juga string
    raw_status = d.get("status", d.get("onlineStatus", d.get("online", "")))
    online = str(raw_status).lower() in ("1", "true", "online", "moving", "static")
    return {
        "imei": imei,
        "name": str(name),
        "online": online,
        "raw_status": str(raw_status),
        "last_seen": str(d.get("gpsTime") or d.get("lastOnlineTime")
                         or d.get("hbTime") or ""),
        "account": account,
    }


def expand_and_click_tree(page) -> None:
    """Expand semua node account tree lalu klik node stok agar web
    memicu request device-list. Gagal klik bukan fatal — respons awal
    saat page load biasanya sudah tersadap."""
    # 1) klik semua ikon expand/panah yang ada
    for sel in (".el-tree-node__expand-icon:not(.is-leaf)",
                ".userTree .expand", "[class*='expand-icon']"):
        icons = page.locator(sel)
        for i in range(min(icons.count(), 20)):
            try:
                icons.nth(i).click(timeout=2_000)
                page.wait_for_timeout(400)
            except Exception:
                pass

    # 2) klik node "StockN/TotalN" — pakai scroll + force sebagai fallback
    node = page.locator("text=/Stock\\d+\\/Total\\d+/").first
    try:
        node.scroll_into_view_if_needed(timeout=3_000)
        node.click(timeout=5_000)
        log("Account tree diklik normal.")
    except Exception:
        try:
            node.click(timeout=3_000, force=True)
            log("Account tree diklik dengan force=True.")
        except Exception as e:
            log(f"Node tree tetap tidak bisa diklik ({type(e).__name__}); "
                "mengandalkan respons yang tersadap saat page load.")


def check_account(pw, account: dict) -> list[dict]:
    email = account["email"]
    log(f"=== Akun: {email} ===")
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    captured: list[dict] = []

    def on_response(resp):
        try:
            url = resp.url
            if not any(h in url for h in DEVICE_URL_HINTS):
                return
            if "application/json" not in (resp.headers.get("content-type") or ""):
                return
            body = resp.json()
            devices = extract_devices_from_json(body)
            if devices:
                log(f"Tersadap {len(devices)} device dari {url.split('?')[0]}")
                captured.extend(devices)
        except Exception:
            pass  # respons non-JSON / stream — abaikan

    page.on("response", on_response)

    try:
        log("Membuka halaman login...")
        page.goto(LOGIN_URL, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")

        # --- login (sesuaikan selector dengan halaman login TrackSolid-mu) ---
        page.fill("input[type='text'], input[placeholder*='mail' i]", email)
        page.fill("input[type='password']", account["password"])
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT)
        log("Login berhasil.")

        # beri waktu request awal device-list tersadap
        page.wait_for_timeout(4_000)
        expand_and_click_tree(page)
        page.wait_for_timeout(5_000)  # tunggu request hasil klik

        if not captured:
            raise RuntimeError("Tidak ada respons device-list yang tersadap.")

    except Exception as e:
        log(f"GAGAL untuk {email}: {e}")
        DEBUG_DIR.mkdir(exist_ok=True)
        safe = re.sub(r"\W+", "_", email)
        try:
            page.screenshot(path=str(DEBUG_DIR / f"{safe}.png"), full_page=True)
            (DEBUG_DIR / f"{safe}.html").write_text(page.content())
            log(f"Screenshot & HTML disimpan ke {DEBUG_DIR}/")
        except Exception:
            pass
    finally:
        browser.close()

    # dedupe per imei
    seen, result = set(), []
    for d in captured:
        nd = normalize(d, email)
        if nd["imei"] and nd["imei"] in seen:
            continue
        seen.add(nd["imei"])
        result.append(nd)
    log(f"Total device akun ini: {len(result)}")
    return result


def main() -> None:
    accounts = load_accounts()
    all_devices: list[dict] = []
    failed: list[str] = []

    with sync_playwright() as pw:
        for acc in accounts:
            devices = check_account(pw, acc)
            if devices:
                all_devices.extend(devices)
            else:
                failed.append(acc["email"])

    log(f"Total device diterima dari SEMUA akun: {len(all_devices)}")

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / "devices.json"

    # simpan versi sebelumnya untuk perbandingan (tidak fatal jika belum ada)
    if out.exists():
        (OUT_DIR / "devices_previous.json").write_text(out.read_text())

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "accounts_checked": [a["email"] for a in accounts],
        "accounts_failed": failed,
        "total": len(all_devices),
        "online": sum(1 for d in all_devices if d["online"]),
        "devices": sorted(all_devices, key=lambda d: (d["online"], d["name"])),
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    log(f"Ditulis: {out}")

    # Exit 1 hanya jika SEMUA akun gagal — data parsial tetap berguna.
    if len(failed) == len(accounts):
        sys.exit(1)


if __name__ == "__main__":
    main()
