# SPDX-License-Identifier: GPL-3.0
# Copyright (c) 2014-2026 William Edwards <shadowapex@gmail.com>, Benjamin Bean <superman2k5@gmail.com>
from collections.abc import Callable
from typing import Final

import pygame
from pygame.event import Event

from tuxemon.platform.const import buttons
from tuxemon.platform.events import PlayerInput

_EVENT_MAP: Final[dict[int, Callable[[], Event]]] = {
    buttons.UP: lambda: Event(pygame.KEYDOWN, key=pygame.K_UP),
    buttons.DOWN: lambda: Event(pygame.KEYDOWN, key=pygame.K_DOWN),
    buttons.LEFT: lambda: Event(pygame.KEYDOWN, key=pygame.K_LEFT),
    buttons.RIGHT: lambda: Event(pygame.KEYDOWN, key=pygame.K_RIGHT),
    buttons.BACK: lambda: Event(pygame.KEYDOWN, key=pygame.K_ESCAPE),
    buttons.A: lambda: Event(pygame.KEYDOWN, key=pygame.K_RETURN),
    buttons.B: lambda: Event(pygame.KEYDOWN, key=pygame.K_ESCAPE),
}


def playerinput_to_event(event: PlayerInput) -> Event | None:
    if event.button == buttons.MOUSELEFT:
        if event.pressed:
            pos = event.value
            if not isinstance(pos, (tuple, list)) or len(pos) != 2:
                return None
            return Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1)
        if event.released:
            # release() zeroes PlayerInput.value, so the click position is
            # no longer available there; use the live cursor position.
            return Event(
                pygame.MOUSEBUTTONUP, pos=pygame.mouse.get_pos(), button=1
            )
        return None

    factory = _EVENT_MAP.get(event.button)
    if not factory:
        return None
    return factory()
