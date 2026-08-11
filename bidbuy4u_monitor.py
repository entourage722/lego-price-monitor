"""
擷取 bidbuy4u 全部樂高商品（標題、特價、原價、是否售完、連結），
每次執行都會把結果整批 POST 給 Google Apps Script，寫入時採「快照模式」
（該站的分頁會被整個清空重寫，不是一直往下累加）。

用法（本機測試）:
    pip install playwright requests
    playwright install --with-deps chromium
    GAS_WEBHOOK_URL=... GAS_WEBHOOK_SECRET=... python bidbuy4u_monitor.py
"""

import asyncio
import os
import sys

import requests
from playwright.async_api import async_playwright

BASE_URL = "https://www.bidbuy4u.com.tw/products?sort_by=lowest_price&order_by=desc&limit=72"
PAGE_SIZE = 72
MAX_PAGES = 30  # 安全上限

GAS_WEBHOOK_URL = os.environ.get("GAS_WEBHOOK_URL")
GAS_WEBHOOK_SECRET = os.environ.get("GAS_WEBHOOK_SECRET", "")


async def extract_products(page):
    cards = await page.query_selector_all(".product-item")
    items = []
    for card in cards:
        title_el = await card.query_selector(".title")
        title = (await title_el.inner_text()).strip() if title_el else None
        if not title:
            continue

        sale_el = await card.query_selector(".price__sale")
        crossed_el = await card.query_selector(".price-crossed")
        regular_el = await card.query_selector(".price__regular")

        sale_text = (await sale_el.inner_text()).strip() if sale_el else None
        crossed_text = (await crossed_el.inner_text()).strip() if crossed_el else None
        regular_text = (await regular_el.inner_text()).strip() if regular_el else None

        def to_int(s):
            if not s:
                return None
            digits = "".join(ch for ch in s if ch.isdigit())
            return int(digits) if digits else None

        if sale_text:
            sale_price = to_int(sale_text)
            orig_price = to_int(crossed_text) or to_int(regular_text)
        else:
            sale_price = to_int(regular_text)
            orig_price = sale_price

        text = await card.inner_text()
        sold_out = "售完" in text

        url = await card.evaluate('el => { const a = el.querySelector("a"); return a ? a.href : null; }')

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


async def scrape():
    all_items = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(locale="zh-TW")

        for page_num in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}&page={page_num}"
            await page.goto(url, wait_until="networkidle", timeout=60000)
            try:
                await page.wait_for_selector(".product-item", timeout=10000)
            except Exception:
                break

            items = await extract_products(page)
            print(f"第 {page_num} 頁：{len(items)} 件商品", file=sys.stderr)
            if not items:
                break

            all_items.extend(items)
            if len(items) < PAGE_SIZE:
                break

        await browser.close()
    return all_items


def send_to_sheet(items):
    if not GAS_WEBHOOK_URL:
        print("未設定 GAS_WEBHOOK_URL，僅印出結果，不寫入 Sheet。", file=sys.stderr)
        return
    payload = {"secret": GAS_WEBHOOK_SECRET, "source": "bidbuy4u", "items": items}
    resp = requests.post(GAS_WEBHOOK_URL, json=payload, timeout=60)
    resp.raise_for_status()
    print("寫入結果:", resp.text)


if __name__ == "__main__":
    data = asyncio.run(scrape())
    print(f"共擷取 {len(data)} 件商品")
    send_to_sheet(data)
