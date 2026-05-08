"""Pure logic for the "Rewind check" feature.

Locate an old commit on the current branch whose snapshot of the user's
modified files matches the working tree byte-for-byte, so the branch head can
be safely rewound to it. UI orchestration lives in ``cola.widgets.rewind``.
"""

from __future__ import annotations

import os

from typing import Callable
from typing import Optional

from .git import STDOUT


_REWIND_PREFIX = 'rewind_'
_MAX_BRANCH_SUFFIX = 99

# Sentinel for dirty paths that have been removed from the worktree. A
# candidate commit matches a deleted path iff that path is absent from the
# commit's tree.
DELETED = '__deleted__'


def commits_touching_paths(
    context, branch: str, paths: list[str], max_n: int = 200
) -> list[str]:
    """Return commit oids on ``branch`` (HEAD-first) that touched any of ``paths``.

    Uses ``--first-parent --no-renames`` so the search stays on the branch's
    primary lineage and matches by literal path (no rename following).
    """
    if not paths:
        return []
    args = [branch, '--']
    args.extend(paths)
    out = context.git.log(
        '--first-parent',
        '--no-renames',
        '-n',
        str(max_n),
        '--pretty=%H',
        *args,
        _readonly=True,
    )[STDOUT]
    return [line.strip() for line in out.splitlines() if line.strip()]


def first_parent_commits(context, branch: str, max_n: int = 200) -> list[str]:
    """Return commit oids on ``branch``'s first-parent line (HEAD-first)."""
    out = context.git.log(
        '--first-parent',
        '-n',
        str(max_n),
        '--pretty=%H',
        branch,
        _readonly=True,
    )[STDOUT]
    return [line.strip() for line in out.splitlines() if line.strip()]


def tree_blob_entries(
    context, oid: str, paths: list[str]
) -> dict[str, tuple[str, str]]:
    """Return ``{path: (mode, blob_oid)}`` for ``paths`` at commit ``oid``.

    Paths absent from the tree at ``oid`` are omitted from the result. Only
    regular blobs (``100644``/``100755``/``120000``) are usable for hash
    comparison; callers should treat other modes as a miss.
    """
    if not paths:
        return {}
    status, out, _ = context.git.ls_tree(
        '--full-tree', oid, '--', *paths, _readonly=True
    )
    result: dict[str, tuple[str, str]] = {}
    if status != 0 or not out:
        return result
    for line in out.splitlines():
        if not line:
            continue
        # ls-tree output: "<mode> <type> <oid>\t<path>"
        try:
            meta, path = line.split('\t', 1)
        except ValueError:
            continue
        parts = meta.split(' ')
        if len(parts) != 3:
            continue
        mode, _type, blob_oid = parts
        result[path] = (mode, blob_oid)
    return result


def worktree_blob_oid(context, path: str) -> Optional[str]:
    """Return the blob oid git would store for ``path`` in the worktree.

    Uses default filters so CRLF / text=auto normalization matches what is
    already in the index/tree. Returns ``DELETED`` when the file is absent
    from the worktree, or ``None`` when git refuses to hash it for any
    other reason.
    """
    worktree = context.git.worktree() or context.git.getcwd()
    if worktree and not os.path.lexists(os.path.join(worktree, path)):
        return DELETED
    status, out, _ = context.git.hash_object('--', path, _readonly=True)
    if status != 0:
        return None
    out = (out or '').strip()
    return out or None


def _all_match(
    tree_entries: dict[str, tuple[str, str]],
    worktree_oids: dict[str, str],
    dirty_paths: list[str],
) -> bool:
    """Return True iff every dirty path either matches its candidate commit
    blob (for present files) or is absent from the tree (for deleted files).
    """
    for path in dirty_paths:
        expected = worktree_oids.get(path)
        entry = tree_entries.get(path)
        if expected == DELETED:
            # Deleted in worktree -> commit must also lack the path.
            if entry is not None:
                return False
            continue
        if entry is None:
            return False
        mode, blob_oid = entry
        # Regular file (100644/100755) or symlink (120000). Reject gitlinks
        # (160000) and tree entries (040000).
        if mode not in ('100644', '100755', '120000'):
            return False
        if blob_oid != expected:
            return False
    return True


def find_rewind_target(
    context,
    branch: str,
    dirty_paths: list[str],
    progress_cb: Callable[[int], bool],
    *,
    max_commits: int = 200,
    batch: int = 10,
) -> Optional[str]:
    """Walk ``branch`` history looking for a commit whose tree matches the
    worktree for every path in ``dirty_paths``.

    ``progress_cb(checked_count)`` is invoked once per ``batch`` of commits
    that did not match. If it returns False the search stops early. Returns
    the matching commit oid, or ``None`` when no match is found / the user
    cancels / a dirty path cannot be hashed.
    """
    if not branch or not dirty_paths:
        return None

    worktree_oids: dict[str, str] = {}
    has_deleted = False
    for path in dirty_paths:
        oid = worktree_blob_oid(context, path)
        if oid is None:
            return None
        if oid == DELETED:
            has_deleted = True
        worktree_oids[path] = oid

    # When any dirty path is a deletion, the matching commit may pre-date the
    # path's introduction and therefore not appear in `git log -- <path>`.
    # Fall back to the full first-parent history in that case.
    if has_deleted:
        candidates = first_parent_commits(context, branch, max_n=max_commits)
    else:
        candidates = commits_touching_paths(
            context, branch, dirty_paths, max_n=max_commits
        )

    checked = 0
    for commit_oid in candidates:
        entries = tree_blob_entries(context, commit_oid, dirty_paths)
        checked += 1
        if _all_match(entries, worktree_oids, dirty_paths):
            return commit_oid
        if batch > 0 and checked % batch == 0:
            if not progress_cb(checked):
                return None
    return None


def existing_local_branches(context) -> set[str]:
    """Return the set of local branch short names."""
    out = context.git.for_each_ref(
        '--format=%(refname:short)', 'refs/heads/', _readonly=True
    )[STDOUT]
    return {line.strip() for line in out.splitlines() if line.strip()}


def unique_rewind_branch_name(context, branch: str) -> Optional[str]:
    """Return a non-conflicting backup branch name for rewinding ``branch``.

    Always uses the form ``rewind_<branch>/<N>`` starting at ``N=0`` and
    incrementing on conflict, so the ``rewind_<branch>`` namespace stays a
    directory of refs (avoiding git's directory/file ref conflict). A
    candidate conflicts if it equals an existing branch name or is a
    path-prefix of one. Returns ``None`` if all candidates up to the limit
    are taken.
    """
    existing = existing_local_branches(context)

    def _conflicts(name: str) -> bool:
        if name in existing:
            return True
        for other in existing:
            if other.startswith(name + '/') or name.startswith(other + '/'):
                return True
        return False

    for n in range(0, _MAX_BRANCH_SUFFIX + 1):
        candidate = f'{_REWIND_PREFIX}{branch}/{n}'
        if not _conflicts(candidate):
            return candidate
    return None
