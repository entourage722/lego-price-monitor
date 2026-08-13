"""
監控 momo 購物網 LEGO 分類頁，找出「特價 / 原價 <= 0.70」（70折以下）的商品，
並把結果 POST 給一個 Google Apps Script Web App，寫入 Google Sheet。

用法（本機測試）:
    pip install playwright requests
    playwright install --with-deps chromium
    GAS_WEBHOOK_URL=... GAS_WEBHOOK_SECRET=... python momo_monitor.py
"""

import asyncio
import os
import re
import sys

import requests
from playwright.async_api import async_playwright

CATEGORY_URL = "https://www.momoshop.com.tw/categories/2118900239"
DISCOUNT_THRESHOLD = 0.70  # <= 70折才收錄
MAX_PAGES = 30  # 安全上限，目前分類約 23 頁

GAS_WEBHOOK_URL = os.environ.get("GAS_WEBHOOK_URL")
GAS_WEBHOOK_SECRET = os.environ.get("GAS_WEBHOOK_SECRET", "")

# 商品卡片外層 div 的其中一個 class（momo 前台用 Tailwind，class 名稱長但穩定）
CARD_CLASS_MARKER = "mu-min-h-[280px]"


async def extract_cards(page):
    """取出目前頁面所有商品卡片的原始資訊。"""
    cards = await page.query_selector_all(f'div[class*="{CARD_CLASS_MARKER}"]')
    items = []
    for card in cards:
        title = await card.get_attribute("title")
        if not title:
            continue
        text = await card.inner_text()
        # 商品卡片文字裡會依序出現「$」「特價數字」「$」「原價數字」
        nums = re.findall(r"\$\s*\n?\s*([\d,]+)", text)
        if len(nums) < 2:
            # 沒有原價/特價兩個數字 = 沒有標示折扣，略過
            continue
        try:
            sale = int(nums[0].replace(",", ""))
            orig = int(nums[1].replace(",", ""))
        except ValueError:
            continue
        if orig <= 0 or sale <= 0 or sale > orig:
            continue
        url = await card.evaluate(
            'el => { const a = el.querySelector("a"); return a ? a.href : null; }'
        )
        items.append({"name": title.strip(), "sale_price": sale, "orig_price": orig, "url": url})
    return items


async def scrape():
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(locale="zh-TW")
        await page.goto(CATEGORY_URL, wait_until="networkidle", timeout=60000)

        for page_num in range(1, MAX_PAGES + 1):
            await page.wait_for_selector(f'div[class*="{CARD_CLASS_MARKER}"]', timeout=20000)
            items = await extract_cards(page)
            print(f"第 {page_num} 頁：擷取到 {len(items)} 件商品", file=sys.stderr)

            for item in items:
                ratio = item["sale_price"] / item["orig_price"]
                if ratio <= DISCOUNT_THRESHOLD:
                    results.append(
                        {
                            "name": item["name"],
                            "sale_price": item["sale_price"],
                            "orig_price": item["orig_price"],
                            "discount": round(ratio * 10, 2),  # e.g. 6.5 折
                            "url": item["url"],
                        }
                    )

            next_btn = await page.query_selector('button:has-text("下一頁")')
            if not next_btn:
                break
            disabled = await next_btn.get_attribute("disabled")
            if disabled is not None:
                break
            await next_btn.click()
            await page.wait_for_timeout(1500)

        await browser.close()
    return results


def send_to_sheet(items):
    if not GAS_WEBHOOK_URL:
        print("未設定 GAS_WEBHOOK_URL，僅印出結果，不寫入 Sheet。", file=sys.stderr)
        return
    payload = {"secret": GAS_WEBHOOK_SECRET, "source": "momo", "items": items}
    resp = requests.post(GAS_WEBHOOK_URL, json=payload, timeout=30)
    resp.raise_for_status()
    print("寫入結果:", resp.text)


if __name__ == "__main__":
    data = asyncio.run(scrape())
    print(f"符合 70 折以下的商品共 {len(data)} 件")
    for d in data:
        print(f"  - {d['name']}  {d['sale_price']} / {d['orig_price']} ({d['discount']}折)")
    send_to_sheet(data)
