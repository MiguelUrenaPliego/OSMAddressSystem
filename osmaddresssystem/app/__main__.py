"""Command line: ``python -m osmaddresssystem.app init | setup | serve``.

* ``init --project DIR --name NAME [--streets-osm F] [--area F] [--points F] [--buildings F] [--street-edits F] [--edge-edits F]
  [--network DIR]`` creates the project folder and database and copies the files in.
* ``setup --project DIR [--force]`` builds ``network/`` from ``input/streets.osm`` (and ``input/area.gpkg``).
* ``serve --project DIR [--host 127.0.0.1] [--port 8000] [--db URL]`` starts the app (one worker).
* ``backup --project DIR --to FILE`` copies the SQLite database.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from .store import Store


def main(argv: list[str] | None = None) -> None:
    """Entry point.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).
    """
    ap = argparse.ArgumentParser(prog="python -m osmaddresssystem.app", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="create a project folder")
    i.add_argument("--project", type=Path, required=True)
    i.add_argument("--name", default=None)
    for opt, dst in (("--streets-osm", "streets.osm"), ("--area", "area.gpkg"), ("--points", "points.gpkg"), ("--buildings", "buildings.gpkg"),
                     ("--street-edits", "street_edits.csv"), ("--edge-edits", "edge_edits.csv")):
        i.add_argument(opt, type=Path, help=f"copied to input/{dst}")
    i.add_argument("--default-reference", help="LAT,LON,NAME of the default reference point (the editors confirm or move it)")
    i.add_argument("--network", type=Path, help="folder with nodes.gpkg, edges.gpkg, streets.csv to use instead of building them")
    s = sub.add_parser("setup", help="build network/ from input/")
    s.add_argument("--project", type=Path, required=True)
    s.add_argument("--force", action="store_true")
    v = sub.add_parser("serve", help="start the app")
    v.add_argument("--project", type=Path, default=os.environ.get("ADDRESS_PROJECT"), required=not os.environ.get("ADDRESS_PROJECT"), help="project folder (default: $ADDRESS_PROJECT)")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    v.add_argument("--db", default=None, help="database URL (default: project.db, or $ADDRESS_DB_URL), e.g. postgresql+psycopg://user:pw@host/db")
    for name, hlp in (("backup", "copy the SQLite database"),):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--project", type=Path, required=True)
        p.add_argument("--db", default=None)
        p.add_argument("--to", type=Path, required=True)
    a = ap.parse_args(argv)

    if a.cmd == "init":
        root = a.project
        (root / "input").mkdir(parents=True, exist_ok=True)
        for opt, dst in (("streets_osm", "streets.osm"), ("area", "area.gpkg"), ("points", "points.gpkg"), ("buildings", "buildings.gpkg"),
                         ("street_edits", "street_edits.csv"), ("edge_edits", "edge_edits.csv")):
            if getattr(a, opt):
                shutil.copyfile(getattr(a, opt), root / "input" / dst)
        if a.default_reference:
            la, lo, *nm = a.default_reference.split(",", 2)
            (root / "input" / "default_reference.json").write_text(__import__("json").dumps({"name": nm[0] if nm else "reference", "lat": float(la), "lon": float(lo)}), encoding="utf-8")
        if a.network:
            (root / "network").mkdir(exist_ok=True)
            for f in ("nodes.gpkg", "edges.gpkg", "streets.csv"):
                shutil.copyfile(a.network / f, root / "network" / f)
        Store(root, a.name)
        print("project created in", root)
    elif a.cmd == "setup":
        from .setup import build_network

        print("network built" if build_network(a.project, a.force) else "network already exists (use --force to rebuild)")
    elif a.cmd == "backup":
        print("backup written to", Store(a.project, url=a.db).backup(a.to))
    else:
        import uvicorn

        from .server import create_app

        uvicorn.run(create_app(a.project, db_url=a.db), host=a.host, port=a.port)


if __name__ == "__main__":
    main()
