"""
location_filter.py — the configured search area, shared by
board_fetch.py (postings, at fetch time) and jobs_screen.py (digest
rows, before ranking), so both sources are cut by the same rule:
  - a location naming one of the metro cities passes;
  - so does a remote location open to the US. Platforms with structured
    remote/country fields pass them in; the location text is the
    fallback, and the only input for digest rows.
"""
import re

REMOTE_RE = re.compile(r"\bremote\b|\banywhere\b", re.I)
US_COUNTRIES = {"us", "usa", "united states", "united states of america"}

def location_matcher(metro_cities, remote_regions=()):
    """location_ok(location, remote=None, country=None) for the area
    configured by metro_cities and remote_regions.

    The metro city list lives in config (PII routing). Its names are
    joined into one regex searched anywhere in the location; an empty
    list matches every location. remote_regions adds your own region,
    e.g. a state name and its abbreviation, matched as whole words, so
    "Remote - <region>" counts as US when there is no country field.
    """
    metro_re = re.compile("|".join(metro_cities), re.I)
    # Only consulted for remote locations without a country field
    us_re = re.compile(
        r"\bus\b|\busa\b|\bu\.s\.|united states|north america"
        + "".join(rf"|\b{re.escape(r)}\b" for r in remote_regions),
        re.I,
    )

    def location_ok(location, remote=None, country=None):
        """True for a location in a configured metro city, or a remote
        one open to the US. remote/country are None when the source has
        no structured fields for them."""
        if metro_re.search(location):
            return True
        if not (remote or REMOTE_RE.search(location)):
            return False
        if country:
            return country.strip().lower() in US_COUNTRIES
        # No country field: keep bare "Remote" or US-marked text, drop
        # "Remote - Canada", "Remote (EMEA)", ...
        rest = REMOTE_RE.sub("", location)
        return bool(us_re.search(location)) or not re.search(r"[a-z]", rest, re.I)

    return location_ok
