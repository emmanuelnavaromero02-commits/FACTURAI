"""Allow a leased viewer to use a merchant's own embedded verification widget."""
import asyncio
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from playwright.async_api import Error as BrowserError

from .catalog import public_portal_origin


def widget_origin(url):
    try:
        parts = urlsplit(url)
        origin = public_portal_origin(parts._replace(query='', fragment='').geturl())
    except (TypeError, ValueError):
        return None
    if not origin or parts.port not in (None, 443):
        return None
    host = parts.hostname
    if host in {'www.google.com', 'recaptcha.google.com', 'www.recaptcha.net', 'recaptcha.net'}:
        return origin if parts.path.startswith('/recaptcha/') else None
    if host == 'challenges.cloudflare.com':
        return origin if parts.path.startswith('/cdn-cgi/challenge-platform/') else None
    if host == 'hcaptcha.com' or host.endswith('.hcaptcha.com'):
        return origin if parts.path.startswith('/captcha/') else None
    return None


class WidgetTransport:
    def __init__(self):
        self.sessions = WeakKeyDictionary()
        self.lock = asyncio.Lock()

    async def arm(self, frame):
        origin = widget_origin(frame.url)
        if not origin or not frame.parent_frame:
            return
        async with self.lock:
            if frame in self.sessions:
                return
            session = None
            try:
                session = await frame.page.context.new_cdp_session(frame)

                async def response(event):
                    # Chrome follows HTTP redirects after Playwright's request route.
                    # Stop them before the network stack can forward a widget POST.
                    request_id = event['requestId']
                    try:
                        if event['request']['method'] == 'POST' and 300 <= event.get('responseStatusCode', 0) < 400:
                            await session.send('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'BlockedByClient'})
                        else:
                            await session.send('Fetch.continueRequest', {'requestId': request_id})
                    except BrowserError:
                        pass  # The operator may have closed the frame.

                session.on('Fetch.requestPaused', response)
                session.on('close', lambda *_: self.sessions.pop(frame, None))
                await session.send('Fetch.enable', {'patterns': [
                    {'urlPattern': origin+'/*', 'resourceType': kind, 'requestStage': 'Response'} for kind in ('XHR', 'Fetch')
                ]})
                self.sessions[frame] = origin
            except BrowserError:
                if session:
                    try:
                        await session.detach()
                    except BrowserError:
                        pass

    async def allows(self, runner, request, frame, owner, merchant_origin):
        origin = widget_origin(frame.url)
        if (request.method != 'POST' or request.resource_type not in {'xhr', 'fetch'}
                or request.is_navigation_request() or request.redirected_from is not None
                or not origin or self.sessions.get(frame) != origin or widget_origin(request.url) != origin
                or origin == merchant_origin or public_portal_origin(owner.url) != merchant_origin):
            return False
        parent = frame.parent_frame
        while parent is not None and parent is not owner.main_frame:
            if widget_origin(parent.url) != origin and public_portal_origin(parent.url) != merchant_origin:
                return False
            parent = parent.parent_frame
        if parent is None:
            return False
        pending = runner.pending.get(runner.active_id)

        def human_has_page():
            return (pending and runner.pending.get(runner.active_id) is pending and pending['kind'] == 'captcha'
                    and pending.get('challenge_seen') and not pending['future'].done() and pending['page'] is owner
                    and runner.page_job.get(owner) == runner.active_id and runner.screen is runner.viewer
                    and runner.viewer.page is owner and runner.viewer.manual and bool(runner.viewer.channel)
                    and runner.viewer.control_epoch != pending.get('control_epoch') and owner.url == pending['url'])

        if not human_has_page():
            return False
        try:
            element = await frame.frame_element()
            if not await element.is_visible() or await request.header_value('origin') != origin:
                return False
        except BrowserError:
            return False
        return bool(human_has_page() and self.sessions.get(frame) == origin and widget_origin(frame.url) == origin)
