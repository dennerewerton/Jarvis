# -*- coding: utf-8 -*-
"""UNO module package.

Drop this folder next to your site_bot.py and then register it:

    from uno import register_uno
    register_uno(app)

Put your PNG cards in: uno/cartas_uno/
"""

from .uno_game import uno_bp, register_uno  # noqa
