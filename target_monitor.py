"""
擷取 Target.com「LEGO」分類頁的全部商品，每次執行都會把結果整批 POST 給
Google Apps Script，寫入時採「快照模式」（該站的分頁會被整個清空重寫，
不是一直往下累加）。

這個分類頁的商品列表是前端 React 應用程式渲染出來的，加上 Target 用了
PerimeterX 這套防爬蟲機制（比 PChome 那邊擋 requests 的機制更嚴格），
所以用 Playwright 開真的瀏覽器抓，跟 momo、pchome 那兩支一樣的做法。

⚠️ 重要提醒：PerimeterX 是業界數一數二嚴格的反爬蟲系統，即使用 Playwright
開真瀏覽器，從 GitHub Actions 這種雲端機房 IP 送出的請求仍然有不小機率被
擋下來或跳出人機驗證（CAPTCHA）。這支程式目前還沒辦法完全確定能穩定通過，
需要實際跑一次 GitHub Actions 才能確認——如果被擋，可能要考慮其他方式
（例如降低頻率、換代理 IP，或乾脆放棄自動化改成人工偶爾檢查）。

分頁是用網址參數 `Nao`（每頁 24 件，Nao=0,24,48...）,目前全站約 783 件、
33 頁左右。

這個分類頁也沒找到明確的「已售完/缺貨」標記，所以 sold_out 欄位目前一律
回傳 False。原價/折扣資訊：頁面上如果商品在特價，通常會同時顯示特價跟
「reg $X」原價；如果只找到一個價格，就當作沒有折扣（orig_price = sale_price）。

用法（本機測試）:
    pip install playwright requests
    playwright install --with-deps chromium
    GAS_WEBHOOK_URL=... GAS_WEBHOOK_SECRET=... python target_monitor.py
"""

import asyncio
import os
import re
import sys

import requests
from playwright.async_api import async_playwright

BASE_URL = "https://www.target.com/b/lego/-/N-56h5n"
PAGE_SIZE = 24
MAX_PAGES = 40  # 安全上限（目前約 33 頁、783 件商品）

GAS_WEBHOOK_URL = os.environ.get("GAS_WEBHOOK_URL")
GAS_WEBHOOK_SECRET = os.environ.get("GAS_WEBHOOK_SECRET", "")


def to_float(s):
    if not s:
        return None
    cleaned = re.sub(r"[^\d.]", "", s)
    return float(cleaned) if cleaned else None


async def extract_products(page):
    cards = await page.query_selector_all(
        '[data-test="@web/ProductCard/ProductCardVariantWrapper"]'
    )
    items = []
    for card in cards:
        title_el = await card.query_selector('[data-test="@web/ProductCard/title"]')
        title = (await title_el.inner_text()).strip() if title_el else None
        if not title:
            continue

        price_el = await card.query_selector('[data-test="current-price"]')
        price_text = (await price_el.inner_text()) if price_el else None
        sale_price = to_float(price_text)

        # 有些商品特價時會同時顯示原價（reg $X），沒有的話就當作沒打折
        reg_el = await card.query_selector('[data-test="regular-price"]')
        reg_text = (await reg_el.inner_text()) if reg_el else None
        orig_price = to_float(reg_text) or sale_price

        link_el = await card.query_selector('a[href*="/p/"]')
        href = await link_el.get_attribute("href") if link_el else None
        url = None
        if href:
            url = href if href.startswith("http") else "https://www.target.com" + href

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


async def scrape():
    all_items = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(locale="en-US")

        offset = 0
        for _ in range(MAX_PAGES):
            url = f"{BASE_URL}?type=products&Nao={offset}"
            await page.goto(url, wait_until="networkidle", timeout=60000)
            try:
                await page.wait_for_selector(
                    '[data-test="@web/ProductCard/ProductCardVariantWrapper"]',
                    timeout=15000,
                )
            except Exception:
                break

            items = await extract_products(page)
            print(f"offset {offset}：{len(items)} 件商品", file=sys.stderr)
            if not items:
                break

            all_items.extend(items)
            if len(items) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

        await browser.close()

    return all_items


def send_to_sheet(items):
    if not GAS_WEBHOOK_URL:
        print("未設定 GAS_WEBHOOK_URL，僅印出結果，不寫入 Sheet。", file=sys.stderr)
        return
    payload = {"secret": GAS_WEBHOOK_SECRET, "source": "target", "items": items}
    resp = requests.post(GAS_WEBHOOK_URL, json=payload, timeout=60)
    resp.raise_for_status()
    print("寫入結果:", resp.text)


if __name__ == "__main__":
    data = asyncio.run(scrape())
    print(f"共擷取 {len(data)} 件商品")
    send_to_sheet(data)
