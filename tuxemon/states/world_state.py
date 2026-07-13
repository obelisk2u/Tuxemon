# SPDX-License-Identifier: GPL-3.0
# Copyright (c) 2014-2026 William Edwards <shadowapex@gmail.com>, Benjamin Bean <superman2k5@gmail.com>
from __future__ import annotations

import logging
from collections import deque
from collections.abc import Mapping
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    no_type_check,
)

import pygame
from pygame.font import Font, get_default_font
from pygame.surface import Surface

from tuxemon.camera.camera import Camera, project, unproject
from tuxemon.db import Direction
from tuxemon.event.eventmiddleware import (
    CameraControlMiddleware,
    DevToolsMiddleware,
    InputTranslatorMiddleware,
    MovementMiddleware,
    WorldCommandMiddleware,
)
from tuxemon.faction.manager import FactionManager
from tuxemon.graphics import load_and_scale
from tuxemon.item.filter import ItemFilter
from tuxemon.platform.const import buttons
from tuxemon.platform.const.graphics import WHITE_COLOR
from tuxemon.platform.events import PlayerInput
from tuxemon.prepare import DEV_TOOLS
from tuxemon.save_system.save_state import WorldSave
from tuxemon.session import Session
from tuxemon.state.state import State
from tuxemon.ui.text_renderer import TextRenderer
from tuxemon.world.manager import WorldMenuManager
from tuxemon.world.transition import WorldTransition

if TYPE_CHECKING:
    from tuxemon.base_client import BaseClient
    from tuxemon.network.networking import EventData, update_client

logger = logging.getLogger(__name__)


class WorldState(State):
    """The state responsible for the world game play"""

    name: ClassVar[str] = "WorldState"

    def __init__(
        self,
        client: BaseClient,
        session: Session,
        map_name: str | None = None,
        yaml_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(client=client, **kwargs)
        mw = self.client.event_manager.get_middleware_instance(
            InputTranslatorMiddleware
        )
        if mw is None:
            mw = InputTranslatorMiddleware()
            self.client.event_manager.add_middleware(mw, priority=0)

        self.input_translator_mw = mw
        self.session = session
        self.session.set_world(self)
        self.tile_size = self.client.context.tile_size
        self.menu_manager = WorldMenuManager(self.client)
        self.transition_manager = WorldTransition(
            self, self.client.movement_manager, self.client.context.resolution
        )
        self.player = self.session.player
        self.camera = Camera(
            self.player, self.client.boundary, self.client.context
        )
        self.client.camera_manager.add_camera(self.player.slug, self.camera)
        self.faction_manager = FactionManager(self.client.event_bus)
        self.client.map_transition.change_map(map_name, yaml_name)
        self.client.reset_renderer()

        self.command_mw = WorldCommandMiddleware(
            self.player,
            self.client.state_manager,
            self.client.input_manager,
            self.client.event_manager,
            self.menu_manager,
        )
        self.camera_mw = CameraControlMiddleware(self.client.camera_manager)
        self.movement_mw = MovementMiddleware(
            self.player,
            self.client.movement_manager,
            self.client.camera_manager,
        )
        self.devtools_mw = DevToolsMiddleware(
            self.player,
            self.client.map_manager,
            self.client.event_manager,
            self.client.input_manager,
        )
        self.client.event_manager.add_middleware(self.camera_mw, priority=5)
        self.client.event_manager.add_middleware(self.movement_mw, priority=10)
        if DEV_TOOLS:
            self.client.event_manager.add_middleware(
                self.devtools_mw, priority=20
            )
        self.client.event_manager.add_middleware(self.command_mw, priority=30)

        # load_and_scale()'s default scale parameter is bound at import time
        # (DISPLAY_CONTEXT.scale as it was when tuxemon.graphics was first
        # imported), not the real runtime scale, so it must be passed
        # explicitly here to avoid an under-scaled icon.
        bag_icon = load_and_scale(
            "gfx/ui/item/backpack.png", self.client.context.scale
        )
        half_size = (bag_icon.get_width() // 2, bag_icon.get_height() // 2)
        self._bag_icon = pygame.transform.smoothscale(bag_icon, half_size)
        self._bag_icon_rect = self._bag_icon.get_rect()

    def get_state(self, session: Session) -> WorldSave:
        """Returns a WorldSave model representing the current world state."""
        return WorldSave(
            factions_manager=self.faction_manager.set_state(
                self.client.npc_manager
            ),
            menu_flags=self.menu_manager.menu_flags.export(),
        )

    def set_state(self, session: Session, save_data: WorldSave) -> None:
        """Recreates the World from the provided saved data."""
        self.faction_manager.get_state(save_data.factions_manager)
        self.menu_manager.menu_flags.import_flags(save_data.menu_flags)

    def prepare_for_teleport(self) -> None:
        """
        Stops all WorldState background activity and locks player controls
        in preparation for a map change or teleport.
        """
        self.remove_animations_of(self)
        self.stop_scheduled_callbacks()
        self.client.movement_manager.stop_char(self.player)
        self.client.movement_manager.lock_controls(self.player)

    def resume(self) -> None:
        """Called after returning focus to this state"""
        self.client.event_manager.add_middleware(
            self.input_translator_mw, priority=0
        )

    def pause(self) -> None:
        """Called before another state gets focus"""
        self.client.event_manager.remove_middleware(self.input_translator_mw)
        self.client.movement_manager.stop_char(self.player)

    def broadcast_player_teleport_change(self) -> None:
        """Tell clients/host that player has moved after teleport."""
        self.client.npc_manager.handle_player_teleport(
            self.client, self.player, self.client.network_manager
        )

    def update(self, dt: float) -> None:
        super().update(dt)
        self.faction_manager.update(dt, self.session)
        self.client.npc_manager.update_npcs(dt, self.client)
        self.client.npc_manager.update_npcs_off_map(dt, self.client)
        self.client.map_renderer.update(dt)

    def draw(self, surface: Surface) -> None:
        """Draw the game world to the screen."""
        self.client.map_renderer.draw(
            surface, self.client.map_manager.current_map
        )
        self.transition_manager.draw(surface)
        self._draw_position_hud(surface)
        self._draw_click_target_marker(surface)
        self._draw_bag_icon(surface)

    def _draw_bag_icon(self, surface: Surface) -> None:
        """Draw a clickable bag icon in the bottom-right corner."""
        self._bag_icon_rect.bottomright = (
            surface.get_width() - 8,
            surface.get_height() - 8,
        )
        surface.blit(self._bag_icon, self._bag_icon_rect)

    def _draw_position_hud(self, surface: Surface) -> None:
        """Draw the player's current tile position in the top-right corner."""
        if self.player is None:
            return
        x, y = self.player.tile_pos
        renderer = TextRenderer(
            scaling=self.client.context.scaling,
            font_color=WHITE_COLOR,
            font=Font(get_default_font(), 15),
        )
        image = renderer.shadow_text(f"X: {x}  Y: {y}")
        rect = image.get_rect()
        rect.topright = (surface.get_width() - 8, 8)
        surface.blit(image, rect)

    def _map_renderer_offset_and_ratio(
        self,
    ) -> tuple[float, float, float, float] | None:
        """
        Returns (offset_x, offset_y, ratio_x, ratio_y) that convert a
        world/project-space pixel position into a true final on-screen pixel
        position (i.e. within the actual surface passed to draw()).

        NOTE: pyscroll's `translate_point()`/`_real_ratio_x/y` are NOT usable
        for this directly. BufferedRenderer.__init__ sets `_zoom_level` from
        the `zoom` constructor kwarg without going through the `zoom`
        property setter, so `_real_ratio_x/y` never gets computed from it and
        stays at its default of 1.0 - meaning translate_point() actually
        returns coordinates in the oversized internal zoom-buffer space
        (e.g. 2560x1440 for a 1280x720 screen at 0.5 zoom), not final screen
        pixels. Sprites get away with this because pyscroll's own draw()
        rescales the whole composited buffer afterward; code that draws
        directly onto the final surface (like this marker) must apply that
        buffer->screen scale itself. We compute it from the actual buffer
        size rather than hardcoding WORLD_VIEW_ZOOM, so it stays correct even
        if that constant or the buffer sizing logic changes.
        """
        current_map = self.client.map_manager.current_map
        if current_map is None or current_map.renderer is None:
            return None
        renderer = current_map.renderer
        offset_x, offset_y = renderer.get_center_offset()
        buffer_size = renderer._zoom_buffer.get_size()
        screen_size = renderer._size
        ratio_x = screen_size[0] / buffer_size[0]
        ratio_y = screen_size[1] / buffer_size[1]
        return offset_x, offset_y, ratio_x, ratio_y

    def _world_to_screen(self, world_pos: tuple[float, float]) -> tuple[int, int] | None:
        offsets = self._map_renderer_offset_and_ratio()
        if offsets is None:
            return None
        offset_x, offset_y, ratio_x, ratio_y = offsets
        return (
            round(round(world_pos[0] + offset_x) * ratio_x),
            round(round(world_pos[1] + offset_y) * ratio_y),
        )

    def _screen_to_world(self, screen_pos: tuple[float, float]) -> tuple[float, float] | None:
        offsets = self._map_renderer_offset_and_ratio()
        if offsets is None:
            return None
        offset_x, offset_y, ratio_x, ratio_y = offsets
        return (
            screen_pos[0] / ratio_x - offset_x,
            screen_pos[1] / ratio_y - offset_y,
        )

    def _draw_click_target_marker(self, surface: Surface) -> None:
        """Draw a marker over the tile the player is currently walking to."""
        if self.player is None:
            return
        target = self.player.path_controller.pathfinding
        if target is None:
            return

        context = self.client.context
        tile_w, tile_h = context.tile_size
        target_world = project(context, target)
        target_center = (
            target_world[0] + tile_w / 2,
            target_world[1] + tile_h / 2,
        )

        screen_pos = self._world_to_screen(target_center)
        if screen_pos is None:
            return
        pygame.draw.circle(surface, (255, 40, 40), screen_pos, 7, 2)
        pygame.draw.circle(surface, (255, 40, 40), screen_pos, 1)

    def _handle_click_to_move(self, event: PlayerInput) -> bool:
        """Pathfind the player to the tile clicked on the map."""
        if event.button != buttons.MOUSELEFT or not event.pressed:
            return False

        if not self.client.movement_manager.is_movement_allowed(self.player):
            return False

        camera = self.client.camera_manager.get_active_camera()
        if camera is None or not camera.is_following():
            return False

        screen_pos = event.value
        if not isinstance(screen_pos, (tuple, list)) or len(screen_pos) != 2:
            return False

        world_pos = self._screen_to_world(screen_pos)
        if world_pos is None:
            return False
        tile_pos = unproject(self.client.context, world_pos)

        destination = self._nearest_reachable_tile(tile_pos)
        if destination is None:
            return True

        self.player.pathfind(destination)
        return True

    def _nearest_reachable_tile(
        self, target: tuple[int, int], max_radius: int = 8
    ) -> tuple[int, int] | None:
        """
        Returns `target` if it's reachable from the player, otherwise the
        closest reachable tile to it (so clicking on/near a building or
        other obstacle still walks the player as close as possible instead
        of silently doing nothing).
        """
        pathfinder = self.player.path_controller.pathfinder
        start = self.player.tile_pos

        reachable: set[tuple[int, int]] = {start}
        queue: deque[tuple[int, int]] = deque([start])
        while queue:
            pos = queue.popleft()
            if pos == target:
                return target
            for neighbor in pathfinder.get_exits(
                position=pos, facing=self.player.facing
            ):
                if neighbor not in reachable:
                    reachable.add(neighbor)
                    queue.append(neighbor)

        if target in reachable:
            return target

        best: tuple[int, int] | None = None
        best_dist = None
        for pos in reachable:
            dx = pos[0] - target[0]
            dy = pos[1] - target[1]
            dist = dx * dx + dy * dy
            if best_dist is None or dist < best_dist:
                best = pos
                best_dist = dist

        if best is None or best == start:
            return None
        if max(abs(best[0] - target[0]), abs(best[1] - target[1])) > max_radius:
            return None
        return best

    def process_event(self, event: PlayerInput) -> PlayerInput | None:
        """
        Handles player input events.

        This function is only called when the player provides input such
        as pressing a key or clicking the mouse.

        Since this is part of a chain of event handlers, the return value
        from this method becomes input for the next one.  Returning None
        signifies that this method has dealt with an event and wants it
        exclusively.  Return the event and others can use it as well.

        You should return None if you have handled input here.

        Parameters:
            event: Event to handle.

        Returns:
            Passed events, if other states should process it, ``None``
            otherwise.
        """
        if self.player is None:
            return None
        if self._handle_bag_icon_click(event):
            return None
        if self._handle_click_to_move(event):
            return None
        return event

    def _handle_bag_icon_click(self, event: PlayerInput) -> bool:
        """Open the bag when the bottom-right bag icon is clicked."""
        if event.button != buttons.MOUSELEFT or not event.pressed:
            return False

        screen_pos = event.value
        if not isinstance(screen_pos, (tuple, list)) or len(screen_pos) != 2:
            return False

        if not self._bag_icon_rect.collidepoint(screen_pos):
            return False

        items_filtered = ItemFilter(self.player.items)
        items_filtered.set_filter_all_visible()
        self.client.push_state(
            "ItemMenuState",
            character=self.player,
            source="WorldMenuState",
            item_filter=items_filtered,
            escape_key_exits=True,
        )
        return True

    @no_type_check  # only used by multiplayer which is disabled
    def check_interactable_space(self) -> bool:
        """
        Checks to see if any Npc objects around the player are interactable.

        It then populates a menu of possible actions.

        Returns:
            ``True`` if there is an Npc to interact with. ``False`` otherwise.
        """
        collision_dict = self.get_collision_map()
        player_tile_pos = self.player.tile_pos
        collisions = self.player.collision_check(
            player_tile_pos,
            collision_dict,
            self.client.map_manager.collision_lines_map,
        )
        if not collisions:
            pass
        else:
            for direction in collisions:
                if self.player.facing == direction:
                    if direction == Direction.UP:
                        tile = (player_tile_pos[0], player_tile_pos[1] - 1)
                    elif direction == Direction.DOWN:
                        tile = (player_tile_pos[0], player_tile_pos[1] + 1)
                    elif direction == Direction.LEFT:
                        tile = (player_tile_pos[0] - 1, player_tile_pos[1])
                    elif direction == Direction.RIGHT:
                        tile = (player_tile_pos[0] + 1, player_tile_pos[1])
                    for npc in self.client.npc_manager.npcs:
                        tile_pos = (
                            int(round(npc.tile_pos[0])),
                            int(round(npc.tile_pos[1])),
                        )
                        if tile_pos == tile:
                            logger.info("Opening interaction menu!")
                            self.client.push_state("InteractionMenu")
                            return True
                        else:
                            continue

        return False

    @no_type_check  # FIXME: dead code
    def handle_interaction(
        self, event_data: EventData, registry: Mapping[str, Any]
    ) -> None:
        """
        Presents options window when another player has interacted with this player.

        :param event_data: Information on the type of interaction and who sent it.
        :param registry:

        :type event_data: Dictionary
        :type registry: Dictionary
        """
        target = registry[event_data["target"]]["sprite"]
        target_name = str(target.name)
        update_client(target, event_data["char_dict"], self.client)
        if event_data["interaction"] == "DUEL":
            if not event_data["response"]:
                self.interaction_menu.visible = True
                self.interaction_menu.interactable = True
                self.interaction_menu.player = target
                self.interaction_menu.interaction = "DUEL"
                self.interaction_menu.menu_items = [
                    target_name + " would like to Duel!",
                    "Accept",
                    "Decline",
                ]
            else:
                if self.wants_duel:
                    if event_data["response"] == "Accept":
                        pd = self.player.__dict__
                        event_data = {
                            "type": "CLIENT_INTERACTION",
                            "interaction": "START_DUEL",
                            "target": [event_data["target"]],
                            "response": None,
                            "char_dict": {
                                "monsters": pd["monsters"],
                                "inventory": pd["inventory"],
                            },
                        }
                        self.client.server.notify_client_interaction(
                            "cuuid", event_data
                        )
