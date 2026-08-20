"""
擷取 PChome 24h 購物「LEGO樂高 新品專區」分類頁的全部商品，
每次執行都會把結果整批 POST 給 Google Apps Script，寫入時採「快照模式」
（該站的分頁會被整個清空重寫，不是一直往下累加）。

這個分類頁是伺服器端直接算好塞進 HTML 裡（不是前端另外打 API 抓資料），
所以不需要 Playwright 開瀏覽器渲染，用 requests 直接抓就好。

注意：這個分類頁除了正式的商品列表，頁面下方還有一個「你可能也喜歡」的
推薦區塊，裡面會混雜跟 LEGO 完全無關的商品（衛生紙、筆電…）。所以擷取時
特別把 CSS 選取範圍限定在 `section.u-mb12`（正式商品列表容器），避免把
推薦區塊的無關商品也一起抓進來。

另外，這個分類頁面沒有找到明確的「已售完/缺貨」標記，所以 sold_out 欄位
目前一律回傳 False——如果之後發現有商品其實已經缺貨但這裡沒標示出來，
需要再回頭調整判斷方式。

用法（本機測試）:
    pip install requests beautifulsoup4
    GAS_WEBHOOK_URL=... GAS_WEBHOOK_SECRET=... python pchome_monitor.py
"""

import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://24h.pchome.com.tw/category/DEDJ16C"
MAX_PAGES = 20  # 安全上限（目前約 10 頁、366 件商品）

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

GAS_WEBHOOK_URL = os.environ.get("GAS_WEBHOOK_URL")
GAS_WEBHOOK_SECRET = os.environ.get("GAS_WEBHOOK_SECRET", "")


def to_int(s):
    if not s:
        return None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def extract_products(html):
    soup = BeautifulSoup(html, "html.parser")
    # 只在正式商品列表容器裡找，避開頁面下方「你可能也喜歡」的無關推薦商品
    container = soup.select_one("section.u-mb12")
    if not container:
        return []

    cards = container.select("div.c-prodInfoV2")
    items = []
    for card in cards:
        title_el = card.select_one("h3.c-prodInfoV2__title")
        title = title_el.get_text(strip=True) if title_el else None
        if not title:
            continue

        sale_el = card.select_one(".c-prodInfoV2__priceValue--m")
        orig_el = card.select_one(".c-prodInfoV2__priceValue--xs")

        sale_price = to_int(sale_el.get_text(strip=True) if sale_el else None)
        orig_price = to_int(orig_el.get_text(strip=True) if orig_el else None) or sale_price

        link_el = card.select_one("a.c-prodInfoV2__link")
        href = link_el.get("href") if link_el else None
        url = None
        if href:
            url = href if href.startswith("http") else "https://24h.pchome.com.tw" + href

        discount = None
        if sale_price and orig_price and orig_price > 0:
            discount = round((sale_price / orig_price) * 10, 2)

        items.append(
            {
                "name": title,
                "sale_price": sale_price,
                "orig_price": orig_price,
                "discount": discount,
                "sold_out": False,  # 這個分類頁沒找到明確的售完標記
                "url": url,
            }
        )
    return items


def scrape():
    all_items = []
    session = requests.Session()
    session.headers.update(HEADERS)

    for page_num in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}?p={page_num}"
        resp = session.get(url, timeout=30)
        resp.raise_for_status()

        items = extract_products(resp.text)
        print(f"第 {page_num} 頁：{len(items)} 件商品", file=sys.stderr)
        if not items:
            break

        all_items.extend(items)
        time.sleep(0.3)  # 對網站客氣一點

    return all_items


def send_to_sheet(items):
    if not GAS_WEBHOOK_URL:
        print("未設定 GAS_WEBHOOK_URL，僅印出結果，不寫入 Sheet。", file=sys.stderr)
        return
    payload = {"secret": GAS_WEBHOOK_SECRET, "source": "pchome", "items": items}
    resp = requests.post(GAS_WEBHOOK_URL, json=payload, timeout=60)
    resp.raise_for_status()
    print("寫入結果:", resp.text)


if __name__ == "__main__":
    data = scrape()
    print(f"共擷取 {len(data)} 件商品")
    send_to_sheet(data)
