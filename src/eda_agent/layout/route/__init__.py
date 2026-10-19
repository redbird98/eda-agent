# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The router: a grid of exact clearances, searched net by net, with
nets negotiating for contested space until none share it."""

from .router import Router, route_adaptive, route_board

__all__ = ["Router", "route_adaptive", "route_board"]
