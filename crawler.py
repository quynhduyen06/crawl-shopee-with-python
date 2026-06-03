import argparse
import asyncio
from urllib.parse import quote_plus

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright


async def crawl_shopee(keyword: str, site: str, limit: int) -> None:
    search_url = f"https://{site}/search?keyword={quote_plus(keyword)}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(search_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            cards = await page.query_selector_all("a[data-sqe='link']")

            count = 0
            for card in cards:
                title_node = await card.query_selector("div[role='heading']")
                price_node = await card.query_selector("span:has-text('₫')")

                title = (await title_node.inner_text()) if title_node else None
                price = (await price_node.inner_text()) if price_node else None
                link = await card.get_attribute("href")

                if title:
                    count += 1
                    print(
                        {
                            "title": title.strip(),
                            "price": price.strip() if price else None,
                            "url": f"https://{site}{link}" if link and link.startswith("/") else link,
                        }
                    )
                    if count >= limit:
                        break
        except PlaywrightError as err:
            print(f"Unable to crawl Shopee: {err}")
        finally:
            await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Basic Shopee crawler with Playwright")
    parser.add_argument("--keyword", required=True, help="Keyword to search on Shopee")
    parser.add_argument("--site", default="shopee.vn", help="Shopee site domain (e.g. shopee.vn)")
    parser.add_argument("--limit", type=int, default=10, help="Maximum products to print")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(crawl_shopee(args.keyword, args.site, args.limit))
