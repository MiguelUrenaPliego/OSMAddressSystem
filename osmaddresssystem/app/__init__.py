"""Community address app: assign street ids, street names and house addresses on a map.

The app is a small local web server (FastAPI) with a Leaflet front end:

* :mod:`project` - a project folder with the base network, the chosen reference node, the settings, the street names
  given so far, a change history and saved versions (save / load / import / export),
* :mod:`engine` - the compute pipeline (always *path numbering*): plan which streets still need a name, route to the
  reference through named streets, re-assign (and cut) street ids by distance, derive the names of sub-streets
  (``3-xyz``; ``3-#512`` while ``xyz`` has no name yet), then house numbers, full addresses and street signs,
* :mod:`server` - the HTTP API and the static front end (three tabs: IDs, Names, Addresses).

Run ``python -m osmaddresssystem.app serve --project <folder>``; create a project with ``... init``.
"""
