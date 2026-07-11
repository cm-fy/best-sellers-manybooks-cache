#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch ManyBooks category listings and cache them as JSON for the Best-Sellers
calibre plugin.

ManyBooks category pages (https://manybooks.net/categories/<CODE>) are served
behind a Cloudflare challenge that blocks scripted clients, so we mirror the
server-rendered book lists into static JSON files served via GitHub Pages.  The
plugin's ManyBooks category sources point at these cached files.

The scraper is designed to run from a machine/browser session that can pass the
Cloudflare check (e.g. a real browser via Playwright, or a warmed cookie jar).
When run in CI without a usable session it records an empty array per list so the
plugin can fall back to direct fetching.

Output structure:
    mb/<slug>.json            — one file per category (e.g. mb/non.json)
    mb/<slug>_downloads.json  — category sorted by downloads (e.g. mb/non_downloads.json)
    mb/meta.json              — metadata about all cached lists

Book schema (matches the plugin's _manybooks_entry output):
    rank, title, authors, cover_url, source_url, manybooks_download_url,
    manybooks_format
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

try:
    from urllib.parse import urljoin
except ImportError:
    from urlparse import urljoin

HOST = 'manybooks.net'
BASE_URL = 'https://' + HOST + '/'

# (slug, label, category_code) — codes from https://manybooks.net/categories
CATEGORIES = [
    ('adv', 'Adventure', 'ADV'),
    ('afr', 'African-American Studies', 'AFR'),
    ('art', 'Art', 'ART'),
    ('ban', 'Banned Books', 'BAN'),
    ('bio', 'Biography', 'BIO'),
    ('bus', 'Business', 'BUS'),
    ('can', 'Canadian Literature', 'CAN'),
    ('cla', 'Classic', 'CLA'),
    ('com', 'Computers', 'COM'),
    ('coo', 'Cooking', 'COO'),
    ('cor', 'Correspondence', 'COR'),
    ('ccl', 'Creative Commons', 'CCL'),
    ('cri', 'Criticism', 'CRI'),
    ('dra', 'Drama', 'DRA'),
    ('spy', 'Espionage', 'SPY'),
    ('ess', 'Essays', 'ESS'),
    ('ett', 'Etiquette', 'ETT'),
    ('fan', 'Fantasy', 'FAN'),
    ('fic', 'Fiction and Literature', 'FIC'),
    ('gam', 'Games', 'GAM'),
    ('gay', 'Gay/Lesbian', 'GAY'),
    ('gho', 'Ghost Stories', 'GHO'),
    ('got', 'Gothic', 'GOT'),
    ('gov', 'Government Publication', 'GOV'),
    ('har', 'Harvard Classics', 'HAR'),
    ('hea', 'Health', 'HEA'),
    ('his', 'History', 'HIS'),
    ('hor', 'Horror', 'HOR'),
    ('hum', 'Humor', 'HUM'),
    ('ins', 'Instructional', 'INS'),
    ('lan', 'Language', 'LAN'),
    ('mus', 'Music', 'MUS'),
    ('mys', 'Mystery/Detective', 'MYS'),
    ('myt', 'Myth', 'MYT'),
    ('nat', 'Nature', 'NAT'),
    ('nau', 'Nautical', 'NAU'),
    ('non', 'Non-fiction', 'NON'),
    ('occ', 'Occult', 'OCC'),
    ('per', 'Periodical', 'PER'),
    ('phi', 'Philosophy', 'PHI'),
    ('pir', 'Pirate Tales', 'PIR'),
    ('poe', 'Poetry', 'POE'),
    ('pol', 'Politics', 'POL'),
    ('mod', 'Post-1930', 'MOD'),
    ('psy', 'Psychology', 'PSY'),
    ('pul', 'Pulp', 'PUL'),
    ('ran', 'Random Selection', 'RAN'),
    ('ref', 'Reference', 'REF'),
    ('rel', 'Religion', 'REL'),
    ('rom', 'Romance', 'ROM'),
    ('sat', 'Satire', 'SAT'),
    ('sci', 'Science', 'SCI'),
    ('sfc', 'Science Fiction', 'SFC'),
    ('sex', 'Sexuality', 'sex'),
    ('sst', 'Short Story', 'SST'),
    ('sho', 'Short Story Collection', 'SHO'),
    ('thr', 'Thriller', 'THR'),
    ('tra', 'Travel', 'TRA'),
    ('war', 'War', 'WAR'),
    ('wes', 'Western', 'WES'),
    ('wom', "Women's Studies", 'WOM'),
    ('chi', 'Young Readers', 'CHI'),
]

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
}

# Optional: a warmed cookie string exported from a browser session that passed
# the Cloudflare challenge (set via MB_COOKIE env var or mb_cookie.txt).
COOKIE = os.environ.get('MB_COOKIE', '')


def _decode_html(text):
    try:
        from html import unescape
        return unescape(text or '')
    except Exception:
        return text or ''


def _session():
    """Return an HTTP session. Prefers Playwright when available so the
    Cloudflare challenge can be solved; otherwise falls back to requests with an
    optional cookie."""
    try:
        from playwright.sync_api import sync_playwright
        return ('playwright', sync_playwright())
    except Exception:
        import requests
        s = requests.Session()
        s.headers.update(HEADERS)
        if COOKIE:
            s.headers['Cookie'] = COOKIE
        return ('requests', s)


def _fetch_html(target, url):
    kind, sess = target
    if kind == 'playwright':
        with sess as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=HEADERS['User-Agent'])
            page = ctx.new_page()
            page.goto(url, timeout=60000, wait_until='domcontentloaded')
            # Let any client-side rendering settle.
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
            return html
    else:
        import requests
        r = sess.get(url, timeout=60)
        r.raise_for_status()
        return r.text


def _extract_books(html, source_url, cover_dir='', cover_base_url=''):
    """Parse book cards from a ManyBooks category page.

    When ``cover_dir`` is provided, cover images are downloaded into that local
    directory and ``cover_url`` is rewritten to ``cover_base_url`` so the cached
    JSON references the GitHub Pages mirror instead of the Cloudflare-protected
    ManyBooks CDN (which blocks scripted clients).
    """
    try:
        from lxml import html as lxml_html
        root = lxml_html.fromstring((html or '').encode('utf-8'))
    except Exception:
        return []
    out = []
    nodes = root.xpath(
        '//div[contains(concat(" ", normalize-space(@class), " "), " views-row ")]'
        '//article[contains(concat(" ", normalize-space(@class), " "), " book ")]')
    if not nodes:
        nodes = root.xpath('//article[contains(concat(" ", normalize-space(@class), " "), " book ")]')
    for node in nodes:
        # Prefer the title inside .content (the hover duplicate is ignored).
        title_node = node.xpath(
            './/div[contains(concat(" ", normalize-space(@class), " "), " content ")]'
            '//div[contains(concat(" ", normalize-space(@class), " "), " field--name-field-title ")]'
            '//a/text()')
        if not title_node:
            title_node = node.xpath(
                './/div[contains(concat(" ", normalize-space(@class), " "), " field--name-field-title ")]'
                '//a/text()')
        if not title_node:
            title_node = node.xpath('.//a[@hreflang]/text()')
        # De-duplicate repeated text nodes (e.g. cover alt + title link).
        seen = []
        for t in title_node:
            t = t.strip()
            if t and t not in seen:
                seen.append(t)
        title = ' '.join(seen).strip()
        hrefs = node.xpath('.//a[contains(@href, "/titles/")]/@href')
        book_url = hrefs[0] if hrefs else ''
        if book_url and not book_url.startswith('http'):
            book_url = urljoin(BASE_URL, book_url)
        authors = ' / '.join([a.strip() for a in node.xpath(
            './/div[contains(concat(" ", normalize-space(@class), " "), " field--name-field-author-er ")]'
            '//a/text()') if a.strip()])
        covers = node.xpath(
            './/div[contains(concat(" ", normalize-space(@class), " "), " field--name-field-cover ")]'
            '//img/@src')
        cover_url = covers[0] if covers else ''
        if cover_url and not cover_url.startswith('http'):
            cover_url = urljoin(BASE_URL, cover_url)
        # Cache the cover locally when a cover directory is configured.
        local_cover = ''
        if cover_url and cover_dir:
            local_cover = _cache_cover(cover_url, title, cover_dir, cover_base_url)
        if local_cover:
            cover_url = local_cover
        epubs = node.xpath('.//a[contains(@href, ".epub")]/@href')
        download_url = ''
        for e in epubs:
            if 'sites/default/files' in e or e.lower().startswith('http'):
                download_url = e
                break
        if not download_url and epubs:
            download_url = epubs[0]
        if download_url and not download_url.startswith('http'):
            download_url = urljoin(BASE_URL, download_url)
        if title and book_url:
            out.append({
                'rank': str(len(out) + 1),
                'title': title,
                'authors': authors or '—',
                'cover_url': cover_url or '',
                'source_url': book_url,
                'manybooks_download_url': download_url or '',
                'manybooks_format': 'EPUB',
            })
    return out


def _cache_cover(cover_url, title, cover_dir, cover_base_url):
    """Download a cover image into ``cover_dir`` and return the mirrored URL.

    Returns the original URL on failure so the plugin can still attempt a direct
    fetch (which may succeed from a real browser session).
    """
    try:
        import requests
        from urllib.parse import urlparse
        os.makedirs(cover_dir, exist_ok=True)
        # Derive a stable filename from the URL path.
        path = urlparse(cover_url).path
        fname = re.sub(r'[^A-Za-z0-9._-]', '_', path.split('/')[-1] or 'cover.jpg')
        if not fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
            fname += '.jpg'
        dest = os.path.join(cover_dir, fname)
        if not os.path.exists(dest):
            r = requests.get(cover_url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            with open(dest, 'wb') as f:
                f.write(r.content)
        return (cover_base_url.rstrip('/') + '/' + fname) if cover_base_url else cover_url
    except Exception:
        return cover_url


def fetch_category(slug, code, sort_by_downloads=False, cover_dir='', cover_base_url=''):
    url = BASE_URL + 'categories/' + code
    if sort_by_downloads:
        url += '?sort_by=field_downloads'
    try:
        target = _session()
        html = _fetch_html(target, url)
        books = _extract_books(html, url, cover_dir=cover_dir, cover_base_url=cover_base_url)
        return books, None
    except Exception as e:
        return [], str(e)


def main():
    out_dir = 'mb'
    os.makedirs(out_dir, exist_ok=True)
    # Mirror covers into the repo so the plugin fetches them from GitHub Pages
    # (not the Cloudflare-protected ManyBooks CDN).  The base URL is rewritten at
    # deploy time via the PAGES_BASE_URL env var; default to the canonical repo.
    cover_dir = os.path.join(out_dir, 'covers')
    pages_base = os.environ.get(
        'PAGES_BASE_URL',
        'https://cfmazur.github.io/best-sellers-manybooks-cache')
    cover_base_url = pages_base.rstrip('/') + '/mb/covers'
    meta = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': 'https://manybooks.net/categories',
        'lists': [],
    }
    for slug, label, code in CATEGORIES:
        books, err = fetch_category(slug, code, sort_by_downloads=False,
                                    cover_dir=cover_dir, cover_base_url=cover_base_url)
        path = os.path.join(out_dir, slug + '.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(books, f, indent=2, ensure_ascii=False)
        meta['lists'].append({
            'slug': slug, 'label': label, 'code': code,
            'count': len(books), 'status': 'ok' if not err else 'error',
            'error': err or '',
        })
        # Non-fiction also cached sorted by downloads.
        if code == 'NON':
            books_d, err_d = fetch_category(slug, code, sort_by_downloads=True,
                                            cover_dir=cover_dir, cover_base_url=cover_base_url)
            path_d = os.path.join(out_dir, slug + '_downloads.json')
            with open(path_d, 'w', encoding='utf-8') as f:
                json.dump(books_d, f, indent=2, ensure_ascii=False)
            meta['lists'].append({
                'slug': slug + '_downloads', 'label': label + ' (by downloads)',
                'code': code, 'count': len(books_d),
                'status': 'ok' if not err_d else 'error', 'error': err_d or '',
            })
        print('{}: {} books{}'.format(slug, len(books), ('  ERR: ' + err) if err else ''))
        time.sleep(1)
    with open(os.path.join(out_dir, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print('Wrote meta.json with {} lists'.format(len(meta['lists'])))


if __name__ == '__main__':
    main()
