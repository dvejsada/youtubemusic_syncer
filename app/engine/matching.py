"""Title / artist normalisation and similarity: the matching heuristics shared by all services."""
from __future__ import annotations

import difflib
import re
import unicodedata


NOISE = re.compile(
    r"\s*[\(\[]\s*(feat\.?|ft\.?|featuring|with|prod\.?|official|lyric|audio|video|"
    r"visuali[sz]er|directed by|explicit|clean|remaster(ed)?|\d{4} remaster(ed)?|deluxe|"
    r"expanded|bonus|single|ep|radio edit|album version|live edit)[^\)\]]*[\)\]]",
    re.I,
)
# "(U.S. Version)", "(Bonus Edition)", "(International Deluxe)", "(2011 Remastered)" ...
EDITION = re.compile(r"\s*[\(\[][^\)\]]*\b(edition|version|deluxe|remaster(ed)?)\s*[\)\]]", re.I)
DASH_SUFFIX = re.compile(r"\s+-\s+(\d{4}\s+)?(remaster(ed)?|live|mono|stereo|radio edit|single version).*$", re.I)
PIPE_SUFFIX = re.compile(r"\s*[|｜].*$")
EMPTY_PARENS = re.compile(r"\s*[\(\[]\s*[\)\]]")
JUNK_WORDS = re.compile(r"\b(4k|hd|hq|official (music )?video|lyric video)\b", re.I)


def clean(s: str) -> str:
    """Strip editions, feat., video tags etc. but keep the original casing/characters."""
    s = PIPE_SUFFIX.sub("", s or "")
    s = EDITION.sub("", s)
    s = NOISE.sub("", s)
    s = DASH_SUFFIX.sub("", s)
    s = JUNK_WORDS.sub("", s)
    s = EMPTY_PARENS.sub("", s)
    return re.sub(r"\s+", " ", s).strip(" -")


def norm(s: str) -> str:
    s = clean(s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))  # é -> e, but keeps 中文 / 한국어
    s = s.lower().replace("&", " and ").replace("’", "'").replace("‐", "-")
    s = re.sub(r"[\W_]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def sim(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = sorted((a, b), key=len)
    # containment only counts if the strings are of similar length
    # ("The Album" must not match "Hit Collection, Volume 1: The Album New Edition")
    if short in long_ and len(short) / len(long_) >= 0.6:
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def split_title(title: str, artists: list):
    """Video titles like 'Artist - Song | OFFICIAL VIDEO' -> (artist, 'Song').
    Returns (artist_or_None, cleaned_title)."""
    t = PIPE_SUFFIX.sub("", title or "").strip()
    found_artist = None
    # drop leading "<listed artist> - " (channel name repeated in the title), possibly twice
    for _ in range(2):
        if " - " not in t:
            break
        left, right = t.split(" - ", 1)
        if any(sim(left, a) > 0.85 for a in artists):
            t = right.strip()
        else:
            break
    # anything still in "X - Song" form: X is probably the real artist (e.g. uploader remixes)
    if " - " in t:
        left, right = t.split(" - ", 1)
        if len(left) < 60 and not DASH_SUFFIX.search(t):
            found_artist, t = left.strip(), right.strip()
    t = EMPTY_PARENS.sub("", JUNK_WORDS.sub("", t))
    return found_artist, re.sub(r"\s+", " ", t).strip(" -")
