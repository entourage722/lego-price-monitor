"""
監控 Coupang 台灣搜尋頁「lego」關鍵字的結果，只保留品牌是 LEGO 的官方商品
（用商品名稱是否以「LEGO」開頭來過濾，排除副廠/相容積木）。
每次執行都把結果整批 POST 給 Google Apps Script，寫入時採「快照模式」。

注意：Coupang 有 Akamai 機器人防護，實測結果不穩定（有時候能正常抓到完整頁面，
有時候會被擋下顯示「沒有權限存取此頁面」）。這支腳本先在 GitHub Actions
的環境裡實際跑跑看，確認能不能穩定運作。

用法（本機測試）:
    pip install playwright requests
    playwright install --with-deps chromium
    GAS_WEBHOOK_URL=... GAS_WEBHOOK_SECRET=... python coupang_monitor.py
"""

import asyncio
import os
import re
import sys

import requests
from playwright.async_api import async_playwright

SEARCH_URL = "https://www.tw.coupang.com/search?q=lego&channel=relate"
MAX_SCROLLS = 6  # 往下捲動觸發延遲載入的次數上限

GAS_WEBHOOK_URL = os.environ.get("GAS_WEBHOOK_URL")
GAS_WEBHOOK_SECRET = os.environ.get("GAS_WEBHOOK_SECRET", "")


def parse_prices(text):
    """從卡片文字中取出所有 $數字，回傳 (sale_price, orig_price)。"""
    nums = re.findall(r"\$\s*([\d,]+)", text)
    nums = [int(n.replace(",", "")) for n in nums if n]
    if not nums:
        return None, None
    if len(nums) == 1:
        return nums[0], nums[0]
    # 有劃線原價 + 折扣後價格時，畫面上通常是「原價」在前、「特價」在後
    return nums[-1], nums[0]


async def extract_products(page):
    cards = await page.query_selector_all('a[href*="/products/"]')
    items = []
    seen_urls = set()
    for card in cards:
        href = await card.get_attribute("href")
        if not href:
            continue
        url = href if href.startswith("http") else f"https://www.tw.coupang.com{href}"
        if url in seen_urls:
            continue

        text = await card.inner_text()
        text = text.strip()
        if not text:
            continue

        # 商品名稱：取文字的第一行（Coupang 卡片文字通常是「名稱...價格...其他資訊」）
        first_line = text.split("\n")[0].strip()
        if not first_line:
            continue

        # 只保留品牌是 LEGO 開頭的官方商品，排除副廠／相容積木
        if not first_line.upper().startswith("LEGO"):
            continue

        sale_price, orig_price = parse_prices(text)
        if sale_price is None:
            continue

        seen_urls.add(url)
        items.append(
            {
                "name": first_line,
                "sale_price": sale_price,
                "orig_price": orig_price,
                "url": url,
            }
        )
    return items


async def scrape():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(locale="zh-TW")
        await page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)

        # 檢查是否被擋下（Coupang 的「沒有權限」頁面）
        body_text = await page.inner_text("body")
        if "沒有權限存取此頁面" in body_text or "permission" in body_text.lower():
            print("被 Coupang 擋下：頁面顯示沒有權限存取。", file=sys.stderr)
            await browser.close()
            return None

        for i in range(MAX_SCROLLS):
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(1000)

        items = await extract_products(page)
        print(f"擷取到 {len(items)} 件 LEGO 品牌商品", file=sys.stderr)

        await browser.close()
    return items


def send_to_sheet(items):
    if not GAS_WEBHOOK_URL:
        print("未設定 GAS_WEBHOOK_URL，僅印出結果，不寫入 Sheet。", file=sys.stderr)
        return
    payload = {"secret": GAS_WEBHOOK_SECRET, "source": "coupang", "items": items}
    resp = requests.post(GAS_WEBHOOK_URL, json=payload, timeout=60)
    resp.raise_for_status()
    print("寫入結果:", resp.text)


if __name__ == "__main__":
    data = asyncio.run(scrape())
    if data is None:
        print("本次執行被 Coupang 擋下，不寫入 Sheet，也不視為程式錯誤（結束碼 0）。", file=sys.stderr)
        sys.exit(0)
    print(f"共 {len(data)} 件商品")
    for d in data[:10]:
        print(f"  - {d['name']}  {d['sale_price']} / {d['orig_price']}")
    send_to_sheet(data)
