# scraper.py
import re
import json
import time
import logging
import cloudscraper
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BASE_URL = "https://kaamdesi.com"
MAX_PAGES = 10  # Max pages to scrape
REQUEST_DELAY = 3  # Seconds between requests


def get_scraper():
    """Create a cloudscraper session to bypass Cloudflare."""
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
                      'Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Referer': BASE_URL,
        'Connection': 'keep-alive',
    })
    return scraper


def fetch_page(scraper, url, retries=3):
    """Fetch a page with retry logic."""
    for attempt in range(retries):
        try:
            logger.info(f"Fetching: {url} (attempt {attempt + 1})")
            response = scraper.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed for {url}: {e}")
            if attempt < retries - 1:
                time.sleep(REQUEST_DELAY * (attempt + 1))
    return None


def extract_video_links_from_listing(html, base_url):
    """Extract video page links from the listing/index page."""
    soup = BeautifulSoup(html, 'lxml')
    videos = []

    # Common selectors for video listing pages
    selectors = [
        'article a[href]',
        '.post-thumbnail a[href]',
        '.entry-title a[href]',
        'h2 a[href]',
        'h3 a[href]',
        '.video-item a[href]',
        '.thumb a[href]',
        '.videos-list a[href]',
        'a.post-link[href]',
        '.content a[href]',
        '.item a[href]',
        '.video-block a[href]',
        '.video-card a[href]',
        'a[href*="/video/"]',
        'a[href*="/watch/"]',
        'a[href*="/post/"]',
    ]

    found_links = set()

    for selector in selectors:
        elements = soup.select(selector)
        for elem in elements:
            href = elem.get('href', '').strip()
            if not href or href == '#':
                continue
            full_url = urljoin(base_url, href)
            # Filter only links from the same domain that look like content pages
            parsed = urlparse(full_url)
            if parsed.netloc and BASE_URL.replace('https://', '').replace('http://', '') in parsed.netloc:
                # Skip pagination, category, tag links
                skip_patterns = [
                    '/page/', '/category/', '/tag/', '/author/',
                    '/login', '/register', '/wp-admin', '/feed',
                    '#', 'javascript:', '/search/'
                ]
                if not any(pat in full_url.lower() for pat in skip_patterns):
                    if full_url not in found_links:
                        found_links.add(full_url)
                        # Try to get title
                        title = elem.get('title', '').strip()
                        if not title:
                            title = elem.get_text(strip=True)
                        if not title:
                            img = elem.find('img')
                            if img:
                                title = img.get('alt', '').strip()
                        videos.append({
                            'page_url': full_url,
                            'title': title if title else full_url.split('/')[-1]
                        })

    # Deduplicate by URL
    seen = set()
    unique_videos = []
    for v in videos:
        if v['page_url'] not in seen:
            seen.add(v['page_url'])
            unique_videos.append(v)

    return unique_videos


def extract_stream_url(html, page_url):
    """Extract the actual video/stream URL from a video page."""
    soup = BeautifulSoup(html, 'lxml')
    stream_urls = []

    # Method 1: Look for <video> and <source> tags
    for video_tag in soup.find_all('video'):
        src = video_tag.get('src', '').strip()
        if src:
            stream_urls.append(urljoin(page_url, src))
        for source in video_tag.find_all('source'):
            src = source.get('src', '').strip()
            if src:
                stream_urls.append(urljoin(page_url, src))

    # Method 2: Look for iframe embeds
    for iframe in soup.find_all('iframe'):
        src = iframe.get('src', '').strip()
        if src and not any(x in src.lower() for x in ['ads', 'banner', 'google', 'facebook']):
            stream_urls.append(urljoin(page_url, src))

    # Method 3: Look for .m3u8 links in the page source
    m3u8_pattern = re.compile(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', re.IGNORECASE)
    matches = m3u8_pattern.findall(html)
    stream_urls.extend(matches)

    # Method 4: Look for .mp4 links in the page source
    mp4_pattern = re.compile(r'(https?://[^\s\'"<>]+\.mp4[^\s\'"<>]*)', re.IGNORECASE)
    matches = mp4_pattern.findall(html)
    stream_urls.extend(matches)

    # Method 5: Look for common video player configurations in scripts
    for script in soup.find_all('script'):
        script_text = script.string or ''
        if not script_text:
            continue

        # Look for source/file/url assignments
        patterns = [
            r'["\']?(?:source|file|src|url|video_url|stream_url)["\']?\s*[:=]\s*["\']([^"\']+\.(?:m3u8|mp4|webm)[^"\']*)["\']',
            r'(?:source|file|src|url):\s*["\']([^"\']+)["\']',
            r'player\.src\(["\']([^"\']+)["\']',
            r'Clappr\.Player\(\{[^}]*source:\s*["\']([^"\']+)["\']',
            r'videojs\([^)]*\)\.src\(["\']([^"\']+)["\']',
            r'jwplayer\([^)]*\)\.setup\(\{[^}]*file:\s*["\']([^"\']+)["\']',
            r'flowplayer\([^)]*\{[^}]*clip:\s*\{[^}]*url:\s*["\']([^"\']+)["\']',
        ]
        for pattern in patterns:
            found = re.findall(pattern, script_text, re.IGNORECASE)
            for f in found:
                full = urljoin(page_url, f)
                stream_urls.append(full)

        # Look for JSON configs
        json_pattern = re.compile(r'\{[^{}]*"(?:source|file|src|url)"[^{}]*\}', re.IGNORECASE)
        json_matches = json_pattern.findall(script_text)
        for jm in json_matches:
            try:
                data = json.loads(jm)
                for key in ['source', 'file', 'src', 'url']:
                    if key in data and isinstance(data[key], str):
                        stream_urls.append(urljoin(page_url, data[key]))
            except (json.JSONDecodeError, TypeError):
                pass

    # Method 6: Look for embed URLs (common for tube sites)
    embed_pattern = re.compile(r'(https?://[^\s\'"<>]+/embed/[^\s\'"<>]+)', re.IGNORECASE)
    matches = embed_pattern.findall(html)
    stream_urls.extend(matches)

    # Method 7: Look for player div data attributes
    for div in soup.find_all(['div', 'span', 'a'], attrs=True):
        for attr_name, attr_val in div.attrs.items():
            if isinstance(attr_val, str) and (
                '.m3u8' in attr_val or '.mp4' in attr_val or 'embed' in attr_val
            ):
                stream_urls.append(urljoin(page_url, attr_val))

    # Clean and deduplicate URLs
    cleaned = []
    seen = set()
    for url in stream_urls:
        # Unescape common escapes
        url = url.replace('\\/', '/').replace('&amp;', '&').strip()
        if url and url not in seen:
            seen.add(url)
            cleaned.append(url)

    return cleaned


def get_best_stream(stream_urls):
    """Pick the best stream URL from candidates."""
    # Priority: m3u8 > mp4 > embed/iframe
    m3u8_urls = [u for u in stream_urls if '.m3u8' in u.lower()]
    mp4_urls = [u for u in stream_urls if '.mp4' in u.lower()]
    embed_urls = [u for u in stream_urls if 'embed' in u.lower() or 'iframe' in u.lower()]
    other_urls = [u for u in stream_urls if u not in m3u8_urls + mp4_urls + embed_urls]

    if m3u8_urls:
        return m3u8_urls[0]
    if mp4_urls:
        return mp4_urls[0]
    if embed_urls:
        return embed_urls[0]
    if other_urls:
        return other_urls[0]
    return None


def find_next_page(html, current_url, current_page):
    """Find the next page URL."""
    soup = BeautifulSoup(html, 'lxml')

    # Try common next page patterns
    next_page_num = current_page + 1

    # Pattern 1: /page/N/
    next_selectors = [
        f'a[href*="/page/{next_page_num}"]',
        'a.next',
        'a.nextpostslink',
        '.pagination a.next',
        '.nav-links a.next',
        'a[rel="next"]',
        '.next a',
    ]

    for selector in next_selectors:
        elem = soup.select_one(selector)
        if elem:
            href = elem.get('href', '').strip()
            if href:
                return urljoin(current_url, href)

    # Construct URL manually
    next_url = f"{BASE_URL}/page/{next_page_num}/"
    return next_url


def sanitize_title(title):
    """Clean up title for M3U playlist."""
    if not title:
        return "Untitled"
    # Remove extra whitespace
    title = ' '.join(title.split())
    # Remove problematic characters for M3U
    title = title.replace('\n', ' ').replace('\r', ' ')
    return title[:200]  # Limit length


def generate_m3u(entries):
    """Generate M3U playlist content."""
    lines = ['#EXTM3U']
    lines.append(f'#PLAYLIST:KaamDesi Playlist')
    lines.append('')

    for i, entry in enumerate(entries):
        title = sanitize_title(entry.get('title', f'Video {i+1}'))
        stream_url = entry.get('stream_url', '')
        page_url = entry.get('page_url', '')
        duration = -1  # Unknown duration

        lines.append(f'#EXTINF:{duration},{title}')
        if page_url:
            lines.append(f'#EXTVLCOPT:http-referrer={page_url}')
            lines.append(f'#EXTVLCOPT:http-origin={BASE_URL}')
        lines.append(stream_url)
        lines.append('')

    return '\n'.join(lines)


def main():
    scraper = get_scraper()
    all_entries = []

    logger.info("Starting scraper...")

    for page_num in range(1, MAX_PAGES + 1):
        if page_num == 1:
            page_url = f"{BASE_URL}/page/{page_num}/"
        else:
            page_url = f"{BASE_URL}/page/{page_num}/"

        html = fetch_page(scraper, page_url)
        if not html:
            logger.warning(f"Could not fetch page {page_num}, stopping.")
            break

        # Extract video listing
        video_list = extract_video_links_from_listing(html, page_url)
        logger.info(f"Page {page_num}: Found {len(video_list)} video links")

        if not video_list:
            logger.info(f"No more videos found on page {page_num}, stopping.")
            break

        # Visit each video page to extract stream URL
        for video in video_list:
            time.sleep(REQUEST_DELAY)
            video_html = fetch_page(scraper, video['page_url'])
            if not video_html:
                continue

            stream_urls = extract_stream_url(video_html, video['page_url'])
            best_stream = get_best_stream(stream_urls)

            if best_stream:
                entry = {
                    'title': video.get('title', 'Untitled'),
                    'stream_url': best_stream,
                    'page_url': video['page_url']
                }
                all_entries.append(entry)
                logger.info(f"  ✅ {entry['title'][:60]}... -> {best_stream[:80]}...")
            else:
                # If no direct stream found, use the page URL as fallback (for embed)
                logger.warning(f"  ❌ No stream found for: {video['page_url']}")

        time.sleep(REQUEST_DELAY)

    logger.info(f"\nTotal entries found: {len(all_entries)}")

    if all_entries:
        # Generate M3U playlist
        m3u_content = generate_m3u(all_entries)
        with open('playlist.m3u', 'w', encoding='utf-8') as f:
            f.write(m3u_content)
        logger.info("✅ playlist.m3u generated successfully!")

        # Generate JSON for the HTML player
        playlist_json = json.dumps(all_entries, indent=2, ensure_ascii=False)
        with open('playlist.json', 'w', encoding='utf-8') as f:
            f.write(playlist_json)
        logger.info("✅ playlist.json generated successfully!")

        # Update index.html with embedded playlist data
        generate_html_player(all_entries)
        logger.info("✅ index.html updated successfully!")
    else:
        logger.warning("No entries found. Creating empty playlist.")
        with open('playlist.m3u', 'w', encoding='utf-8') as f:
            f.write('#EXTM3U\n#PLAYLIST:KaamDesi - No videos found\n')
        with open('playlist.json', 'w', encoding='utf-8') as f:
            f.write('[]')
        generate_html_player([])


def generate_html_player(entries):
    """Generate the HTML player page with embedded playlist."""
    playlist_json = json.dumps(entries, indent=2, ensure_ascii=False)

    html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Stream Player</title>

    <!-- HLS.js for m3u8 support -->
    <script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js"></script>

    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, sans-serif;
            background: #0a0a0a;
            color: #e0e0e0;
            min-height: 100vh;
        }}

        .header {{
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            padding: 20px;
            text-align: center;
            border-bottom: 2px solid #e94560;
        }}

        .header h1 {{
            font-size: 1.8rem;
            color: #e94560;
            margin-bottom: 5px;
        }}

        .header p {{
            color: #888;
            font-size: 0.9rem;
        }}

        .container {{
            display: flex;
            flex-direction: column;
            max-width: 1400px;
            margin: 0 auto;
            min-height: calc(100vh - 100px);
        }}

        @media (min-width: 992px) {{
            .container {{
                flex-direction: row;
            }}
        }}

        .player-section {{
            flex: 1;
            padding: 20px;
            min-width: 0;
        }}

        .video-wrapper {{
            position: relative;
            width: 100%;
            padding-bottom: 56.25%;
            background: #000;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
        }}

        .video-wrapper video,
        .video-wrapper iframe {{
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            border: none;
        }}

        .video-placeholder {{
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-direction: column;
            color: #555;
        }}

        .video-placeholder svg {{
            width: 80px;
            height: 80px;
            margin-bottom: 15px;
            fill: #333;
        }}

        .now-playing {{
            margin-top: 15px;
            padding: 15px;
            background: #1a1a2e;
            border-radius: 8px;
            border-left: 4px solid #e94560;
        }}

        .now-playing h3 {{
            color: #e94560;
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 5px;
        }}

        .now-playing p {{
            color: #ddd;
            font-size: 1rem;
        }}

        .controls {{
            margin-top: 15px;
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }}

        .btn {{
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.9rem;
            font-weight: 600;
            transition: all 0.3s ease;
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .btn-primary {{
            background: #e94560;
            color: white;
        }}

        .btn-primary:hover {{
            background: #c73652;
            transform: translateY(-2px);
        }}

        .btn-secondary {{
            background: #1a1a2e;
            color: #e0e0e0;
            border: 1px solid #333;
        }}

        .btn-secondary:hover {{
            background: #16213e;
            border-color: #e94560;
        }}

        .playlist-section {{
            width: 100%;
            background: #111;
            border-left: 1px solid #222;
            display: flex;
            flex-direction: column;
        }}

        @media (min-width: 992px) {{
            .playlist-section {{
                width: 400px;
                max-height: calc(100vh - 100px);
            }}
        }}

        .playlist-header {{
            padding: 15px 20px;
            background: #1a1a2e;
            border-bottom: 1px solid #222;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .playlist-header h2 {{
            font-size: 1rem;
            color: #e94560;
        }}

        .playlist-count {{
            background: #e94560;
            color: white;
            padding: 2px 10px;
            border-radius: 12px;
            font-size: 0.8rem;
        }}

        .search-box {{
            padding: 10px 20px;
            border-bottom: 1px solid #222;
        }}

        .search-box input {{
            width: 100%;
            padding: 10px 15px;
            background: #1a1a2e;
            border: 1px solid #333;
            border-radius: 8px;
            color: #e0e0e0;
            font-size: 0.9rem;
            outline: none;
            transition: border-color 0.3s;
        }}

        .search-box input:focus {{
            border-color: #e94560;
        }}

        .search-box input::placeholder {{
            color: #555;
        }}

        .playlist-items {{
            flex: 1;
            overflow-y: auto;
            padding: 10px;
        }}

        .playlist-items::-webkit-scrollbar {{
            width: 6px;
        }}

        .playlist-items::-webkit-scrollbar-track {{
            background: #111;
        }}

        .playlist-items::-webkit-scrollbar-thumb {{
            background: #333;
            border-radius: 3px;
        }}

        .playlist-item {{
            display: flex;
            align-items: center;
            padding: 12px 15px;
            margin-bottom: 4px;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s ease;
            gap: 12px;
        }}

        .playlist-item:hover {{
            background: #1a1a2e;
        }}

        .playlist-item.active {{
            background: linear-gradient(135deg, #1a1a2e, #16213e);
            border: 1px solid #e94560;
        }}

        .playlist-item .index {{
            color: #555;
            font-size: 0.85rem;
            min-width: 30px;
            text-align: center;
        }}

        .playlist-item.active .index {{
            color: #e94560;
        }}

        .playlist-item .info {{
            flex: 1;
            min-width: 0;
        }}

        .playlist-item .info .title {{
            font-size: 0.9rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            color: #ddd;
        }}

        .playlist-item.active .info .title {{
            color: #fff;
        }}

        .playlist-item .info .type {{
            font-size: 0.75rem;
            color: #666;
            margin-top: 2px;
        }}

        .playlist-item .play-icon {{
            color: #555;
            font-size: 1.2rem;
        }}

        .playlist-item.active .play-icon {{
            color: #e94560;
        }}

        .no-results {{
            text-align: center;
            padding: 40px 20px;
            color: #555;
        }}

        .status-bar {{
            padding: 10px 20px;
            background: #0d0d0d;
            border-top: 1px solid #222;
            font-size: 0.8rem;
            color: #555;
            text-align: center;
        }}

        .autoplay-toggle {{
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.85rem;
            color: #888;
        }}

        .toggle-switch {{
            position: relative;
            width: 40px;
            height: 22px;
        }}

        .toggle-switch input {{
            opacity: 0;
            width: 0;
            height: 0;
        }}

        .toggle-slider {{
            position: absolute;
            cursor: pointer;
            inset: 0;
            background: #333;
            border-radius: 22px;
            transition: 0.3s;
        }}

        .toggle-slider:before {{
            content: "";
            position: absolute;
            height: 16px;
            width: 16px;
            left: 3px;
            bottom: 3px;
            background: white;
            border-radius: 50%;
            transition: 0.3s;
        }}

        .toggle-switch input:checked + .toggle-slider {{
            background: #e94560;
        }}

        .toggle-switch input:checked + .toggle-slider:before {{
            transform: translateX(18px);
        }}

        .loading-spinner {{
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 2px solid #333;
            border-top-color: #e94560;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }}

        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}

        .empty-state {{
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 60px 20px;
            color: #555;
            text-align: center;
        }}

        .empty-state h3 {{
            margin-top: 15px;
            color: #777;
        }}

        .empty-state p {{
            margin-top: 8px;
            font-size: 0.9rem;
        }}
    </style>
</head>
<body>

    <div class="header">
        <h1>🎬 Stream Player</h1>
        <p>Auto-updated playlist • Last build: <span id="lastUpdate"></span></p>
    </div>

    <div class="container">
        <div class="player-section">
            <div class="video-wrapper" id="videoWrapper">
                <div class="video-placeholder" id="placeholder">
                    <svg viewBox="0 0 24 24">
                        <path d="M8 5v14l11-7z"/>
                    </svg>
                    <p>Select a video from the playlist</p>
                </div>
            </div>

            <div class="now-playing" id="nowPlaying" style="display:none;">
                <h3>▶ Now Playing</h3>
                <p id="nowPlayingTitle">-</p>
            </div>

            <div class="controls">
                <button class="btn btn-primary" onclick="playPrevious()">⏮ Previous</button>
                <button class="btn btn-primary" onclick="playNext()">Next ⏭</button>
                <button class="btn btn-secondary" onclick="shufflePlaylist()">🔀 Shuffle</button>
                <a id="downloadLink" class="btn btn-secondary" href="playlist.m3u" download>
                    📥 Download M3U
                </a>
                <div class="autoplay-toggle">
                    <label class="toggle-switch">
                        <input type="checkbox" id="autoplayToggle" checked>
                        <span class="toggle-slider"></span>
                    </label>
                    <span>Autoplay</span>
                </div>
            </div>
        </div>

        <div class="playlist-section">
            <div class="playlist-header">
                <h2>📋 Playlist</h2>
                <span class="playlist-count" id="playlistCount">0</span>
            </div>

            <div class="search-box">
                <input type="text" id="searchInput" placeholder="🔍 Search videos..."
                       oninput="filterPlaylist()">
            </div>

            <div class="playlist-items" id="playlistContainer">
                <!-- Items populated by JS -->
            </div>

            <div class="status-bar">
                Auto-refreshes every 6 hours via GitHub Actions
            </div>
        </div>
    </div>

    <script>
        // Embedded playlist data (updated by scraper)
        const PLAYLIST_DATA = {playlist_json};

        let currentIndex = -1;
        let hlsInstance = null;
        let filteredIndices = [];

        // Initialize
        document.addEventListener('DOMContentLoaded', () => {{
            document.getElementById('lastUpdate') .textContent = new Date().toLocaleString();
            initPlaylist();
            if (PLAYLIST_DATA.length > 0) {{
                playVideo(0);
            }}
        }});

        function initPlaylist() {{
            const container = document.getElementById('playlistContainer');
            const countEl = document.getElementById('playlistCount');
            countEl.textContent = PLAYLIST_DATA.length;

            if (PLAYLIST_DATA.length === 0) {{
                container.innerHTML = `
                    <div class="empty-state">
                        <svg width="60" height="60" viewBox="0 0 24 24" fill="#333">
                            <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z"/>
                        </svg>
                        <h3>No Videos Available</h3>
                        <p>The playlist is empty. Wait for the next update.</p>
                    </div>`;
                return;
            }}

            filteredIndices = PLAYLIST_DATA.map((_, i) => i);
            renderPlaylist(filteredIndices);
        }}

        function renderPlaylist(indices) {{
            const container = document.getElementById('playlistContainer');

            if (indices.length === 0) {{
                container.innerHTML = '<div class="no-results">No matching videos found</div>';
                return;
            }}

            container.innerHTML = indices.map(i => {{
                const item = PLAYLIST_DATA[i];
                const isActive = i === currentIndex;
                const streamUrl = item.stream_url || '';
                let type = 'Video';
                if (streamUrl.includes('.m3u8')) type = 'HLS Stream';
                else if (streamUrl.includes('.mp4')) type = 'MP4';
                else if (streamUrl.includes('embed')) type = 'Embed';

                return `
                    <div class="playlist-item ${{isActive ? 'active' : ''}}"
                         onclick="playVideo(${{i}})" title="${{escapeHtml(item.title)}}">
                        <span class="index">${{i + 1}}</span>
                        <div class="info">
                            <div class="title">${{escapeHtml(item.title)}}</div>
                            <div class="type">${{type}}</div>
                        </div>
                        <span class="play-icon">${{isActive ? '🔊' : '▶'}}</span>
                    </div>`;
            }}).join('');
        }}

        function playVideo(index) {{
            if (index < 0 || index >= PLAYLIST_DATA.length) return;

            currentIndex = index;
            const item = PLAYLIST_DATA[index];
            const wrapper = document.getElementById('videoWrapper');
            const placeholder = document.getElementById('placeholder');
            const nowPlaying = document.getElementById('nowPlaying');
            const nowPlayingTitle = document.getElementById('nowPlayingTitle');

            // Destroy previous HLS instance
            if (hlsInstance) {{
                hlsInstance.destroy();
                hlsInstance = null;
            }}

            // Remove existing video/iframe
            const existingMedia = wrapper.querySelector('video, iframe');
            if (existingMedia) existingMedia.remove();
            if (placeholder) placeholder.style.display = 'none';

            const streamUrl = item.stream_url;

            if (streamUrl.includes('.m3u8')) {{
                // HLS Stream
                const video = document.createElement('video');
                video.controls = true;
                video.autoplay = true;
                video.id = 'videoPlayer';
                wrapper.appendChild(video);

                if (Hls.isSupported()) {{
                    hlsInstance = new Hls({{
                        xhrSetup: function(xhr) {{
                            xhr.setRequestHeader('Referer', item.page_url || '');
                        }}
                    }});
                    hlsInstance.loadSource(streamUrl);
                    hlsInstance.attachMedia(video);
                    hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {{
                        video.play().catch(() => {{}});
                    }});
                    hlsInstance.on(Hls.Events.ERROR, (event, data) => {{
                        if (data.fatal) {{
                            console.error('HLS Error:', data);
                            if (document.getElementById('autoplayToggle').checked) {{
                                setTimeout(() => playNext(), 3000);
                            }}
                        }}
                    }});
                }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
                    video.src = streamUrl;
                    video.play().catch(() => {{}});
                }}

                video.onended = () => {{
                    if (document.getElementById('autoplayToggle').checked) {{
                        playNext();
                    }}
                }};

            }} else if (streamUrl.includes('.mp4') || streamUrl.includes('.webm')) {{
                // Direct video file
                const video = document.createElement('video');
                video.controls = true;
                video.autoplay = true;
                video.id = 'videoPlayer';
                video.src = streamUrl;
                wrapper.appendChild(video);

                video.play().catch(() => {{}});
                video.onended = () => {{
                    if (document.getElementById('autoplayToggle').checked) {{
                        playNext();
                    }}
                }};
                video.onerror = () => {{
                    if (document.getElementById('autoplayToggle').checked) {{
                        setTimeout(() => playNext(), 3000);
                    }}
                }};

            }} else {{
                // Embed / iframe
                const iframe = document.createElement('iframe');
                iframe.src = streamUrl;
                iframe.allow = 'autoplay; encrypted-media; fullscreen';
                iframe.allowFullscreen = true;
                iframe.id = 'videoPlayer';
                wrapper.appendChild(iframe);
            }}

            // Update UI
            nowPlaying.style.display = 'block';
            nowPlayingTitle.textContent = item.title;
            document.title = `▶ ${{item.title}} - Stream Player`;

            // Update playlist highlighting
            renderPlaylist(filteredIndices);

            // Scroll active item into view
            setTimeout(() => {{
                const activeItem = document.querySelector('.playlist-item.active');
                if (activeItem) {{
                    activeItem.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                }}
            }}, 100);
        }}

        function playNext() {{
            if (currentIndex < PLAYLIST_DATA.length - 1) {{
                playVideo(currentIndex + 1);
            }} else {{
                playVideo(0); // Loop back
            }}
        }}

        function playPrevious() {{
            if (currentIndex > 0) {{
                playVideo(currentIndex - 1);
            }} else {{
                playVideo(PLAYLIST_DATA.length - 1);
            }}
        }}

        function shufflePlaylist() {{
            if (PLAYLIST_DATA.length === 0) return;
            let randomIndex;
            do {{
                randomIndex = Math.floor(Math.random() * PLAYLIST_DATA.length);
            }} while (randomIndex === currentIndex && PLAYLIST_DATA.length > 1);
            playVideo(randomIndex);
        }}

        function filterPlaylist() {{
            const query = document.getElementById('searchInput').value.toLowerCase().trim();

            if (!query) {{
                filteredIndices = PLAYLIST_DATA.map((_, i) => i);
            }} else {{
                filteredIndices = PLAYLIST_DATA
                    .map((item, i) => ({{ index: i, title: item.title.toLowerCase() }}))
                    .filter(item => item.title.includes(query))
                    .map(item => item.index);
            }}

            document.getElementById('playlistCount').textContent = filteredIndices.length;
            renderPlaylist(filteredIndices);
        }}

        function escapeHtml(text) {{
            const div = document.createElement('div');
            div.textContent = text || '';
            return div.innerHTML;
        }}

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {{
            if (e.target.tagName === 'INPUT') return;

            switch(e.key) {{
                case 'ArrowRight':
                case 'n':
                    e.preventDefault();
                    playNext();
                    break;
                case 'ArrowLeft':
                case 'p':
                    e.preventDefault();
                    playPrevious();
                    break;
                case 's':
                    e.preventDefault();
                    shufflePlaylist();
                    break;
                case ' ':
                    e.preventDefault();
                    const video = document.getElementById('videoPlayer');
                    if (video && video.tagName === 'VIDEO') {{
                        video.paused ? video.play() : video.pause();
                    }}
                    break;
            }}
        }});
    </script>
</body>
</html>'''

    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html_content)


if __name__ == '__main__':
    main()
