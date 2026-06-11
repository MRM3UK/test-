# 🎬 Auto Stream Playlist Generator

Automatically scrapes video content and generates an M3U playlist with a built-in web player.

## Features

- 🔄 Auto-updates every 6 hours via GitHub Actions
- 📋 Generates standard M3U playlist file
- 🌐 Built-in HTML5 web player with HLS support
- 🔍 Search & filter videos
- ▶️ Autoplay with next/previous navigation
- ⌨️ Keyboard shortcuts (← → Space N P S)
- 📱 Fully responsive design

## Usage

### Watch Online
Visit the GitHub Pages URL for your repo:
`https://<username>.github.io/<repo-name>/`

### Download Playlist
Download `playlist.m3u` and open with any media player:
- VLC Media Player
- IPTV apps
- Any M3U compatible player

### Manual Trigger
Go to **Actions** tab → **Generate M3U Playlist** → **Run workflow**

## Setup

1. Fork this repository
2. Enable **GitHub Pages** (Settings → Pages → Source: `main` branch)
3. Enable **GitHub Actions** (Actions tab → Enable workflows)
4. The workflow will auto-run, or trigger manually

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `→` or `N` | Next video |
| `←` or `P` | Previous video |
| `S` | Shuffle |
| `Space` | Play/Pause |

## Files

| File | Description |
|------|-------------|
| `playlist.m3u` | Generated M3U playlist |
| `playlist.json` | JSON playlist data |
| `index.html` | Web player interface |
| `scraper.py` | Content scraper script |
