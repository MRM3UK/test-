import re
import os
import sys
import json
import time
import logging
import argparse
import cloudscraper
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

BASE_URL = "https://kaamdesi.com"
DELAY = 2

# ---- Limits per mode ----
BATCH_PAGES = 33       # pages per batch run
REFRESH_PAGES = 2      # pages for new content check
MAX_PAGE_LIMIT = 250   # absolute max page number (safety stop)

STATE_FILE = "scraper_state.json"
PLAYLIST_JSON = "playlist.json"
PLAYLIST_M3U = "playlist.m3u"


# ======================== STATE MANAGEMENT ========================

def load_state():
    """Load scraper state from previous runs."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        'last_page': 0,
        'completed': False,
        'total_videos': 0,
        'last_run': '',
        'run_count': 0,
        'last_mode': ''
    }


def save_state(state):
    state['last_run'] = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
    state['run_count'] = state.get('run_count', 0) + 1
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    log.info(f"State saved: page={state['last_page']}, completed={state['completed']}, videos={state['total_videos']}")


def load_existing_playlist():
    """Load existing playlist entries."""
    if os.path.exists(PLAYLIST_JSON):
        try:
            with open(PLAYLIST_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []


def dedup_entries(entries):
    """Remove duplicates by stream_url, keep latest."""
    seen = {}
    for entry in entries:
        url = entry.get('stream_url', '')
        if url:
            seen[url] = entry  # later entry overwrites earlier (keeps freshest)
    return list(seen.values())


# ======================== HTTP ========================

def make_scraper():
    s = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': BASE_URL + '/',
    })
    return s


def fetch(s, url, ref=None, tries=3):
    h = {}
    if ref:
        h['Referer'] = ref
    for attempt in range(tries):
        try:
            r = s.get(url, timeout=30, headers=h, allow_redirects=True)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
            log.warning(f"HTTP {r.status_code}: {url[:80]}")
        except Exception as e:
            log.warning(f"Attempt {attempt+1} failed: {e}")
        time.sleep(DELAY * (attempt + 1))
    return None


# ======================== PAGE DETECTION ========================

def is_page_valid(html):
    """Check if page has actual content (not 404/empty)."""
    if not html:
        return False
    soup = BeautifulSoup(html, 'lxml')
    title = soup.find('title')
    if title:
        t = title.get_text(strip=True).lower()
        if '404' in t or 'not found' in t or 'page not found' in t:
            return False
    return True


def has_next_page(html, current_page):
    """Check if there's a next page."""
    soup = BeautifulSoup(html, 'lxml')
    next_num = current_page + 1

    # Direct next page link
    selectors = [
        f'a[href*="/page/{next_num}"]',
        'a.next', 'a.nextpostslink', '.pagination a.next',
        '.nav-links a.next', 'a[rel="next"]', '.next a',
        'li.next a', '.pager .next a', '.wp-pagenavi a.nextpostslink',
    ]
    for sel in selectors:
        if soup.select_one(sel):
            return True

    # Check page numbers in pagination
    max_page = current_page
    for link in soup.select('.pagination a, .nav-links a, .wp-pagenavi a, .paginator a'):
        href = link.get('href', '')
        m = re.search(r'/page/(\d+)', href)
        if m:
            max_page = max(max_page, int(m.group(1)))
        text = link.get_text(strip=True)
        if text.isdigit():
            max_page = max(max_page, int(text))

    return max_page > current_page


# ======================== CONTENT EXTRACTION ========================

def get_listings(html, page_url):
    """Get content links from listing page."""
    soup = BeautifulSoup(html, 'lxml')
    items = []
    seen = set()
    base_domain = urlparse(BASE_URL).netloc.replace('www.', '')

    # Strategy 1: Article blocks
    for sel in ['article', '.post', '.video-item', '.entry', '.item',
                '.thumb-block', '.video-block', '.post-item', '.hentry', '.type-post']:
        for block in soup.select(sel):
            link = block.find('a', href=True)
            if not link:
                continue
            href = urljoin(page_url, link['href'].strip())
            if not is_content_url(href, base_domain) or href in seen:
                continue
            title = extract_block_title(block)
            thumb = extract_block_thumbnail(block, page_url)
            if title:
                seen.add(href)
                items.append({'page_url': href, 'title': title, 'thumbnail': thumb})

    # Strategy 2: Heading links
    for h_tag in soup.select('h1 a, h2 a, h3 a, h4 a, .entry-title a, .post-title a'):
        href = urljoin(page_url, h_tag.get('href', '').strip())
        if not is_content_url(href, base_domain) or href in seen:
            continue
        title = h_tag.get_text(strip=True)
        if not title or len(title) < 3:
            continue
        parent = h_tag.find_parent(['article', 'div', 'li', 'section'])
        thumb = extract_block_thumbnail(parent, page_url) if parent else ''
        seen.add(href)
        items.append({'page_url': href, 'title': title, 'thumbnail': thumb})

    # Strategy 3: Image links
    for a_tag in soup.find_all('a', href=True):
        href = urljoin(page_url, a_tag['href'].strip())
        if not is_content_url(href, base_domain) or href in seen:
            continue
        img = a_tag.find('img')
        if not img:
            continue
        title = (img.get('alt', '') or img.get('title', '') or a_tag.get('title', '')).strip()
        if not title or len(title) < 3:
            continue
        thumb = get_img_src(img, page_url)
        seen.add(href)
        items.append({'page_url': href, 'title': title, 'thumbnail': thumb})

    for item in items:
        item['title'] = clean_title(item['title'])

    return items


def extract_block_title(block):
    if not block:
        return ''
    for h in block.find_all(['h1', 'h2', 'h3', 'h4']):
        text = h.get_text(strip=True)
        if text and len(text) >= 3:
            return text
    for sel in ['.entry-title', '.post-title', '.title', '.video-title']:
        el = block.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) >= 3:
                return text
    first_link = block.find('a', href=True)
    if first_link:
        t = first_link.get('title', '').strip()
        if t and len(t) >= 3:
            return t
    img = block.find('img')
    if img:
        alt = (img.get('alt', '') or img.get('title', '')).strip()
        if alt and len(alt) >= 3:
            return alt
    if first_link:
        text = first_link.get_text(strip=True)
        if text and len(text) >= 5:
            return text
    return ''


def extract_block_thumbnail(block, page_url):
    if not block:
        return ''
    for img in block.find_all('img'):
        src = get_img_src(img, page_url)
        if src and not is_tiny_image(src):
            return src
    for el in block.find_all(True, style=True):
        style = el.get('style', '')
        m = re.search(r'background(?:-image)?\s*:\s*url\(["\']?([^"\')\s]+)', style, re.I)
        if m:
            return urljoin(page_url, m.group(1))
    for el in block.find_all(True):
        for attr in ['data-thumb', 'data-thumbnail', 'data-bg', 'data-image',
                     'data-src', 'data-lazy-src', 'data-original', 'data-poster']:
            val = el.get(attr, '').strip()
            if val and not is_tiny_image(val):
                return urljoin(page_url, val)
    return ''


def get_img_src(img, page_url):
    for attr in ['src', 'data-src', 'data-lazy-src', 'data-original', 'data-thumb']:
        val = img.get(attr, '').strip()
        if val and not val.startswith('data:'):
            return urljoin(page_url, val)
    return ''


def is_tiny_image(url):
    ul = url.lower()
    return any(t in ul for t in ['1x1', 'spacer', 'blank', 'pixel', 'loading',
                                  'spinner', 'placeholder', 'avatar', 'icon',
                                  'logo', 'gravatar', 'emoji', 'smilies', 'wp-includes'])


def is_content_url(url, base_domain):
    parsed = urlparse(url)
    if base_domain not in parsed.netloc:
        return False
    path = parsed.path.lower().rstrip('/')
    if not path or path == '/':
        return False
    skip = ['/page', '/category', '/tag', '/author', '/wp-admin', '/wp-content',
            '/wp-includes', '/wp-json', '/feed', '/login', '/register', '/search',
            '/contact', '/about', '/privacy', '/terms', '/dmca', '/sitemap',
            '/comments', '/trackback', '/xmlrpc', '/wp-login', '/cdn-cgi']
    if any(path.startswith(s) or path == s for s in skip):
        return False
    skip_ext = ['.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico',
                '.xml', '.txt', '.pdf', '.zip', '.mp4', '.mp3']
    if any(path.endswith(ext) for ext in skip_ext):
        return False
    return True


def clean_title(title):
    if not title:
        return ''
    title = ' '.join(title.split())
    for sep in [' - ', ' | ', ' – ', ' — ', ' :: ']:
        if sep in title:
            parts = title.split(sep)
            title = max(parts, key=len)
    return title.strip('| -–—·•>»').strip()[:250]


# ======================== VIDEO EXTRACTION ========================

def find_mp4_links(html, page_url):
    """Find all .mp4 URLs in page."""
    found = set()

    patterns = [
        r'(https?://[^\s"\'<>\\\)]+\.mp4[^\s"\'<>\\\)]*)',
        r'(https?:\\/\\/[^\s"\'<>]+\.mp4[^\s"\'<>]*)',
        r'(//[^\s"\'<>\\\)]+\.mp4[^\s"\'<>\\\)]*)',
        r'(https?%3A%2F%2F[^\s"\'<>&]+\.mp4[^\s"\'<>&]*)',
        r'(https?://server\d+\.mmsbee\d*\.[a-z]+/[^\s"\'<>\\\)]+\.mp4)',
    ]

    for pattern in patterns:
        for m in re.findall(pattern, html, re.I):
            url = m.replace('\\/', '/')
            if url.startswith('//'):
                url = 'https:' + url
            if '%3A' in url:
                url = unquote(url)
            url = clean_url(url)
            if url:
                found.add(url)

    soup = BeautifulSoup(html, 'lxml')

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if '.mp4' in href.lower():
            found.add(clean_url(urljoin(page_url, href)))

    for tag in soup.find_all(['video', 'source']):
        for attr in ['src', 'data-src', 'data-lazy-src']:
            val = tag.get(attr, '').strip()
            if val and '.mp4' in val.lower():
                found.add(clean_url(urljoin(page_url, val)))

    for tag in soup.find_all(True):
        for attr_name in ['href', 'src', 'data-src', 'data-url', 'data-file',
                          'data-video', 'data-mp4', 'content', 'value']:
            val = tag.get(attr_name, '')
            if isinstance(val, str) and '.mp4' in val.lower():
                found.add(clean_url(urljoin(page_url, val)))

    for script in soup.find_all('script'):
        txt = script.string or ''
        if not txt:
            continue
        for m in re.findall(r'''["'](https?://[^"']+\.mp4[^"']*)["']''', txt, re.I):
            found.add(clean_url(m.replace('\\/', '/')))
        for m in re.findall(r'''["'](https?:\\/\\/[^"']+\.mp4[^"']*)["']''', txt, re.I):
            found.add(clean_url(m.replace('\\/', '/')))

    found.discard('')
    return list(found)


def find_iframe_mp4(html, page_url, scraper, depth=0):
    if depth > 3:
        return []
    results = []
    soup = BeautifulSoup(html, 'lxml')
    for iframe in soup.find_all('iframe'):
        src = iframe.get('src', '') or iframe.get('data-src', '') or iframe.get('data-lazy-src', '')
        if not src or src.startswith('about:') or src.startswith('javascript:'):
            continue
        iframe_url = urljoin(page_url, src.strip())
        skip = ['google', 'facebook', 'twitter', 'doubleclick', 'adsense', 'disqus']
        if any(s in iframe_url.lower() for s in skip):
            continue
        if '.mp4' in iframe_url.lower():
            results.append(clean_url(iframe_url))
            continue
        log.info(f"  {'  '*depth}↳ iframe[{depth+1}]: {iframe_url[:80]}")
        time.sleep(1)
        iframe_html = fetch(scraper, iframe_url, ref=page_url, tries=2)
        if iframe_html:
            results.extend(find_mp4_links(iframe_html, iframe_url))
            results.extend(find_iframe_mp4(iframe_html, iframe_url, scraper, depth + 1))
    return results


def extract_page_title(html, fallback=''):
    soup = BeautifulSoup(html, 'lxml')
    for sel in ['.entry-title', '.post-title', 'h1.title', 'h1', 'h2.title']:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) >= 3:
                return clean_title(text)
    og = soup.find('meta', property='og:title')
    if og:
        text = og.get('content', '').strip()
        if text and len(text) >= 3:
            return clean_title(text)
    title_tag = soup.find('title')
    if title_tag:
        text = title_tag.get_text(strip=True)
        if text and len(text) >= 3:
            return clean_title(text)
    return fallback


def extract_page_thumbnail(html, page_url, fallback=''):
    soup = BeautifulSoup(html, 'lxml')
    og = soup.find('meta', property='og:image')
    if og:
        val = og.get('content', '').strip()
        if val:
            return urljoin(page_url, val)
    tw = soup.find('meta', attrs={'name': 'twitter:image'})
    if tw:
        val = tw.get('content', '').strip()
        if val:
            return urljoin(page_url, val)
    video = soup.find('video')
    if video:
        poster = video.get('poster', '').strip()
        if poster:
            return urljoin(page_url, poster)
    for sel in ['.entry-content', '.post-content', '.content', 'article', 'main']:
        content = soup.select_one(sel)
        if content:
            for img in content.find_all('img'):
                src = get_img_src(img, page_url)
                if src and not is_tiny_image(src):
                    return src
    return fallback


def pick_best_mp4(urls):
    if not urls:
        return None
    scored = []
    for url in urls:
        score = 0
        ul = url.lower()
        if 'mmsbee' in ul:
            score += 100
        if '/uploads/' in ul:
            score += 50
        if '/myfiless/' in ul:
            score += 50
        if re.search(r'/\d+\.mp4', ul):
            score += 30
        if 'thumb' in ul or 'preview' in ul or 'sample' in ul:
            score -= 100
        scored.append((score, url))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def clean_url(url):
    if not url:
        return ''
    url = url.replace('\\/', '/').replace('\\u0026', '&').replace('&amp;', '&')
    url = url.strip().strip('"').strip("'").strip()
    url = re.sub(r'[\s"\'<>\\}\]\);,]+$', '', url)
    if url.startswith('//'):
        url = 'https:' + url
    if url.startswith('http://') or url.startswith('https://'):
        return url
    return ''


# ======================== M3U GENERATION ========================

def save_playlist(entries):
    """Save M3U and JSON."""
    # JSON
    with open(PLAYLIST_JSON, 'w', encoding='utf-8') as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)

    # M3U
    m3u = ['#EXTM3U', '']
    for e in entries:
        thumb = e.get('thumbnail', '')
        m3u.append(f'#EXTINF:-1 tvg-logo="{thumb}",{e["title"]}')
        m3u.append(f'#EXTVLCOPT:http-referrer={e["page_url"]}')
        m3u.append(e['stream_url'])
        m3u.append('')
    with open(PLAYLIST_M3U, 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u))

    log.info(f"💾 Saved {len(entries)} entries to playlist.m3u and playlist.json")


# ======================== SCRAPE LOGIC ========================

def scrape_pages(scraper, start_page, num_pages, existing_stream_urls):
    """Scrape a range of pages, return new entries and last page reached."""
    new_entries = []
    current_page = start_page
    end_page = start_page + num_pages - 1
    reached_end = False
    consecutive_empty = 0

    while current_page <= end_page and current_page <= MAX_PAGE_LIMIT:
        if current_page == 1:
            page_url = BASE_URL + "/"
        else:
            page_url = f"{BASE_URL}/page/{current_page}/"

        log.info(f"\n{'='*50}")
        log.info(f"📄 PAGE {current_page} (target: {start_page}-{end_page})")
        log.info(f"{'='*50}")

        html = fetch(scraper, page_url)

        if not html or not is_page_valid(html):
            log.info(f"🛑 Page {current_page} not found or invalid - END OF SITE")
            reached_end = True
            break

        listings = get_listings(html, page_url)
        log.info(f"Found {len(listings)} content links")

        if not listings:
            consecutive_empty += 1
            log.info(f"Empty page (consecutive: {consecutive_empty})")
            if consecutive_empty >= 3:
                log.info("🛑 3 consecutive empty pages - END")
                reached_end = True
                break
            current_page += 1
            time.sleep(DELAY)
            continue
        else:
            consecutive_empty = 0

        page_found = 0
        for idx, item in enumerate(listings):
            log.info(f"\n  [{idx+1}/{len(listings)}] {item['title'][:55]}")

            time.sleep(DELAY)
            item_html = fetch(scraper, item['page_url'], ref=page_url)
            if not item_html:
                log.warning("  ❌ Could not fetch")
                continue

            # Get better title & thumbnail from actual page
            page_title = extract_page_title(item_html, fallback=item['title'])
            page_thumb = extract_page_thumbnail(item_html, item['page_url'],
                                                 fallback=item.get('thumbnail', ''))

            # Find MP4
            mp4_links = find_mp4_links(item_html, item['page_url'])
            iframe_mp4s = find_iframe_mp4(item_html, item['page_url'], scraper)
            mp4_links.extend(iframe_mp4s)
            mp4_links = list(set(filter(None, mp4_links)))

            if mp4_links:
                best = pick_best_mp4(mp4_links)
                if best and best not in existing_stream_urls:
                    new_entries.append({
                        'title': page_title,
                        'stream_url': best,
                        'page_url': item['page_url'],
                        'thumbnail': page_thumb
                    })
                    existing_stream_urls.add(best)
                    page_found += 1
                    log.info(f"  ✅ NEW [{len(new_entries)}] {page_title[:45]}")
                elif best in existing_stream_urls:
                    log.info(f"  ⏭ Already exists, skipping")
                else:
                    log.warning(f"  ❌ No suitable MP4")
            else:
                log.warning(f"  ❌ No MP4 found")

        log.info(f"\n  Page {current_page}: {page_found} new videos")

        # Check for next page
        if not has_next_page(html, current_page):
            log.info(f"🛑 No next page after {current_page} - END OF SITE")
            reached_end = True
            break

        current_page += 1
        time.sleep(DELAY)

    return new_entries, current_page, reached_end


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['batch', 'refresh', 'reset'], default='batch')
    args = parser.parse_args()

    mode = args.mode
    log.info(f"{'='*60}")
    log.info(f"MODE: {mode.upper()}")
    log.info(f"{'='*60}")

    # Load state
    state = load_state()

    # Handle reset
    if mode == 'reset':
        log.info("🔄 RESET: Starting fresh")
        state = {'last_page': 0, 'completed': False, 'total_videos': 0,
                 'last_run': '', 'run_count': 0, 'last_mode': 'reset'}
        existing = []
    else:
        existing = load_existing_playlist()

    log.info(f"Existing playlist: {len(existing)} videos")
    log.info(f"State: last_page={state['last_page']}, completed={state['completed']}")

    # Build set of existing stream URLs for dedup
    existing_urls = set(e['stream_url'] for e in existing if e.get('stream_url'))

    scraper = make_scraper()
    new_entries = []
    last_page = state.get('last_page', 0)
    reached_end = state.get('completed', False)

    if mode == 'batch' or mode == 'reset':
        # ---- BATCH: Scrape next 33 pages from where we left off ----
        if reached_end and mode != 'reset':
            log.info("✅ All pages already scraped. Switching to refresh mode.")
            mode = 'refresh'
        else:
            start = last_page + 1
            log.info(f"📦 BATCH: Scraping pages {start} to {start + BATCH_PAGES - 1}")
            new_entries, last_page_reached, reached_end = scrape_pages(
                scraper, start, BATCH_PAGES, existing_urls
            )
            last_page = last_page_reached - 1 if not reached_end else last_page_reached

    if mode == 'refresh':
        # ---- REFRESH: Scrape first 2 pages for new content ----
        log.info(f"🔄 REFRESH: Checking first {REFRESH_PAGES} pages for new content")
        new_entries, _, _ = scrape_pages(
            scraper, 1, REFRESH_PAGES, existing_urls
        )
        # Don't update last_page or completed for refresh

    # ---- Merge results ----
    if mode == 'refresh':
        # New content goes to TOP of playlist
        all_entries = new_entries + existing
    else:
        # Batch: append to end
        all_entries = existing + new_entries

    # Dedup by stream_url
    all_entries = dedup_entries(all_entries)

    log.info(f"\n{'='*60}")
    log.info(f"📊 SUMMARY")
    log.info(f"  Mode:          {mode}")
    log.info(f"  New videos:    {len(new_entries)}")
    log.info(f"  Total after:   {len(all_entries)}")
    log.info(f"  Last page:     {last_page}")
    log.info(f"  Completed:     {reached_end}")
    log.info(f"{'='*60}")

    # Save playlist
    save_playlist(all_entries)

    # Update state
    if mode != 'refresh':
        state['last_page'] = last_page
        state['completed'] = reached_end
    state['total_videos'] = len(all_entries)
    state['last_mode'] = mode
    save_state(state)

    # Print full list
    log.info(f"\n📋 PLAYLIST ({len(all_entries)} videos):")
    for i, e in enumerate(all_entries[:50]):
        log.info(f"  {i+1}. {e['title'][:50]}")
        log.info(f"     {e['stream_url'][:70]}")
    if len(all_entries) > 50:
        log.info(f"  ... and {len(all_entries) - 50} more")


if __name__ == '__main__':
    main()
