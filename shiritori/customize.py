"""User-owned copy and visual settings.

This file is intentionally small and independent.  It is a good first place
for the repository owner to make changes without touching the game engine.
See CONTRIBUTING.md for the suggested collaboration workflow.
"""

APP_TITLE = "しりとり"
APP_KICKER = "ことばを、つないで、どこまでも。"
APP_DESCRIPTION = "最後のひらがなから始まることばを入力してください。"

# The starting word must contain at least two hiragana characters and must not
# end with 「ん」.  Try choosing your own word as a first contribution.
START_WORD = "しりとり"

# These values become CSS custom properties in ``shiritori/page.py``.
PRIMARY_COLOR = "#5B4BDB"
SECONDARY_COLOR = "#18A999"
ACCENT_COLOR = "#FFB84D"

REPOSITORY_URL = "https://github.com/akatonboboonboon/Siritori-app"
