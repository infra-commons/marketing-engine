"""
Banned phrase lockfile for the shared content compliance gate (all brands).

This is the single source of truth. To add a phrase, open a PR against this file.
Phrases are matched case-insensitively. The gate hard-fails on any match.
"""

# ─────────────────────────────────────────────────────────────────────────────
# FORMATTING TELLS — punctuation and structural patterns that flag AI writing
# ─────────────────────────────────────────────────────────────────────────────

# Em dashes are a strong AI writing tell. Check for the Unicode character (U+2014)
# and the HTML entity. In code, check with: "—" in text or "&mdash;" in html
EM_DASH_CHARS = [
    "—",   # — Unicode em dash
    "&mdash;",  # HTML entity
]

# ─────────────────────────────────────────────────────────────────────────────
# LEXICAL TELLS — banned outright
# ─────────────────────────────────────────────────────────────────────────────

BANNED_PHRASES = [
    # Exploration
    "delve",
    "delving",
    "delved",

    # Connector overuse (as paragraph openers — checked structurally, but also banned inline)
    "moreover,",
    "furthermore,",
    "additionally,",

    # AI boilerplate openers
    "in today's fast-paced",
    "in today's rapidly",
    "in today's ever-changing",

    # Navigation metaphors
    "navigate the complexities",
    "navigating the complexities",
    "navigate the landscape",
    "navigating the landscape",
    "navigating these challenges",

    # Unlock family
    "unlock potential",
    "unlock value",
    "unlock opportunities",
    "unlock insights",
    "unlock growth",
    "unlocking potential",
    "unlocking value",
    "unlocking opportunities",

    # Leverage (as verb)
    "leverage your",
    "leverage the",
    "leveraging your",
    "leveraging the",

    # Streamline family
    "streamline",
    "streamlined",
    "streamlining",

    # Robust
    "robust solution",
    "robust system",
    "robust process",

    # Seamless family
    "seamless",
    "seamlessly",

    # Cutting-edge / state-of-the-art
    "cutting-edge",
    "state-of-the-art",

    # Summary openers
    "in conclusion,",
    "to conclude,",
    "in summary,",
    "to summarise,",
    "to summarize,",

    # Hedging tells
    "it's important to note that",
    "it is important to note that",
    "it's worth noting",
    "it is worth noting",

    # False universalism
    "whether you're a",
    "whether you are a",

    # Landscape / realm / tapestry (as metaphor)
    "the world of",
    "the realm of",
    "the landscape of",
    "tapestry of",

    # Journey
    "embark on a journey",
    "embark on this journey",

    # Core metaphors
    "at the heart of",
    "lies at the core of",
    "at its core,",

    # Game-changer
    "game-changer",
    "game-changing",
    "game changer",

    # Overused emphasis
    "pivotal moment",
    "pivotal role",
    "crucial role",
    "crucial importance",

    # Dive / deep dive (as verb)
    "dive deep",
    "deep dive into",
    "let's dive into",
    "we'll dive into",

    # Explainer-headline clichés — the "here's why this is significant" tell.
    # Strong forms only (low false-positive); looser title-only tells live in
    # TITLE_CLICHES below.
    "here's why it matters",
    "here's why that matters",
    "here's what it means",
    "here's what that means",
    "here's what that looks like",
    "the reason this matters is",
    "what this means for you",
]

# ─────────────────────────────────────────────────────────────────────────────
# TITLE-ONLY TELLS — applied to the headline/title, not the body
# ─────────────────────────────────────────────────────────────────────────────
# The title is the highest-visibility surface: it is rendered verbatim into the
# <title>, <h1>, <h2>, social meta and image alt text. Two things that are
# tolerated (or even sanctioned) in body prose are hard fails in a title:
#
#   1. The em dash. The approved phrase bank deliberately uses it as a rhythm
#      device in body sentences, but a title has no legitimate need for one —
#      there it is a strong AI tell. Any of these characters fails the title.
TITLE_EM_DASH_CHARS = ["—", "&mdash;", "–"]  # em dash, HTML entity, en dash

#   2. Explainer-cliché tails. These are too generic to ban in body prose
#      ("...understand why it matters") but are a dead AI giveaway as a headline,
#      typically tacked on after a colon or dash. Case-insensitive substring.
TITLE_CLICHES = [
    "why it matters",
    "what it means for",
    "what you need to know",
    "everything you need to know",
    "what the number hides",
    "what the numbers hide",
    "here's what",
    "here's why",
]

# ─────────────────────────────────────────────────────────────────────────────
# SYNONYM LISTS — any match in these lists also triggers a hard fail
# ─────────────────────────────────────────────────────────────────────────────

LEVERAGE_SYNONYMS = [
    "capitalise on",
    "capitalize on",
    "harness the power",
    "harness the potential",
    "make the most of",          # allow only with specific referent, not as filler
]

DELVE_SYNONYMS = [
    "dig deeper into",
    "dig into this",
    "unpack this",               # allow in casual context, but flag for review
]

UNLOCK_SYNONYMS = [
    "unleash potential",
    "unleash value",
    "tap into potential",
    "tap into opportunities",
]

# ─────────────────────────────────────────────────────────────────────────────
# USAGE
# ─────────────────────────────────────────────────────────────────────────────
# Import and use in compliance_gate.py:
#
#   from banned_phrases import (
#       BANNED_PHRASES, LEVERAGE_SYNONYMS, DELVE_SYNONYMS, UNLOCK_SYNONYMS,
#       EM_DASH_CHARS
#   )
#
#   ALL_BANNED = BANNED_PHRASES + LEVERAGE_SYNONYMS + DELVE_SYNONYMS + UNLOCK_SYNONYMS
#
#   def check_lexical(text: str) -> list[str]:
#       lower = text.lower()
#       return [phrase for phrase in ALL_BANNED if phrase in lower]
#
#   def check_formatting(text: str) -> list[str]:
#       return [char for char in EM_DASH_CHARS if char in text]
#
# check_lexical: case-insensitive phrase match. Empty list = pass.
# check_formatting: literal character match (case-sensitive). Empty list = pass.
