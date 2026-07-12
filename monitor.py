"""
TikTok Repost İzleyici
Belirli bir kullanıcının Reposts sekmesini kontrol eder; yeni repost varsa Telegram'a bildirir.
Orijinal video URL'si kaydedilir — repost kaldırılsa bile video izlenebilir.
"""

import os
import json
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# --- Yapılandırma (Şifreler GitHub Secrets kasasından çekilir) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TIKTOK_USERNAME = os.environ.get("TIKTOK_USERNAME", "")

# --- Genel ayarlar ---
HEADLESS = True
LAST_REPOST_FILE = Path(__file__).parent / "last_repost.json"
LEGACY_REPOST_FILE = Path(__file__).parent / "last_repost.txt"
PROFILE_URL = "https://www.tiktok.com/@{username}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

REPOST_TAB_LABELS = (
    "Reposts",
    "Repost",
    "Yeniden yayınlananlar",
    "Yeniden Yayınlananlar",
    "Yeniden paylaşılanlar",
)

VIDEO_ID_PATTERN = re.compile(r"/video/(\d+)")
USERNAME_PATTERN = re.compile(r"tiktok\.com/@([^/]+)/video/")


@dataclass
class RepostInfo:
    video_id: str
    original_author: str
    original_url: str
    reposted_by: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RepostInfo":
        return cls(
            video_id=str(data["video_id"]),
            original_author=data["original_author"],
            original_url=data["original_url"],
            reposted_by=data["reposted_by"],
        )


def human_delay(min_seconds: float = 1.5, max_seconds: float = 3.5) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def normalize_username(username: str) -> str:
    return username.strip().lstrip("@")


def extract_video_id(url: str) -> str | None:
    match = VIDEO_ID_PATTERN.search(url)
    return match.group(1) if match else None


def extract_author_from_url(url: str) -> str | None:
    match = USERNAME_PATTERN.search(url)
    return match.group(1) if match else None


def build_video_url(video_id: str, author: str) -> str:
    return f"https://www.tiktok.com/@{author}/video/{video_id}"


def repost_from_api_item(item: dict, reposted_by: str) -> RepostInfo | None:
    video_id = item.get("id") or item.get("aweme_id")
    if not video_id:
        return None

    author = item.get("author") or {}
    original_author = author.get("uniqueId")
    if not original_author:
        return None

    video_id = str(video_id)
    return RepostInfo(
        video_id=video_id,
        original_author=original_author,
        original_url=build_video_url(video_id, original_author),
        reposted_by=reposted_by,
    )


def repost_from_url(url: str, reposted_by: str) -> RepostInfo | None:
    video_id = extract_video_id(url)
    author = extract_author_from_url(url)
    if not video_id or not author:
        return None

    return RepostInfo(
        video_id=video_id,
        original_author=author,
        original_url=build_video_url(video_id, author),
        reposted_by=reposted_by,
    )


def read_last_repost() -> RepostInfo | None:
    if LAST_REPOST_FILE.exists():
        data = json.loads(LAST_REPOST_FILE.read_text(encoding="utf-8"))
        return RepostInfo.from_dict(data)

    if LEGACY_REPOST_FILE.exists():
        legacy_url = LEGACY_REPOST_FILE.read_text(encoding="utf-8").strip()
        if legacy_url:
            info = repost_from_url(legacy_url, normalize_username(TIKTOK_USERNAME))
            if info:
                save_last_repost(info)
                return info

    return None


def save_last_repost(info: RepostInfo) -> None:
    LAST_REPOST_FILE.write_text(
        json.dumps(info.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[UYARI] Telegram token veya chat ID bos; mesaj gonderilmedi.")
        return

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    response = requests.post(
        api_url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False},
        timeout=30,
    )
    response.raise_for_status()
    print("[BILGI] Telegram mesaji gonderildi.")


def dismiss_cookie_banner(page) -> None:
    selectors = [
        'button:has-text("Tumunu kabul et")',
        'button:has-text("Tümünü kabul et")',
        'button:has-text("Accept all")',
        'button:has-text("Decline optional cookies")',
        'button:has-text("Istege bagli cerezleri reddet")',
        'button:has-text("İsteğe bağlı çerezleri reddet")',
        'button:has-text("Reject all")',
    ]
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if button.is_visible(timeout=1500):
                button.click()
                human_delay(0.8, 1.5)
                return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue


def click_reposts_tab(page) -> None:
    page.wait_for_selector('[role="tab"]', timeout=30000)
    human_delay(1.0, 2.0)

    strategies = [
        lambda: page.get_by_role("tab", name=re.compile(r"reposts?", re.I)),
        lambda: page.get_by_role("tab", name=re.compile(r"yeniden", re.I)),
        lambda: page.locator('[data-e2e="repost-tab"]'),
        lambda: page.locator('[data-e2e="user-repost-tab"]'),
    ]

    for get_locator in strategies:
        try:
            tab = get_locator()
            if tab.count() > 0:
                tab.first.click()
                human_delay(2.0, 4.0)
                return
        except Exception:
            continue

    for label in REPOST_TAB_LABELS:
        tab = page.get_by_role("tab", name=label, exact=False)
        try:
            if tab.count() > 0:
                tab.first.click()
                human_delay(2.0, 4.0)
                return
        except Exception:
            continue

    raise RuntimeError("Reposts sekmesi bulunamadi. Hesap gizli olabilir veya sekme kapalidir.")


def parse_repost_api_payload(payload: dict, reposted_by: str) -> list[RepostInfo]:
    results: list[RepostInfo] = []
    item_list = payload.get("itemList") or payload.get("items") or []
    for item in item_list:
        info = repost_from_api_item(item, reposted_by)
        if info:
            results.append(info)
    return results


def parse_universal_data(page, reposted_by: str) -> RepostInfo | None:
    script_content = page.locator(
        'script#__UNIVERSAL_DATA_FOR_REHYDRATION__'
    ).text_content(timeout=5000)
    if not script_content:
        return None

    data = json.loads(script_content)
    default_scope = data.get("__DEFAULT_SCOPE__", {})

    for key, value in default_scope.items():
        if not isinstance(value, dict):
            continue
        if "repost" not in key.lower():
            continue
        item_list = value.get("itemList") or value.get("items") or []
        for item in item_list:
            info = repost_from_api_item(item, reposted_by)
            if info:
                return info

    return None


def get_latest_repost(
    page,
    reposted_by: str,
    captured_reposts: list[RepostInfo],
) -> RepostInfo:
    if captured_reposts:
        return captured_reposts[0]

    links = page.locator('a[href*="/video/"]').all()
    for link in links:
        href = link.get_attribute("href")
        if not href:
            continue
        if href.startswith("/"):
            href = f"https://www.tiktok.com{href}"
        info = repost_from_url(href, reposted_by)
        if info:
            return info

    from_universal = parse_universal_data(page, reposted_by)
    if from_universal:
        return from_universal

    raise RuntimeError("Repost videosu bulunamadi. Sayfa yuklenmemis veya repost yok.")


def scrape_latest_repost(username: str) -> RepostInfo:
    username = normalize_username(username)
    profile_url = PROFILE_URL.format(username=username)
    captured_reposts: list[RepostInfo] = []

    with Stealth().use_sync(sync_playwright()) as playwright:
        browser = playwright.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            extra_http_headers={
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        page = context.new_page()

        def on_response(response) -> None:
            if "/api/repost/item_list" not in response.url:
                return
            if response.status != 200:
                return
            try:
                payload = response.json()
                captured_reposts.extend(parse_repost_api_payload(payload, username))
            except Exception:
                return

        page.on("response", on_response)

        print(f"[BILGI] Profil aciliyor: {profile_url}")
        page.goto(profile_url, wait_until="networkidle", timeout=90000)
        human_delay(3.0, 5.0)

        dismiss_cookie_banner(page)
        human_delay(1.0, 2.0)

        page.mouse.wheel(0, random.randint(200, 500))
        human_delay(1.0, 2.0)

        click_reposts_tab(page)

        try:
            page.wait_for_selector('a[href*="/video/"]', timeout=20000)
        except PlaywrightTimeoutError:
            human_delay(2.0, 3.0)

        human_delay(1.5, 2.5)
        latest = get_latest_repost(page, username, captured_reposts)

        browser.close()
        return latest


def main() -> int:
    username = normalize_username(TIKTOK_USERNAME)
    if not username or username == "kullaniciadi":
        print("[HATA] Lutfen TIKTOK_USERNAME degiskenini doldurun.")
        return 1

    try:
        latest = scrape_latest_repost(username)
    except Exception as exc:
        print(f"[HATA] Kazima basarisiz: {exc}")
        return 1

    print(f"[BILGI] En son repost (orijinal): {latest.original_url}")
    print(f"[BILGI] Orijinal yazar: @{latest.original_author}")

    previous = read_last_repost()
    if previous is None:
        save_last_repost(latest)
        print("[BILGI] Ilk calistirma; orijinal URL kaydedildi, bildirim gonderilmedi.")
        return 0

    if latest.video_id == previous.video_id:
        if latest.original_url != previous.original_url:
            save_last_repost(latest)
            print("[BILGI] Kayitli URL guncellendi (orijinal yazar linki).")
        print("[BILGI] Yeni repost yok.")
        return 0

    message = (
        f"Yeni TikTok repost!\n"
        f"Repost eden: @{latest.reposted_by}\n"
        f"Orijinal yazar: @{latest.original_author}\n"
        f"Video (repost kaldirilsa da calisir):\n{latest.original_url}"
    )
    try:
        send_telegram_message(message)
    except requests.RequestException as exc:
        print(f"[HATA] Telegram gonderimi basarisiz: {exc}")
        return 1

    save_last_repost(latest)
    print("[BILGI] Yeni orijinal URL kaydedildi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
