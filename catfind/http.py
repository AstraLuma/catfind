import contextlib

from flask import current_app
import httpx


@contextlib.contextmanager
def client():
    ua = current_app.config['USER_AGENT']
    with httpx.Client(headers={'User-Agent': ua}) as client:
        yield client
