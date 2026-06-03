# crawl-shopee-with-python

Basic setup to crawl Shopee search results with Python + Playwright.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
python crawler.py --keyword "tai nghe" --limit 10
```

### Optional arguments

- `--site`: Shopee site domain (default: `shopee.vn`)
- `--limit`: max number of products to print