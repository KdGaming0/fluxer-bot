"""
utils/detection.py
------------------
Shared mod-question detection logic used by cogs/hypixel_monitor.py
and cogs/reddit_monitor.py.

Both cogs watch different *sources* (Hypixel forums vs Reddit) but apply
identical scoring to the text they find.  Keeping the logic here means
keyword lists, weights, and false-positive patterns only have to be edited
in one place.

Public API
----------
DEFAULT_KEYWORDS        dict[str, list[str]]   — tiered keyword lists
FALSE_POSITIVE_PATTERNS list[re.Pattern]        — patterns that veto a match
CONTEXT_PATTERNS        list[re.Pattern]        — patterns that boost score

score_text(title, body, keywords) -> ScoreResult
    Pure function.  Returns a dataclass with all scoring detail.

should_notify(title, body, result, threshold) -> bool
    Pure function.  Returns True if the post should trigger a notification.

Detection tiers
---------------
higher   → immediate notify regardless of threshold (VIP names / exact slugs)
normal   → +6.0 title / +3.0 body per phrase  (÷2 for single words)
lower    → +3.0 title / +1.5 body per phrase  (÷2 for single words)
negative → −4.0 title / −2.0 body per phrase  (÷2 for single words)

Context boost: +0.5 per matching CONTEXT_PATTERN, capped at +2.0.
Only applied when at least one normal- or lower-tier keyword matched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

# ── Tier weights ──────────────────────────────────────────────────────────────
# (title_phrase, body_phrase) — single-word hits use half these values.
_TIER_WEIGHT: Dict[str, tuple[float, float]] = {
    "higher":   (0.0,  0.0),   # score irrelevant; only flips `immediate`
    "normal":   (6.0,  3.0),
    "lower":    (3.0,  1.5),
    "negative": (-4.0, -2.0),
}
_SINGLE_DIVISOR = 2.0

# ── Minimum score delta required above threshold when the post is borderline ─
_BORDERLINE_MARGIN = 1.5

# ── Default keyword lists ─────────────────────────────────────────────────────
DEFAULT_KEYWORDS: Dict[str, List[str]] = {
    # Immediate-trigger keywords — bypass threshold entirely.
    # Add exact mod names / usernames you specifically support here.
    "higher": [
        "skyblock enhanced", "sb enhanced",
        "kd_gaming1", "kdgaming1", "kdgaming",
        "packcore", "scale me", "scaleme",
    ],

    # Normal keywords — mod names, loaders, and tech-help vocabulary.
    # The negative list handles false positives; don't over-restrict here.
    "normal": [
        # Mod loaders / build tools
        "forge", "fabric", "modpack", "modpacks",
        "configs", "config", "configuration", "modrinth",
        "1.21.5", "1.21.8", "1.21.10", "1.21.11", "26.1", "26.2",

        # Generic modding terms
        "mod", "mods", "modded", "modding",
        "modification", "loader", "addon", "plugin",
        "skyblock addons", "not enough updates",
        "texture pack", "resource pack",
        "shader", "shaders", "optifine",
        "optimization", "optimize", "tweak", "utility",

        # 1.21+ SkyBlock mods
        "firmament", "skyblock tweaks", "modern warp menu",
        "skyblockaddons unofficial", "skyhanni", "hypixel mod api",
        "skyocean", "skyblock profile viewer", "bazaar utils",
        "skyblocker", "cookies-mod", "aaron's mod",
        "custom scoreboard", "skycubed", "nofrills",
        "nobaaddons", "sky cubed", "dulkirmod",
        "skyblock 21", "skycofl",

        # 1.8.9 SkyBlock mods
        "notenoughupdates", "neu", "polysprint",
        "skyblockaddons", "sba", "polypatcher",
        "hypixel plus", "furfsky", "dungeons guide",
        "skyguide", "partly sane skies",
        "secret routes mod", "skytils",

        # Performance mods
        "more culling", "badoptimizations",
        "concurrent chunk management", "very many players",
        "threadtweak", "scalablelux", "particle core",
        "sodium", "lithium", "iris",
        "entity culling", "ferritecore", "immediatelyfast",

        # QoL mods
        "scrollable tooltips", "fzzy config",
        "no chat reports", "no resource pack warnings",
        "auth me", "betterf3", "no double sneak",
        "centered crosshair", "continuity", "3d skin layers",
        "wavey capes", "sound controller",
        "cubes without borders", "sodium shadowy path blocks",

        # Popular clients / launchers
        "ladymod", "laby", "badlion", "lunar", "essential",
        "lunarclient", "feather",

        # Performance problems
        "fps boost", "fps drop", "frame drop", "low fps", "bad performance",
        "stuttering", "choppy", "frames", "frame rate",
        "performance", "fps", "lag",
        "memory", "ram", "cpu", "gpu", "graphics",

        # Technical problem words
        "bug", "error", "glitch", "crash", "crashing",
        "freezing", "not working", "broken",
        "fix", "troubleshoot",
        "install", "installation", "setup",
        "configure", "compatibility",

        # Mod-specific install / crash phrases
        "install mod", "mod installation", "how to install mod",
        "mod not loading", "mod not working", "mods not loading",
        "mod crashing", "mod crash", "client crash",
        "mod conflict", "mod incompatible",
        "java crash", "java error", "memory leak",

        # Platform / runtime
        "java", "minecraft", "windows", "linux",
    ],

    # Lower tier — intentionally sparse; add very weak signals here if needed.
    "lower": [],

    # Negative tier — penalise game-content posts that are rarely mod-related.
    "negative": [
        # Economy / trading
        "auction house", "bazaar", "trading",
        "selling", "buying", "worth", "price check",
        "price", "coins", "bits",
        "money making", "farming coins",

        # Game progression / gear
        "minion", "dungeon master", "catacombs", "slayer", "dragon",
        "collection", "skill", "enchanting", "reforge",
        "talisman", "accessory", "weapon", "armor", "pet",
        "bestiary", "crimson isle", "kuudra",

        # Farming / garden game content
        "crop", "crops", "crop fever", "farming",
        "greenhouse", "garden", "mutation", "mutations",
        "dicer", "melon dicer", "visitor", "compost",
        "plot", "plots", "jacob", "pest",

        # World / exploration content
        "foraging", "foraging island", "jungle island", "mining island",
        "rift", "living cave", "autocap", "autonull",
        "dwarven mines", "crystal hollows", "deep caverns",
        "spider's den", "blazing fortress",
        "new profile", "profile",

        # Fishing content
        "fishing", "trophy fish", "lava fishing",

        # Combat / boss content
        "dungeon", "floor", "boss", "mob", "monster",
        "damage", "effective hp", "ehp", "dps",
    ],
}

# ── False-positive content patterns ───────────────────────────────────────────
# A match on any of these vetos the notification entirely, regardless of score.
FALSE_POSITIVE_PATTERNS: List[re.Pattern] = [
    re.compile(r'\b(selling|buying|trade|auction|price\s*check|worth)\b', re.I),
    re.compile(r'\b(looking\s*for|want\s*to\s*buy|WTB|WTS)\b', re.I),
    re.compile(r'\b(what.{0,20}worth|how\s+much|value)\b', re.I),
    re.compile(r'\b(crop|crops|greenhouse|mutation|mutations|farming|harvest|garden|dicer|compost|visitor|jacob)\b', re.I),
    re.compile(r'\b(foraging\s+island|jungle\s+island|rift\s+(?!client)|living\s+cave|dwarven|crystal\s+hollow)\b', re.I),
    re.compile(r'\b(new\s+profile|fresh\s+profile|my\s+profile|profile\s+reset)\b', re.I),
    re.compile(r'\b(dungeon\s+(?:run|floor|room)|slayer\s+(?:quest|boss)|dragon\s+(?:eye|armor|fight))\b', re.I),
    re.compile(r'\b(skill\s+(?:level|cap|xp|exp)|collection\s+(?:level|req))\b', re.I),
]

# ── Context-boost patterns ────────────────────────────────────────────────────
# Each match adds +0.5 to the score, capped at +2.0 total.
CONTEXT_PATTERNS: List[re.Pattern] = [
    re.compile(r'\b(help|issue|problem|crash|fix|install|setup|configure)\b', re.I),
    re.compile(r"\b(not\s+working|broken|won'?t\s+work|can'?t\s+get|having\s+trouble)\b", re.I),
    re.compile(r'\b(fps|performance|lag|optimization|memory|ram|java)\b', re.I),
    re.compile(r'\b(how\s+do\s+i|how\s+to|anyone\s+know|can\s+someone|need\s+help|please\s+help)\b', re.I),
    re.compile(r'\?'),
]


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class ScoreResult:
    """The full output of :func:`score_text`."""

    immediate:     bool
    """True when any *higher*-tier keyword matched — skip threshold check."""

    score:         float
    """Aggregate relevance score (positive = more relevant)."""

    matches:       Dict[str, List[str]] = field(default_factory=lambda: {
        "higher": [], "normal": [], "lower": [], "negative": [],
    })
    """Which keywords matched, keyed by tier."""

    context_boost: float = 0.0
    """How much the context patterns added to the score."""

    breakdown:     Dict[str, tuple] = field(default_factory=dict)
    """Per-keyword detail: ``{keyword: (tier, points_awarded)}``."""


# ── Core scoring function ─────────────────────────────────────────────────────

def score_text(
    title: str,
    body: str,
    keywords: Dict[str, List[str]],
) -> ScoreResult:
    """Score *title* and *body* against *keywords* and return a :class:`ScoreResult`.

    Title hits count for the phrase/word's *title weight*; body-only hits use
    the *body weight*.  Single-word keywords use half the phrase weight for
    both title and body hits.

    Args:
        title:    The post/thread title.
        body:     The post/thread body text.
        keywords: A dict with keys ``higher``, ``normal``, ``lower``,
                  ``negative``, each mapping to a list of keyword strings.
                  Typically :data:`DEFAULT_KEYWORDS` or a per-guild override.

    Returns:
        A populated :class:`ScoreResult`.
    """
    title_l  = title.lower()
    body_l   = body.lower()
    combined = f"{title_l}\n{body_l}"

    matches:   Dict[str, List[str]] = {"higher": [], "normal": [], "lower": [], "negative": []}
    breakdown: Dict[str, tuple]     = {}
    score = 0.0

    for tier in ("higher", "normal", "lower", "negative"):
        tw_phrase, bw_phrase = _TIER_WEIGHT[tier]
        tw_single = tw_phrase / _SINGLE_DIVISOR
        bw_single = bw_phrase / _SINGLE_DIVISOR

        for kw in keywords.get(tier, []):
            kw_l = kw.lower()
            is_phrase = " " in kw_l

            if is_phrase:
                in_title = kw_l in title_l
                in_body  = kw_l in body_l and not in_title
                if not (in_title or in_body):
                    continue
                pts = tw_phrase if in_title else bw_phrase
            else:
                pat      = rf'\b{re.escape(kw_l)}\b'
                in_title = bool(re.search(pat, title_l))
                in_body  = bool(re.search(pat, body_l)) and not in_title
                if not (in_title or in_body):
                    continue
                pts = tw_single if in_title else bw_single

            matches[tier].append(kw)
            score += pts
            breakdown[kw] = (tier, pts)

    # Context boost — only meaningful when there are positive keyword hits
    context_boost = 0.0
    if matches["normal"] or matches["lower"]:
        for cp in CONTEXT_PATTERNS:
            if cp.search(combined):
                context_boost = min(context_boost + 0.5, 2.0)
        score += context_boost

    return ScoreResult(
        immediate=bool(matches["higher"]),
        score=round(score, 2),
        matches=matches,
        context_boost=context_boost,
        breakdown=breakdown,
    )


# ── Notification gate ─────────────────────────────────────────────────────────

def should_notify(
    title: str,
    body: str,
    result: ScoreResult,
    threshold: float,
) -> bool:
    """Return True if a post with the given *result* should trigger a notification.

    This is a pure function — callers are responsible for extracting ``title``
    and ``body`` from whatever source object they have (Hypixel thread dict,
    asyncpraw Submission, etc.) before calling.

    Args:
        title:     The post/thread title (used for false-positive pattern checks).
        body:      The post/thread body text.
        result:    A :class:`ScoreResult` returned by :func:`score_text`.
        threshold: The minimum score required to trigger a notification.
                   Typically ``3.0``; lower = more sensitive.

    Returns:
        ``True`` if the post should be notified, ``False`` otherwise.
    """
    # Immediate tier always wins.
    if result.immediate:
        return True

    if result.score < threshold:
        return False

    combined = f"{title.lower()} {body.lower()}"

    # Too many negative signals relative to positive — likely a game-content post.
    neg = len(result.matches["negative"])
    pos = len(result.matches["normal"]) + len(result.matches["lower"])
    if neg >= pos and neg > 1:
        return False

    # Hard-veto on well-known false-positive content patterns.
    for pat in FALSE_POSITIVE_PATTERNS:
        if pat.search(combined):
            return False

    # Borderline score: require at least one normal-tier match OR a meaningful
    # context boost before notifying.
    if result.score < threshold + _BORDERLINE_MARGIN:
        if not result.matches["normal"] and result.context_boost < 1.0:
            return False

    return True