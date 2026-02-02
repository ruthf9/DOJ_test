"""
Scraper for PDF documents from mdlcentrality.com/SocialMedia/IndexMDL

Uses Selenium to load the page, select "Show 300" from the ASP.NET dropdown,
verifies that 209 entries are present, then downloads all PDF links.
"""

import os
import re
import shutil
import subprocess
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
EXPECTED_ENTRIES = 209

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

# ASP.NET dropdown element ID for "Show N" selector
DROPDOWN_ID = "ContentPlaceHolder1_ddlPageSize"


def get_session() -> requests.Session:
    """Create a requests session with browser-like headers."""
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _ensure_chrome_installed() -> None:
    """Install Google Chrome Stable if no suitable browser is found.

    Colab ships with an old Chromium (v114) whose chromedriver crashes.
    Installing google-chrome-stable gives us a modern browser, and
    Selenium 4.6+ auto-downloads the matching chromedriver.
    """
    if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        return  # Already installed

    log.info("google-chrome not found. Attempting auto-install...")
    try:
        subprocess.run(
            "wget -q -O /tmp/chrome.deb "
            "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb "
            "&& apt-get install -y -qq /tmp/chrome.deb 2>/dev/null "
            "&& rm /tmp/chrome.deb",
            shell=True, check=True, capture_output=True,
        )
        log.info("Google Chrome Stable installed successfully.")
    except (subprocess.CalledProcessError, FileNotFoundError):
        log.warning(
            "Auto-install failed. Please install Chrome manually:\n"
            "  Google Colab:\n"
            "    !wget -q -O /tmp/chrome.deb "
            "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb\n"
            "    !apt-get install -y /tmp/chrome.deb\n"
            "  macOS:  brew install --cask google-chrome\n"
            "  Ubuntu: sudo apt install google-chrome-stable"
        )


def _create_chrome_driver() -> webdriver.Chrome:
    """Create a headless Chrome driver.

    Installs google-chrome-stable if needed, then lets Selenium 4.6+
    auto-manage the matching chromedriver.
    """
    _ensure_chrome_installed()

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    # Prefer google-chrome (modern) over old system chromium
    for binary in ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]:
        path = shutil.which(binary)
        if path:
            log.info("Using browser: %s", path)
            chrome_options.binary_location = path
            break

    # Selenium 4.6+ will auto-download matching chromedriver
    log.info("Starting Chrome (Selenium auto-manages chromedriver)...")
    driver = webdriver.Chrome(options=chrome_options)
    driver.set_page_load_timeout(60)
    return driver


def fetch_page_with_all_entries(url: str) -> str:
    """Load the page, select 'Show 300' from the ASP.NET dropdown, return HTML."""
    driver = _create_chrome_driver()
    try:
        log.info("Loading page: %s", url)
        driver.get(url)

        # Wait for the dropdown to be present
        dropdown_el = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, DROPDOWN_ID))
        )
        log.info("Page loaded. Found dropdown #%s.", DROPDOWN_ID)

        # Select "300" to show all entries
        select = Select(dropdown_el)
        current_value = select.first_selected_option.get_attribute("value")
        log.info("Current dropdown value: %s", current_value)

        if current_value != "300":
            log.info("Selecting '300' from dropdown...")
            select.select_by_value("300")

            # The ASP.NET __doPostBack will cause a page reload.
            # Wait for the page to become stale, then wait for the new table.
            WebDriverWait(driver, 30).until(EC.staleness_of(dropdown_el))
            log.info("Page is reloading after postback...")

            # Wait for the new page to fully load
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.ID, DROPDOWN_ID))
            )
            # Verify the new dropdown has 300 selected
            new_dropdown = driver.find_element(By.ID, DROPDOWN_ID)
            new_select = Select(new_dropdown)
            log.info(
                "Dropdown now shows: %s",
                new_select.first_selected_option.get_attribute("value"),
            )
        else:
            log.info("Dropdown already set to 300.")

        # Give the table time to fully render
        time.sleep(2)

        html = driver.page_source
        log.info("Captured page source (%d characters).", len(html))
        return html

    finally:
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

        if not re.search(r"\.pdf(\?.*)?$", href, re.IGNORECASE):
            continue

        full_url = urljoin(base_url, href)

        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

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
            log.warning(
                "Download failed for %s (attempt %d/%d): %s – retrying in %ds",
                url, attempt + 1, retries, e, wait,
            )
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
    expected_entries: int = EXPECTED_ENTRIES,
) -> list[str]:
    """Main scraping function.

    1. Load page and select "Show 300"
    2. Verify that exactly `expected_entries` PDF links are found
    3. Download all PDFs

    Returns list of paths to successfully downloaded files.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Step 1: Load page with Selenium and select "Show 300"
    html = fetch_page_with_all_entries(start_url)

    # Step 2: Extract PDF links
    pdf_links = extract_pdf_links(html, start_url)

    if not pdf_links:
        log.error("No PDF links found on the page. Aborting.")
        return []

    log.info("Found %d PDF links.", len(pdf_links))

    # Step 3: Verify expected count
    if len(pdf_links) != expected_entries:
        log.error(
            "Expected %d entries but found %d. "
            "The page content may have changed. Aborting download.",
            expected_entries,
            len(pdf_links),
        )
        log.info("Found links:")
        for i, link in enumerate(pdf_links, 1):
            log.info("  %d. %s -> %s", i, link["text"], link["url"])
        return []

    log.info("Entry count matches expected %d. Starting downloads.", expected_entries)

    # Step 4: Download all PDFs
    session = get_session()
    downloaded = []
    for i, link in enumerate(pdf_links, 1):
        filename = sanitize_filename(link["filename"])
        dest_path = os.path.join(output_dir, filename)

        # Handle duplicate filenames
        if os.path.exists(dest_path) and not skip_existing:
            base, ext = os.path.splitext(filename)
            filename = f"{base}_{i}{ext}"
            dest_path = os.path.join(output_dir, filename)

        log.info("[%d/%d] %s", i, len(pdf_links), link["text"])
        success = download_pdf(
            session, link["url"], dest_path, skip_existing=skip_existing,
        )
        if success:
            downloaded.append(dest_path)

        if delay > 0 and i < len(pdf_links):
            time.sleep(delay)

    log.info(
        "Done. Downloaded %d/%d files to '%s'.",
        len(downloaded), len(pdf_links), output_dir,
    )
    return downloaded


def main():
    parser = argparse.ArgumentParser(
        description="Scrape PDF documents from mdlcentrality.com/SocialMedia/IndexMDL"
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
        help="Delay between downloads in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--expected-entries",
        type=int,
        default=EXPECTED_ENTRIES,
        help="Expected number of PDF entries (default: %(default)s)",
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
        expected_entries=args.expected_entries,
    )


if __name__ == "__main__":
    main()
