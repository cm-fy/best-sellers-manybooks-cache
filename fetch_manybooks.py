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
    optional cookie.

    Playwright's Sync API can raise at start time in some environments (e.g.
    "Sync API inside asyncio loop"), so we actually start it here and fall back
    to requests on ANY failure — not just on import error.
    """
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        # Smoke-test that the browser can launch; if not, fall back.
        browser = pw.chromium.launch(headless=True)
        browser.close()
        return ('playwright', pw)
    except Exception as e:
        print('Playwright unavailable ({}), falling back to requests.'.format(e))
        import requests
        s = requests.Session()
        s.headers.update(HEADERS)
        if COOKIE:
            s.headers['Cookie'] = COOKIE
        return ('requests', s)


def _fetch_html(target, url):
    kind, sess = target
    if kind == 'playwright':
        # sess is an already-started sync_playwright() instance.
        browser = sess.chromium.launch(headless=True)
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
        # Prefer the title inside .content, but exclude the hover duplicate
        # (book-hover-content) which otherwise repeats the same title text.
        title_node = node.xpath(
            './/div[contains(concat(" ", normalize-space(@class), " "), " content ")]'
            '//div[contains(concat(" ", normalize-space(@class), " "), " field--name-field-title ")]'
            '[not(ancestor::div[contains(concat(" ", normalize-space(@class), " "), " book-hover-content ")])]'
            '//a/text()')
        if not title_node:
            title_node = node.xpath(
                './/div[contains(concat(" ", normalize-space(@class), " "), " field--name-field-title ")]'
                '[not(ancestor::div[contains(concat(" ", normalize-space(@class), " "), " book-hover-content ")])]'
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
        # Collapse any residual whole-title repetition (e.g. "X X").
        _half = len(title) // 2
        if _half and title[:_half].strip() and title[:_half].strip() == title[_half:].strip():
            title = title[:_half].strip()
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


def _extract_detail(html):
    """Parse a ManyBooks book detail page (https://manybooks.net/titles/…) for
    the enrichment fields the plugin surfaces on its cards.

    Returns a dict with: blurb, excerpt, published (year as int or ''),
    pages (int or ''), downloads (int or '').  Any field not found is left
    empty so callers can fall back gracefully.
    """
    try:
        from lxml import html as lxml_html
        root = lxml_html.fromstring((html or '').encode('utf-8'))
    except Exception:
        return {'blurb': '', 'excerpt': '', 'published': '', 'pages': '', 'downloads': ''}

    def _field_text(field_class):
        # The field block's own element carries both field--name-<x> and
        # field--item classes, so the value is the node's own text_content()
        # (there is no separate descendant field--item element).
        nodes = root.xpath(
            '//div[contains(concat(" ", normalize-space(@class), " "), " field--name-{0} ")]'.format(field_class))
        if not nodes:
            return ''
        txt = ' '.join((nodes[0].text_content() or '').split())
        return txt.strip()

    def _int(text):
        digits = re.sub(r'[^\d]', '', text or '')
        return int(digits) if digits else ''

    blurb = _field_text('field-description')
    excerpt = _field_text('field-excerpt')
    published = _int(_field_text('field-published-year'))
    pages = _int(_field_text('field-pages'))
    downloads = _int(_field_text('field-downloads'))
    return {
        'blurb': blurb,
        'excerpt': excerpt,
        'published': published,
        'pages': pages,
        'downloads': downloads,
    }


def fetch_book_details(books, playwright_ctx=None, delay_range=(2.0, 5.0), max_books=None):
    """Enrich each book dict with detail-page fields (blurb, excerpt, published,
    pages, downloads).

    This is intentionally OFF by default and only invoked when MB_DETAILS=1 is
    set, because it opens every book's detail page — a large number of requests
    that would otherwise look like a scraping/DoS burst to ManyBooks' Cloudflare
    front-end.  When run, it is deliberately polite:

      * reuses a single Playwright browser context (one browser launch, not one
        per book);
      * waits for the page to settle and honours a randomised inter-request
        delay (``delay_range``) so traffic is human-paced;
      * caps the number of detail pages per run via ``max_books`` so a single
        job can never crawl the entire catalogue in one sitting;
      * never raises — failures simply leave the field empty and move on.

    Uses the **Async API** (not Sync) because the Sync API raises
    "Sync API inside asyncio loop" in some CI environments.  ``playwright_ctx``
    lets the caller pass an already-open async context; otherwise one is created
    and torn down here.  A private event loop is used so the function works both
    when called from sync ``main()`` and from an existing asyncio context.
    """
    if not books:
        return books
    # Cap how many detail pages we CRAWL, but never truncate the returned list
    # (the caller writes it to cache — truncating would lose books).  We crawl
    # at most ``max_books`` and leave the rest untouched.
    crawl_books = books if max_books is None else books[:max_books]

    import asyncio
    import random
    import threading
    from playwright.async_api import async_playwright

    async def _run(ctx):
        for book in crawl_books:
            src = book.get('source_url', '')
            if not src:
                continue
            try:
                page = await ctx.new_page()
                await page.goto(src, timeout=60000, wait_until='domcontentloaded')
                await page.wait_for_timeout(2500)
                detail = _extract_detail(await page.content())
                for k, v in detail.items():
                    if v not in (None, ''):
                        book[k] = v
                await page.close()
            except Exception:
                # Leave fields empty on failure; never abort the whole run.
                pass
            # Randomised, human-paced delay between detail requests.
            lo, hi = delay_range
            await asyncio.sleep(lo + (hi - lo) * random.random())
        return books

    async def _run_owned():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=HEADERS['User-Agent'])
            try:
                return await _run(ctx)
            finally:
                await browser.close()

    def _run_in_thread():
        # Run the async Playwright work in a brand-new thread with its own
        # isolated event loop.  This sidesteps the "Sync API inside asyncio
        # loop" error that some CI runners raise when an event loop is already
        # running in the host process (Playwright's async init detects it).
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(_run_owned())
        finally:
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass
            loop.close()

    try:
        if playwright_ctx is not None:
            # Caller owns the context; run in an isolated thread too.
            def _run_owned_ctx():
                loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(loop)
                    return loop.run_until_complete(_run(playwright_ctx))
                finally:
                    try:
                        asyncio.set_event_loop(None)
                    except Exception:
                        pass
                    loop.close()
            _run_owned_ctx()
            return books
        else:
            _run_in_thread()
            return books
    except Exception:
        return books


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
        'https://cm-fy.github.io/best-sellers-manybooks-cache')
    cover_base_url = pages_base.rstrip('/') + '/mb/covers'
    meta = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': 'https://manybooks.net/categories',
        'lists': [],
    }
    # Opt-in detail enrichment.  Disabled by default so the weekly CI job only
    # mirrors the category lists (cheap, one request per category).  Set
    # MB_DETAILS=1 to also crawl each book's detail page for blurb/excerpt/
    # published/pages/downloads.  This is deliberately throttled (see
    # fetch_book_details) and should be run from a warmed browser session, never
    # on every scheduled run, to avoid tripping ManyBooks' anti-scraping defences.
    enrich_details = os.environ.get('MB_DETAILS', '') in ('1', 'true', 'yes')
    if enrich_details:
        print('Detail enrichment ENABLED (MB_DETAILS=1) — throttled, Playwright Async API.')

    for slug, label, code in CATEGORIES:
        books, err = fetch_category(slug, code, sort_by_downloads=False,
                                    cover_dir=cover_dir, cover_base_url=cover_base_url)
        if enrich_details and books:
            # Cap detail crawls per category so a single run stays polite.
            max_details = int(os.environ.get('MB_DETAILS_MAX', '50'))
            books = fetch_book_details(books, max_books=max_details)
        path = os.path.join(out_dir, slug + '.json')
        # Defensive: never overwrite a previously-populated cache file with an
        # empty result (e.g. when Cloudflare blocks the fetch or Playwright
        # fails).  Keep the last good data so the plugin never sees a wiped list.
        if not books and os.path.exists(path) and os.path.getsize(path) > 2:
            try:
                with open(path, encoding='utf-8') as _f:
                    if json.load(_f):
                        print('{}: fetch returned 0 books (ERR: {}) — KEEPING previous cache'.format(
                            slug, err or 'unknown'))
                        books = json.load(open(path, encoding='utf-8'))
            except Exception:
                pass
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
            if enrich_details and books_d:
                books_d = fetch_book_details(books_d, max_books=max_details)
            path_d = os.path.join(out_dir, slug + '_downloads.json')
            # Same defensive guard for the downloads-sorted file.
            if not books_d and os.path.exists(path_d) and os.path.getsize(path_d) > 2:
                try:
                    with open(path_d, encoding='utf-8') as _f:
                        if json.load(_f):
                            books_d = json.load(open(path_d, encoding='utf-8'))
                except Exception:
                    pass
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
