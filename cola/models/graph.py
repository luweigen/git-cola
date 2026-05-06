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
    orphan_isolate: bool = False,
) -> GraphResult:
    """Build a row-based graph representation from a list of commits.

    Commits are received in topo order from RepoReader (oldest first).

    When ``orphan_isolate`` is true, an orphan-root commit that closes
    its lane keeps the column reserved (rendered as a blank column) for
    the rest of the build, so an unrelated chain processed later cannot
    reuse it and visually fuse with the orphan chain. The default
    (false) preserves the historical behavior of trimming the closed
    column immediately.
    """
    active_lanes: list[str | None] = []
    # Per-column reservation flags parallel to ``active_lanes``. ``True``
    # pins a None slot so trim and non-first-parent reuse skip it.
    lane_reserved: list[bool] = []
    color_map: dict[str, int] = {}
    next_color = 0
    rows: list[GraphRow] = []
    max_columns = 0

    # The graph is built top-to-bottom (newest first), so the input is reversed.
    for oid, parent_oids in reversed(commits):
        # Find the commit in active_lanes or allocate a new lane.
        if oid in active_lanes:
            commit_column = active_lanes.index(oid)
        else:
            commit_column = len(active_lanes)
            active_lanes.append(oid)
            lane_reserved.append(False)

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
                        # Try to reuse a None slot, but skip slots
                        # reserved for orphan-chain isolation.
                        parent_col = -1
                        for slot, lane_oid in enumerate(active_lanes):
                            if lane_oid is None and not lane_reserved[slot]:
                                parent_col = slot
                                break
                        if parent_col >= 0:
                            active_lanes[parent_col] = parent_oid
                        else:
                            # Append new
                            parent_col = len(active_lanes)
                            active_lanes.append(parent_oid)
                            lane_reserved.append(False)

                edges.append(
                    EdgeSegment(
                        from_column=commit_column,
                        to_column=parent_col,
                        color_index=parent_color,
                    )
                )
        else:
            # Root commit - remove its lane. With ``orphan_isolate`` the
            # column stays reserved for the remainder of the build, so an
            # unrelated chain processed later cannot land on the same
            # column.
            active_lanes[commit_column] = None
            if orphan_isolate:
                lane_reserved[commit_column] = True

        max_columns = max(max_columns, len(active_lanes))

        # Trim trailing None slots, but keep reserved slots so the visual
        # gap survives for the rest of the build.
        while (
            active_lanes
            and active_lanes[-1] is None
            and not lane_reserved[-1]
        ):
            active_lanes.pop()
            lane_reserved.pop()

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
