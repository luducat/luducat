# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# steam_scraper.py

"""
Web scraper for Steam store pages.
"""

import os
import logging
from bs4 import BeautifulSoup
from typing import List, Dict, Any

from luducat.plugins.sdk.network import RequestException, RequestTimeout, Response

from .config import STEAM_STORE_BASE, CACHE_DIR, REQUEST_TIMEOUT, PROBE_TIMEOUT
from .exceptions import ScrapingError, AppNotFoundError

logger = logging.getLogger(__name__)


class SteamScraper:
    """Scraper for Steam store pages."""

    def __init__(self, cache_dir: str = None, http_client=None):
        """Initialize Steam scraper.

        Args:
            cache_dir: Directory to store downloaded images (defaults to config.CACHE_DIR)
            http_client: PluginHttpClient for all HTTP requests
        """
        self.cache_dir = cache_dir or CACHE_DIR
        self._http = http_client

        # Set cookies to bypass age gate on the plugin session.
        # birthtime=-473385600 is January 1, 1955 (old enough for any age restriction)
        if self._http:
            sess = self._http.session
            sess.cookies.set('birthtime', '-473385600', domain='store.steampowered.com')
            sess.cookies.set('mature_content', '1', domain='store.steampowered.com')
            sess.cookies.set('wants_mature_content', '1', domain='store.steampowered.com')

        # Create cache directory if it doesn't exist
        os.makedirs(self.cache_dir, exist_ok=True)

    def get_store_page(self, appid: int) -> str:
        """Fetch Steam store page HTML.

        Args:
            appid: Steam application ID

        Returns:
            HTML content of the store page

        Raises:
            AppNotFoundError: If page returns 404
            ScrapingError: If request fails
        """
        url = f"{STEAM_STORE_BASE}/app/{appid}"

        try:
            response = self._http.get(url, timeout=REQUEST_TIMEOUT)

            if response.status_code == 404:
                raise AppNotFoundError(f"App {appid} not found on Steam store")

            response.raise_for_status()

            # Check if we hit an age gate despite cookies
            if self._is_age_gate(response.text):
                logger.warning(f"Age gate detected for app {appid}, attempting to bypass...")
                response = self._handle_age_gate(appid)

            return response.text

        except RequestTimeout as e:
            raise ScrapingError(f"Timeout while fetching store page for app {appid}") from e
        except RequestException as e:
            raise ScrapingError(f"Failed to fetch store page: {e}") from e

    def _is_age_gate(self, html: str) -> bool:
        """Check if the response is an age gate page.

        Args:
            html: HTML content to check

        Returns:
            True if age gate detected, False otherwise
        """
        age_gate_indicators = [
            'app_agegate',
            'Please enter your birth date',
            'agegate_birthday_selector',
            'agecheck_form'
        ]
        return any(indicator in html for indicator in age_gate_indicators)

    def _handle_age_gate(self, appid: int) -> Response:
        """Handle age gate by submitting verification form.

        Args:
            appid: Steam application ID

        Returns:
            Response after age verification

        Raises:
            ScrapingError: If age gate cannot be bypassed
        """
        # Submit age verification form
        verify_url = f"{STEAM_STORE_BASE}/agecheckset/app/{appid}/"

        # Form data to bypass age gate (born in 1955, old enough for anything)
        data = {
            'ageDay': '1',
            'ageMonth': 'January',
            'ageYear': '1955',
            'snr': '1_agecheck_agecheck__age-gate'
        }

        try:
            # Submit the form
            self._http.post(verify_url, data=data, timeout=REQUEST_TIMEOUT)

            # Try to fetch the page again
            url = f"{STEAM_STORE_BASE}/app/{appid}"
            response = self._http.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()

            # Check if still getting age gate
            if self._is_age_gate(response.text):
                raise ScrapingError(f"Unable to bypass age gate for app {appid}")

            logger.info(f"Successfully bypassed age gate for app {appid}")
            return response

        except RequestException as e:
            raise ScrapingError(f"Failed to handle age gate: {e}") from e

    def extract_screenshots(self, appid: int, html: str = None) -> List[str]:
        """Extract screenshot URLs from store page.

        Args:
            appid: Steam application ID
            html: Optional HTML content (will fetch if not provided)

        Returns:
            List of high-resolution screenshot URLs

        Raises:
            ScrapingError: If extraction fails
        """
        if html is None:
            html = self.get_store_page(appid)

        try:
            soup = BeautifulSoup(html, 'html.parser')
            screenshot_urls = []

            # Find screenshot thumbnails in the carousel
            screenshot_thumbs = soup.find_all('a', class_='highlight_screenshot_link')

            for thumb in screenshot_thumbs:
                # Get the data-screenshotid or href
                href = thumb.get('href', '')

                # Extract full-size image URL from the thumbnail link
                # Steam uses pattern: steam/apps/{appid}/ss_xxxxx.1920x1080.jpg
                if 'screenshot' in href:
                    # The href contains the full URL to screenshot page
                    # We need to extract the actual image URL
                    img_tag = thumb.find('img')
                    if img_tag:
                        thumb_url = img_tag.get('src', '')
                        # Convert thumbnail URL to full-size
                        # Thumbnails: .116x65.jpg or .600x338.jpg
                        # Full size: .1920x1080.jpg
                        full_url = thumb_url.replace('.116x65.jpg', '.1920x1080.jpg')
                        full_url = full_url.replace('.600x338.jpg', '.1920x1080.jpg')

                        if full_url and 'apps/' in full_url:
                            screenshot_urls.append(full_url)

            logger.info(f"Found {len(screenshot_urls)} screenshots for app {appid}")
            return screenshot_urls

        except Exception as e:
            raise ScrapingError(f"Failed to extract screenshots: {e}") from e

    def download_image(self, url: str, filepath: str) -> bool:
        """Download an image from URL to filepath.

        Args:
            url: Image URL
            filepath: Destination file path

        Returns:
            True if successful, False otherwise
        """
        try:
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(filepath), exist_ok=True)

            # Skip if file already exists
            if os.path.exists(filepath):
                logger.debug(f"Image already exists: {filepath}")
                return True

            response = self._http.get(url, timeout=REQUEST_TIMEOUT, stream=True)
            response.raise_for_status()

            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.debug(f"Downloaded image: {filepath}")
            return True

        except Exception as e:
            logger.error(f"Failed to download image {url}: {e}")
            return False

    def scrape_screenshots(
        self, appid: int,
        screenshot_urls: List[str] = None,
    ) -> List[Dict[str, Any]]:
        """Download screenshots for an app.

        Args:
            appid: Steam application ID
            screenshot_urls: Optional list of screenshot URLs (will extract if not provided)

        Returns:
            List of dictionaries with filename and order information
        """
        if screenshot_urls is None:
            screenshot_urls = self.extract_screenshots(appid)

        # Create app-specific cache directory
        app_cache_dir = os.path.join(self.cache_dir, str(appid))
        os.makedirs(app_cache_dir, exist_ok=True)

        downloaded_images = []

        for idx, url in enumerate(screenshot_urls, start=1):
            # Determine file extension
            ext = '.jpg'
            if '.png' in url.lower():
                ext = '.png'
            elif '.gif' in url.lower():
                ext = '.gif'

            # Create filename: {appid}_1.jpg, {appid}_2.jpg, etc.
            filename = f"{appid}_{idx}{ext}"
            filepath = os.path.join(app_cache_dir, filename)

            # Download image
            if self.download_image(url, filepath):
                downloaded_images.append({
                    'filename': filename,
                    'order': idx,
                    'url': url
                })

        logger.info(f"Downloaded {len(downloaded_images)} screenshots for app {appid}")
        return downloaded_images

    def close(self):
        """Close the scraper."""
        pass  # Session lifecycle managed by NetworkManager

    def scrape_review_counts(self, appid: int) -> Dict[str, Any]:
        """Scrape review counts from store page as fallback.

        Used when SteamSpy has <20 reviews for positive/negative.
        Parses text like "81% of the 5,282 user reviews are positive"
        into numeric positive and negative counts.

        Args:
            appid: Steam application ID

        Returns:
            Dictionary with 'positive' and 'negative' counts, or empty dict
        """
        try:
            url = f"{STEAM_STORE_BASE}/app/{appid}/"
            response = self._http.get(url, timeout=REQUEST_TIMEOUT)

            if response.status_code == 404:
                logger.warning(f"Store page not found for app {appid}")
                return {}

            response.raise_for_status()

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.content, 'html.parser')

            # Find review section
            # Look for text like "81% of the 5,282 user reviews in your language are positive"
            review_summary = soup.find('div', class_='user_reviews_summary_row')
            if not review_summary:
                return {}

            desc_elem = review_summary.find('span', class_='responsive_hidden')
            if not desc_elem:
                return {}

            desc_text = desc_elem.get_text(strip=True)

            # Parse: "81% of the 5,282 user reviews are positive"
            import re
            pct_match = re.search(r'(\d+)%', desc_text)
            count_match = re.search(r'of the ([\d,]+) user reviews', desc_text)

            if pct_match and count_match:
                percentage = int(pct_match.group(1))
                total_str = count_match.group(1).replace(',', '')
                total = int(total_str)

                # Calculate positive and negative
                positive = int(total * percentage / 100)
                negative = total - positive

                logger.info(
                    "Scraped reviews for app %s: "
                    "%s positive, %s negative",
                    appid, positive, negative,
                )

                return {
                    'positive': positive,
                    'negative': negative
                }

            return {}

        except Exception as e:
            logger.error(f"Failed to scrape review counts for app {appid}: {e}")
            return {}

    def probe_asset_url(self, url: str) -> bool:
        """Probe if URL points to a valid image asset.

        Uses HEAD request to check Content-Type without downloading.

        Args:
            url: URL to probe

        Returns:
            True if URL returns an image Content-Type, False otherwise
        """
        try:
            # HEAD request - only get headers, don't download
            response = self._http.head(url, timeout=PROBE_TIMEOUT, allow_redirects=True)

            # Check status code
            if response.status_code != 200:
                logger.debug(
                    f"Asset probe failed: {url} "
                    f"returned status {response.status_code}"
                )
                return False

            # Check Content-Type
            content_type = response.headers.get('Content-Type', '').lower()

            # Valid image types
            valid_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/jpg']

            if any(img_type in content_type for img_type in valid_types):
                return True
            else:
                logger.debug(
                    f"Asset probe failed: {url} "
                    f"has invalid Content-Type '{content_type}' (expected image/*)"
                )
                return False

        except RequestTimeout:
            logger.debug(f"Asset probe timeout: {url}")
            return False
        except RequestException as e:
            logger.debug(f"Asset probe error for {url}: {e}")
            return False
        except Exception as e:
            logger.debug(f"Asset probe blocked for {url}: {e}")
            return False
