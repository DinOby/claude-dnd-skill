"""coins.py — money is never an inventory line.

Sitzung 3 showed coin totals ("31 GM 6 SM") sent as inventory items: the
display listed them as things, the log stored them as items, and the
comparison reported them as inventory nobody owned. Money lives in the sheet's
**Currency:** line (game.py bogen --muenzen) and shows on the display as its
own line under the inventory.

is_coin_line() is used by send.py and push_stats.py (refuse such an item), the
shadow writer (never log one) and the display server (sort old ones out).
"""

import re

_COIN = r"(?:pp|gp|ep|sp|cp|pm|gm|em|sm|km|gold|silber|kupfer|platin|electrum)"
_COIN_LINE = re.compile(rf"^\s*(?:geld\s*:\s*)?(?:\d+\s*{_COIN}\.?[\s,;/]*)+$", re.IGNORECASE)


def is_coin_line(text) -> bool:
    """True for '31 GM 6 SM', '25gp 3sp 0cp', 'Geld: 12 Gold' — not for '50 Gildentaler (Beweis)'."""
    return bool(text) and isinstance(text, str) and bool(_COIN_LINE.match(text))


def split_coins(items) -> "tuple[list, list]":
    """(items that are things, coin lines) from an inventory list; other values pass unchanged."""
    things, coins = [], []
    for item in items or []:
        (coins if is_coin_line(item) else things).append(item)
    return things, coins
