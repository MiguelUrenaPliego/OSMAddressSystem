"""Use, residents, name and services of every surveyed building of Fotouni, from the EFYL survey export.

The survey is free text in French, English and local spellings, so the classification is a rule table plus **manual corrections**
(``NAME_FIXES``, ``PRODUCTS``, ``CATEGORY_SERVICE``): edit them and run this script again.

Output ``inputs/building_attributes.csv`` (one row per ``building_id`` of ``inputs/survey_buildings.csv``):

| column | meaning |
|---|---|
| ``use`` | ``residential``, ``office``, ``commercial``, ``industrial``, ``public`` (public building) or ``unknown`` (address taken, no questionnaire) |
| ``residents`` | number of people living there (household questionnaire; empty otherwise) |
| ``name`` | name of the shop / institution / site (corrected spelling), empty for houses |
| ``service`` | what a non-residential building offers, in English (``; `` separated), empty for houses |
| ``workers`` | people working there when the survey asked |
| ``review`` | what to check by hand (unclear text, very large households) |
| ``azimuth`` | direction (degrees from north) the surveyor pointed from the street to the door, when recorded: tells on which side of the road the building is |

Run::

    uv run experiments/Fotouni/building_attributes.py
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
IN = HERE / "inputs"

# ------------------------------------------------------------------------------------------------ manual corrections
NAME_FIXES = {
    "boutque de jean": "Boutique de Jean", "chex nicolas kengni": "Chez Nicolas Kengni", "boutique systèm": "Boutique Système",
    "ecole publque de bamendjo-fotouni": "École publique de Bamendjo-Fotouni", "école publique de. bagueka": "École publique de Bagueka",
    "provenderi": "Provenderie", "provenderi moderne la différence": "Provenderie moderne la différence",
    "eglise catholique . paroisse de kwogue": "Église catholique, paroisse de Kwogue", "inconnu": "", "lieu de commerce": "",
    "hotel zogang derrière l'église catholique": "Hôtel Zogang (derrière l'église catholique)", "chefferie supérieur fotouni": "Chefferie supérieure de Fotouni",
    "ngankam jule": "Ngankam Jule",
}
"""Spelling corrections of names, keyed by the lower-case text as entered. An empty value means the entry was not a name."""

PRODUCTS = {  # product / activity chosen in the questionnaire (or typed) -> (English service, use)
    "alimentation générale": ("general food store", "commercial"), "boissons": ("drinks", "commercial"), "boisson": ("drinks", "commercial"),
    "restauration / plats cuisinés": ("restaurant / prepared food", "commercial"), "pain & pâtisserie": ("bakery", "commercial"),
    "viande & poisson": ("meat and fish", "commercial"), "fruits & légumes": ("fruit and vegetables", "commercial"),
    "tissus & couture": ("fabrics and tailoring", "commercial"), "pièces détachées": ("spare parts", "commercial"),
    "papeterie & scolaire": ("stationery and school supplies", "commercial"), "médicaments / pharmacie": ("pharmacy / medicines", "commercial"),
    "soins traditionnelle": ("traditional healing", "commercial"), "électronique": ("electronics", "commercial"),
    "boutique phytosanitaires": ("agricultural inputs (phytosanitary products)", "commercial"),
    "produits phytosanitaires": ("agricultural inputs (phytosanitary products)", "commercial"),
    "produit phytosanitaires": ("agricultural inputs (phytosanitary products)", "commercial"), "coiffure": ("hairdressing", "commercial"),
    "produits ménagers": ("household products", "commercial"), "téléphones & accessoires": ("phones and accessories", "commercial"),
    "crédit téléphonique & mobile money": ("airtime and mobile money", "commercial"), "quincaillerie": ("hardware store", "commercial"),
    "meubles": ("furniture", "commercial"), "huile de ménage": ("cooking oil", "commercial"), "hébergement": ("accommodation", "commercial"),
    "snack bar": ("snack bar", "commercial"), "imprimerie": ("printing", "commercial"), "fermier": ("farming", "industrial"),
    "atelier soudure": ("welding workshop", "industrial"), "atelier de soudure": ("welding workshop", "industrial"),
    "atelier menuiserie": ("carpentry workshop", "industrial"), "mécanique": ("mechanics workshop", "industrial"),
    "atelier de mécanique": ("mechanics workshop", "industrial"), "non opérationnel": ("", None), "rien": ("", None),
    "bentaria": ("unclear: 'Bentaria'", None),
}
NAME_SERVICES = [  # keyword in the name -> (service, use); used when the products do not say it
    (r"garage", ("vehicle repair garage", "industrial")), (r"menuiserie", ("carpentry workshop", "industrial")),
    (r"provenderi", ("animal feed mill", "industrial")), (r"pressoir", ("oil press", "industrial")), (r"soudure", ("welding workshop", "industrial")),
    (r"h[ôo]tel", ("accommodation (hotel)", "commercial")), (r"coiffure", ("hairdressing", "commercial")),
    (r"couture", ("tailoring", "commercial")), (r"restau", ("restaurant", "commercial")), (r"\bbar\b|bar$", ("bar", "commercial")),
    (r"boulangerie", ("bakery", "commercial")), (r"ferme", ("farm", "commercial")), (r"micro ?finance", ("microfinance / savings", "office")),
    (r"p[ée]trol", ("fuel / petroleum products", "commercial")), (r"clinique traditionnelle", ("traditional healing clinic", "commercial")),
    (r"[ée]lectronique", ("electronics", "commercial")), (r"alimentation|alimentaire", ("food store", "commercial")), (r"boutique", ("shop", "commercial")),
]
CATEGORY_SERVICE = {  # public_category of the questionnaire -> English service
    "school": "education (school)", "health": "health care", "religious": "religious site", "community": "community facility",
    "water_point": "water point", "government": "administration / chiefdom", "antenna": "telecommunication / radio",
}
PUBLIC_NAME_SERVICES = [  # more precise service from the name
    (r"lyc[ée]e technique", "technical high school"), (r"lyc[ée]e", "secondary school"), (r"maternelle", "nursery school"),
    (r"[ée]cole primaire|[ée]cole publique|[ée]cole de|[ée]cole ", "primary school"), (r"centre de sant[ée]|\bcsi\b|\bcma\b|centre m[ée]dical", "health centre"),
    (r"[ée]glise", "church"), (r"lieu sacr[ée]", "sacred place"), (r"stade", "stadium"), (r"foyer", "community hall"), (r"agro ?pastoral", "agro-pastoral centre"),
    (r"apiculteur", "beekeepers' and organic farmers' house"), (r"point d'eau", "water point"), (r"radio", "radio station"), (r"chef", "chiefdom"),
]
HERITAGE = {"sacred_forest": "sacred forest", "cave": "cave", "worship": "place of worship", "chiefdom": "chiefdom", "other": "other site"}
KIND = {"sacred": "sacred site", "tourist": "tourist site", "both": "sacred and tourist site"}
REVIEW_HOUSEHOLD = 15


def clean_name(text) -> str:
    """Corrected spelling of a name typed in the survey.

    Args:
        text: Raw name.

    Returns:
        The corrected name (empty when the entry was not a name).
    """
    if not isinstance(text, str) or not text.strip():
        return ""
    t = re.sub(r"\s+", " ", text).strip()
    fixed = NAME_FIXES.get(t.lower())
    if fixed is not None:
        return fixed
    t = re.sub(r"^eglise\b", "Église", t, flags=re.I)
    t = re.sub(r"^ecole\b", "École", t, flags=re.I)
    return t[0].upper() + t[1:]


def _n(v):
    """A survey number (``12``, ``Quatre``, ``Plus de 230``) as an int, or None.

    Args:
        v: Raw value.

    Returns:
        The number or None.
    """
    words = {"un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10}
    if isinstance(v, (int, float)) and pd.notna(v):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"\d+", v)
        if m:
            return int(m.group())
        return words.get(v.strip().lower())
    return None


def classify(r: pd.Series) -> dict:
    """Use, residents, name, service and review note of one survey record.

    Args:
        r: A row of the EFYL export.

    Returns:
        ``{"use", "residents", "name", "service", "workers", "review"}``.
    """
    out = {"use": "unknown", "residents": None, "name": "", "service": "", "workers": None, "review": "", "azimuth": r.get("azimuth_degrees")}
    t = r.get("survey_type")
    if t == "household":
        n = _n(r.get("survey_total_persons"))
        out.update(use="residential", residents=n)
        if n and n > REVIEW_HOUSEHOLD:
            out["review"] = f"{n} residents: check (boarding house?)"
    elif t == "commerce":
        name = clean_name(r.get("commerce_name"))
        services, uses, notes = [], [], []
        for tok in str(r.get("commerce_products") or "").split(","):
            tok = tok.strip()
            if not tok:
                continue
            svc, use = PRODUCTS.get(tok.lower(), (None, None))
            if tok.lower() == "non opérationnel":
                notes.append("not operating")
            if svc is None:
                notes.append(f"unknown product '{tok}'")
                svc = tok.lower()
            if svc and svc not in services:
                services.append(svc)
            if use:
                uses.append(use)
            if svc.startswith("unclear"):
                notes.append(svc)
        for pat, (svc, use) in NAME_SERVICES:                 # the name can say what the products do not
            if re.search(pat, name.lower() or str(r.get("commerce_name") or "").lower()):
                if pat in (r"alimentation|alimentaire", r"boutique") and services:
                    continue                                   # a generic word: the products already say more
                if svc not in services:
                    services.append(svc)
                uses.append(use)
        if "industrial" in uses:
            use = "industrial"
        elif "office" in uses and "commercial" not in uses[:-1]:
            use = "office"
        else:
            use = "commercial"
        out.update(use=use, name=name, service="; ".join(s for s in services if not s.startswith("unclear")), workers=_n(r.get("commerce_workers")),
                   review="; ".join(dict.fromkeys(notes)))
        if not out["service"]:
            out["review"] = (out["review"] + "; no activity given").strip("; ")
    elif t == "public":
        name = clean_name(r.get("public_name"))
        cat = r.get("public_category")
        use = "public"
        if cat == "other":
            other = str(r.get("public_category_other") or "").lower()
            svc = "accommodation (hotel)" if "hotel" in other or "hôtel" in other else other or "other"
            use = "commercial" if "hotel" in other or "hôtel" in other else "public"
            parts = [svc]
        else:
            parts = [CATEGORY_SERVICE.get(cat, str(cat))]
            for pat, svc in PUBLIC_NAME_SERVICES:             # the name is more precise than the category
                if re.search(pat, name.lower()):
                    parts = [svc]
                    break
        out.update(use=use, name=name, service="; ".join(parts), workers=_n(r.get("public_workers")))
    elif t == "heritage":
        cat, kind = r.get("heritage_category"), r.get("heritage_kind")
        what = HERITAGE.get(cat, str(cat))
        if cat == "other" and isinstance(r.get("heritage_category_other"), str):
            what = r["heritage_category_other"].strip().lower()
        parts = [f"heritage: {what}", KIND.get(kind, str(kind))]
        if isinstance(r.get("heritage_facilities"), str) and r["heritage_facilities"].strip():
            parts.append(f"facilities: {r['heritage_facilities'].strip()}")
        out.update(use="public", name=clean_name(r.get("heritage_name")), service="; ".join(parts))
    return out


def build() -> pd.DataFrame:
    """Attributes of every building of ``inputs/survey_buildings.csv``.

    Returns:
        One row per ``building_id`` with the columns documented in the module docstring.
    """
    exp = pd.read_csv(IN / "survey_export_20260827.csv", low_memory=False)
    exp["building_id"] = exp["owner_id"].astype(str) + "|" + exp["address_id"].astype(str) + "|" + exp["created_at"].astype(str)
    b = pd.read_csv(IN / "survey_buildings.csv")[["building_id"]]
    rows = []
    by_id = exp.drop_duplicates("building_id").set_index("building_id")
    for bid in b["building_id"]:
        out = classify(by_id.loc[bid]) if bid in by_id.index else classify(pd.Series(dtype=object))
        rows.append({"building_id": bid, **out})
    df = pd.DataFrame(rows)
    df["residents"] = df["residents"].astype("Int64")
    df["workers"] = df["workers"].astype("Int64")
    return df


if __name__ == "__main__":
    df = build()
    df.to_csv(IN / "building_attributes.csv", index=False)
    print(df["use"].value_counts().to_string())
    print(f"residents total {int(df['residents'].sum())}; to review: {(df['review'] != '').sum()}")
    print(df[df["review"] != ""][["building_id", "name", "service", "review"]].to_string())
