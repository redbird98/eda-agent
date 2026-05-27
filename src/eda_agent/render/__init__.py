# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""In-house rendering of Altium designs to SVG.

Independent of any third-party Altium parser: geometry is pulled live
from the running Altium session via the MCP/DelphiScript bridge, then
rendered to SVG in Python here. The output is interactive-ready
(``data-*`` attributes on every group so a downstream dashboard or
LLM tool can hook click / hover / cross-probe without re-parsing).
"""

from .sch_svg import render_sch_svg, SchRenderOptions
from .pcb_svg import render_pcb_svg, PcbRenderOptions

__all__ = [
    "render_sch_svg", "SchRenderOptions",
    "render_pcb_svg", "PcbRenderOptions",
]
