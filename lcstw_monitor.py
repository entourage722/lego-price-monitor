"""
擷取 lcstw.com.tw（LEGO 官方授權經銷商 SUNKIDS）「限時折價」頁面的折扣商品，
每次執行都會把結果整批 POST 給 Google Apps Script，寫入時採「快照模式」
（該站的分頁會被整個清空重寫，不是一直往下累加）。

這個頁面（/categories/sales）本身就只列出「已經在打折」的商品，而且是伺服器端
直接算好塞進 HTML 裡（不是前端另外打 API 抓資料），所以不需要像 momo 那樣自己
設門檻篩選，也不需要 Playwright 開瀏覽器渲染，用 requests 直接抓就好，快很多。

用法（本機測試）:
    pip install requests beautifulsoup4
    GAS_WEBHOOK_URL=... GAS_WEBHOOK_SECRET=... python lcstw_monitor.py
"""

import os
import re
import sys

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.lcstw.com.tw/categories/sales"
PAGE_SIZE = 72
MAX_PAGES = 10  # 安全上限（目前只有 1~2 頁）

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
    cards = soup.select("a.Product-item")
    items = []
    for card in cards:
        title_el = card.select_one(".title")
        title = title_el.get_text(strip=True) if title_el else None
        if not title:
            continue

        sale_el = card.select_one(".price__sale, .price-sale")
        crossed_el = card.select_one(".price-crossed")
        regular_el = card.select_one(".price__regular")

        sale_text = sale_el.get_text(strip=True) if sale_el else None
        crossed_text = crossed_el.get_text(strip=True) if crossed_el else None
        regular_text = regular_el.get_text(strip=True) if regular_el else None

        if sale_text:
            sale_price = to_int(sale_text)
            orig_price = to_int(crossed_text) or to_int(regular_text)
        else:
            sale_price = to_int(regular_text)
            orig_price = sale_price

        sold_out = card.select_one(".sold-out-item") is not None

        href = card.get("href")
        url = None
        if href:
            url = href if href.startswith("http") else "https://www.lcstw.com.tw" + href

        discount = None
        if sale_price and orig_price and orig_price > 0:
            discount = round((sale_price / orig_price) * 10, 2)

        items.append(
            {
                "name": title,
                "sale_price": sale_price,
                "orig_price": orig_price,
                "discount": discount,
                "sold_out": sold_out,
                "url": url,
            }
        )
    return items


def scrape():
    all_items = []
    session = requests.Session()
    session.headers.update(HEADERS)

    for page_num in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}?page={page_num}&limit={PAGE_SIZE}"
        resp = session.get(url, timeout=30)
        resp.raise_for_status()

        items = extract_products(resp.text)
        print(f"第 {page_num} 頁：{len(items)} 件商品", file=sys.stderr)
        if not items:
            break

        all_items.extend(items)
        if len(items) < PAGE_SIZE:
            break

    return all_items


def send_to_sheet(items):
    if not GAS_WEBHOOK_URL:
        print("未設定 GAS_WEBHOOK_URL，僅印出結果，不寫入 Sheet。", file=sys.stderr)
        return
    payload = {"secret": GAS_WEBHOOK_SECRET, "source": "lcstw", "items": items}
    resp = requests.post(GAS_WEBHOOK_URL, json=payload, timeout=60)
    resp.raise_for_status()
    print("寫入結果:", resp.text)


if __name__ == "__main__":
    data = scrape()
    print(f"共擷取 {len(data)} 件商品")
    send_to_sheet(data)
