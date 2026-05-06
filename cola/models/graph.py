from __future__ import annotations
from dataclasses import dataclass
from dataclasses import field
from enum import Enum


class GraphRowColor(Enum):
    NORMAL = 0
    MERGE = 1
    HEAD = 2


@dataclass
class EdgeSegment:
    from_column: int
    to_column: int
    color_index: int


@dataclass
class GraphRow:
    commit_oid: str
    commit_column: int
    edges_to_parent: list[EdgeSegment] = field(default_factory=list)
    color: GraphRowColor = GraphRowColor.NORMAL


@dataclass
class GraphResult:
    rows: list[GraphRow]
    max_columns: int


def build_graph(
    commits: list[tuple[str, list[str]]],
    head_oid: str | None = None,
    orphan_cooldown: int = 0,
) -> GraphResult:
    """Build a row-based graph representation from a list of commits.

    Commits are received in topo order from RepoReader (oldest first).

    When ``orphan_cooldown`` is set to ``N >= 1`` an orphan-root commit
    that closes its lane keeps the column reserved (rendered as a blank
    column) until the cooldown counter ticks back down to zero, so that
    an unrelated chain processed shortly afterwards cannot reuse the same
    column. The counter is initialized to ``N`` at the orphan row and
    decremented at the start of every subsequent row; ``N = 1`` is enough
    to push the next new tip into a fresh column. The default ``0``
    preserves the historical behavior of trimming the closed column
    immediately.
    """
    active_lanes: list[str | None] = []
    # Per-column cooldown counters parallel to ``active_lanes``. A positive
    # value pins a None slot in place; the counter is decremented at the
    # start of every row.
    lane_cooldown: list[int] = []
    cooldown = max(0, orphan_cooldown)
    color_map: dict[str, int] = {}
    next_color = 0
    rows: list[GraphRow] = []
    max_columns = 0

    # The graph is built top-to-bottom (newest first), so the input is reversed.
    for oid, parent_oids in reversed(commits):
        # Tick down lane cooldowns at the top of every row so a column held
        # by ``orphan_cooldown`` becomes reusable exactly N rows later.
        for i, value in enumerate(lane_cooldown):
            if value > 0:
                lane_cooldown[i] = value - 1

        # Find the commit in active_lanes or allocate a new lane.
        if oid in active_lanes:
            commit_column = active_lanes.index(oid)
        else:
            commit_column = len(active_lanes)
            active_lanes.append(oid)
            lane_cooldown.append(0)

        # Assign a color for this commit's lane.
        commit_color = color_map.get(oid, None)
        if commit_color is None:
            commit_color = next_color
            next_color += 1
        else:
            # This is the last time we see this commit, remove it from color_map to reduce
            # max memory consumption
            color_map.pop(oid)

        edges: list[EdgeSegment] = []

        # Pass through lanes
        for i, lane_oid in enumerate(active_lanes):
            if lane_oid is not None and lane_oid != oid:
                edges.append(
                    EdgeSegment(
                        from_column=i,
                        to_column=i,
                        color_index=color_map[lane_oid],
                    )
                )

        if parent_oids:
            for i, parent_oid in enumerate(parent_oids):
                # Select color if not selected: first parent gets commit color,
                # others get next color
                parent_color = color_map.get(parent_oid, None)
                if parent_color is None:
                    if i == 0:
                        parent_color = commit_color
                    else:
                        parent_color = next_color
                        next_color += 1
                    color_map[parent_oid] = parent_color

                if parent_oid in active_lanes:
                    parent_col = active_lanes.index(parent_oid)
                    if i == 0:
                        # First parent means commit no longer uses its column
                        active_lanes[commit_column] = None
                else:
                    if i == 0:
                        # First parent takes the commit's lane.
                        active_lanes[commit_column] = parent_oid
                        parent_col = commit_column
                    else:
                        # Try to reuse a None slot, but skip slots whose
                        # cooldown is still active (orphan-closed lanes).
                        parent_col = -1
                        for slot, lane_oid in enumerate(active_lanes):
                            if lane_oid is None and lane_cooldown[slot] <= 0:
                                parent_col = slot
                                break
                        if parent_col >= 0:
                            active_lanes[parent_col] = parent_oid
                            lane_cooldown[parent_col] = 0
                        else:
                            # Append new
                            parent_col = len(active_lanes)
                            active_lanes.append(parent_oid)
                            lane_cooldown.append(0)

                edges.append(
                    EdgeSegment(
                        from_column=commit_column,
                        to_column=parent_col,
                        color_index=parent_color,
                    )
                )
        else:
            # Root commit - remove its lane and start its cooldown so the
            # column cannot be re-used by an unrelated chain in the next
            # ``orphan_cooldown`` rows.
            active_lanes[commit_column] = None
            if cooldown > 0:
                lane_cooldown[commit_column] = cooldown

        max_columns = max(max_columns, len(active_lanes))

        # Trim trailing None slots, but keep slots that are still cooling
        # down so the visual gap survives until the cooldown expires.
        while (
            active_lanes
            and active_lanes[-1] is None
            and lane_cooldown[-1] <= 0
        ):
            active_lanes.pop()
            lane_cooldown.pop()

        if head_oid is not None and oid == head_oid:
            color = GraphRowColor.HEAD
        elif len(parent_oids) > 1:
            color = GraphRowColor.MERGE
        else:
            color = GraphRowColor.NORMAL

        row = GraphRow(
            commit_oid=oid,
            commit_column=commit_column,
            edges_to_parent=edges,
            color=color,
        )
        rows.append(row)

    return GraphResult(rows=rows, max_columns=max_columns)
