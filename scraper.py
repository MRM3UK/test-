# scraper.py
import re
import json
import time
import base64
import logging
import hashlib
from urllib.parse import urljoin, urlparse, unquote, parse_qs

import requests
import cloudscraper
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BASE_URL = "https://kaamdesi.com"
MAX_PAGES = 5
REQUEST_DELAY = 2
MAX_IFRAME_DEPTH = 5  # Follow iframes up to 5 levels deep


def get_scraper():
    """Create cloudscraper session."""
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True
        }
    )
    scraper.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
    })
    return scraper


def fetch_page(scraper, url, referer=None, retries=3):
    """Fetch a page with retry logic."""
    headers = {}
    if referer:
        headers['Referer'] = referer
        headers['Origin'] = urlparse(referer).scheme + '://' + urlparse(referer).netloc

    for attempt in range(retries):
        try:
            logger.info(f"  Fetching: {url[:100]}... (attempt {attempt + 1})")
            response = scraper.get(url, timeout=30, headers=headers, allow_redirects=True)
            if response.status_code == 200:
                return response.text
            elif response.status_code == 403:
                logger.warning(f"  403 Forbidden: {url[:80]}")
                time.sleep(REQUEST_DELAY * 2)
            else:
                logger.warning(f"  HTTP {response.status_code}: {url[:80]}")
        except Exception as e:
            logger.warning(f"  Attempt {attempt + 1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(REQUEST_DELAY * (attempt + 1))
    return None


def extract_video_listings(html, page_url):
    """Extract all video page links from listing page."""
    soup = BeautifulSoup(html, 'lxml')
    videos = []
    found_urls = set()

    # ----- Try multiple selector strategies -----

    # Strategy 1: Article/post based
    for article in soup.select('article, .post, .video-item, .entry, .item, .thumb-block, .video-block'):
        link = article.find('a', href=True)
        if link:
            href = urljoin(page_url, link['href'])
            if href not in found_urls and is_content_url(href):
                title = extract_title_from_element(article, link)
                thumb = extract_thumbnail(article)
                found_urls.add(href)
                videos.append({'page_url': href, 'title': title, 'thumbnail': thumb})

    # Strategy 2: Heading links
    for heading in soup.select('h1 a, h2 a, h3 a, h4 a, .entry-title a, .post-title a'):
        href = urljoin(page_url, heading.get('href', ''))
        if href and href not in found_urls and is_content_url(href):
            title = heading.get_text(strip=True)
            found_urls.add(href)
            videos.append({'page_url': href, 'title': title, 'thumbnail': ''})

    # Strategy 3: Thumbnail/image links
    for a_tag in soup.select('a[href]'):
        href = urljoin(page_url, a_tag['href'])
        if href in found_urls:
            continue
        img = a_tag.find('img')
        if img and is_content_url(href):
            title = img.get('alt', '') or a_tag.get('title', '') or a_tag.get_text(strip=True)
            thumb = img.get('src', '') or img.get('data-src', '') or img.get('data-lazy-src', '')
            found_urls.add(href)
            videos.append({'page_url': href, 'title': title, 'thumbnail': thumb})

    # Strategy 4: All remaining links that look like content
    for a_tag in soup.find_all('a', href=True):
        href = urljoin(page_url, a_tag['href'])
        if href not in found_urls and is_content_url(href):
            title = a_tag.get('title', '') or a_tag.get_text(strip=True)
            if title and len(title) > 5:
                found_urls.add(href)
                videos.append({'page_url': href, 'title': title, 'thumbnail': ''})

    # Deduplicate
    seen = set()
    unique = []
    for v in videos:
        if v['page_url'] not in seen:
            seen.add(v['page_url'])
            v['title'] = clean_title(v['title'])
            if v['title']:
                unique.append(v)

    return unique


def is_content_url(url):
    """Check if URL looks like a content/video page."""
    parsed = urlparse(url)

    # Must be from the same domain or known patterns
    base_domain = urlparse(BASE_URL).netloc.replace('www.', '')

    skip_patterns = [
        '/page/', '/category/', '/tag/', '/author/', '/wp-admin/',
        '/wp-content/', '/wp-includes/', '/feed/', '/login', '/register',
        '/search/', '/contact', '/about', '/privacy', '/terms', '/dmca',
        '.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico',
        '#', 'javascript:', 'mailto:', '/cdn-cgi/'
    ]

    url_lower = url.lower()
    for pattern in skip_patterns:
        if pattern in url_lower:
            return False

    if base_domain in parsed.netloc:
        # Must have a path beyond just /
        path = parsed.path.strip('/')
        if path and '/' not in path and len(path) > 3:
            return True
        if path and len(path) > 5:
            return True

    return False


def extract_title_from_element(container, link):
    """Try to extract a good title from article container."""
    # Try heading first
    heading = container.find(['h1', 'h2', 'h3', 'h4'])
    if heading:
        return heading.get_text(strip=True)

    # Try link title attribute
    title = link.get('title', '').strip()
    if title:
        return title

    # Try image alt
    img = container.find('img')
    if img:
        alt = img.get('alt', '').strip()
        if alt:
            return alt

    # Try link text
    text = link.get_text(strip=True)
    if text and len(text) > 3:
        return text

    return ''


def extract_thumbnail(container):
    """Extract thumbnail URL from container."""
    img = container.find('img')
    if img:
        return (img.get('src', '') or img.get('data-src', '') or
                img.get('data-lazy-src', '') or img.get('data-original', ''))
    return ''


def clean_title(title):
    """Clean up a title string."""
    if not title:
        return ''
    title = ' '.join(title.split())
    title = title.strip('| -–—·•')
    title = title.strip()
    return title[:250]


# ==================== DEEP VIDEO EXTRACTION ====================

def extract_all_stream_urls(html, page_url, scraper, depth=0):
    """
    Aggressively extract video URLs using multiple methods.
    Follows iframes recursively.
    """
    if depth > MAX_IFRAME_DEPTH:
        return []

    all_urls = []
    soup = BeautifulSoup(html, 'lxml')

    # ===== Method 1: Direct <video> and <source> tags =====
    for video in soup.find_all('video'):
        src = video.get('src', '').strip()
        if src and is_video_url(src):
            all_urls.append(('direct', urljoin(page_url, src)))
        for source in video.find_all('source'):
            src = source.get('src', '').strip()
            if src and is_video_url(src):
                all_urls.append(('direct', urljoin(page_url, src)))
        # Check data attributes
        for attr in video.attrs:
            val = str(video[attr])
            if is_video_url(val):
                all_urls.append(('data-attr', urljoin(page_url, val)))

    # ===== Method 2: Regex scan for m3u8 / mp4 URLs in full HTML =====
    # Standard URLs
    m3u8_re = re.compile(r'''(https?://[^\s'"<>\\\)]+\.m3u8[^\s'"<>\\\)]*)''', re.I)
    mp4_re = re.compile(r'''(https?://[^\s'"<>\\\)]+\.mp4[^\s'"<>\\\)]*)''', re.I)

    for match in m3u8_re.findall(html):
        cleaned = clean_url(match)
        if cleaned:
            all_urls.append(('regex-m3u8', cleaned))

    for match in mp4_re.findall(html):
        cleaned = clean_url(match)
        if cleaned:
            all_urls.append(('regex-mp4', cleaned))

    # ===== Method 3: Escaped/encoded URLs =====
    # Many players use escaped slashes: https:\/\/domain.com\/path
    escaped_m3u8 = re.compile(r'''(https?:\\/\\/[^\s'"<>]+\.m3u8[^\s'"<>]*)''', re.I)
    escaped_mp4 = re.compile(r'''(https?:\\/\\/[^\s'"<>]+\.mp4[^\s'"<>]*)''', re.I)

    for match in escaped_m3u8.findall(html):
        cleaned = clean_url(match.replace('\\/', '/'))
        if cleaned:
            all_urls.append(('escaped-m3u8', cleaned))

    for match in escaped_mp4.findall(html):
        cleaned = clean_url(match.replace('\\/', '/'))
        if cleaned:
            all_urls.append(('escaped-mp4', cleaned))

    # ===== Method 4: Look in ALL <script> tags for player configs =====
    for script in soup.find_all('script'):
        script_text = script.string or ''
        if script_text:
            all_urls.extend(extract_from_script(script_text, page_url))

        # Also check src for external scripts that might contain player config
        script_src = script.get('src', '')
        if script_src and any(kw in script_src.lower() for kw in ['player', 'video', 'embed', 'stream']):
            ext_html = fetch_page(scraper, urljoin(page_url, script_src), referer=page_url, retries=1)
            if ext_html:
                all_urls.extend(extract_from_script(ext_html, page_url))

    # ===== Method 5: Base64 encoded URLs =====
    b64_pattern = re.compile(r'(?:atob|decode|base64)[(\s]*["\']([A-Za-z0-9+/=]{20,})["\']', re.I)
    for match in b64_pattern.findall(html):
        try:
            decoded = base64.b64decode(match).decode('utf-8', errors='ignore')
            if 'http' in decoded:
                for url_match in re.findall(r'https?://[^\s"\'<>]+', decoded):
                    if is_video_url(url_match):
                        all_urls.append(('base64', clean_url(url_match)))
        except Exception:
            pass

    # ===== Method 6: Hex encoded URLs =====
    hex_pattern = re.compile(r'\\x([0-9a-fA-F]{2})')
    hex_strings = re.findall(r'(?:\\x[0-9a-fA-F]{2}){10,}', html)
    for hex_str in hex_strings:
        try:
            decoded = hex_pattern.sub(lambda m: chr(int(m.group(1), 16)), hex_str)
            if 'http' in decoded:
                for url_match in re.findall(r'https?://[^\s"\'<>]+', decoded):
                    if is_video_url(url_match):
                        all_urls.append(('hex', clean_url(url_match)))
        except Exception:
            pass

    # ===== Method 7: URL encoded strings =====
    encoded_urls = re.findall(r'(https?%3A%2F%2F[^\s"\'<>&]+)', html, re.I)
    for eu in encoded_urls:
        try:
            decoded = unquote(eu)
            if is_video_url(decoded):
                all_urls.append(('urlencoded', clean_url(decoded)))
        except Exception:
            pass

    # ===== Method 8: Follow iframes recursively =====
    for iframe in soup.find_all('iframe'):
        iframe_src = iframe.get('src', '') or iframe.get('data-src', '') or iframe.get('data-lazy-src', '')
        if not iframe_src or iframe_src.startswith('about:'):
            continue

        iframe_url = urljoin(page_url, iframe_src.strip())

        # Skip known ad/tracking iframes
        ad_domains = ['google', 'facebook', 'twitter', 'doubleclick', 'adsense',
                      'analytics', 'syndication', 'disqus']
        if any(ad in iframe_url.lower() for ad in ad_domains):
            continue

        logger.info(f"  {'  ' * depth}↳ Following iframe (depth {depth + 1}): {iframe_url[:80]}...")

        # If iframe URL itself is a direct video
        if is_video_url(iframe_url):
            all_urls.append(('iframe-direct', iframe_url))
            continue

        # Fetch iframe content and recurse
        time.sleep(1)
        iframe_html = fetch_page(scraper, iframe_url, referer=page_url, retries=2)
        if iframe_html:
            nested = extract_all_stream_urls(iframe_html, iframe_url, scraper, depth + 1)
            all_urls.extend(nested)

            # Also check if iframe page has redirects / meta refresh
            meta_refresh = re.findall(
                r'<meta[^>]*http-equiv=["\']refresh["\'][^>]*content=["\'][^"\']*url=([^"\'>\s]+)',
                iframe_html, re.I
            )
            for redirect_url in meta_refresh:
                rurl = urljoin(iframe_url, redirect_url.strip())
                if is_video_url(rurl):
                    all_urls.append(('meta-refresh', rurl))
                else:
                    time.sleep(1)
                    rhtml = fetch_page(scraper, rurl, referer=iframe_url, retries=1)
                    if rhtml:
                        nested = extract_all_stream_urls(rhtml, rurl, scraper, depth + 1)
                        all_urls.extend(nested)

            # Check for window.location / document.location redirects
            location_redirects = re.findall(
                r'(?:window|document)\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
                iframe_html, re.I
            )
            for loc in location_redirects:
                loc_url = urljoin(iframe_url, loc.strip())
                if is_video_url(loc_url):
                    all_urls.append(('js-redirect', loc_url))

    # ===== Method 9: Look for API endpoints / AJAX calls =====
    api_patterns = [
        r'(?:fetch|axios\.get|ajax|XMLHttpRequest)[^"\']*["\']([^"\']+)["\']',
        r'url\s*:\s*["\']([^"\']+/(?:api|video|stream|source|embed)[^"\']*)["\']',
        r'\.get\(["\']([^"\']+(?:video|stream|source|play)[^"\']*)["\']',
        r'\.post\(["\']([^"\']+(?:video|stream|source|play)[^"\']*)["\']',
    ]
    for pattern in api_patterns:
        for match in re.findall(pattern, html, re.I):
            api_url = urljoin(page_url, match)
            if is_video_url(api_url):
                all_urls.append(('api', api_url))
            elif 'api' in api_url.lower() or 'source' in api_url.lower():
                # Try fetching the API endpoint
                time.sleep(1)
                api_resp = fetch_page(scraper, api_url, referer=page_url, retries=1)
                if api_resp:
                    try:
                        data = json.loads(api_resp)
                        urls_from_json = extract_urls_from_json(data, api_url)
                        all_urls.extend(urls_from_json)
                    except json.JSONDecodeError:
                        for url_match in re.findall(r'https?://[^\s"\'<>]+', api_resp):
                            if is_video_url(url_match):
                                all_urls.append(('api-response', clean_url(url_match)))

    # ===== Method 10: Data attributes on any element =====
    for elem in soup.find_all(True):
        for attr_name, attr_val in elem.attrs.items():
            if isinstance(attr_val, str) and is_video_url(attr_val):
                all_urls.append(('data-attr', urljoin(page_url, attr_val)))
            elif isinstance(attr_val, str) and ('video' in attr_name.lower() or 'stream' in attr_name.lower() or 'source' in attr_name.lower()):
                if attr_val.startswith('http'):
                    all_urls.append(('data-attr', urljoin(page_url, attr_val)))

    # ===== Method 11: Object/embed tags =====
    for obj in soup.find_all(['object', 'embed']):
        for attr in ['src', 'data', 'value']:
            val = obj.get(attr, '')
            if val and is_video_url(val):
                all_urls.append(('object', urljoin(page_url, val)))

    # Clean up results
    final = []
    seen = set()
    for source_type, url in all_urls:
        if url and url not in seen:
            url = clean_url(url)
            if url:
                seen.add(url)
                final.append((source_type, url))

    return final


def extract_from_script(script_text, page_url):
    """Extract video URLs from JavaScript code."""
    results = []

    # Common player setup patterns
    patterns = [
        # file/source/url/src assignments
        r'''["']?(?:file|source|src|url|video[_-]?url|stream[_-]?url|hls[_-]?url|mp4[_-]?url|video[_-]?src|play[_-]?url|m3u8[_-]?url)["']?\s*[:=]\s*["']([^"']+)["']''',
        # JW Player
        r'''jwplayer[^}]*file\s*:\s*["']([^"']+)["']''',
        r'''jwplayer[^}]*sources\s*:\s*\[([^\]]+)\]''',
        # Video.js
        r'''videojs[^}]*src\s*[:=]\s*["']([^"']+)["']''',
        # Clappr
        r'''Clappr[^}]*source\s*:\s*["']([^"']+)["']''',
        # Flowplayer
        r'''flowplayer[^}]*clip\s*:\s*\{[^}]*url\s*:\s*["']([^"']+)["']''',
        # Plyr
        r'''Plyr[^}]*source\s*[:=][^}]*src\s*:\s*["']([^"']+)["']''',
        # Generic player
        r'''player\.src\(\s*["']([^"']+)["']''',
        r'''player\.src\(\s*\{[^}]*src\s*:\s*["']([^"']+)["']''',
        r'''\.src\(\s*["']([^"']+\.(?:m3u8|mp4)[^"']*)["']''',
        r'''\.load\(\s*["']([^"']+\.(?:m3u8|mp4)[^"']*)["']''',
        r'''loadSource\(\s*["']([^"']+)["']''',
        r'''attachSource\(\s*["']([^"']+)["']''',
        # Direct assignments
        r'''var\s+(?:video|stream|source|file|url|src)\s*=\s*["']([^"']+)["']''',
        r'''let\s+(?:video|stream|source|file|url|src)\s*=\s*["']([^"']+)["']''',
        r'''const\s+(?:video|stream|source|file|url|src)\s*=\s*["']([^"']+)["']''',
        # PHP/template vars
        r'''video_url\s*=\s*["']([^"']+)["']''',
        r'''embed_url\s*=\s*["']([^"']+)["']''',
        r'''iframe_src\s*=\s*["']([^"']+)["']''',
        # JSON-like source arrays
        r'''"src"\s*:\s*"([^"]+\.(?:m3u8|mp4)[^"]*)"''',
        r'''"file"\s*:\s*"([^"]+)"''',
        r'''"url"\s*:\s*"([^"]+\.(?:m3u8|mp4)[^"]*)"''',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, script_text, re.I):
            url = match.replace('\\/', '/').replace('\\u0026', '&')
            if url.startswith('http') or url.startswith('//'):
                full_url = urljoin(page_url, url)
                if is_video_url(full_url) or 'embed' in full_url.lower():
                    results.append(('script-pattern', clean_url(full_url)))

    # Try to find and parse JSON objects in scripts
    json_patterns = [
        r'sources\s*[:=]\s*(\[(?:[^\[\]]*|\[(?:[^\[\]]*|\[[^\[\]]*\])*\])*\])',
        r'source\s*[:=]\s*(\{[^{}]*\})',
        r'setup\(\s*(\{(?:[^{}]*|\{(?:[^{}]*|\{[^{}]*\})*\})*\})',
        r'config\s*[:=]\s*(\{(?:[^{}]*|\{(?:[^{}]*|\{[^{}]*\})*\})*\})',
    ]

    for pattern in json_patterns:
        for match in re.findall(pattern, script_text, re.I):
            try:
                # Fix common JSON issues
                fixed = match.replace("'", '"').replace('\\/', '/')
                data = json.loads(fixed)
                urls = extract_urls_from_json(data, page_url)
                results.extend(urls)
            except (json.JSONDecodeError, TypeError):
                # Still try regex on the match
                for url_m in re.findall(r'https?://[^\s"\'<>\\]+', match.replace('\\/', '/')):
                    if is_video_url(url_m):
                        results.append(('json-regex', clean_url(url_m)))

    return results


def extract_urls_from_json(data, base_url):
    """Recursively extract video URLs from JSON data."""
    results = []

    if isinstance(data, str):
        if is_video_url(data):
            results.append(('json', urljoin(base_url, data)))
    elif isinstance(data, list):
        for item in data:
            results.extend(extract_urls_from_json(item, base_url))
    elif isinstance(data, dict):
        for key, value in data.items():
            key_lower = key.lower()
            if isinstance(value, str):
                if is_video_url(value) or key_lower in ('src', 'file', 'url', 'source', 'stream', 'hls', 'mp4'):
                    full = urljoin(base_url, value)
                    results.append(('json-key', full))
            else:
                results.extend(extract_urls_from_json(value, base_url))

    return results


def is_video_url(url):
    """Check if URL looks like a video stream."""
    if not url:
        return False
    url_lower = url.lower()
    video_indicators = [
        '.m3u8', '.mp4', '.webm', '.flv', '.avi', '.mkv', '.ts',
        '.m4v', '.mov', '.3gp', '.ogv',
        '/embed/', '/player/', '/stream/', '/video/',
        'master.m3u8', 'index.m3u8', 'playlist.m3u8',
    ]
    return any(indicator in url_lower for indicator in video_indicators)


def clean_url(url):
    """Clean and normalize a URL."""
    if not url:
        return ''
    url = url.replace('\\/', '/').replace('\\u0026', '&').replace('&amp;', '&')
    url = url.strip().strip('"').strip("'").strip()
    # Remove trailing garbage
    url = re.sub(r'[\s"\'<>\\})\];,]+$', '', url)
    # Validate
    if url.startswith('http://') or url.startswith('https://') or url.startswith('//'):
        if url.startswith('//'):
            url = 'https:' + url
        return url
    return ''


def get_best_stream(stream_urls):
    """Select the best stream URL from candidates."""
    if not stream_urls:
        return None

    # Scoring
    scored = []
    for source_type, url in stream_urls:
        score = 0
        url_lower = url.lower()

        # Prefer m3u8
        if '.m3u8' in url_lower:
            score += 100
            if 'master' in url_lower:
                score += 20
            if 'index' in url_lower:
                score += 15

        # Then mp4
        elif '.mp4' in url_lower:
            score += 80
            # Prefer higher quality hints
            if '1080' in url_lower:
                score += 15
            elif '720' in url_lower:
                score += 10
            elif '480' in url_lower:
                score += 5

        # Embeds (less preferred but still valid)
        elif '/embed/' in url_lower or '/player/' in url_lower:
            score += 30

        # Other video formats
        elif any(ext in url_lower for ext in ['.webm', '.flv', '.ts']):
            score += 60

        # Bonus for direct sources
        if source_type in ('direct', 'script-pattern', 'regex-m3u8', 'regex-mp4'):
            score += 10
        if source_type in ('iframe-direct',):
            score += 5

        # Penalize CDN preview/thumbnail URLs
        if 'thumb' in url_lower or 'preview' in url_lower or 'poster' in url_lower:
            score -= 50

        scored.append((score, source_type, url))

    scored.sort(key=lambda x: x[0], reverse=True)

    if scored:
        best_score, best_type, best_url = scored[0]
        logger.info(f"    Best stream ({best_type}, score={best_score}): {best_url[:100]}")
        return best_url

    return None


def generate_m3u(entries):
    """Generate M3U playlist content."""
    lines = ['#EXTM3U']
    lines.append('')

    for i, entry in enumerate(entries):
        title = entry.get('title', f'Video {i+1}')
        stream_url = entry.get('stream_url', '')
        page_url = entry.get('page_url', '')
        thumbnail = entry.get('thumbnail', '')

        lines.append(f'#EXTINF:-1 tvg-logo="{thumbnail}",{title}')
        if page_url:
            lines.append(f'#EXTVLCOPT:http-referrer={page_url}')
            domain = urlparse(page_url).scheme + '://' + urlparse(page_url).netloc
            lines.append(f'#EXTVLCOPT:http-origin={domain}')
        lines.append(stream_url)
        lines.append('')

    return '\n'.join(lines)


def generate_html_player(entries):
    """Generate the HTML player page."""
    playlist_json = json.dumps(entries, indent=2, ensure_ascii=False)
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())

    html = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Stream Player</title>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a0a; color: #e0e0e0; min-height: 100vh;
        }
        .header {
            background: linear-gradient(135deg, #1a1a2e, #16213e);
            padding: 15px 20px; text-align: center;
            border-bottom: 2px solid #e94560;
        }
        .header h1 { font-size: 1.5rem; color: #e94560; }
        .header p { color: #666; font-size: 0.8rem; margin-top: 4px; }
        .main { display: flex; flex-direction: column; max-width: 1400px; margin: 0 auto; }
        @media(min-width:992px) { .main { flex-direction: row; height: calc(100vh - 70px); } }
        .player-area { flex: 1; padding: 15px; display: flex; flex-direction: column; min-width: 0; }
        .video-box {
            position: relative; width: 100%; padding-bottom: 56.25%;
            background: #000; border-radius: 10px; overflow: hidden;
            box-shadow: 0 4px 20px rgba(0,0,0,0.5);
        }
        .video-box video, .video-box iframe {
            position: absolute; top: 0; left: 0; width: 100%; height: 100%; border: none;
        }
        .placeholder {
            position: absolute; top: 0; left: 0; width: 100%; height: 100%;
            display: flex; align-items: center; justify-content: center;
            flex-direction: column; color: #444;
        }
        .placeholder svg { width: 60px; height: 60px; fill: #333; margin-bottom: 10px; }
        .now-playing {
            margin-top: 12px; padding: 12px 15px; background: #1a1a2e;
            border-radius: 8px; border-left: 3px solid #e94560; display: none;
        }
        .now-playing small { color: #e94560; text-transform: uppercase; letter-spacing: 1px; font-size: 0.7rem; }
        .now-playing p { color: #ddd; margin-top: 4px; font-size: 0.95rem; }
        .controls {
            margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
        }
        .btn {
            padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer;
            font-size: 0.85rem; font-weight: 600; transition: all 0.2s;
            display: inline-flex; align-items: center; gap: 5px;
        }
        .btn-p { background: #e94560; color: #fff; }
        .btn-p:hover { background: #d13350; }
        .btn-s { background: #1a1a2e; color: #ccc; border: 1px solid #333; }
        .btn-s:hover { border-color: #e94560; }
        .toggle-wrap { display: flex; align-items: center; gap: 6px; margin-left: auto; font-size: 0.8rem; color: #777; }
        .toggle { position: relative; width: 36px; height: 20px; }
        .toggle input { opacity: 0; width: 0; height: 0; }
        .toggle span {
            position: absolute; inset: 0; background: #333; border-radius: 20px;
            cursor: pointer; transition: 0.3s;
        }
        .toggle span:before {
            content: ""; position: absolute; height: 14px; width: 14px; left: 3px; bottom: 3px;
            background: #fff; border-radius: 50%; transition: 0.3s;
        }
        .toggle input:checked+span { background: #e94560; }
        .toggle input:checked+span:before { transform: translateX(16px); }

        .sidebar {
            width: 100%; background: #111; border-left: 1px solid #1a1a1a;
            display: flex; flex-direction: column;
        }
        @media(min-width:992px) { .sidebar { width: 380px; } }
        .sidebar-head {
            padding: 12px 16px; background: #1a1a2e; border-bottom: 1px solid #222;
            display: flex; justify-content: space-between; align-items: center;
        }
        .sidebar-head h2 { font-size: 0.95rem; color: #e94560; }
        .badge {
            background: #e94560; color: #fff; padding: 1px 8px;
            border-radius: 10px; font-size: 0.75rem;
        }
        .search-wrap { padding: 8px 16px; border-bottom: 1px solid #222; }
        .search-wrap input {
            width: 100%; padding: 8px 12px; background: #1a1a2e; border: 1px solid #333;
            border-radius: 6px; color: #e0e0e0; font-size: 0.85rem; outline: none;
        }
        .search-wrap input:focus { border-color: #e94560; }
        .search-wrap input::placeholder { color: #444; }
        .list { flex: 1; overflow-y: auto; padding: 8px; }
        .list::-webkit-scrollbar { width: 5px; }
        .list::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
        .list-item {
            display: flex; align-items: center; padding: 10px 12px; margin-bottom: 3px;
            border-radius: 6px; cursor: pointer; transition: background 0.2s; gap: 10px;
        }
        .list-item:hover { background: #1a1a2e; }
        .list-item.active { background: #16213e; border: 1px solid #e94560; }
        .list-item .num { color: #444; font-size: 0.8rem; min-width: 28px; text-align: center; }
        .list-item.active .num { color: #e94560; }
        .list-item .meta { flex: 1; min-width: 0; }
        .list-item .meta .t {
            font-size: 0.85rem; white-space: nowrap; overflow: hidden;
            text-overflow: ellipsis; color: #ccc;
        }
        .list-item.active .meta .t { color: #fff; }
        .list-item .meta .s { font-size: 0.7rem; color: #555; margin-top: 2px; }
        .list-item .ico { color: #444; }
        .list-item.active .ico { color: #e94560; }
        .foot { padding: 8px; background: #0d0d0d; border-top: 1px solid #1a1a1a; text-align: center; font-size: 0.7rem; color: #444; }
        .error-msg { color: #e94560; background: #1a1a2e; padding: 12px; border-radius: 6px; margin-top: 10px; font-size: 0.85rem; display: none; }
    </style>
</head>
<body>
<div class="header">
    <h1>🎬 Stream Player</h1>
    <p>Last updated: ''' + timestamp + '''</p>
</div>
<div class="main">
    <div class="player-area">
        <div class="video-box" id="vBox">
            <div class="placeholder" id="ph">
                <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
                <p>Select a video to play</p>
            </div>
        </div>
        <div class="now-playing" id="np">
            <small>▶ Now Playing</small>
            <p id="npTitle"></p>
        </div>
        <div class="error-msg" id="errMsg"></div>
        <div class="controls">
            <button class="btn btn-p" onclick="prev()">⏮ Prev</button>
            <button class="btn btn-p" onclick="next()">Next ⏭</button>
            <button class="btn btn-s" onclick="shuffle()">🔀 Shuffle</button>
            <a class="btn btn-s" href="playlist.m3u" download>📥 M3U</a>
            <div class="toggle-wrap">
                <label class="toggle"><input type="checkbox" id="ap" checked><span></span></label>
                Autoplay
            </div>
        </div>
    </div>
    <div class="sidebar">
        <div class="sidebar-head">
            <h2>📋 Playlist</h2>
            <span class="badge" id="cnt">0</span>
        </div>
        <div class="search-wrap">
            <input type="text" id="q" placeholder="🔍 Search..." oninput="filter()">
        </div>
        <div class="list" id="lst"></div>
        <div class="foot">Auto-updates every 6 hours</div>
    </div>
</div>
<script>
const DATA=''' + playlist_json + ''';
let ci=-1,hls=null,fi=[];

document.addEventListener('DOMContentLoaded',()=>{
    document.getElementById('cnt').textContent=DATA.length;
    fi=DATA.map((_,i)=>i);
    render();
    if(DATA.length>0)play(0);
});

function render(){
    const c=document.getElementById('lst');
    if(!fi.length){c.innerHTML='<div style="text-align:center;padding:40px;color:#555">No results</div>';return;}
    c.innerHTML=fi.map(i=>{
        const d=DATA[i],a=i===ci,u=d.stream_url||'';
        let tp='Video';
        if(u.includes('.m3u8'))tp='HLS';
        else if(u.includes('.mp4'))tp='MP4';
        else if(u.includes('embed'))tp='Embed';
        return`<div class="list-item${a?' active':''}" onclick="play(${i})" title="${esc(d.title)}">
            <span class="num">${i+1}</span>
            <div class="meta"><div class="t">${esc(d.title)}</div><div class="s">${tp}</div></div>
            <span class="ico">${a?'🔊':'▶'}</span></div>`;
    }).join('');
}

function play(i){
    if(i<0||i>=DATA.length)return;
    ci=i;
    const d=DATA[i],w=document.getElementById('vBox'),
          ph=document.getElementById('ph'),np=document.getElementById('np'),
          err=document.getElementById('errMsg');
    if(hls){hls.destroy();hls=null;}
    const old=w.querySelector('video,iframe');
    if(old)old.remove();
    if(ph)ph.style.display='none';
    err.style.display='none';
    np.style.display='block';
    document.getElementById('npTitle').textContent=d.title;
    document.title='▶ '+d.title;

    const u=d.stream_url;

    if(u.includes('.m3u8')){
        const v=document.createElement('video');
        v.controls=true;v.autoplay=true;v.id='vp';
        v.style.cssText='position:absolute;top:0;left:0;width:100%;height:100%';
        w.appendChild(v);
        if(Hls.isSupported()){
            hls=new Hls({xhrSetup:x=>{if(d.page_url)x.setRequestHeader('Referer',d.page_url);}});
            hls.loadSource(u);hls.attachMedia(v);
            hls.on(Hls.Events.MANIFEST_PARSED,()=>v.play().catch(()=>{}));
            hls.on(Hls.Events.ERROR,(_,e)=>{
                if(e.fatal){showErr('Stream error. Trying next...');if(document.getElementById('ap').checked)setTimeout(next,3000);}
            });
        }else if(v.canPlayType('application/vnd.apple.mpegurl')){
            v.src=u;v.play().catch(()=>{});
        }
        v.onended=()=>{if(document.getElementById('ap').checked)next();};
    }else if(u.includes('.mp4')||u.includes('.webm')){
        const v=document.createElement('video');
        v.controls=true;v.autoplay=true;v.id='vp';
        v.style.cssText='position:absolute;top:0;left:0;width:100%;height:100%';
        v.src=u;w.appendChild(v);
        v.play().catch(()=>{});
        v.onended=()=>{if(document.getElementById('ap').checked)next();};
        v.onerror=()=>{showErr('Playback error. Trying next...');if(document.getElementById('ap').checked)setTimeout(next,3000);};
    }else{
        const f=document.createElement('iframe');
        f.src=u;f.allow='autoplay;encrypted-media;fullscreen';
        f.allowFullscreen=true;f.id='vp';
        f.style.cssText='position:absolute;top:0;left:0;width:100%;height:100%;border:none';
        w.appendChild(f);
    }
    render();
    setTimeout(()=>{const a=document.querySelector('.list-item.active');if(a)a.scrollIntoView({behavior:'smooth',block:'center'});},100);
}

function next(){ci<DATA.length-1?play(ci+1):play(0);}
function prev(){ci>0?play(ci-1):play(DATA.length-1);}
function shuffle(){if(!DATA.length)return;let r;do{r=Math.floor(Math.random()*DATA.length);}while(r===ci&&DATA.length>1);play(r);}
function filter(){
    const q=document.getElementById('q').value.toLowerCase().trim();
    fi=q?DATA.map((d,i)=>({i,t:d.title.toLowerCase()})).filter(x=>x.t.includes(q)).map(x=>x.i):DATA.map((_,i)=>i);
    document.getElementById('cnt').textContent=fi.length;
    render();
}
function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
function showErr(m){const e=document.getElementById('errMsg');e.textContent=m;e.style.display='block';setTimeout(()=>e.style.display='none',5000);}

document.addEventListener('keydown',e=>{
    if(e.target.tagName==='INPUT')return;
    switch(e.key){
        case'ArrowRight':case'n':e.preventDefault();next();break;
        case'ArrowLeft':case'p':e.preventDefault();prev();break;
        case's':e.preventDefault();shuffle();break;
        case' ':e.preventDefault();const v=document.getElementById('vp');if(v&&v.tagName==='VIDEO'){v.paused?v.play():v.pause();}break;
    }
});
</script>
</body>
</html>'''
    return html


def main():
    scraper = get_scraper()
    all_entries = []

    logger.info("=" * 60)
    logger.info("Starting deep scraper...")
    logger.info("=" * 60)

    for page_num in range(1, MAX_PAGES + 1):
        page_url = f"{BASE_URL}/page/{page_num}/"
        if page_num == 1:
            # Also try base URL
            page_url = f"{BASE_URL}/"

        html = fetch_page(scraper, page_url)
        if not html:
            # Try alternate URL format
            if page_num == 1:
                html = fetch_page(scraper, f"{BASE_URL}/page/1/")
            if not html:
                logger.warning(f"Could not fetch page {page_num}, stopping pagination.")
                break

        # Extract listings
        video_list = extract_video_listings(html, page_url)
        logger.info(f"\n📄 Page {page_num}: Found {len(video_list)} video links")

        if not video_list:
            # If page 1, try extracting from current page directly (maybe it's a single page site)
            if page_num == 1:
                logger.info("No listings found on page 1, trying direct extraction...")
                streams = extract_all_stream_urls(html, page_url, scraper)
                if streams:
                    best = get_best_stream(streams)
                    if best:
                        all_entries.append({
                            'title': 'Homepage Video',
                            'stream_url': best,
                            'page_url': page_url,
                            'thumbnail': ''
                        })
            break

        # Visit each video page
        for idx, video in enumerate(video_list):
            logger.info(f"\n  [{idx+1}/{len(video_list)}] Processing: {video['title'][:60]}")
            logger.info(f"  URL: {video['page_url']}")

            time.sleep(REQUEST_DELAY)
            video_html = fetch_page(scraper, video['page_url'], referer=page_url)
            if not video_html:
                logger.warning(f"  ❌ Could not fetch video page")
                continue

            # Deep extraction
            stream_urls = extract_all_stream_urls(video_html, video['page_url'], scraper)

            if stream_urls:
                logger.info(f"  Found {len(stream_urls)} potential stream URLs:")
                for st, su in stream_urls[:5]:
                    logger.info(f"    [{st}] {su[:100]}")

                best = get_best_stream(stream_urls)
                if best:
                    entry = {
                        'title': video['title'],
                        'stream_url': best,
                        'page_url': video['page_url'],
                        'thumbnail': video.get('thumbnail', '')
                    }
                    all_entries.append(entry)
                    logger.info(f"  ✅ Added: {best[:100]}")
                else:
                    logger.warning(f"  ❌ No suitable stream found")
            else:
                logger.warning(f"  ❌ No stream URLs found at all")

        # Next page
        if page_num == 1:
            continue
        time.sleep(REQUEST_DELAY)

    # ===== Generate outputs =====
    logger.info(f"\n{'=' * 60}")
    logger.info(f"Total videos found: {len(all_entries)}")
    logger.info(f"{'=' * 60}")

    # M3U
    m3u = generate_m3u(all_entries)
    with open('playlist.m3u', 'w', encoding='utf-8') as f:
        f.write(m3u)
    logger.info("✅ playlist.m3u saved")

    # JSON
    with open('playlist.json', 'w', encoding='utf-8') as f:
        json.dump(all_entries, f, indent=2, ensure_ascii=False)
    logger.info("✅ playlist.json saved")

    # HTML Player
    html_content = generate_html_player(all_entries)
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html_content)
    logger.info("✅ index.html saved")

    # Summary
    if all_entries:
        logger.info("\n📋 Playlist summary:")
        for i, e in enumerate(all_entries):
            logger.info(f"  {i+1}. {e['title'][:60]} -> {e['stream_url'][:80]}")
    else:
        logger.warning("\n⚠️ No videos were found! The site structure may have changed.")
        logger.warning("Check the site manually and update the selectors.")


if __name__ == '__main__':
    main()
