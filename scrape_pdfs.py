"""
Scraper for PDF documents from mdlcentrality.com/SocialMedia/IndexMDL

Downloads all linked PDF files from the page into a local directory.
Handles pagination, retries, and rate-limiting.
"""

import os
import re
import time
import argparse
import logging
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

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


def fetch_page(session: requests.Session, url: str, retries: int = 3) -> str | None:
    """Fetch a page with retries and exponential backoff."""
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            wait = 2 ** (attempt + 1)
            log.warning("Failed to fetch %s (attempt %d/%d): %s – retrying in %ds",
                        url, attempt + 1, retries, e, wait)
            time.sleep(wait)
    log.error("Could not fetch %s after %d attempts", url, retries)
    return None


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


def find_next_page(html: str, base_url: str) -> str | None:
    """Look for a 'next page' link for paginated listings."""
    soup = BeautifulSoup(html, "html.parser")

    # Common patterns for pagination links
    for a_tag in soup.find_all("a", href=True):
        text = a_tag.get_text(strip=True).lower()
        classes = " ".join(a_tag.get("class", [])).lower()

        if any(kw in text for kw in ("next", "nächste", "»", "›")):
            return urljoin(base_url, a_tag["href"])
        if "next" in classes:
            return urljoin(base_url, a_tag["href"])

    return None


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
    follow_pagination: bool = True,
    max_pages: int = 50,
) -> list[str]:
    """Main scraping function.

    Args:
        start_url: The URL to scrape PDF links from.
        output_dir: Directory to save downloaded PDFs.
        delay: Seconds to wait between downloads (be polite).
        skip_existing: Skip files that already exist locally.
        follow_pagination: Follow 'next page' links if present.
        max_pages: Maximum number of pages to follow.

    Returns:
        List of paths to successfully downloaded files.
    """
    os.makedirs(output_dir, exist_ok=True)
    session = get_session()

    all_pdf_links = []
    current_url = start_url
    page_num = 0

    # Collect PDF links from all pages
    while current_url and page_num < max_pages:
        page_num += 1
        log.info("Fetching page %d: %s", page_num, current_url)

        html = fetch_page(session, current_url)
        if html is None:
            break

        pdf_links = extract_pdf_links(html, current_url)
        log.info("Found %d PDF link(s) on page %d", len(pdf_links), page_num)
        all_pdf_links.extend(pdf_links)

        if follow_pagination:
            current_url = find_next_page(html, current_url)
        else:
            break

    if not all_pdf_links:
        log.warning("No PDF links found on the page(s).")
        return []

    log.info("Total PDF links found: %d", len(all_pdf_links))

    # Download all PDFs
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
        "--no-skip",
        action="store_true",
        help="Re-download files even if they already exist",
    )
    parser.add_argument(
        "--no-pagination",
        action="store_true",
        help="Don't follow pagination links",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=50,
        help="Maximum number of pages to follow (default: 50)",
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
        follow_pagination=not args.no_pagination,
        max_pages=args.max_pages,
    )


if __name__ == "__main__":
    main()
