"""Suggest static billing links from a previously authorized merchant's public homepage."""
import asyncio
from hashlib import sha256
import time
from urllib.parse import urljoin, urlsplit

import httpx
from playwright.async_api import Error as BrowserError

from .auth import read_json, write_private
from .catalog import BILLING_WORD, CUSTOM_PORTAL, UNRELATED, public_addresses, public_portal_origin
from .read_http import retry_seconds


def digest(value):
    return sha256(value.encode()).hexdigest()


def review_links(root, key, receipt):
    url = receipt.get('portal_url', '')
    if receipt.get('provider') != CUSTOM_PORTAL or not public_portal_origin(url):
        return []
    record = read_json(root/'portal_discovery'/'tickets'/digest(key), {})
    if record.get('source') != digest(url):
        return []
    return [link for link in record['links'] if public_portal_origin(link) == public_portal_origin(url)]


async def fetch_home(origin):
    addresses = await public_addresses(origin)
    if not addresses:
        return '', 0
    parts = urlsplit(origin)
    # Connect to the very IP that passed the public DNS check. TLS verifies the
    # merchant hostname, including SNI on shared hosting. No cookies or proxy.
    address = addresses[0]
    target = httpx.URL(origin + '/').copy_with(host=address)
    headers = {'Host': parts.netloc, 'Accept': 'text/html', 'Accept-Encoding': 'identity'}
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        async with client.stream('GET', target, headers=headers, extensions={'sni_hostname': parts.hostname}) as response:
            wait = retry_seconds(response.headers.get('retry-after'))
            if (response.status_code != 200 or wait or response.headers.get('content-encoding', 'identity') != 'identity'
                    or response.headers.get('content-type', '').split(';')[0].strip().lower() != 'text/html'):
                return '', wait
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 512 * 1024:
                    return '', 0
                body.extend(chunk)
            try:
                return body.decode(response.encoding or 'utf-8', errors='replace'), 0
            except LookupError:
                return body.decode('utf-8', errors='replace'), 0


async def visible_links(browser, html, origin):
    # Render the returned HTML with inline styles. Scripts, additional requests,
    # sign-ins, downloaded files and subsequent navigation are unnecessary.
    context = await browser.new_context(java_script_enabled=False, service_workers='block', offline=True,
                                        accept_downloads=False)
    try:
        await context.route('**/*', lambda route: route.abort())
        page = await context.new_page()
        await page.set_content(html, wait_until='domcontentloaded', timeout=3500)
        if await page.locator('input[type=password], iframe[src*="captcha" i], [class*="captcha" i]').count():
            return []
        base, anchors = await page.evaluate("""() => [
          document.querySelector('base[href]')?.getAttribute('href') || '',
          Array.from(document.querySelectorAll('a[href]')).slice(0, 1500)
            .filter(a => a.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})
                       && !a.closest('[hidden],[aria-hidden="true"],[inert]'))
            .map(a => [a.innerText.trim().slice(0,200), a.getAttribute('href')])]""")
        base = urljoin(origin + '/', base)
        links = []
        for label, href in anchors:
            if not BILLING_WORD.search(label) or UNRELATED.search(label) or not href or href.startswith(('#', '?')):
                continue
            try:
                candidate = urljoin(base, href)
                if public_portal_origin(candidate) != origin:
                    continue
                parts = urlsplit(candidate)
            except ValueError:
                continue
            if parts.query or parts.fragment or parts.path in {'', '/'} or UNRELATED.search(parts.path):
                continue
            if candidate not in links:
                links.append(candidate)
                if len(links) == 5:
                    break
        return links
    finally:
        await context.close()


async def review_missing_portal(page, response, url, receipt, challenge, store, key):
    origin = public_portal_origin(url)
    if (receipt.provider != CUSTOM_PORTAL or url != receipt.portal_url or not origin
            or response.status not in {404, 410}):
        return
    notice = f'El enlace confirmado respondió HTTP {response.status}.'
    if (public_portal_origin(response.url) != origin or public_portal_origin(page.url) != origin
            or urlsplit(url).path in {'', '/'}):
        raise ValueError(notice)
    try:
        snapshot = await page.locator('body').aria_snapshot(timeout=2500)
        if await challenge(page, snapshot):
            return
    except BrowserError:
        raise ValueError(notice) from None
    cache = store.root/'portal_discovery'
    site = cache/'sites'/digest(origin)
    cached = read_json(site, {})
    wait = retry_seconds(response.headers.get('retry-after'))
    if wait:
        write_private(site, {'until': max(cached.get('until', 0), time.time()+wait), 'links': []})
        write_private(cache/'tickets'/digest(key), {'source': digest(url), 'links': []})
        raise ValueError(notice + f' El comercio pidió esperar {wait} segundos.')
    if cached.get('until', 0) > time.time():
        links = cached['links']
    else:
        links = []
        try:
            html, wait = await asyncio.wait_for(fetch_home(origin), 8)
            if html and page.context.browser:
                links = await asyncio.wait_for(visible_links(page.context.browser, html, origin), 7)
        except (httpx.HTTPError, OSError, asyncio.TimeoutError, BrowserError, ValueError):
            pass
        write_private(site, {'until': time.time()+max(wait, 300), 'links': links})
    alternatives = [link for link in links if urlsplit(link).path != urlsplit(url).path]
    write_private(cache/'tickets'/digest(key), {'source': digest(url), 'links': alternatives})
    if alternatives:
        notice += ' La portada del comercio ofrece otro enlace de facturación. Revísalo en Editar datos → Leer texto, QR y barras.'
    else:
        notice += ' Revisa los enlaces impresos del original o solicita el CFDI al comercio.'
    raise ValueError(notice)
