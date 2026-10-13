"""engine — the game-state engine: event log, protocol, text guard, shadow writer.

Only scripts/game.py and, while `engine: schatten` is set, the shadow writer
(push_stats.py, tracker.py, xp.py) write the event log. The display never does.
Plan: docs/plans/spielzustand-engine.md.
"""

PROTOCOL_VERSION = 1
