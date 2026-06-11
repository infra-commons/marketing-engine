"""
Hand-authored phrase banks for the Cashbucket content engine.

The Draft Generator selects from these. It does NOT generate its own.
Add variants freely. Keep each slot at 20–50 entries — more becomes maintenance.

Slots:
  OPENERS         — first sentence of the article (sets tone, must have NZ hook)
  TRANSITIONS     — mid-article paragraph connectors
  CASHBUCKET_CTAS — the soft Cashbucket mention (late in article, one paragraph)
  CLOSES          — final sentence or two of the article

IMPORTANT: Every OPENER must contain a placeholder {NZ_SPECIFIC} for the
draft generator to fill with actual NZ data from the brief (date, figure, entity).
"""

import random


# ─────────────────────────────────────────────────────────────────────────────
# OPENERS
# Each must set up a specific NZ context. {NZ_SPECIFIC} is filled by the
# Draft Generator from the brief's key facts.
# ─────────────────────────────────────────────────────────────────────────────

OPENERS = [
    # News-reaction type
    "{NZ_SPECIFIC} landed without much fanfare, but the businesses that noticed it first will have an advantage over the ones that catch up later.",
    "When {NZ_SPECIFIC} came through, most business owners were too busy to read past the headline. That's usually when the real implications start to matter.",
    "{NZ_SPECIFIC} is the kind of change that looks minor until you run the numbers on what it actually means for your cash position.",

    # Sector/seasonal type
    "Every {NZ_SPECIFIC}, the same pattern plays out across New Zealand: businesses that planned for it six weeks ago are fine; businesses that didn't are scrambling.",
    "The {NZ_SPECIFIC} is not news to anyone running a business in this sector — but knowing it's coming and being ready for it are different things.",

    # Founder voice type
    "Most of the business owners we talk to already know {NZ_SPECIFIC} is a problem. What they're less clear on is what to do about it before it becomes a crisis.",
    "There's a version of {NZ_SPECIFIC} that ruins a business quietly. It doesn't announce itself. It just compresses your margins by a few percent until, one quarter, you notice the number has turned.",

    # Contrarian/specific opener
    "The conventional advice on {NZ_SPECIFIC} is fine as far as it goes, which is not very far.",
    "If your accountant told you not to worry about {NZ_SPECIFIC}, it's worth asking them to show you the numbers behind that advice.",
]


# ─────────────────────────────────────────────────────────────────────────────
# TRANSITIONS
# Connect paragraphs without using banned connectors (moreover, furthermore, etc.)
# ─────────────────────────────────────────────────────────────────────────────

TRANSITIONS = [
    # Contrast
    "The catch is",
    "That's the easy part.",
    "The harder question is",
    "Most businesses get the first part right.",
    "Where it gets complicated is",
    "That holds true until it doesn't.",

    # Elaboration (specific)
    "Here's what that looks like in practice.",
    "The numbers back this up.",
    "Put another way:",
    "To make this concrete:",
    "One way to think about this:",

    # Pivot
    "The situation is different for businesses that",
    "It depends heavily on",
    "This is where sector matters.",
    "For a business of that size,",
    "For seasonal operators, the calculation shifts.",

    # Causation
    "The reason this matters is",
    "What drives this is straightforward:",
    "It comes down to timing.",
    "Cash flow is the mechanism.",
    "The underlying issue is",
]


# ─────────────────────────────────────────────────────────────────────────────
# CASHBUCKET MENTIONS
# One paragraph, soft, late. Cashbucket as one option, not the answer.
# Never use: game-changer, streamline, seamless, robust, unlock, leverage.
# ─────────────────────────────────────────────────────────────────────────────

CASHBUCKET_CTAS = [
    (
        "For businesses that want a closer view of their cash position without building it "
        "from scratch in a spreadsheet, Cashbucket is worth looking at. It's built for "
        "NZ-based SMEs, connects to Xero, and gives you a rolling forward view of your "
        "cash — not just what happened last month."
    ),
    (
        "One tool that a number of NZ SMEs use for this kind of visibility is Cashbucket. "
        "It's not the only option, but it's designed specifically around the way cash "
        "moves through a small business — receivables timing, payroll cycles, seasonal "
        "dips — rather than being a general-purpose accounting view."
    ),
    (
        "If you're currently managing this in a spreadsheet that's become unwieldy, "
        "Cashbucket is worth a look. It connects directly to Xero and gives you a "
        "forward-looking cash model that updates as your actual figures come in — "
        "useful when the next 90 days matter more than last quarter's report."
    ),
    (
        "Cashbucket is a NZ-built cash flow tool that addresses this specific problem: "
        "the gap between what accounting software tells you about the past and what "
        "you actually need to know about the next 60 to 90 days. Whether it fits "
        "depends on your setup, but the problem it's solving is the one described above."
    ),
    (
        "Tools like Cashbucket are built around this premise — that a business owner "
        "needs a cash view that's forward-looking and tied to real numbers, not a "
        "monthly summary that's already three weeks out of date by the time you read it. "
        "Worth a look if this resonates."
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# CLOSES
# End on something that earns the read. No summary of the article.
# No "in conclusion." No restatement of the headline.
# ─────────────────────────────────────────────────────────────────────────────

CLOSES = [
    "The businesses that handle this well aren't smarter than average. They just check the numbers more often.",
    "None of this is complicated. The hard part is making it a habit before you need it.",
    "A good cash forecast doesn't guarantee anything. But it does mean you'll see the problem in time to do something about it.",
    "The alternative — waiting until the pressure is obvious — is a strategy too, just not one many business owners would choose if they thought it through.",
    "What makes the difference, consistently, is visibility and speed. Not one or the other. Both.",
    "Run the scenario. You might be fine. But you'll know, rather than guess.",
    "It's a solvable problem. Most of the businesses that have gone through this are glad they dealt with it before they had to.",
]


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def pick_opener(nz_specific: str) -> str:
    """Select a random opener and fill in the NZ-specific placeholder."""
    template = random.choice(OPENERS)
    return template.replace("{NZ_SPECIFIC}", nz_specific)


def pick_transition() -> str:
    return random.choice(TRANSITIONS)


def pick_cashbucket_cta() -> str:
    return random.choice(CASHBUCKET_CTAS)


def pick_close() -> str:
    return random.choice(CLOSES)
