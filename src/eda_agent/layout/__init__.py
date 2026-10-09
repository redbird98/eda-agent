# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Layout engines for real boards: exact geometry, rules, DRC, placement
and routing, all in-house.

``model`` holds the board as the engines see it, ``geom`` the exact shape
arithmetic every clearance decision rests on.
"""

from .model import LayoutBoard

__all__ = ["LayoutBoard"]
