"""Build experiments/index.html: a single page combining every generated
geocode map into one dropdown-selectable viewer.

Scans ``experiments/*/**/geocode_map.html`` at run time, so any new town
folder (e.g. ``experiments/<NewTown>/<subregion>/geocode_map.html``) is
picked up automatically the next time this script is run - no hardcoded
list of towns/subregions to maintain.

Usage:
    uv run experiments/build_index.py
"""

from __future__ import annotations

import html
from pathlib import Path

EXPERIMENTS_ROOT = Path(__file__).resolve().parent
INDEX_PATH = EXPERIMENTS_ROOT / "index.html"


def find_maps() -> dict[str, list[tuple[str, str]]]:
    """Return {town_name: [(subregion_name, relative_path), ...]}, sorted."""
    maps: dict[str, list[tuple[str, str]]] = {}
    for map_path in sorted(EXPERIMENTS_ROOT.glob("*/**/geocode_map.html")):
        town = map_path.relative_to(EXPERIMENTS_ROOT).parts[0]
        subregion_name = map_path.parent.name
        rel_path = map_path.relative_to(EXPERIMENTS_ROOT).as_posix()
        maps.setdefault(town, []).append((subregion_name, rel_path))
    return maps


def build_html(maps: dict[str, list[tuple[str, str]]]) -> str:
    """Build the HTML of the dropdown page that shows one geocode map at a time.

    Args:
        maps: Mapping from town name to ``(subregion_name, relative_path)`` pairs.

    Returns:
        The complete HTML document.
    """
    options = []
    first_path = None
    for town, subregions in maps.items():
        group = [f'  <optgroup label="{html.escape(town)}">']
        for subregion_name, rel_path in subregions:
            if first_path is None:
                first_path = rel_path
            group.append(
                f'    <option value="{html.escape(rel_path)}">{html.escape(subregion_name)}</option>'
            )
        group.append("  </optgroup>")
        options.append("\n".join(group))
    options_html = "\n".join(options)

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>OSM Address System — Experiments</title>
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; font-family: sans-serif; }}
  #bar {{
    position: fixed; top: 0; left: 50%; transform: translateX(-50%);
    z-index: 1000; background: white; padding: 8px 16px; border-radius: 0 0 8px 8px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.4); text-align: center;
  }}
  #bar label {{ font-size: 13px; margin-right: 8px; }}
  #bar select {{ font-size: 14px; padding: 4px 8px; }}
  #viewer {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%; border: none; }}
</style>
</head>
<body>
<div id="bar">
  <label for="map-select">Map:</label>
  <select id="map-select">
{options_html}
  </select>
</div>
<iframe id="viewer" src="{html.escape(first_path or "")}"></iframe>
<script>
  const select = document.getElementById("map-select");
  const viewer = document.getElementById("viewer");
  select.addEventListener("change", () => {{
    viewer.src = select.value;
  }});
</script>
</body>
</html>
"""


def main() -> None:
    """Scan for geocode maps and write ``index.html``."""
    maps = find_maps()
    if not maps:
        print(f"No geocode_map.html files found under {EXPERIMENTS_ROOT}")
        return
    INDEX_PATH.write_text(build_html(maps))
    n_maps = sum(len(v) for v in maps.values())
    print(f"Wrote {INDEX_PATH} with {n_maps} maps across {len(maps)} town(s)")


if __name__ == "__main__":
    main()
