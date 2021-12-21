from datetime import datetime, timezone, timedelta
import importlib.resources
import itertools
import json
import logging
import random

import click
from flask import Flask, redirect, make_response, request, render_template_string
from pony.flask import Pony
from pony import orm
from pony.orm import select

from .discovery import Guesser
from .inventory import Inventory
from .pypi_simple import PyPISimple

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_object('catfind.config')

db = orm.Database()


def render_template(pkg, resource, **context):
    txt = importlib.resources.read_text(pkg, resource)
    return render_template_string(txt, **context)


# Current schema management: lol

# Under PostgreSQL (Jamie's SQL of choice), there is no difference between
# VARCHAR and TEXT, so don't worry about length limits

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
    first_seen = orm.Required(datetime, sql_default='CURRENT_TIMESTAMP')

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
    first_seen = orm.Required(datetime, sql_default='CURRENT_TIMESTAMP')

    # Some source metadata, to help with re-discovering URLs later
    pypi_pkg_name = orm.Optional(str)  # This is basically sins, see guess_pypi()
    rtd_slug = orm.Optional(str)

    # TODO: Indexing metadata to clean up dead projects


db.bind(**app.config['PONY'])
db.generate_mapping(create_tables=True)
Pony(app)


@app.route("/")
def homepage():
    return render_template(__name__, 'homepage.html')


if app.config['DEBUG']:
    @app.route("/!projects")
    def projects():
        return {'projects': [
            {
                'url': proj.inv_url,
                'name': proj.name,
                'version': proj.version,
                'last_indexed':
                    None if proj.last_indexed is None else proj.last_indexed.isoformat(),
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
        if accepted is None:
            # Probably a previewer or something
            accepted = 'text/html'
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
                logger.info("Adding initial inventory %r", url)
                Project(inv_url=url)
            # Let the scheduled task load the initial data


@app.cli.command('index')
@click.argument("url")
@orm.db_session
def index(url):
    """
    Index the given URL
    """
    now = datetime.now(timezone.utc)
    logger.info("Downloading %s", url)
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

    orm.commit()

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
        orm.commit()

    proj.last_indexed = now
    orm.commit()

    # At this point, any Entry with an old last_updated was not seen this pass,
    # so clean it up.
    orm.delete(e for e in proj.entries if e.last_indexed < now)
    orm.commit()


def dyna_shuffle(seq: list, weights: list, k: int):
    """Kinda like random.shuffle(seq)[:k], but with weights.

    Modifies both seq and weight in-place.
    """
    for _ in range(k):
        if not seq:
            break
        (i, value), = random.choices(list(enumerate(seq)), weights)
        yield value
        del seq[i]
        del weights[i]


@app.cli.command('auto-index')
@click.option('-n', '--number', type=int, default=1, help="Number of projects to process")
@click.pass_context
def auto_index(ctx, number):
    """
    Automatically select & index one project.

    Won't index a project more than once a day, and will prefer older projects
    """
    with orm.db_session():
        now = datetime.now(timezone.utc)
        index_before = now - timedelta(days=1)

        projs = list(select(
            p for p in Project if not p.last_indexed or p.last_indexed <= index_before
        ))

    if not projs:
        # No projects
        return

    def time_since(val):
        if val is None:
            return 1e6  # idk, a lot i guess
        else:
            return (now - val.replace(tzinfo=timezone.utc)).total_seconds()

    if len(projs) > number:
        weights = [time_since(p.last_indexed) for p in projs]
        projs = dyna_shuffle(projs, weights, number)

    for proj in projs:
        print(f"Updating {proj.name} ({proj.inv_url})")
        ctx.invoke(index, url=proj.inv_url)


def unique(seq):
    """Filters a sequence, producing each item just once.
    """
    seen = set()
    for item in seq:
        if item not in seen:
            seen.add(item)
            yield item


@app.cli.command('guess-pypi')
@click.argument("pkg")
@click.option('--add/--no-add', default=False, help="Automatically add to the app")
@click.option('--all/--no-all', default=False, help="Keep checking after the first is found")
@click.option('--quiet/--no-quiet', default=False, help="Be quiet")
def guess_pypi(pkg, add, all, quiet):
    """
    Given a PyPI package name, guess its object inventory
    """
    with Guesser() as guesser:
        # Using iterator chaining here for the unique() states.
        # Basically, make sure unique(guesser.perform_guessing()) applies to
        # all roots.
        for url in unique(guesser.perform_guessing(unique(itertools.chain(
            # First, just _try_ the name as an RTD site. This is so common it's
            # got at least a 50/50 of working.
            [f"https://{pkg}.readthedocs.io/"],
            # Ok, dig deeper.
            guesser.guess_for_pypi(pkg),
        )))):
            if not quiet:
                click.echo(url)
            if add:
                with orm.db_session():
                    if not Project.get(inv_url=str(url)):
                        Project(
                            inv_url=str(url),
                            # Too noisy, can't positively identify that a given
                            # link is for this package specifically.
                            # Does act as an audit log, though, so we'll keep it
                            # for now.
                            pypi_pkg_name=pkg,
                            rtd_slug=guesser.rtd_slug(url) or '',
                        )
            # Since this in priority order, skip others
            if not all:
                break


@app.cli.command('guess-rtd')
@click.argument("slug")
@click.option('--add/--no-add', default=False, help="Automatically add to the app")
@click.option('--quiet/--no-quiet', default=False, help="Be quiet")
def guess_rtd(slug, add, quiet):
    """
    Given a Read The Docs slug, guess its object inventory
    """
    with Guesser() as guesser:
        for url in unique(guesser.perform_guessing([f"https://{slug}.readthedocs.io/"])):
            if not quiet:
                click.echo(url)
            if add:
                with orm.db_session():
                    if not Project.get(inv_url=str(url)):
                        Project(inv_url=str(url), rtd_slug=slug)


@app.cli.command('crawl-pypi')
@click.pass_context
def crawl_pypi(ctx):
    """
    Attempt to index all of PyPI
    """
    click.echo("Loading package list...")
    with PyPISimple() as pypi:
        names = list(pypi.stream_project_names())

    # Shuffle the names so that repeated failures don't keep retreading over the
    # same packages.
    # Unfortunately means we have to preload the entire package list.
    random.shuffle(names)

    # Not too worried about rate limiting against PyPI, since this process is
    #   S   L   O   W
    with click.progressbar(names, item_show_func=lambda a: a) as bar:
        for name in bar:
            # TODO: Is there a way to do this that's nice to progress bar?
            ctx.invoke(guess_pypi, pkg=name, add=True, quiet=True)
