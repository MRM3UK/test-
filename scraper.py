import re
import json
import time
import logging
import cloudscraper
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

BASE_URL = "https://kaamdesi.com"
MAX_PAGES = 37  # No limit - scrape ALL pages until no more
DELAY = 2


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
                log.info(f"  404 - Page not found: {url}")
                return None
            log.warning(f"HTTP {r.status_code} for {url}")
        except Exception as e:
            log.warning(f"Attempt {attempt+1} failed for {url}: {e}")
        time.sleep(DELAY * (attempt + 1))
    return None


def has_next_page(html, current_page):
    """Check if there's a next page in pagination."""
    soup = BeautifulSoup(html, 'lxml')
    next_page = current_page + 1

    # Method 1: Look for next page link
    next_selectors = [
        f'a[href*="/page/{next_page}"]',
        'a.next', 'a.nextpostslink',
        '.pagination a.next',
        '.nav-links a.next',
        'a[rel="next"]',
        '.next a',
        'a.pagination-next',
        '.wp-pagenavi a.nextpostslink',
        '.paginator a.next',
        'li.next a',
        '.pager .next a',
    ]
    for sel in next_selectors:
        if soup.select_one(sel):
            return True

    # Method 2: Check if current page number exists in pagination
    # and there are higher numbers
    page_links = soup.select('.pagination a, .nav-links a, .wp-pagenavi a, .paginator a, .pager a')
    max_page = current_page
    for link in page_links:
        href = link.get('href', '')
        text = link.get_text(strip=True)
        # Extract page number from href
        m = re.search(r'/page/(\d+)', href)
        if m:
            max_page = max(max_page, int(m.group(1)))
        # Extract from link text
        if text.isdigit():
            max_page = max(max_page, int(text))

    if max_page > current_page:
        return True

    # Method 3: Check for "older posts" / "next" text links
    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True).lower()
        if text in ['next', 'next »', 'next ›', '»', '›', 'older posts', 'next page', 'older entries', '→']:
            href = a['href']
            if '/page/' in href:
                return True

    return False


def get_listings(html, page_url):
    """Get all content links from a listing page with UNIQUE titles and thumbnails."""
    soup = BeautifulSoup(html, 'lxml')
    items = []
    seen_urls = set()
    base_domain = urlparse(BASE_URL).netloc.replace('www.', '')

    # ====== Strategy 1: Article/Post blocks (BEST for unique title+thumb) ======
    article_selectors = [
        'article',
        '.post',
        '.video-item',
        '.entry',
        '.item',
        '.thumb-block',
        '.video-block',
        '.post-item',
        '.blog-post',
        '.hentry',
        '.type-post',
    ]

    for selector in article_selectors:
        for block in soup.select(selector):
            link = block.find('a', href=True)
            if not link:
                continue

            href = urljoin(page_url, link['href'].strip())
            if not is_content_url(href, base_domain) or href in seen_urls:
                continue

            title = extract_block_title(block)
            thumb = extract_block_thumbnail(block, page_url)

            if title:
                seen_urls.add(href)
                items.append({'page_url': href, 'title': title, 'thumbnail': thumb})

    # ====== Strategy 2: Heading links (h2 a, h3 a etc) ======
    for h_tag in soup.select('h1 a, h2 a, h3 a, h4 a, .entry-title a, .post-title a'):
        href = urljoin(page_url, h_tag.get('href', '').strip())
        if not is_content_url(href, base_domain) or href in seen_urls:
            continue

        title = h_tag.get_text(strip=True)
        if not title or len(title) < 3:
            continue

        # Find thumbnail near this heading
        parent = h_tag.find_parent(['article', 'div', 'li', 'section'])
        thumb = ''
        if parent:
            thumb = extract_block_thumbnail(parent, page_url)

        seen_urls.add(href)
        items.append({'page_url': href, 'title': title, 'thumbnail': thumb})

    # ====== Strategy 3: Image links with alt text ======
    for a_tag in soup.find_all('a', href=True):
        href = urljoin(page_url, a_tag['href'].strip())
        if not is_content_url(href, base_domain) or href in seen_urls:
            continue

        img = a_tag.find('img')
        if not img:
            continue

        title = (img.get('alt', '') or img.get('title', '') or a_tag.get('title', '')).strip()
        if not title or len(title) < 3:
            continue

        thumb = get_img_src(img, page_url)
        seen_urls.add(href)
        items.append({'page_url': href, 'title': title, 'thumbnail': thumb})

    # ====== Strategy 4: Remaining links with text ======
    for a_tag in soup.find_all('a', href=True):
        href = urljoin(page_url, a_tag['href'].strip())
        if not is_content_url(href, base_domain) or href in seen_urls:
            continue

        title = a_tag.get('title', '').strip() or a_tag.get_text(strip=True)
        if not title or len(title) < 5:
            continue

        # Skip navigation/menu text
        skip_texts = ['home', 'about', 'contact', 'privacy', 'terms', 'dmca',
                      'login', 'register', 'search', 'menu', 'close', 'open',
                      'read more', 'continue reading', 'more', 'next', 'prev',
                      'previous', 'older', 'newer', 'page', 'comment', 'reply']
        if title.lower().strip() in skip_texts:
            continue

        parent = a_tag.find_parent(['article', 'div', 'li'])
        thumb = ''
        if parent:
            thumb = extract_block_thumbnail(parent, page_url)

        seen_urls.add(href)
        items.append({'page_url': href, 'title': title, 'thumbnail': thumb})

    # Clean titles
    for item in items:
        item['title'] = clean_title(item['title'])

    return items


def extract_block_title(block):
    """Extract the BEST title from an article/post block."""
    # Priority 1: Heading tags
    for h in block.find_all(['h1', 'h2', 'h3', 'h4']):
        text = h.get_text(strip=True)
        if text and len(text) >= 3:
            return text

    # Priority 2: Title class/attribute elements
    for sel in ['.entry-title', '.post-title', '.title', '.video-title', '.card-title']:
        el = block.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) >= 3:
                return text

    # Priority 3: First link's title attribute
    first_link = block.find('a', href=True)
    if first_link:
        title = first_link.get('title', '').strip()
        if title and len(title) >= 3:
            return title

    # Priority 4: Image alt text
    img = block.find('img')
    if img:
        alt = (img.get('alt', '') or img.get('title', '')).strip()
        if alt and len(alt) >= 3:
            return alt

    # Priority 5: Link text
    if first_link:
        text = first_link.get_text(strip=True)
        if text and len(text) >= 5:
            return text

    return ''


def extract_block_thumbnail(block, page_url):
    """Extract the BEST thumbnail from a block."""
    # Priority 1: img inside the block
    for img in block.find_all('img'):
        src = get_img_src(img, page_url)
        if src and not is_tiny_image(src):
            return src

    # Priority 2: Background image in style
    for el in block.find_all(True, style=True):
        style = el.get('style', '')
        m = re.search(r'background(?:-image)?\s*:\s*url\(["\']?([^"\')\s]+)', style, re.I)
        if m:
            return urljoin(page_url, m.group(1))

    # Priority 3: data-thumb, data-bg, data-image attributes
    for el in block.find_all(True):
        for attr in ['data-thumb', 'data-thumbnail', 'data-bg', 'data-image',
                     'data-src', 'data-lazy-src', 'data-original', 'data-poster']:
            val = el.get(attr, '').strip()
            if val and not is_tiny_image(val):
                return urljoin(page_url, val)

    return ''


def get_img_src(img, page_url):
    """Get the best src from an img tag (handles lazy loading)."""
    for attr in ['src', 'data-src', 'data-lazy-src', 'data-original',
                 'data-thumb', 'srcset', 'data-srcset']:
        val = img.get(attr, '').strip()
        if val:
            # Handle srcset (take first/largest)
            if ',' in val and attr in ('srcset', 'data-srcset'):
                parts = val.split(',')
                # Take last (usually largest)
                val = parts[-1].strip().split()[0]
            if val and not val.startswith('data:'):
                return urljoin(page_url, val)
    return ''


def is_tiny_image(url):
    """Check if URL likely points to a tiny icon/spacer image."""
    url_lower = url.lower()
    tiny_indicators = ['1x1', 'spacer', 'blank', 'pixel', 'loading', 'spinner',
                       'placeholder', 'avatar', 'icon', 'logo', 'gravatar',
                       'emoji', 'smilies', 'wp-includes']
    return any(t in url_lower for t in tiny_indicators)


def is_content_url(url, base_domain):
    """Check if URL is a content page (not utility/nav)."""
    parsed = urlparse(url)

    if base_domain not in parsed.netloc:
        return False

    path = parsed.path.lower().rstrip('/')
    if not path or path == '/':
        return False

    skip = ['/page', '/category', '/tag', '/author', '/wp-admin', '/wp-content',
            '/wp-includes', '/wp-json', '/feed', '/login', '/register', '/search',
            '/contact', '/about', '/privacy', '/terms', '/dmca', '/sitemap',
            '/comments', '/trackback', '/xmlrpc', '/wp-login', '/wp-signup',
            '/cdn-cgi']
    if any(path.startswith(s) or path == s for s in skip):
        return False

    skip_ext = ['.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico',
                '.xml', '.txt', '.pdf', '.zip', '.mp4', '.mp3']
    if any(path.endswith(ext) for ext in skip_ext):
        return False

    return True


def clean_title(title):
    """Clean up title."""
    if not title:
        return ''
    title = ' '.join(title.split())
    # Remove site name suffixes
    for sep in [' - ', ' | ', ' – ', ' — ', ' :: ', ' >> ']:
        if sep in title:
            parts = title.split(sep)
            # Keep the longer part (usually the actual title)
            title = max(parts, key=len)
    title = title.strip('| -–—·•>»')
    title = title.strip()
    return title[:250]


def find_mp4_links(html, page_url):
    """Find ALL .mp4 URLs in page HTML."""
    found = set()

    # Regex patterns for mp4 URLs
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

    # Parse HTML for href/src attributes
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
    """Follow iframes to find mp4 links."""
    if depth > 3:
        return []

    results = []
    soup = BeautifulSoup(html, 'lxml')

    for iframe in soup.find_all('iframe'):
        src = iframe.get('src', '') or iframe.get('data-src', '') or iframe.get('data-lazy-src', '')
        if not src or src.startswith('about:') or src.startswith('javascript:'):
            continue

        iframe_url = urljoin(page_url, src.strip())

        skip = ['google', 'facebook', 'twitter', 'doubleclick', 'adsense', 'disqus', 'syndication']
        if any(s in iframe_url.lower() for s in skip):
            continue

        if '.mp4' in iframe_url.lower():
            results.append(clean_url(iframe_url))
            continue

        log.info(f"  {'  '*depth}↳ iframe[{depth+1}]: {iframe_url[:80]}")
        time.sleep(1)
        iframe_html = fetch(scraper, iframe_url, ref=page_url, tries=2)
        if iframe_html:
            mp4s = find_mp4_links(iframe_html, iframe_url)
            results.extend(mp4s)
            nested = find_iframe_mp4(iframe_html, iframe_url, scraper, depth + 1)
            results.extend(nested)

    return results


def extract_page_title(html, fallback=''):
    """Extract title from individual video page for better accuracy."""
    soup = BeautifulSoup(html, 'lxml')

    # Priority 1: Entry title / post title heading
    for sel in ['.entry-title', '.post-title', 'h1.title', 'h1', 'h2.title']:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) >= 3:
                return clean_title(text)

    # Priority 2: <title> tag
    title_tag = soup.find('title')
    if title_tag:
        text = title_tag.get_text(strip=True)
        if text and len(text) >= 3:
            return clean_title(text)

    # Priority 3: og:title meta
    og = soup.find('meta', property='og:title')
    if og:
        text = og.get('content', '').strip()
        if text and len(text) >= 3:
            return clean_title(text)

    return fallback


def extract_page_thumbnail(html, page_url, fallback=''):
    """Extract thumbnail from individual video page."""
    soup = BeautifulSoup(html, 'lxml')

    # Priority 1: og:image meta
    og = soup.find('meta', property='og:image')
    if og:
        val = og.get('content', '').strip()
        if val:
            return urljoin(page_url, val)

    # Priority 2: twitter:image meta
    tw = soup.find('meta', attrs={'name': 'twitter:image'})
    if tw:
        val = tw.get('content', '').strip()
        if val:
            return urljoin(page_url, val)

    # Priority 3: Video poster
    video = soup.find('video')
    if video:
        poster = video.get('poster', '').strip()
        if poster:
            return urljoin(page_url, poster)

    # Priority 4: First large image in content area
    content_selectors = ['.entry-content', '.post-content', '.content', 'article', '.single-content', 'main']
    for sel in content_selectors:
        content = soup.select_one(sel)
        if content:
            for img in content.find_all('img'):
                src = get_img_src_simple(img, page_url)
                if src and not is_tiny_image(src):
                    return src

    return fallback


def get_img_src_simple(img, page_url):
    for attr in ['src', 'data-src', 'data-lazy-src', 'data-original']:
        val = img.get(attr, '').strip()
        if val and not val.startswith('data:'):
            return urljoin(page_url, val)
    return ''


def pick_best_mp4(urls):
    """Pick the best MP4 URL."""
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
        nums = re.findall(r'/(\d+)\.mp4', ul)
        if nums:
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


def main():
    scraper = make_scraper()
    all_entries = []
    all_seen_urls = set()  # Track across ALL pages to avoid duplicates

    log.info("=" * 60)
    log.info(f"SCRAPING ALL PAGES: {BASE_URL}")
    log.info("=" * 60)

    page_num = 1
    consecutive_empty = 0

    while page_num <= MAX_PAGES:
        if page_num == 1:
            page_url = BASE_URL + "/"
        else:
            page_url = f"{BASE_URL}/page/{page_num}/"

        log.info(f"\n{'='*50}")
        log.info(f"📄 PAGE {page_num}: {page_url}")
        log.info(f"{'='*50}")

        html = fetch(scraper, page_url)
        if not html:
            log.info(f"❌ Cannot fetch page {page_num} - END OF PAGES")
            break

        # Check if we got a 404 page or empty page
        soup_check = BeautifulSoup(html, 'lxml')
        title_check = soup_check.find('title')
        if title_check and ('not found' in title_check.get_text(strip=True).lower() or
                           '404' in title_check.get_text(strip=True)):
            log.info(f"🛑 Page {page_num} is 404 - END OF PAGES")
            break

        listings = get_listings(html, page_url)

        # Filter out already-seen URLs
        new_listings = []
        for item in listings:
            if item['page_url'] not in all_seen_urls:
                all_seen_urls.add(item['page_url'])
                new_listings.append(item)

        log.info(f"Found {len(listings)} links, {len(new_listings)} new")

        if not new_listings:
            consecutive_empty += 1
            log.info(f"No new listings (consecutive empty: {consecutive_empty})")
            if consecutive_empty >= 3:
                log.info("🛑 3 consecutive empty pages - stopping")
                break
            # Still try next page
            page_num += 1
            time.sleep(DELAY)
            continue
        else:
            consecutive_empty = 0

        # Process each video page
        page_found = 0
        for idx, item in enumerate(new_listings):
            log.info(f"\n  [{idx+1}/{len(new_listings)}] {item['title'][:60]}")
            log.info(f"  URL: {item['page_url']}")

            time.sleep(DELAY)
            item_html = fetch(scraper, item['page_url'], ref=page_url)
            if not item_html:
                log.warning("  ❌ Could not fetch")
                continue

            # Extract BETTER title from the actual video page
            page_title = extract_page_title(item_html, fallback=item['title'])

            # Extract BETTER thumbnail from the actual video page
            page_thumb = extract_page_thumbnail(item_html, item['page_url'], fallback=item.get('thumbnail', ''))

            # Find MP4 links
            mp4_links = find_mp4_links(item_html, item['page_url'])

            # Follow iframes
            iframe_mp4s = find_iframe_mp4(item_html, item['page_url'], scraper)
            mp4_links.extend(iframe_mp4s)

            # Deduplicate
            mp4_links = list(set(filter(None, mp4_links)))

            if mp4_links:
                log.info(f"  Found {len(mp4_links)} MP4:")
                for u in mp4_links[:5]:
                    log.info(f"    📹 {u}")

                best = pick_best_mp4(mp4_links)
                if best:
                    # Check not duplicate stream URL
                    if not any(e['stream_url'] == best for e in all_entries):
                        all_entries.append({
                            'title': page_title,
                            'stream_url': best,
                            'page_url': item['page_url'],
                            'thumbnail': page_thumb
                        })
                        page_found += 1
                        log.info(f"  ✅ [{len(all_entries)}] {page_title[:50]}")
                        log.info(f"     Stream: {best}")
                        log.info(f"     Thumb: {page_thumb[:80]}")
                    else:
                        log.info(f"  ⏭ Duplicate stream URL, skipping")
            else:
                log.warning(f"  ❌ No MP4 found")
                # Debug info
                soup_dbg = BeautifulSoup(item_html, 'lxml')
                iframes = soup_dbg.find_all('iframe')
                videos = soup_dbg.find_all('video')
                log.info(f"    Debug: {len(iframes)} iframes, {len(videos)} video tags")
                for iframe in iframes[:3]:
                    log.info(f"    iframe: {iframe.get('src', 'none')[:100]}")

        log.info(f"\n  Page {page_num} result: {page_found} new videos (total: {len(all_entries)})")

        # Check if there's a next page
        if not has_next_page(html, page_num):
            log.info(f"🛑 No next page link found after page {page_num} - END")
            break

        page_num += 1
        time.sleep(DELAY)

    # ========== OUTPUT ==========
    log.info(f"\n{'='*60}")
    log.info(f"✅ COMPLETE! Total pages scraped: {page_num}")
    log.info(f"✅ Total videos: {len(all_entries)}")
    log.info(f"{'='*60}")

    # Save M3U
    m3u = ['#EXTM3U', '']
    for e in all_entries:
        thumb = e.get('thumbnail', '')
        m3u.append(f'#EXTINF:-1 tvg-logo="{thumb}",{e["title"]}')
        m3u.append(f'#EXTVLCOPT:http-referrer={e["page_url"]}')
        m3u.append(e['stream_url'])
        m3u.append('')

    with open('playlist.m3u', 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u))
    log.info(f"✅ playlist.m3u saved ({len(all_entries)} entries)")

    # Save JSON
    with open('playlist.json', 'w', encoding='utf-8') as f:
        json.dump(all_entries, f, indent=2, ensure_ascii=False)
    log.info("✅ playlist.json saved")

    # Save HTML player
    write_html_player(all_entries)
    log.info("✅ index.html saved")

    # Summary
    log.info(f"\n📋 FULL PLAYLIST ({len(all_entries)} videos):")
    for i, e in enumerate(all_entries):
        log.info(f"  {i+1}. {e['title'][:55]}")
        log.info(f"     {e['stream_url']}")
        if e.get('thumbnail'):
            log.info(f"     🖼 {e['thumbnail'][:70]}")


def write_html_player(entries):
    pjson = json.dumps(entries, ensure_ascii=False)
    ts = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
    count = len(entries)

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stream Player - {count} Videos</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a0a;color:#e0e0e0;min-height:100vh}}
.hdr{{background:linear-gradient(135deg,#1a1a2e,#16213e);padding:14px 20px;text-align:center;border-bottom:2px solid #e94560}}
.hdr h1{{font-size:1.4rem;color:#e94560}}.hdr p{{color:#555;font-size:.78rem;margin-top:4px}}
.wrap{{display:flex;flex-direction:column;max-width:1500px;margin:0 auto}}
@media(min-width:992px){{.wrap{{flex-direction:row;height:calc(100vh - 68px)}}}}
.left{{flex:1;padding:15px;display:flex;flex-direction:column;min-width:0;overflow-y:auto}}
.vbox{{position:relative;width:100%;padding-bottom:56.25%;background:#000;border-radius:10px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.6);flex-shrink:0}}
.vbox video{{position:absolute;top:0;left:0;width:100%;height:100%}}
.ph{{position:absolute;top:0;left:0;width:100%;height:100%;display:flex;align-items:center;justify-content:center;flex-direction:column;color:#333}}
.ph svg{{width:60px;height:60px;fill:#222;margin-bottom:10px}}
.np{{margin-top:10px;padding:10px 14px;background:#1a1a2e;border-radius:8px;border-left:3px solid #e94560;display:none}}
.np small{{color:#e94560;text-transform:uppercase;letter-spacing:1px;font-size:.65rem}}
.np p{{color:#ddd;font-size:.9rem;margin-top:3px;word-break:break-word}}
.err{{color:#e94560;background:#1a0a0e;padding:8px 12px;border-radius:6px;margin-top:8px;font-size:.8rem;display:none}}
.ctrl{{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.b{{padding:8px 14px;border:none;border-radius:6px;cursor:pointer;font-size:.82rem;font-weight:600;transition:all .2s;display:inline-flex;align-items:center;gap:4px;text-decoration:none}}
.bp{{background:#e94560;color:#fff}}.bp:hover{{background:#d13350}}
.bs{{background:#1a1a2e;color:#aaa;border:1px solid #333}}.bs:hover{{border-color:#e94560;color:#fff}}
.tg{{display:flex;align-items:center;gap:6px;margin-left:auto;font-size:.78rem;color:#666}}
.sw{{position:relative;width:36px;height:19px}}.sw input{{opacity:0;width:0;height:0}}
.sw span{{position:absolute;inset:0;background:#333;border-radius:19px;cursor:pointer;transition:.3s}}
.sw span:before{{content:"";position:absolute;height:13px;width:13px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.3s}}
.sw input:checked+span{{background:#e94560}}.sw input:checked+span:before{{transform:translateX(17px)}}
.right{{width:100%;background:#111;border-left:1px solid #1a1a1a;display:flex;flex-direction:column}}
@media(min-width:992px){{.right{{width:420px}}}}
.rh{{padding:10px 14px;background:#1a1a2e;border-bottom:1px solid #222;display:flex;justify-content:space-between;align-items:center}}
.rh h2{{font-size:.9rem;color:#e94560}}
.badge{{background:#e94560;color:#fff;padding:2px 9px;border-radius:10px;font-size:.72rem}}
.sb{{padding:8px 12px;border-bottom:1px solid #222}}
.sb input{{width:100%;padding:8px 12px;background:#1a1a2e;border:1px solid #333;border-radius:6px;color:#e0e0e0;font-size:.85rem;outline:none}}
.sb input:focus{{border-color:#e94560}}.sb input::placeholder{{color:#444}}
.ls{{flex:1;overflow-y:auto;padding:6px}}
.ls::-webkit-scrollbar{{width:5px}}.ls::-webkit-scrollbar-thumb{{background:#333;border-radius:3px}}
.li{{display:flex;align-items:center;padding:8px;margin-bottom:3px;border-radius:6px;cursor:pointer;transition:background .15s;gap:8px;border:1px solid transparent}}
.li:hover{{background:#1a1a2e}}.li.a{{background:#16213e;border-color:#e94560}}
.li .th{{width:80px;height:50px;border-radius:4px;object-fit:cover;background:#1a1a2e;flex-shrink:0}}
.li .th-empty{{width:80px;height:50px;border-radius:4px;background:#1a1a2e;flex-shrink:0;display:flex;align-items:center;justify-content:center;color:#333;font-size:1.2rem}}
.li .n{{color:#444;font-size:.72rem;min-width:22px;text-align:center}}.li.a .n{{color:#e94560}}
.li .m{{flex:1;min-width:0}}.li .m .t{{font-size:.82rem;overflow:hidden;text-overflow:ellipsis;color:#bbb;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}}
.li.a .m .t{{color:#fff}}.li .m .s{{font-size:.65rem;color:#444;margin-top:2px}}
.li .ic{{color:#444;font-size:.85rem;flex-shrink:0}}.li.a .ic{{color:#e94560}}
.ft{{padding:6px;background:#0d0d0d;border-top:1px solid #1a1a1a;text-align:center;font-size:.65rem;color:#333}}
</style>
</head>
<body>
<div class="hdr">
<h1>🎬 Stream Player</h1>
<p>{count} videos • Updated: {ts}</p>
</div>
<div class="wrap">
<div class="left">
<div class="vbox" id="vb">
<div class="ph" id="ph"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg><p>Select a video</p></div>
</div>
<div class="np" id="np"><small>▶ Now Playing</small><p id="npt"></p></div>
<div class="err" id="er"></div>
<div class="ctrl">
<button class="b bp" onclick="P()">⏮ Prev</button>
<button class="b bp" onclick="N()">Next ⏭</button>
<button class="b bs" onclick="S()">🔀 Shuffle</button>
<a class="b bs" href="playlist.m3u" download>📥 M3U</a>
<div class="tg"><label class="sw"><input type="checkbox" id="ap" checked><span></span></label>Auto</div>
</div>
</div>
<div class="right">
<div class="rh"><h2>📋 Playlist</h2><span class="badge" id="cn">{count}</span></div>
<div class="sb"><input id="q" placeholder="🔍 Search {count} videos..." oninput="F()"></div>
<div class="ls" id="ls"></div>
<div class="ft">Auto-updates every 6 hours via GitHub Actions</div>
</div>
</div>
<script>
const D={pjson};
let ci=-1,fi=[];
document.addEventListener('DOMContentLoaded',()=>{{fi=D.map((_,i)=>i);R();if(D.length)pl(0);}});
function R(){{
const c=document.getElementById('ls');
if(!fi.length){{c.innerHTML='<div style="text-align:center;padding:40px;color:#444">No results</div>';return;}}
c.innerHTML=fi.map(i=>{{
const d=D[i],a=i===ci;
let thHtml='';
if(d.thumbnail){{
thHtml=`<img class="th" src="${{X(d.thumbnail)}}" alt="" loading="lazy" onerror="this.outerHTML='<div class=th-empty>🎬</div>'">`;
}}else{{
thHtml='<div class="th-empty">🎬</div>';
}}
return`<div class="li${{a?' a':''}}" onclick="pl(${{i}})" title="${{X(d.title)}}">
<span class="n">${{i+1}}</span>
${{thHtml}}
<div class="m"><div class="t">${{X(d.title)}}</div><div class="s">MP4</div></div>
<span class="ic">${{a?'🔊':'▶'}}</span></div>`;
}}).join('');
}}
function pl(i){{
if(i<0||i>=D.length)return;
ci=i;const d=D[i],w=document.getElementById('vb'),ph=document.getElementById('ph'),
np=document.getElementById('np'),er=document.getElementById('er');
const old=w.querySelector('video');if(old)old.remove();
if(ph)ph.style.display='none';
er.style.display='none';np.style.display='block';
document.getElementById('npt').textContent=d.title;
document.title='▶ '+d.title;
const v=document.createElement('video');
v.controls=true;v.autoplay=true;v.preload='auto';
v.style.cssText='position:absolute;top:0;left:0;width:100%;height:100%';
if(d.thumbnail)v.poster=d.thumbnail;
v.src=d.stream_url;
w.appendChild(v);
v.play().catch(()=>{{}});
v.onended=()=>{{if(document.getElementById('ap').checked)N();}};
v.onerror=()=>{{
er.textContent='⚠ Playback error - trying next...';er.style.display='block';
if(document.getElementById('ap').checked)setTimeout(N,2000);
}};
R();
setTimeout(()=>{{const a=document.querySelector('.li.a');if(a)a.scrollIntoView({{behavior:'smooth',block:'center'}});}},150);
}}
function N(){{ci<D.length-1?pl(ci+1):pl(0);}}
function P(){{ci>0?pl(ci-1):pl(D.length-1);}}
function S(){{if(!D.length)return;let r;do{{r=Math.floor(Math.random()*D.length);}}while(r===ci&&D.length>1);pl(r);}}
function F(){{
const q=document.getElementById('q').value.toLowerCase().trim();
fi=q?D.map((d,i)=>({{i,t:d.title.toLowerCase()}})).filter(x=>x.t.includes(q)).map(x=>x.i):D.map((_,i)=>i);
document.getElementById('cn').textContent=fi.length;R();
}}
function X(s){{const d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}}
document.addEventListener('keydown',e=>{{
if(e.target.tagName==='INPUT')return;
if(e.key==='ArrowRight'||e.key==='n'){{e.preventDefault();N();}}
else if(e.key==='ArrowLeft'||e.key==='p'){{e.preventDefault();P();}}
else if(e.key==='s'){{e.preventDefault();S();}}
else if(e.key===' '){{e.preventDefault();const v=document.querySelector('#vb video');if(v)v.paused?v.play():v.pause();}}
}});
</script>
</body>
</html>'''

    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html)


if __name__ == '__main__':
    main()
