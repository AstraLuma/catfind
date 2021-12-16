#!/usr/bin/python3
import functools
import re

import click
import httpx


# Caching for the duration of this CLI call.
@functools.cache
def resolve(url):
    try:
        resp = httpx.head(url, follow_redirects=True)
    except httpx.ReadTimeout:
        pass
    except httpx.ConnectError:
        pass
    else:
        if resp.is_success:
            return resp.url


@functools.cache
def guess_url(url):
    if not url:
        return
    # TODO: Does it look like an RTD site?

    # Join then check for redirects
    url1 = resolve(httpx.URL(url).join('objects.inv'))

    # Check for redirects and then join
    url2 = resolve(url)
    if url2:
        url2 = resolve(url2.join('objects.inv'))

    if url1:
        yield url1

    if url2:
        yield url2


def check_for_inventory(url):
    with httpx.stream('GET', url, follow_redirects=True) as resp:
        if not resp.is_success:
            return False
        chunk = next(resp.iter_bytes())
        return chunk.startswith(b'# Sphinx')


# From https://stackoverflow.com/questions/6038061/regular-expression-to-find-urls-within-a-string
URL_PATTERN = re.compile('(?:(?:https?|ftp|file):\/\/|www\.|ftp\.)(?:\([-A-Z0-9+&@#\/%=~_|$?!:,.]*\)|[-A-Z0-9+&@#\/%=~_|$?!:,.])*(?:\([-A-Z0-9+&@#\/%=~_|$?!:,.]*\)|[A-Z0-9+&@#\/%=~_|$])', re.I)


@click.group()
def guess():
    pass


def perform_guessing(roots):
    return {
        u
        for urls in map(guess_url, roots)
        for u in urls
        if check_for_inventory(u)
    }


@guess.command()
@click.argument("pkg")
def pypi(pkg):
    """
    Given a PyPI package name, guess its object inventory
    """
    resp = httpx.get(f"https://pypi.org/pypi/{pkg}/json", follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()

    # Check the declared project URLs
    roots = []
    if data['info']['project_urls']:
        roots += [u for u in data['info']['project_urls'].values() if resolve(u)]

    if data['info']['docs_url'] and resolve(data['info']['docs_url']):
        roots.append(data['info']['docs_url'])

    roots = set(roots)

    urls = perform_guessing(roots)

    # Rummage through the README
    if not urls:
        roots = []
        for m in URL_PATTERN.finditer(data['info']['description']):
            roots.append(m.group(0))

        urls |= perform_guessing(roots)

    for u in urls:
        print(u)


if __name__ == '__main__':
    guess()