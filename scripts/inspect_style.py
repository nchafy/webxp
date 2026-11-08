#!/usr/bin/env python3
"""Dump a basemap style's layers, so a repaint targets ids that exist.

Two things this answers that guessing does not: which layer ids carry colour (liberty's are
not positron's, so a palette copied between them silently does nothing), and which layers
are `fill-extrusion` — the 3D buildings, which have to be gated on camera pitch.

    python3 scripts/inspect_style.py liberty            # grouped summary
    python3 scripts/inspect_style.py liberty --json out.json
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from collections import defaultdict

BASE = "https://tiles.openfreemap.org/styles/"

#: Paint keys that carry a colour worth overriding.
COLOUR_KEYS = (
    "background-color",
    "fill-color",
    "fill-outline-color",
    "line-color",
    "fill-extrusion-color",
)


def fetch(name: str) -> dict:
    # A bare urllib request gets 403: the host filters on User-Agent. The browser is
    # unaffected, which is why the map loads and this script did not.
    request = urllib.request.Request(
        BASE + name,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh) 30daymapchallenge-style-inspector"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def summarise(name: str, dump: str | None) -> None:
    style = fetch(name)
    layers = style.get("layers", [])
    if dump:
        with open(dump, "w", encoding="utf-8") as handle:
            json.dump(style, handle, indent=2)
        print(f"{name}: {len(layers)} layers -> {dump}")

    by_type: dict[str, list] = defaultdict(list)
    for layer in layers:
        by_type[layer["type"]].append(layer)

    print(f"\n=== {name}: {len(layers)} layers ===")
    print("by type: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(by_type.items())))

    extrusions = by_type.get("fill-extrusion", [])
    print(f"\n-- fill-extrusion ({len(extrusions)}) : the 3D layers --")
    for layer in extrusions:
        print(
            f"  {layer['id']:<22} src={layer.get('source-layer')} "
            f"minzoom={layer.get('minzoom', '-')}"
        )
        for key, value in (layer.get("paint") or {}).items():
            print(f"      {key}: {json.dumps(value)[:110]}")

    print("\n-- layers carrying a colour --")
    for layer in layers:
        paint = layer.get("paint") or {}
        colours = {k: v for k, v in paint.items() if k in COLOUR_KEYS}
        if not colours:
            continue
        shown = "  ".join(f"{k.split('-')[-2] if False else k}={json.dumps(v)[:46]}"
                          for k, v in colours.items())
        print(f"  {layer['id']:<30} {layer['type']:<14} {shown}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("styles", nargs="*", default=["liberty"])
    parser.add_argument("--json", dest="dump")
    args = parser.parse_args()
    for style_name in args.styles or ["liberty"]:
        summarise(style_name, args.dump)
