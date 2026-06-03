import argparse
import asyncio
import base64
import csv
import io
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import websockets

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


_DEFAULT_PORT = 9222

_msg_id = 0

# ─── CDP helpers ──────────────────────────────────────────────────────────────

async def cdp(ws, method: str, params: dict = None, session_id: str = None) -> dict:
    global _msg_id
    _msg_id += 1
    mid = _msg_id
    payload: dict = {"id": mid, "method": method, "params": params or {}}
    if session_id:
        payload["sessionId"] = session_id
    await ws.send(json.dumps(payload))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == mid:
            return msg.get("result", {})


async def evaluate(ws, sid: str, expression: str):
    result = await cdp(ws, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": True,
    }, session_id=sid)
    return result.get("result", {}).get("value")


# ─── Recorder (screenshots → GIF) ────────────────────────────────────────────

class Recorder:
    def __init__(self, enabled: bool):
        self.enabled = enabled and HAS_PIL
        self.frames: list = []
        if enabled and not HAS_PIL:
            print("[!] Pillow chưa cài — recording bị tắt. Chạy: pip install pillow")

    async def snap(self, ws, sid: str, label: str = "") -> None:
        if not self.enabled:
            return
        try:
            result = await cdp(
                ws, "Page.captureScreenshot",
                {"format": "jpeg", "quality": 55},
                session_id=sid,
            )
            data = result.get("data", "")
            if data:
                img = Image.open(io.BytesIO(base64.b64decode(data)))
                self.frames.append(img)
        except Exception as e:
            print(f"    [snap:{label}] {e}")

    def save(self, path: str) -> None:
        if not self.enabled or not self.frames:
            return
        out = Path(path).with_suffix(".gif")
        # Scale xuống ≤800px width để GIF nhỏ
        w, h = self.frames[0].size
        scale = min(1.0, 800 / w)
        target = (int(w * scale), int(h * scale))
        converted = []
        for f in self.frames:
            resized = f.resize(target, Image.LANCZOS) if f.size != target else f
            converted.append(resized.convert("P", palette=Image.ADAPTIVE, colors=128))
        converted[0].save(
            str(out), save_all=True, append_images=converted[1:],
            loop=0, duration=800, optimize=False,
        )
        print(f"  [✓] Recording: {out}  ({len(self.frames)} frames)")


# ─── Wait helpers ──────────────────────────────────────────────────────────────

_READY_JS = """
(() => {
    const section = document.querySelector('.shop-page__all-products-section');
    const items   = document.querySelectorAll('.shop-search-result-view__item.col-xs-2-4');
    return !!(section && items.length > 0);
})()
"""


async def wait_for_products(ws, sid: str, timeout: float = 30.0, poll: float = 0.5) -> bool:
    elapsed = 0.0
    while elapsed < timeout:
        if await evaluate(ws, sid, _READY_JS):
            return True
        await asyncio.sleep(poll)
        elapsed += poll
    return False


async def wait_for_page_turn(ws, sid: str, timeout: float = 30.0, poll: float = 0.3) -> bool:
    elapsed = 0.0
    # Bước 1: chờ items cũ biến mất
    while elapsed < timeout:
        if not await evaluate(ws, sid, _READY_JS):
            break
        await asyncio.sleep(poll)
        elapsed += poll
    # Bước 2: chờ items mới load
    while elapsed < timeout:
        if await evaluate(ws, sid, _READY_JS):
            return True
        await asyncio.sleep(poll)
        elapsed += poll
    return False


# ─── JS extractor ─────────────────────────────────────────────────────────────

EXTRACT_JS = r"""
(() => {
    const items = document.querySelectorAll('.shop-search-result-view__item.col-xs-2-4');
    return Array.from(items).map(item => {
        const group = item.querySelector('div[role="group"]');
        const title = (group?.getAttribute('aria-label') || '')
                        .replace('Product card: ', '').trim();
        const link = item.querySelector('a[href]')?.href || '';
        const priceContainer = item.querySelector('.truncate.flex.items-baseline');
        const priceNum = priceContainer?.querySelector('span.truncate')?.innerText?.trim() || '';
        const price = priceNum ? priceNum + '₫' : '';
        const ratingImg = item.querySelector('img[alt="rating-star"]');
        const rating = ratingImg?.nextElementSibling?.innerText?.trim() || '';
        const sold = item.querySelector('div.truncate.text-shopee-black87')?.innerText?.trim() || '';
        return { title, price, rating, sold, link };
    });
})()
"""

# ─── Tab management ───────────────────────────────────────────────────────────

async def open_tab(ws, url: str):
    target = await cdp(ws, "Target.createTarget", {"url": url})
    target_id = target["targetId"]
    session = await cdp(ws, "Target.attachToTarget", {"targetId": target_id, "flatten": True})
    sid = session["sessionId"]
    await cdp(ws, "Page.enable", {}, session_id=sid)  # required for screenshots
    return target_id, sid


# ─── Shop crawler ─────────────────────────────────────────────────────────────

def _normalize_shop_url(shop: str, site: str = "shopee.vn") -> str:
    if shop.startswith("http"):
        return shop.split("?")[0].rstrip("/")
    return f"https://{site}/{shop.strip('/')}"


async def crawl_shop(ws, shop: str, site: str, limit: int, rec: Recorder) -> list[dict]:
    base_url = _normalize_shop_url(shop, site)
    shop_name = base_url.rstrip("/").split("/")[-1]

    print(f"\n[shop] {shop_name}  →  {base_url}")
    target_id, sid = await open_tab(ws, base_url)
    await rec.snap(ws, sid, "open")

    # Chờ product grid render xong
    ready = await wait_for_products(ws, sid)
    if not ready:
        print(f"  [!] Timeout trang đầu — bỏ qua {shop_name}")
        await cdp(ws, "Target.closeTarget", {"targetId": target_id})
        return []

    # Click tab #product_list để GIF capture đúng vị trí (non-blocking: bỏ qua nếu Shopee block)
    clicked = await evaluate(
        ws, sid,
        "const a = document.querySelector('a[href$=\"#product_list\"]');"
        " a ? (a.click(), true) : false",
    )
    if clicked:
        await asyncio.sleep(0.5)  # cho scroll/animation chạy nhưng không block luồng
    await rec.snap(ws, sid, "products_loaded")

    current_url = await evaluate(ws, sid, "window.location.href")
    if current_url and ("verify" in current_url or "captcha" in current_url):
        print(f"  [!] Shopee block: {current_url}")
        await cdp(ws, "Target.closeTarget", {"targetId": target_id})
        return []

    total_pages_str = await evaluate(
        ws, sid,
        "document.querySelector('.shopee-mini-page-controller__total')?.innerText",
    )
    total_pages = int(total_pages_str or "1")
    print(f"  [i] Tổng số trang: {total_pages}")

    all_products: list[dict] = []
    page_num = 1

    while True:
        if len(all_products) >= limit:
            break

        print(f"  [→] Trang {page_num}/{total_pages}")
        products = await evaluate(ws, sid, EXTRACT_JS) or []

        if not products:
            print(f"    [!] Không lấy được sản phẩm ở trang {page_num}")
        else:
            for p in products:
                all_products.append(p)
                print(f"    {p['title'][:55]!r}  {p['price']}  ★{p['rating']}  {p['sold']}")
                if len(all_products) >= limit:
                    break

        await rec.snap(ws, sid, f"page_{page_num}")

        if len(all_products) >= limit or page_num >= total_pages:
            break

        next_disabled = await evaluate(
            ws, sid,
            "document.querySelector('.shopee-mini-page-controller__next-btn')?.disabled",
        )
        if next_disabled:
            print("  [i] Đã đến trang cuối")
            break

        await evaluate(
            ws, sid,
            "document.querySelector('.shopee-mini-page-controller__next-btn').click()",
        )

        ready = await wait_for_page_turn(ws, sid)
        if not ready:
            print(f"  [!] Timeout chờ trang {page_num + 1} — dừng {shop_name}")
            break

        page_num += 1

    await cdp(ws, "Target.closeTarget", {"targetId": target_id})
    print(f"  [✓] {shop_name}: {len(all_products)} sản phẩm")
    return all_products


# ─── Search crawler ────────────────────────────────────────────────────────────

async def crawl_search(ws, keyword: str, site: str, limit: int, rec: Recorder) -> list[dict]:
    search_url = f"https://{site}/search?keyword={quote_plus(keyword)}"
    target_id, sid = await open_tab(ws, search_url)
    await rec.snap(ws, sid, "search_open")

    if not await wait_for_products(ws, sid):
        await cdp(ws, "Target.closeTarget", {"targetId": target_id})
        return []

    current_url = await evaluate(ws, sid, "window.location.href")
    if current_url and ("verify" in current_url or "captcha" in current_url):
        await cdp(ws, "Target.closeTarget", {"targetId": target_id})
        return []

    search_js = r"""
    (() => {
        const items = document.querySelectorAll('li[data-sqe="item"]');
        return Array.from(items).map(item => {
            const group = item.querySelector('div[role="group"]');
            const title = (group?.getAttribute('aria-label') || '').replace('Product card: ', '').trim();
            const link = item.querySelector('a[href]')?.href || '';
            const priceContainer = item.querySelector('.truncate.flex.items-baseline');
            const priceNum = priceContainer?.querySelector('span.truncate')?.innerText?.trim() || '';
            const price = priceNum ? priceNum + '₫' : '';
            const ratingImg = item.querySelector('img[alt="rating-star"]');
            const rating = ratingImg?.nextElementSibling?.innerText?.trim() || '';
            const sold = item.querySelector('div.truncate.text-shopee-black87')?.innerText?.trim() || '';
            return { title, price, rating, sold, link };
        });
    })()
    """

    await rec.snap(ws, sid, "search_results")
    products = await evaluate(ws, sid, search_js) or []
    await cdp(ws, "Target.closeTarget", {"targetId": target_id})
    return products[:limit]


# ─── CDP connection (retry 5s × 6 = 30s) ─────────────────────────────────────

async def connect_cdp(cdp_url: str) -> websockets.WebSocketClientProtocol:
    print(f"[→] Kết nối CDP tại {cdp_url}")
    print("    Approve dialog trên Chrome nếu có — đang chờ (timeout 30s)...")
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 30
    attempt = 0
    while True:
        attempt += 1
        try:
            ws = await websockets.connect(cdp_url, open_timeout=4)
            version = await cdp(ws, "Browser.getVersion")
            print(f"[✓] Kết nối thành công — {version.get('product')}")
            return ws
        except Exception as e:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError(
                    f"Timeout 30s — chưa approve Chrome dialog hoặc Chrome chưa bật remote debugging.\n"
                    f"  Đang kết nối tới: {cdp_url}\n"
                    f"  Nếu Chrome chạy port khác, dùng: --port PORT  hoặc  --remote-url ws://127.0.0.1:PORT/devtools/browser"
                ) from e
            print(f"    [{attempt}] Chưa kết nối được ({e.__class__.__name__})  "
                  f"— thử lại sau 5s  (còn {remaining:.0f}s)")
            await asyncio.sleep(min(5.0, remaining))


# ─── Output helpers ───────────────────────────────────────────────────────────

def _stem(output_arg: str | None, shop_name: str, ts: str, multi: bool) -> str:
    """Tạo base name cho file (không extension)."""
    if output_arg:
        return f"{output_arg}-{shop_name}" if multi else output_arg
    return f"{shop_name}-{ts}"


def _save_data(path: str, fmt: str, rows: list[dict]) -> None:
    if not rows:
        print(f"  [!] Không có dữ liệu để lưu")
        return
    if fmt == "json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    else:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    print(f"  [✓] Data: {path}  ({len(rows)} dòng)")


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    if args.remote_url:
        raw = args.remote_url.strip()
        if raw.startswith(("ws://", "wss://")):
            cdp_url = raw
        else:
            # Dạng rút gọn: "127.0.0.1:9222" hoặc "localhost:9222"
            cdp_url = f"ws://{raw}/devtools/browser"
    else:
        cdp_url = f"ws://127.0.0.1:{args.port}/devtools/browser"
    ws = await connect_cdp(cdp_url)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = args.format
    multi = args.shops and len(args.shops) > 1

    try:
        if args.shops:
            for shop in args.shops:
                shop_name = _normalize_shop_url(shop, args.site).rstrip("/").split("/")[-1]
                stem = _stem(args.output, shop_name, ts, multi)
                rec = Recorder(args.record)

                products = await crawl_shop(ws, shop, args.site, args.limit, rec)

                _save_data(f"{stem}.{ext}", ext, products)
                rec.save(f"{stem}.gif")
        else:
            stem = args.output or f"search-{ts}"
            rec = Recorder(args.record)

            products = await crawl_search(ws, args.keyword, args.site, args.limit, rec)

            _save_data(f"{stem}.{ext}", ext, products)
            rec.save(f"{stem}.gif")

        total = args.limit  # rough
        print(f"\n[✓] Xong.")
    finally:
        await ws.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Shopee crawler dùng CDP WebSocket (không bị bot detection)"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--shops", nargs="+", metavar="SHOP",
        help="Tên shop hoặc URL đầy đủ, vd: growplus_officialstore vinamilk10tantrao",
    )
    group.add_argument("--keyword", help="Từ khóa tìm kiếm")
    parser.add_argument("--site", default="shopee.vn")
    cdp_group = parser.add_mutually_exclusive_group()
    cdp_group.add_argument(
        "--port", type=int, default=_DEFAULT_PORT, metavar="PORT",
        help=f"Port Chrome remote debugging (mặc định: {_DEFAULT_PORT}). "
             f"Xem port tại chrome://inspect/#remote-debugging → 'Server running at: 127.0.0.1:PORT'",
    )
    cdp_group.add_argument(
        "--remote-url", metavar="URL",
        help="Full WebSocket URL của Chrome DevTools, vd: ws://127.0.0.1:9222/devtools/browser. "
             "Dùng khi cần override toàn bộ địa chỉ (remote host, port lạ, v.v.)",
    )
    parser.add_argument("--limit", type=int, default=10000,
                        help="Số sản phẩm tối đa mỗi shop (mặc định: 10000)")
    parser.add_argument(
        "--output", default=None,
        help="Base name file đầu ra (không cần extension). "
             "Mặc định: {shop_name}-{timestamp}",
    )
    parser.add_argument(
        "--format", choices=["csv", "json"], default="csv",
        help="Định dạng dữ liệu: csv (mặc định) hoặc json",
    )
    parser.add_argument(
        "--record", action=argparse.BooleanOptionalAction, default=True,
        help="Ghi lại quá trình thành GIF (mặc định: bật). Dùng --no-record để tắt",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
