"""
Scraper for PDF documents from mdlcentrality.com/SocialMedia/IndexMDL

Uses Selenium to load the page, click "Show 300" to display all entries,
then extracts and downloads all PDF links.
"""

import os
import re
import sys
import shutil
import time
import argparse
import logging
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

DEFAULT_URL = "https://www.mdlcentrality.com/SocialMedia/IndexMDL"
DEFAULT_OUTPUT_DIR = "downloaded_pdfs"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.mdlcentrality.com/",
}


def get_session() -> requests.Session:
    """Create a requests session with browser-like headers."""
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _find_chrome_binary() -> str | None:
    """Try to locate a Chrome/Chromium binary on the system."""
    candidates = [
        "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    ]
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    # Common Colab/Linux paths
    for path in [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/lib/chromium-browser/chromium-browser",
    ]:
        if os.path.isfile(path):
            return path
    return None


def _find_chromedriver() -> str | None:
    """Try to locate chromedriver on the system."""
    path = shutil.which("chromedriver")
    if path:
        return path
    for path in [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
        "/usr/local/bin/chromedriver",
    ]:
        if os.path.isfile(path):
            return path
    return None


def fetch_page_with_selenium(url: str, show_entries: int = 300) -> str | None:
    """Use Selenium to load the page, select 'Show N' entries, and return HTML."""
    log.info("Starting headless Chrome browser...")

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    # Auto-detect Chrome/Chromium binary (important for Colab)
    chrome_bin = _find_chrome_binary()
    if chrome_bin:
        log.info("Using Chrome binary: %s", chrome_bin)
        chrome_options.binary_location = chrome_bin

    # Auto-detect chromedriver
    chromedriver_path = _find_chromedriver()
    service = None
    if chromedriver_path:
        log.info("Using chromedriver: %s", chromedriver_path)
        service = Service(executable_path=chromedriver_path)

    driver = None
    try:
        if service:
            driver = webdriver.Chrome(service=service, options=chrome_options)
        else:
            driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(60)

        log.info("Loading page: %s", url)
        driver.get(url)

        # Wait for the page table to load
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.TAG_NAME, "table"))
        )
        log.info("Page loaded successfully.")

        # Try to find and change the "Show entries" dropdown
        try:
            # DataTables typically uses a <select> with name ending in '_length'
            select_el = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((
                    By.CSS_SELECTOR,
                    "select[name$='_length'], .dataTables_length select"
                ))
            )
            select = Select(select_el)

            # Try to select the desired value (e.g. 300, 200, 100, -1 for "All")
            selected = False
            for value in [str(show_entries), "-1", "All"]:
                try:
                    select.select_by_value(value)
                    selected = True
                    log.info("Selected 'Show %s' from dropdown.", value)
                    break
                except Exception:
                    continue

            if not selected:
                # Try selecting by visible text
                for text in [str(show_entries), "All", "300", "200", "100"]:
                    try:
                        select.select_by_visible_text(text)
                        selected = True
                        log.info("Selected 'Show %s' by visible text.", text)
                        break
                    except Exception:
                        continue

            if selected:
                # Wait for table to reload with new entries
                time.sleep(3)
                WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
                )
                log.info("Table reloaded with more entries.")
            else:
                log.warning("Could not change 'Show entries' dropdown. Using default view.")

        except Exception as e:
            log.warning("Could not find 'Show entries' dropdown: %s", e)

        # Give extra time for all rows to render
        time.sleep(2)

        html = driver.page_source
        log.info("Captured page source (%d characters).", len(html))
        return html

    except Exception as e:
        log.error("Selenium error: %s", e)
        log.error(
            "Troubleshooting tips:\n"
            "  - Google Colab: run this cell first:\n"
            "      !apt-get update && apt-get install -y chromium-browser chromium-chromedriver\n"
            "  - Local machine: install Chrome/Chromium and matching chromedriver\n"
            "  - Check 'chromedriver --version' matches your Chrome version"
        )
        return None
    finally:
        if driver:
            driver.quit()
            log.info("Browser closed.")


def extract_pdf_links(html: str, base_url: str) -> list[dict]:
    """Extract all PDF links from the HTML page.

    Returns a list of dicts with keys 'url', 'filename', and 'text'.
    """
    soup = BeautifulSoup(html, "html.parser")
    pdf_links = []
    seen_urls = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()

        # Match links ending in .pdf or containing .pdf before query params
        if not re.search(r"\.pdf(\?.*)?$", href, re.IGNORECASE):
            continue

        full_url = urljoin(base_url, href)

        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        # Derive a filename from the URL
        path = urlparse(full_url).path
        filename = unquote(os.path.basename(path))
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        link_text = a_tag.get_text(strip=True) or filename

        pdf_links.append({
            "url": full_url,
            "filename": filename,
            "text": link_text,
        })

    return pdf_links


def download_pdf(
    session: requests.Session,
    url: str,
    dest_path: str,
    retries: int = 3,
    skip_existing: bool = True,
) -> bool:
    """Download a single PDF file. Returns True on success."""
    if skip_existing and os.path.exists(dest_path):
        log.info("Skipping (already exists): %s", os.path.basename(dest_path))
        return True

    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=60, stream=True)
            resp.raise_for_status()

            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            size_kb = os.path.getsize(dest_path) / 1024
            log.info("Downloaded: %s (%.1f KB)", os.path.basename(dest_path), size_kb)
            return True

        except requests.RequestException as e:
            wait = 2 ** (attempt + 1)
            log.warning("Download failed for %s (attempt %d/%d): %s – retrying in %ds",
                        url, attempt + 1, retries, e, wait)
            time.sleep(wait)

    log.error("Could not download %s after %d attempts", url, retries)
    return False


def sanitize_filename(name: str) -> str:
    """Remove or replace characters that are problematic in filenames."""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(". ")
    return name if name else "unnamed.pdf"


def scrape_pdfs(
    start_url: str = DEFAULT_URL,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    delay: float = 1.0,
    skip_existing: bool = True,
    show_entries: int = 300,
) -> list[str]:
    """Main scraping function.

    Args:
        start_url: The URL to scrape PDF links from.
        output_dir: Directory to save downloaded PDFs.
        delay: Seconds to wait between downloads (be polite).
        skip_existing: Skip files that already exist locally.
        show_entries: Number of entries to show (clicks 'Show N' dropdown).

    Returns:
        List of paths to successfully downloaded files.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Step 1: Use Selenium to load page and click "Show 300"
    html = fetch_page_with_selenium(start_url, show_entries=show_entries)
    if html is None:
        log.error("Failed to load page with Selenium.")
        return []

    # Step 2: Extract PDF links from the fully rendered page
    all_pdf_links = extract_pdf_links(html, start_url)

    if not all_pdf_links:
        log.warning("No PDF links found on the page.")
        return []

    log.info("Total PDF links found: %d", len(all_pdf_links))

    # Step 3: Download all PDFs using requests (faster than Selenium)
    session = get_session()
    downloaded = []
    for i, link in enumerate(all_pdf_links, 1):
        filename = sanitize_filename(link["filename"])
        dest_path = os.path.join(output_dir, filename)

        # Handle duplicate filenames
        if os.path.exists(dest_path) and not skip_existing:
            base, ext = os.path.splitext(filename)
            filename = f"{base}_{i}{ext}"
            dest_path = os.path.join(output_dir, filename)

        log.info("[%d/%d] Downloading: %s", i, len(all_pdf_links), link["text"])
        success = download_pdf(session, link["url"], dest_path,
                               skip_existing=skip_existing)
        if success:
            downloaded.append(dest_path)

        if delay > 0 and i < len(all_pdf_links):
            time.sleep(delay)

    log.info("Done. Downloaded %d/%d files to '%s'",
             len(downloaded), len(all_pdf_links), output_dir)
    return downloaded


def main():
    parser = argparse.ArgumentParser(
        description="Scrape PDF documents from mdlcentrality.com"
    )
    parser.add_argument(
        "-u", "--url",
        default=DEFAULT_URL,
        help="URL to scrape (default: %(default)s)",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory (default: %(default)s)",
    )
    parser.add_argument(
        "-d", "--delay",
        type=float,
        default=1.0,
        help="Delay in seconds between downloads (default: 1.0)",
    )
    parser.add_argument(
        "--show-entries",
        type=int,
        default=300,
        help="Number of entries to show on the page (default: 300)",
    )
    parser.add_argument(
        "--no-skip",
        action="store_true",
        help="Re-download files even if they already exist",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    scrape_pdfs(
        start_url=args.url,
        output_dir=args.output_dir,
        delay=args.delay,
        skip_existing=not args.no_skip,
        show_entries=args.show_entries,
    )


if __name__ == "__main__":
    main()
