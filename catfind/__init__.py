from datetime import datetime, timezone, timedelta
import importlib.resources
import json
import logging
import random

import click
from flask import Flask, redirect, make_response, request, render_template_string
from pony.flask import Pony
from pony import orm
from pony.orm import select

from .inventory import Inventory

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_object('catfind.default_config')

db = orm.Database()


def render_template(pkg, resource, **context):
    txt = importlib.resources.read_text(pkg, resource)
    return render_template_string(txt, **context)


# Current schema management: append only, never change columns.
# (TODO: Better schema management)

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

    # TODO: first_seen

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

    # TODO: pypi_name
    # TODO: rtd_name


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


@app.before_first_request
def load_initial_indexes():
    for url in app.config['INITIAL_INVENTORIES']:
        with orm.db_session():
            proj = Project.get(inv_url=url)

        if proj is None:
            index([url])


@app.cli.command('index')
@click.argument("url")
@orm.db_session
def index(url):
    """
    Index the given URL
    """
    now = datetime.now(timezone.utc)
    logger.info("Downloading %s", url)
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
    logger.info("Found project %r", proj)

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
def auto_index():
    """
    Automatically select & index one project.

    Won't index a project more than once a day, and will prefer older projects
    """
    with orm.db_session():
        now = datetime.now(timezone.utc)
        index_before = now - timedelta(days=1)
        projs = select(p for p in Project if not p.last_indexed or p.last_indexed <= index_before)[:]
        weights = [(now - p.last_indexed.replace(tzinfo=timezone.utc)).total_seconds() for p in projs]

        if not projs:
            # No projects
            return

        proj, = random.choices(projs, weights=weights)

    print(f"Updating {proj.name} ({proj.inv_url})")
    index([proj.inv_url])
