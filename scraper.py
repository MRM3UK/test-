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
MAX_PAGES = 10
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
            log.warning(f"HTTP {r.status_code} for {url}")
        except Exception as e:
            log.warning(f"Attempt {attempt+1} failed for {url}: {e}")
        time.sleep(DELAY * (attempt + 1))
    return None


def find_mp4_links(html, page_url):
    """
    Find all MP4 download/stream links from page HTML.
    Targets: server*.mmsbee*.xyz/uploads/myfiless/id/*.mp4
    Also catches any other .mp4 direct links.
    """
    found = set()

    # === REGEX: Find ALL .mp4 URLs in entire HTML source ===

    # Pattern 1: Standard URLs with .mp4
    for m in re.findall(r'(https?://[^\s"\'<>\\\)]+\.mp4[^\s"\'<>\\\)]*)', html, re.I):
        found.add(clean_url(m))

    # Pattern 2: Escaped slashes (common in JS)
    for m in re.findall(r'(https?:\\/\\/[^\s"\'<>]+\.mp4[^\s"\'<>]*)', html, re.I):
        found.add(clean_url(m.replace('\\/', '/')))

    # Pattern 3: Protocol-relative //server.domain/path.mp4
    for m in re.findall(r'(//[^\s"\'<>\\\)]+\.mp4[^\s"\'<>\\\)]*)', html, re.I):
        found.add(clean_url('https:' + m))

    # Pattern 4: URL-encoded
    for m in re.findall(r'(https?%3A%2F%2F[^\s"\'<>&]+\.mp4[^\s"\'<>&]*)', html, re.I):
        found.add(clean_url(unquote(m)))

    # Pattern 5: Specifically target mmsbee servers
    for m in re.findall(r'(https?://server\d+\.mmsbee\d*\.[a-z]+/[^\s"\'<>\\\)]+\.mp4)', html, re.I):
        found.add(clean_url(m))

    # Pattern 6: In href/src attributes specifically
    soup = BeautifulSoup(html, 'lxml')

    # Check all <a> tags
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if '.mp4' in href.lower():
            found.add(clean_url(urljoin(page_url, href)))

    # Check all <source> and <video> tags
    for tag in soup.find_all(['video', 'source']):
        for attr in ['src', 'data-src', 'data-lazy-src']:
            val = tag.get(attr, '').strip()
            if val and '.mp4' in val.lower():
                found.add(clean_url(urljoin(page_url, val)))

    # Check all elements with download-related attributes
    for tag in soup.find_all(True):
        for attr_name in ['href', 'src', 'data-src', 'data-url', 'data-file',
                          'data-video', 'data-mp4', 'content', 'value']:
            val = tag.get(attr_name, '')
            if isinstance(val, str) and '.mp4' in val.lower():
                found.add(clean_url(urljoin(page_url, val)))

    # Check inside <script> tags for JS variables
    for script in soup.find_all('script'):
        txt = script.string or ''
        if not txt:
            continue
        # var file_url = "https://...mp4"
        for m in re.findall(r'''["'](https?://[^"']+\.mp4[^"']*)["']''', txt, re.I):
            found.add(clean_url(m.replace('\\/', '/')))
        # Also unescaped
        for m in re.findall(r'''["'](https?:\\/\\/[^"']+\.mp4[^"']*)["']''', txt, re.I):
            found.add(clean_url(m.replace('\\/', '/')))

    # Remove empty strings
    found.discard('')
    return list(found)


def find_iframe_mp4(html, page_url, scraper, depth=0):
    """Follow iframes to find mp4 links inside them."""
    if depth > 3:
        return []

    results = []
    soup = BeautifulSoup(html, 'lxml')

    for iframe in soup.find_all('iframe'):
        src = iframe.get('src', '') or iframe.get('data-src', '') or iframe.get('data-lazy-src', '')
        if not src or src.startswith('about:') or src.startswith('javascript:'):
            continue

        iframe_url = urljoin(page_url, src.strip())

        # Skip ad iframes
        skip = ['google', 'facebook', 'twitter', 'doubleclick', 'adsense', 'disqus', 'syndication']
        if any(s in iframe_url.lower() for s in skip):
            continue

        # If iframe URL is directly an mp4
        if '.mp4' in iframe_url.lower():
            results.append(clean_url(iframe_url))
            continue

        log.info(f"  {'  '*depth}↳ iframe depth={depth+1}: {iframe_url[:80]}")
        time.sleep(1)
        iframe_html = fetch(scraper, iframe_url, ref=page_url, tries=2)
        if iframe_html:
            # Find mp4 in iframe content
            mp4s = find_mp4_links(iframe_html, iframe_url)
            results.extend(mp4s)
            # Recurse into nested iframes
            nested = find_iframe_mp4(iframe_html, iframe_url, scraper, depth + 1)
            results.extend(nested)

    return results


def pick_best_mp4(urls):
    """Pick the best MP4 URL from candidates."""
    if not urls:
        return None

    scored = []
    for url in urls:
        score = 0
        ul = url.lower()

        # Prefer mmsbee servers (these are the actual video hosts)
        if 'mmsbee' in ul:
            score += 100

        # Prefer /uploads/ path
        if '/uploads/' in ul:
            score += 50

        # Prefer /myfiless/ path
        if '/myfiless/' in ul:
            score += 50

        # Prefer longer numeric IDs (actual videos vs thumbnails)
        nums = re.findall(r'/(\d+)\.mp4', ul)
        if nums:
            score += 30

        # Penalize small files / thumbnails
        if 'thumb' in ul or 'preview' in ul or 'sample' in ul or 'trailer' in ul:
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


def get_listings(html, page_url):
    """Get all content links from a listing page."""
    soup = BeautifulSoup(html, 'lxml')
    items = []
    seen = set()

    base_domain = urlparse(BASE_URL).netloc.replace('www.', '')

    # Collect ALL links on the page
    for a in soup.find_all('a', href=True):
        href = urljoin(page_url, a['href'].strip())
        parsed = urlparse(href)

        # Must be same domain
        if base_domain not in parsed.netloc:
            continue

        # Skip utility pages
        path = parsed.path.lower().rstrip('/')
        skip = ['/page', '/category', '/tag', '/author', '/wp-admin', '/wp-content',
                '/wp-includes', '/feed', '/login', '/register', '/search',
                '/contact', '/about', '/privacy', '/terms', '/dmca']
        if any(path.startswith(s) or path == s for s in skip):
            continue
        if path in ('', '/'):
            continue

        # Skip file extensions
        if any(path.endswith(ext) for ext in ['.css', '.js', '.png', '.jpg', '.gif', '.svg', '.ico', '.xml']):
            continue

        if href in seen:
            continue
        seen.add(href)

        # Get title
        title = ''
        # From heading in parent article
        parent = a.find_parent(['article', 'div', 'li'])
        if parent:
            h = parent.find(['h1', 'h2', 'h3', 'h4'])
            if h:
                title = h.get_text(strip=True)
        if not title:
            title = a.get('title', '').strip()
        if not title:
            img = a.find('img')
            if img:
                title = (img.get('alt', '') or img.get('title', '')).strip()
        if not title:
            title = a.get_text(strip=True)
        if not title or len(title) < 3:
            # Use URL slug as title
            slug = parsed.path.strip('/').split('/')[-1]
            title = slug.replace('-', ' ').replace('_', ' ').title()

        title = ' '.join(title.split())[:200]

        # Get thumbnail
        thumb = ''
        if parent:
            img = parent.find('img')
            if img:
                thumb = img.get('src', '') or img.get('data-src', '') or img.get('data-lazy-src', '')
                if thumb:
                    thumb = urljoin(page_url, thumb)

        items.append({'page_url': href, 'title': title, 'thumbnail': thumb})

    return items


def main():
    scraper = make_scraper()
    all_entries = []

    log.info("=" * 60)
    log.info(f"Scraping {BASE_URL}")
    log.info("Looking for MP4 links (mmsbee servers)")
    log.info("=" * 60)

    for page_num in range(1, MAX_PAGES + 1):
        if page_num == 1:
            page_url = BASE_URL + "/"
        else:
            page_url = f"{BASE_URL}/page/{page_num}/"

        log.info(f"\n{'='*40}")
        log.info(f"📄 PAGE {page_num}: {page_url}")
        log.info(f"{'='*40}")

        html = fetch(scraper, page_url)
        if not html:
            log.warning(f"Cannot fetch page {page_num}, stopping.")
            break

        # Check if page itself has mp4 links (some sites show videos on listing)
        direct_mp4s = find_mp4_links(html, page_url)
        if direct_mp4s:
            log.info(f"Found {len(direct_mp4s)} MP4 links directly on listing page")

        listings = get_listings(html, page_url)
        log.info(f"Found {len(listings)} content links on page {page_num}")

        if not listings:
            log.info("No more listings, stopping.")
            break

        for idx, item in enumerate(listings):
            log.info(f"\n  [{idx+1}/{len(listings)}] {item['title'][:60]}")
            log.info(f"  URL: {item['page_url']}")

            time.sleep(DELAY)
            item_html = fetch(scraper, item['page_url'], ref=page_url)
            if not item_html:
                log.warning("  ❌ Could not fetch")
                continue

            # Step 1: Find MP4 links in the page
            mp4_links = find_mp4_links(item_html, item['page_url'])

            # Step 2: Follow iframes to find more MP4 links
            iframe_mp4s = find_iframe_mp4(item_html, item['page_url'], scraper)
            mp4_links.extend(iframe_mp4s)

            # Deduplicate
            mp4_links = list(set(filter(None, mp4_links)))

            if mp4_links:
                log.info(f"  Found {len(mp4_links)} MP4 link(s):")
                for u in mp4_links:
                    log.info(f"    📹 {u}")

                best = pick_best_mp4(mp4_links)
                if best:
                    all_entries.append({
                        'title': item['title'],
                        'stream_url': best,
                        'page_url': item['page_url'],
                        'thumbnail': item.get('thumbnail', '')
                    })
                    log.info(f"  ✅ ADDED: {best}")
            else:
                log.warning(f"  ❌ No MP4 found")

                # Debug: Show what we DID find
                soup = BeautifulSoup(item_html, 'lxml')
                iframes = soup.find_all('iframe')
                videos = soup.find_all('video')
                links = [a['href'] for a in soup.find_all('a', href=True) if 'download' in a.get_text(strip=True).lower() or 'download' in a.get('class', [])]
                log.info(f"    Debug: {len(iframes)} iframes, {len(videos)} video tags, {len(links)} download links")
                for iframe in iframes[:3]:
                    log.info(f"    iframe src: {iframe.get('src', 'none')[:100]}")
                for link in links[:3]:
                    log.info(f"    download link: {link[:100]}")

        time.sleep(DELAY)

    # ========== OUTPUT ==========
    log.info(f"\n{'='*60}")
    log.info(f"DONE! Total videos: {len(all_entries)}")
    log.info(f"{'='*60}")

    # Save M3U
    m3u_lines = ['#EXTM3U', '']
    for e in all_entries:
        m3u_lines.append(f'#EXTINF:-1 tvg-logo="{e.get("thumbnail", "")}",{e["title"]}')
        m3u_lines.append(f'#EXTVLCOPT:http-referrer={e["page_url"]}')
        m3u_lines.append(e['stream_url'])
        m3u_lines.append('')

    with open('playlist.m3u', 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u_lines))
    log.info("✅ playlist.m3u saved")

    # Save JSON
    with open('playlist.json', 'w', encoding='utf-8') as f:
        json.dump(all_entries, f, indent=2, ensure_ascii=False)
    log.info("✅ playlist.json saved")

    # Save HTML player
    write_html_player(all_entries)
    log.info("✅ index.html saved")

    # Print summary
    for i, e in enumerate(all_entries[:30]):
        log.info(f"  {i+1}. {e['title'][:50]} -> {e['stream_url']}")


def write_html_player(entries):
    """Write index.html with embedded player."""
    pjson = json.dumps(entries, ensure_ascii=False)
    ts = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stream Player - {len(entries)} Videos</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a0a;color:#e0e0e0;min-height:100vh}}
.hdr{{background:linear-gradient(135deg,#1a1a2e,#16213e);padding:14px 20px;text-align:center;border-bottom:2px solid #e94560}}
.hdr h1{{font-size:1.4rem;color:#e94560}}.hdr p{{color:#555;font-size:.75rem;margin-top:3px}}
.wrap{{display:flex;flex-direction:column;max-width:1400px;margin:0 auto}}
@media(min-width:992px){{.wrap{{flex-direction:row;height:calc(100vh - 65px)}}}}
.left{{flex:1;padding:15px;display:flex;flex-direction:column;min-width:0;overflow-y:auto}}
.vbox{{position:relative;width:100%;padding-bottom:56.25%;background:#000;border-radius:10px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.5);flex-shrink:0}}
.vbox video{{position:absolute;top:0;left:0;width:100%;height:100%}}
.ph{{position:absolute;top:0;left:0;width:100%;height:100%;display:flex;align-items:center;justify-content:center;flex-direction:column;color:#333}}
.ph svg{{width:60px;height:60px;fill:#222;margin-bottom:10px}}
.np{{margin-top:10px;padding:10px 14px;background:#1a1a2e;border-radius:8px;border-left:3px solid #e94560;display:none}}
.np small{{color:#e94560;text-transform:uppercase;letter-spacing:1px;font-size:.65rem}}
.np p{{color:#ddd;font-size:.9rem;margin-top:3px}}
.err{{color:#e94560;background:#1a0a0e;padding:10px;border-radius:6px;margin-top:8px;font-size:.8rem;display:none}}
.ctrl{{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.b{{padding:8px 14px;border:none;border-radius:6px;cursor:pointer;font-size:.8rem;font-weight:600;transition:all .2s;display:inline-flex;align-items:center;gap:4px;text-decoration:none}}
.bp{{background:#e94560;color:#fff}}.bp:hover{{background:#d13350}}
.bs{{background:#1a1a2e;color:#aaa;border:1px solid #333}}.bs:hover{{border-color:#e94560}}
.tg{{display:flex;align-items:center;gap:6px;margin-left:auto;font-size:.75rem;color:#666}}
.sw{{position:relative;width:34px;height:18px}}.sw input{{opacity:0;width:0;height:0}}
.sw span{{position:absolute;inset:0;background:#333;border-radius:18px;cursor:pointer;transition:.3s}}
.sw span:before{{content:"";position:absolute;height:12px;width:12px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.3s}}
.sw input:checked+span{{background:#e94560}}.sw input:checked+span:before{{transform:translateX(16px)}}
.right{{width:100%;background:#111;border-left:1px solid #1a1a1a;display:flex;flex-direction:column}}
@media(min-width:992px){{.right{{width:380px}}}}
.rh{{padding:10px 14px;background:#1a1a2e;border-bottom:1px solid #222;display:flex;justify-content:space-between;align-items:center}}
.rh h2{{font-size:.9rem;color:#e94560}}
.badge{{background:#e94560;color:#fff;padding:1px 8px;border-radius:10px;font-size:.7rem}}
.sb{{padding:8px 14px;border-bottom:1px solid #222}}
.sb input{{width:100%;padding:7px 10px;background:#1a1a2e;border:1px solid #333;border-radius:6px;color:#e0e0e0;font-size:.8rem;outline:none}}
.sb input:focus{{border-color:#e94560}}.sb input::placeholder{{color:#444}}
.ls{{flex:1;overflow-y:auto;padding:6px}}
.ls::-webkit-scrollbar{{width:4px}}.ls::-webkit-scrollbar-thumb{{background:#333;border-radius:2px}}
.li{{display:flex;align-items:center;padding:9px 10px;margin-bottom:2px;border-radius:6px;cursor:pointer;transition:background .15s;gap:8px}}
.li:hover{{background:#1a1a2e}}.li.a{{background:#16213e;border:1px solid #e94560}}
.li .n{{color:#444;font-size:.75rem;min-width:26px;text-align:center}}.li.a .n{{color:#e94560}}
.li .m{{flex:1;min-width:0}}.li .m .t{{font-size:.82rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#bbb}}
.li.a .m .t{{color:#fff}}.li .m .s{{font-size:.65rem;color:#444;margin-top:1px}}
.li .ic{{color:#444;font-size:.9rem}}.li.a .ic{{color:#e94560}}
.ft{{padding:6px;background:#0d0d0d;border-top:1px solid #1a1a1a;text-align:center;font-size:.65rem;color:#333}}
</style>
</head>
<body>
<div class="hdr">
<h1>🎬 Stream Player</h1>
<p>{len(entries)} videos • Updated: {ts}</p>
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
<div class="rh"><h2>📋 Playlist</h2><span class="badge" id="cn">{len(entries)}</span></div>
<div class="sb"><input id="q" placeholder="🔍 Search..." oninput="F()"></div>
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
if(!fi.length){{c.innerHTML='<div style="text-align:center;padding:30px;color:#444">No results</div>';return;}}
c.innerHTML=fi.map(i=>{{
const d=D[i],a=i===ci;
return`<div class="li${{a?' a':''}}" onclick="pl(${{i}})" title="${{X(d.title)}}">
<span class="n">${{i+1}}</span>
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
v.crossOrigin='anonymous';
v.src=d.stream_url;
w.appendChild(v);
v.play().catch(e=>{{console.log('Autoplay blocked:',e);}});
v.onended=()=>{{if(document.getElementById('ap').checked)N();}};
v.onerror=()=>{{
er.textContent='⚠ Playback error - trying next...';er.style.display='block';
if(document.getElementById('ap').checked)setTimeout(N,2000);
}};
R();
setTimeout(()=>{{const a=document.querySelector('.li.a');if(a)a.scrollIntoView({{behavior:'smooth',block:'center'}});}},100);
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
