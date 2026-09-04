"""Backtest: replay a completed season through the real engine.

The engine has only ever been graded against itself — a simulator whose
opponents were ADP bots we wrote, scored on projections we also wrote. That
grades the code for self-consistency, not for being right.

This package grades it against what actually happened: real preseason
projections, the league's real draft, and the points players really scored.
"""
