"""
Tools for finding, resolving, and normalizing sphinx sites.
"""
from __future__ import annotations
import contextlib
import re
from typing import Optional
from urllib.parse import urlparse

from flask import current_app
import httpx

from . import http


# From https://stackoverflow.com/questions/6038061/regular-expression-to-find-urls-within-a-string
URL_PATTERN = re.compile(r'(?:(?:https?|ftp|file):\/\/|www\.|ftp\.)(?:\([-A-Z0-9+&@#\/%=~_|$?!:,.]*\)|[-A-Z0-9+&@#\/%=~_|$?!:,.])*(?:\([-A-Z0-9+&@#\/%=~_|$?!:,.]*\)|[A-Z0-9+&@#\/%=~_|$])', re.I)  # noqa: E501


class RtdClient:
    """
    Read The Docs client
    """
    def __init__(self, *, client: httpx.Client):
        self.client = client

    @property
    def token(self):
        return current_app.config['RTD_TOKEN']

    def canonical_url(self, slug) -> str:
        """Get the URL of the canonical docs of the given project.

        Uses V3 if there is a token configured. V2 otherwise.
        """
        if self.token:
            return self.canonical_url_v3(slug)
        else:
            return self.canonical_url_v2(slug)

    def canonical_url_v2(self, slug):
        resp = self.client.get("https://readthedocs.org/api/v2/project/", params={'slug': slug})
        if not resp.is_success:
            # This can happen if the project declared a URL that doesn't actually exist.
            return
        data = resp.json()
        if data['count']:
            # Just grab the first result
            proj = data['results'][0]
            resp = self.client.get(f"https://readthedocs.org/api/v2/project/{proj['id']}/")
            resp.raise_for_status()
            data = resp.json()
            return data['canonical_url']

    def canonical_url_v3(self, slug):
        resp = self.client.get(
            f"https://readthedocs.org/api/v3/projects/{slug}/",
            params={'expand': 'active_versions'},
            headers={'Authorization': f'Token {self.token}'},
        )
        if not resp.is_success:
            # This can happen if the project declared a URL that doesn't actually exist.
            return
        data = resp.json()
        for version in data['active_versions']:
            if version['slug'] == data['default_version']:
                return version['urls']['documentation']

    def iter_projects(self):
        """Generates all of the projects.

        If by "all" we mean "all the user is a member of".

        Requires a token.
        """
        assert self.token
        # Yeah, we could also implement a v2 version, but using a token feels
        # nicer to the RTD version
        cur_url = "https://readthedocs.org/api/v3/projects/"
        while cur_url:
            resp = self.client.get(
                cur_url,
                headers={'Authorization': f'Token {self.token}'},
            )
            resp.raise_for_status()
            data = resp.json()
            yield from data['results']

            cur_url = data['next']


# FIXME: cap size
_CACHED_RESOLUTIONS = {}


class Guesser(contextlib.ExitStack):
    """Utility to handle all of the blindly poking at things to see if we can
    find a sphinx inventory.
    """
    def __enter__(self):
        super().__enter__()
        self.client = self.enter_context(http.client())
        return self

    def resolve(self, url) -> Optional[httpx.URL]:
        """Resolve a URL--follow redirects, check for existance, etc.
        """
        # Some funky things I've seen:
        # * UNKNOWN

        # Pretty handy bit of debugging, keep this arround
        # print(f"resolve {url=}")

        if url not in _CACHED_RESOLUTIONS:
            _CACHED_RESOLUTIONS[url] = self._real_resolve(url)
        return _CACHED_RESOLUTIONS[url]

    def _real_resolve(self, url):
        try:
            resp = self.client.head(url, follow_redirects=True)
        except httpx.HTTPError:
            pass
        except httpx.InvalidURL:
            pass
        except ValueError:
            pass
        else:
            if resp.is_success:
                return resp.url

    def rtd_slug(self, url: str) -> Optional[str]:
        """Given a URL, get its RTD slug

        Currently, just a string operation.
        """
        bits = urlparse(str(url))
        if not bits.hostname:
            return
        if bits.hostname.endswith('.readthedocs.io') or bits.hostname.endswith('.rtfd.io'):
            slug, _, _ = bits.hostname.partition('.')
            return slug

    def guess_url(self, url):
        """Given a URL, guess at a few possible locations for a sphinx
        inventory.
        """
        if not url or ':' not in str(url):
            return

        # Does it look like a Read The Docs site?
        if slug := self.rtd_slug(url):
            # Yes, so let's just ask RTD instead of probing blindly
            rtd = RtdClient(client=self.client)
            rtd_url = rtd.canonical_url(slug)
            if rtd_url:
                yield httpx.URL(rtd_url).join('objects.inv')
                return

        # TODO: Can we do the same thing for sites with custom domains?

        # Join then check for redirects
        try:
            url = self.resolve(httpx.URL(url).join('objects.inv'))
        except Exception:
            pass
        else:
            if url:
                yield url

        # Check for redirects and then join
        try:
            url = self.resolve(url)
            if url:
                url = self.resolve(url.join('objects.inv'))
        except Exception:
            pass
        else:
            if url:
                yield url

    def check_for_inventory(self, url):
        """Checks if the given URL is actually a sphinx inventory.
        """
        # print(f"check_for_inventory {url=}")
        try:
            with self.client.stream('GET', url, follow_redirects=True) as resp:
                if not resp.is_success:
                    return False
                chunk = next(resp.iter_bytes())
                return chunk.startswith(b'# Sphinx')
        # The rest of the system should filter out errors, but occassionally shit happens
        except httpx.HTTPError:
            return False
        except httpx.InvalidURL:
            return False

    def perform_guessing(self, roots):
        """Given a collection of discovered URLs, process them into a set of
        inventory URLs.
        """
        yield from (
            u
            for urls in map(self.guess_url, roots)
            for u in urls
            if self.check_for_inventory(u)
        )

    def guess_for_pypi(self, pkg: str):
        """Given a PyPI package name, guess at possible URLs.

        They are returned in "importance" order--earlier items are more
        prominent/declared than later ones.
        """
        resp = self.client.get(f"https://pypi.org/pypi/{pkg}/json", follow_redirects=True)
        if not resp.is_success:
            # Not actually a package:
            return
        data = resp.json()

        if data['info']['docs_url'] and self.resolve(data['info']['docs_url']):
            yield data['info']['docs_url']

        if data['info']['project_urls']:
            yield from (
                u
                for u in data['info']['project_urls'].values()
                if self.resolve(u)
            )

            # Rummage through the README
            # TODO: Only do this if the above don't work?
            for m in URL_PATTERN.finditer(data['info']['description']):
                yield m.group(0)
