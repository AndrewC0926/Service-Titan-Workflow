"""City -> county derivation for DMG's 7 territory counties (Los Angeles,
Orange, San Bernardino, Riverside, San Diego, Imperial, Kern -- see
config.yaml's territory.counties). Same discipline as
app.pipeline.iepr._CITY_TO_COUNTY (a separate, smaller table scoped to
utility large-load filings, not reused here since its city coverage serves
a different source): a curated set of UNAMBIGUOUS cities, never a zip-range
guess, never a fuzzy match. A city not listed here returns None -- that is
the correct answer for a city this table's author hasn't confirmed, not a
bug to silence with a broader-but-wrong heuristic.

Keys are the city name lowercased and whitespace-collapsed; city_to_county
does that normalization itself, callers pass the raw string as typed."""
from __future__ import annotations

import re

# A handful of real spellings/abbreviations seen on rep rosters and permit
# data that don't match a canonical city name outright.
_CITY_ALIASES = {
    "la": "los angeles",
    "dtla": "los angeles",
    "so pas": "south pasadena",
    "so pasadena": "south pasadena",
    "n hollywood": "north hollywood",
    "hollywood": "los angeles",
    "van nuys": "los angeles",
    "sherman oaks": "los angeles",
    "woodland hills": "los angeles",
    "canoga park": "los angeles",
    "reseda": "los angeles",
    "encino": "los angeles",
    "chatsworth": "los angeles",
    "san pedro": "los angeles",
    "sylmar": "los angeles",
    "mission hills": "los angeles",
    "boyle heights": "los angeles",
    "eagle rock": "los angeles",
    "highland park": "los angeles",
}

# Deliberately not comprehensive -- every entry below has been confirmed a
# real, unambiguous city-in-county pair. Add to this table, don't guess
# around it; a missing city correctly returns None from city_to_county.
_CITY_TO_COUNTY: dict[str, str] = {
    # Los Angeles County
    "los angeles": "Los Angeles", "long beach": "Los Angeles", "glendale": "Los Angeles",
    "santa clarita": "Los Angeles", "lancaster": "Los Angeles", "palmdale": "Los Angeles",
    "pomona": "Los Angeles", "torrance": "Los Angeles", "pasadena": "Los Angeles",
    "south pasadena": "Los Angeles", "el monte": "Los Angeles", "downey": "Los Angeles",
    "inglewood": "Los Angeles", "west covina": "Los Angeles", "norwalk": "Los Angeles",
    "burbank": "Los Angeles", "compton": "Los Angeles", "carson": "Los Angeles",
    "santa monica": "Los Angeles", "whittier": "Los Angeles", "hawthorne": "Los Angeles",
    "alhambra": "Los Angeles", "lakewood": "Los Angeles", "bellflower": "Los Angeles",
    "baldwin park": "Los Angeles", "gardena": "Los Angeles", "huntington park": "Los Angeles",
    "monterey park": "Los Angeles", "paramount": "Los Angeles", "montebello": "Los Angeles",
    "arcadia": "Los Angeles", "diamond bar": "Los Angeles", "redondo beach": "Los Angeles",
    "rosemead": "Los Angeles", "culver city": "Los Angeles", "san gabriel": "Los Angeles",
    "el segundo": "Los Angeles", "signal hill": "Los Angeles", "vernon": "Los Angeles",
    "commerce": "Los Angeles", "city of industry": "Los Angeles", "santa fe springs": "Los Angeles",
    "cerritos": "Los Angeles", "azusa": "Los Angeles", "covina": "Los Angeles",
    "west hollywood": "Los Angeles", "manhattan beach": "Los Angeles", "hermosa beach": "Los Angeles",
    "glendora": "Los Angeles", "la puente": "Los Angeles", "la mirada": "Los Angeles",
    "bell": "Los Angeles", "bell gardens": "Los Angeles", "temple city": "Los Angeles",
    "walnut": "Los Angeles", "duarte": "Los Angeles", "monrovia": "Los Angeles",
    "claremont": "Los Angeles", "la verne": "Los Angeles", "san dimas": "Los Angeles",
    "hawaiian gardens": "Los Angeles", "lawndale": "Los Angeles", "lomita": "Los Angeles",
    "malibu": "Los Angeles", "calabasas": "Los Angeles", "agoura hills": "Los Angeles",
    "westlake village": "Los Angeles", "rolling hills estates": "Los Angeles",
    "palos verdes estates": "Los Angeles", "rancho palos verdes": "Los Angeles",
    "south gate": "Los Angeles", "maywood": "Los Angeles", "cudahy": "Los Angeles",
    "south el monte": "Los Angeles", "irwindale": "Los Angeles", "bradbury": "Los Angeles",
    "sierra madre": "Los Angeles", "san marino": "Los Angeles", "artesia": "Los Angeles",

    # Orange County
    "anaheim": "Orange", "santa ana": "Orange", "irvine": "Orange", "huntington beach": "Orange",
    "garden grove": "Orange", "orange": "Orange", "fullerton": "Orange", "costa mesa": "Orange",
    "mission viejo": "Orange", "westminster": "Orange", "newport beach": "Orange",
    "buena park": "Orange", "lake forest": "Orange", "tustin": "Orange", "yorba linda": "Orange",
    "san clemente": "Orange", "laguna niguel": "Orange", "la habra": "Orange",
    "fountain valley": "Orange", "placentia": "Orange", "rancho santa margarita": "Orange",
    "aliso viejo": "Orange", "cypress": "Orange", "brea": "Orange", "stanton": "Orange",
    "san juan capistrano": "Orange", "dana point": "Orange", "laguna hills": "Orange",
    "seal beach": "Orange", "la palma": "Orange", "los alamitos": "Orange",
    "laguna beach": "Orange", "laguna woods": "Orange", "villa park": "Orange",

    # San Bernardino County
    "san bernardino": "San Bernardino", "fontana": "San Bernardino", "rancho cucamonga": "San Bernardino",
    "ontario": "San Bernardino", "victorville": "San Bernardino", "rialto": "San Bernardino",
    "hesperia": "San Bernardino", "chino": "San Bernardino", "chino hills": "San Bernardino",
    "upland": "San Bernardino", "redlands": "San Bernardino", "colton": "San Bernardino",
    "yucaipa": "San Bernardino", "apple valley": "San Bernardino", "montclair": "San Bernardino",
    "highland": "San Bernardino", "loma linda": "San Bernardino", "barstow": "San Bernardino",
    "twentynine palms": "San Bernardino", "big bear lake": "San Bernardino",
    "adelanto": "San Bernardino", "grand terrace": "San Bernardino", "needles": "San Bernardino",
    "yucca valley": "San Bernardino",

    # Riverside County
    "riverside": "Riverside", "moreno valley": "Riverside", "corona": "Riverside",
    "murrieta": "Riverside", "temecula": "Riverside", "jurupa valley": "Riverside",
    "indio": "Riverside", "hemet": "Riverside", "menifee": "Riverside", "perris": "Riverside",
    "eastvale": "Riverside", "palm desert": "Riverside", "cathedral city": "Riverside",
    "palm springs": "Riverside", "la quinta": "Riverside", "beaumont": "Riverside",
    "banning": "Riverside", "wildomar": "Riverside", "lake elsinore": "Riverside",
    "coachella": "Riverside", "desert hot springs": "Riverside", "norco": "Riverside",
    "san jacinto": "Riverside", "rancho mirage": "Riverside", "indian wells": "Riverside",
    "blythe": "Riverside", "calimesa": "Riverside", "canyon lake": "Riverside",

    # San Diego County
    "san diego": "San Diego", "chula vista": "San Diego", "oceanside": "San Diego",
    "escondido": "San Diego", "carlsbad": "San Diego", "el cajon": "San Diego",
    "vista": "San Diego", "san marcos": "San Diego", "encinitas": "San Diego",
    "national city": "San Diego", "la mesa": "San Diego", "santee": "San Diego",
    "poway": "San Diego", "coronado": "San Diego", "imperial beach": "San Diego",
    "solana beach": "San Diego", "del mar": "San Diego", "lemon grove": "San Diego",

    # Imperial County
    "el centro": "Imperial", "calexico": "Imperial", "brawley": "Imperial",
    "imperial": "Imperial", "holtville": "Imperial", "calipatria": "Imperial",
    "westmorland": "Imperial",

    # Kern County
    "bakersfield": "Kern", "delano": "Kern", "ridgecrest": "Kern", "tehachapi": "Kern",
    "wasco": "Kern", "shafter": "Kern", "arvin": "Kern", "california city": "Kern",
    "mcfarland": "Kern", "taft": "Kern", "mojave": "Kern",
}


def _clean_key(city: str) -> str:
    key = re.sub(r"\s+", " ", city.strip().lower())
    return _CITY_ALIASES.get(key, key)


def city_to_county(city: str | None) -> str | None:
    """Unambiguous city -> one of Scout's 7 territory counties, or None.
    Never guesses from a zip code or partial match -- a city not in this
    table (or one genuinely shared across counties, e.g. a name repeated
    in more than one state) is left for a human to fill in, same as every
    other unresearched field in this app."""
    if not city:
        return None
    return _CITY_TO_COUNTY.get(_clean_key(city))
