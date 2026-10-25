"""Rank of every important point of Fotouni for the street signs (revised by hand).

``sign_rank`` says how important a reference point is for the street signs: **1** a landmark everybody knows (shown on the signs of the
big intersections too), **2** an important place, **3** a local place (only on the signs of the nearest intersection), **0** not on the signs
(names of villages that are not destinations). The rules below give a first value, ``MANUAL`` overrides single points by name.
The result is ``inputs/point_sign_ranks.csv`` (one row per point, in the order of ``points.gpkg``); edit the CSV or the rules and run
``make_app_project.py`` again. In the app the rank of a point can be changed in the Addresses tab.

Run::

    uv run experiments/Fotouni/point_ranks.py
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
IN = HERE / "inputs"

MANUAL = {                                              # revised by hand: name -> (rank, why)
    "Carrefour Brouwer (hôtel)": (1, "the reference crossroads of the town"),
    "Fotouni": (1, "the town"),
    "Chefferie Fotouni": (1, "the chiefdom of the town"),
    "Bandja": (1, "the town the road leads to"),
    "Foyer culturel Tchisso": (2, "cultural centre"),
    "Place des rois": (2, "the main square of the chiefs"),
    "Lieu sacré Sie Santa'a": (3, "sacred place, local"),
    "Ecole publique Balatchie": (3, "school of a quarter"),
    "Ecole Publique de Batié Chefferie": (3, "a school (listed as a chiefdom in OSM)"),
}


def rank_of(row: pd.Series) -> tuple[int, str]:
    """First value of the sign rank of a point from its name and kind, before the manual revision.

    Args:
        row: A row of ``points.gpkg`` (``name``, ``kind``).

    Returns:
        ``(rank, why)``.
    """
    name, kind = str(row["name"]), str(row["kind"])
    low = name.lower()
    if name in MANUAL:
        return MANUAL[name]
    if kind == "Healthcare centre":
        return 1, "health centre"
    if kind == "Fotouni town centre":
        return 1, "town centre"
    if kind == "town":
        return 1, "town"
    if kind == "School":
        return (2, "secondary school") if re.search(r"lyc[ée]e", low) else (3, "school")
    if kind == "Chiefferie":
        return (3, "school") if "ecole" in low or "école" in low else (2, "chiefdom")
    if kind == "intersection":
        return 2, "named crossroads"
    if kind == "square":
        return 3, "square"
    if kind == "facility":
        return (2, "chiefdom of a quarter") if "chefferie" in low else (3, "meeting place / local facility")
    if kind == "village":
        return 2, "village (a destination)"
    if kind == "hamlet":
        return 3, "hamlet"
    return 3, "other"


def build() -> pd.DataFrame:
    """Sign rank of every point of ``inputs/places_community.csv`` + ``places_osm.csv`` + ``pois_osm.json`` in the order of ``points.gpkg``.

    Returns:
        ``index, name, kind, sign_rank, why``.
    """
    import geopandas as gpd

    pts = gpd.read_file(HERE.parents[1] / "projects" / "fotouni" / "input" / "points.gpkg")
    rows = []
    for i, r in pts.iterrows():
        rank, why = rank_of(r)
        rows.append({"index": i, "name": r["name"], "kind": r["kind"], "sign_rank": rank, "why": why})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    df = build()
    df.to_csv(IN / "point_sign_ranks.csv", index=False)
    print(df.groupby("sign_rank").size().to_string())
