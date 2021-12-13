from datetime import datetime, timezone, timedelta
import importlib.resources
import json
import random
import re

import click
from flask import Flask, redirect, make_response, request, render_template_string
import httpx
from pony.flask import Pony
from pony import orm
from pony.orm import select
from werkzeug.exceptions import NotAcceptable

from .inventory import Inventory

app = Flask(__name__)
app.config.from_object('catfind.default_config')

db = orm.Database()


def render_template(pkg, resource, **context):
    txt = importlib.resources.read_text(pkg, resource)
    return render_template_string(txt, **context)


class Entry(db.Entity):
    id = orm.PrimaryKey(int, auto=True)
    domain = orm.Required(str)
    name = orm.Required(str)
    dispname = orm.Required(str)
    role = orm.Required(str)
    url = orm.Required(str)
    project = orm.Required('Project')
    last_indexed = orm.Required(datetime)
    orm.composite_index(domain, name)  # Primary lookup

    @property
    def kind(self):
        return f"{self.domain}:{self.role}"

    @property
    def display_name(self):
        return self.name if self.dispname == '-' else self.dispname

    def __str__(self):
        return self.display_name


class Project(db.Entity):
    id = orm.PrimaryKey(int, auto=True)
    inv_url = orm.Required(str, unique=True)
    name = orm.Optional(str)
    last_indexed = orm.Optional(datetime)
    version = orm.Optional(str)
    entries = orm.Set(Entry)


db.bind(**app.config['PONY'])
db.generate_mapping(create_tables=True)
Pony(app)


@app.route("/!projects")
def projects():
    return {'projects': [
        {
            'url': proj.inv_url,
            'name': proj.name,
            'version': proj.version,
            'last_indexed': None if proj.last_indexed is None else proj.last_indexed.isoformat(),
        }
        for proj in select(i for i in Project)
    ]}


@app.route("/<domain>/<path:name>")
def lookup(domain, name):
    if domain == '*':
        entries = select(e for e in Entry if e.name == name)[:]
    else:
        entries = select(e for e in Entry if e.name == name and e.domain == domain)[:]

    if len(entries) == 0:
        return "Nothing found", 404
    elif len(entries) == 1:
        e, = entries
        return redirect(e.url, code=303)
    else:
        accepted = request.accept_mimetypes.best_match(LIST_TYPES.keys())
        resp = LIST_TYPES[accepted](entries)
        resp.headers['Content-Type'] = accepted
        return resp


def list_plaintext(entries):
    resp = make_response(
        "\n".join(f"{e.project.name}: {e.kind}: {e.url}" for e in entries),
        300,
    )
    return resp


def list_html(entries):
    resp = make_response(
        render_template('catfind', 'multientry.html', entries=entries),
        300,
    )
    return resp


def list_json(entries):
    data = [
        {
            'name': e.name,
            'type': e.kind,
            'location': e.url,
            'dispname': e.display_name,
        }
        for e in entries
    ]
    resp = make_response(
        json.dumps(data),
        300,
    )
    return resp


LIST_TYPES = {
    'text/plain': list_plaintext,
    'text/html': list_html,
    'application/json': list_json,
}


@app.cli.command('index')
@click.argument("url")
@orm.db_session
def index(url):
    """
    Index the given URL
    """
    now = datetime.now(timezone.utc)
    # FIXME: break this up into several smaller transactions
    inv = Inventory.load_uri(url)

    proj = Project.get(inv_url=inv.uri)
    if proj is None:
        proj = Project.get(inv_url=url)
        if proj is None:
            proj = Project(inv_url=inv.uri)
        else:
            # Was redirected
            proj.inv_url = inv.uri

    proj.name = inv.projname
    proj.version = inv.version

    for item in inv:
        domain, role = item.domain_role
        ent = Entry.get(project=proj, domain=domain, role=role, name=item.name)
        if ent is None:
            ent = Entry(
                project=proj, domain=domain, role=role, name=item.name,
                url=item.location, dispname=item.dispname, last_indexed=now)
        else:
            ent.url = item.location
            ent.last_indexed = now
            ent.dispname = item.dispname

    proj.last_indexed = now

    # TODO: Clean up old entries


@app.cli.command('auto-index')
@orm.db_session
def auto_index():
    """
    Automatically select & index one project.

    Won't index a project more than once a day, and will prefer older projects
    """
    now = datetime.now(timezone.utc)
    index_before = now - timedelta(days=1)
    projs = select(p for p in projects if not p.last_indexed or p.last_indexed <= index_before)[:]
    weights = [(now - p.last_indexed).total_seconds() for p in projs]

    if not projs:
        return

    proj, = random.choices(projs, weights=weights)

    index(proj.inv_url)


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


def guess_url(url):
    if not url:
        return
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
    resp = httpx.get(url, follow_redirects=True)
    if not resp.is_success:
        return False
    chunk = next(resp.iter_bytes())
    return chunk.startswith(b'# Sphinx')


# From https://stackoverflow.com/questions/6038061/regular-expression-to-find-urls-within-a-string
URL_PATTERN = re.compile('(?:(?:https?|ftp|file):\/\/|www\.|ftp\.)(?:\([-A-Z0-9+&@#\/%=~_|$?!:,.]*\)|[-A-Z0-9+&@#\/%=~_|$?!:,.])*(?:\([-A-Z0-9+&@#\/%=~_|$?!:,.]*\)|[A-Z0-9+&@#\/%=~_|$])', re.I)


@app.cli.command('pypi-guess')
@click.argument("pkg")
@orm.db_session
def pypi_guess(pkg):
    """
    Given a PyPI package name, guess its object inventory
    """
    resp = httpx.get(f"https://pypi.org/pypi/{pkg}/json", follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()

    roots = []
    if data['info']['project_urls']:
        roots += [u for u in data['info']['project_urls'].values() if resolve(u)]

    if data['info']['docs_url'] and resolve(data['info']['docs_url']):
        roots.append(data['info']['docs_url'])

    # Find any URLs in the README
    for m in URL_PATTERN.finditer(data['info']['description']):
        roots.append(m.group(0))

    urls = {
        u
        for urls in map(guess_url, roots)
        for u in urls
        if check_for_inventory(u)
    }

    if urls:
        print("Found URLs:")
        for u in urls:
            print(f"* {u!s}")
    else:
        print("No URLs found")
