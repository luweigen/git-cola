"""Tests for cola.rewind"""
import os
import sys

import pytest

from cola import rewind

from . import helper
from .helper import app_context

assert app_context is not None  # silence unused import warning


def _commit(message: str) -> str:
    helper.run_git('add', '-A')
    helper.run_git('commit', '-m', message)
    return helper.run_git('rev-parse', 'HEAD').strip()


def _write(path: str, content: bytes) -> None:
    with open(path, 'wb') as fp:
        fp.write(content)


def _accept(_count: int) -> bool:
    return True


def _reject(_count: int) -> bool:
    return False


def test_find_rewind_target_hits_when_worktree_matches_old_commit(app_context):
    """When the worktree contents equal an older commit's, that oid is returned."""
    _write('a.txt', b'v1\n')
    c1 = _commit('c1')
    assert c1
    _write('a.txt', b'v2\n')
    c2 = _commit('c2')
    _write('a.txt', b'v3\n')
    c3 = _commit('c3')
    assert c3

    # Restore the worktree to the c2 state without committing.
    _write('a.txt', b'v2\n')
    app_context.model.update_status()

    target = rewind.find_rewind_target(
        app_context, 'main', ['a.txt'], _accept
    )
    assert target == c2


def test_find_rewind_target_returns_none_when_no_match(app_context):
    _write('a.txt', b'v1\n')
    _commit('c1')
    _write('a.txt', b'v2\n')
    _commit('c2')

    _write('a.txt', b'never-existed\n')
    target = rewind.find_rewind_target(
        app_context, 'main', ['a.txt'], _accept
    )
    assert target is None


def test_find_rewind_target_requires_all_dirty_paths_to_match(app_context):
    """A commit that only matches some of the dirty paths must be skipped."""
    _write('a.txt', b'a1\n')
    _write('b.txt', b'b1\n')
    _commit('c1')
    _write('a.txt', b'a2\n')
    _commit('c2')  # only changes a.txt
    _write('a.txt', b'a3\n')
    _write('b.txt', b'b2\n')
    c3 = _commit('c3')  # changes both

    # Worktree set to {a3, b2} — matches c3 only.
    _write('a.txt', b'a3\n')
    _write('b.txt', b'b2\n')
    target = rewind.find_rewind_target(
        app_context, 'main', ['a.txt', 'b.txt'], _accept
    )
    assert target == c3


def test_find_rewind_target_skips_when_dirty_path_missing_in_old_commit(
    app_context,
):
    """If a dirty path didn't exist at the candidate commit, that commit is skipped."""
    _write('a.txt', b'a1\n')
    c1 = _commit('c1')  # no b.txt yet
    assert c1
    _write('a.txt', b'a1\n')
    _write('b.txt', b'b1\n')
    c2 = _commit('c2')

    # Worktree: a.txt unchanged from c1, b.txt added. Search both.
    _write('a.txt', b'a1\n')
    _write('b.txt', b'b1\n')
    target = rewind.find_rewind_target(
        app_context, 'main', ['a.txt', 'b.txt'], _accept
    )
    # c1 is missing b.txt; c2 matches.
    assert target == c2


def test_find_rewind_target_progress_callback_can_cancel(app_context):
    """When progress_cb returns False the search stops early and returns None."""
    # Build 12 commits that all touch a.txt with distinct contents so none match
    # the bogus worktree state below.
    for i in range(12):
        _write('a.txt', f'v{i}\n'.encode())
        _commit(f'c{i}')

    _write('a.txt', b'no-such-version\n')
    calls = []

    def cb(checked: int) -> bool:
        calls.append(checked)
        return False  # cancel after the first batch

    target = rewind.find_rewind_target(
        app_context, 'main', ['a.txt'], cb, batch=10
    )
    assert target is None
    assert calls == [10]


def test_find_rewind_target_handles_binary_paths(app_context):
    binary_v1 = bytes(range(256))
    binary_v2 = bytes(reversed(range(256)))
    _write('blob.bin', binary_v1)
    c1 = _commit('c1')
    _write('blob.bin', binary_v2)
    _commit('c2')

    _write('blob.bin', binary_v1)
    target = rewind.find_rewind_target(
        app_context, 'main', ['blob.bin'], _accept
    )
    assert target == c1


def test_find_rewind_target_matches_deletion_to_commit_without_file(
    app_context,
):
    """A deleted dirty file matches a candidate commit where the file is absent."""
    _write('a.txt', b'a1\n')
    c1 = _commit('c1')
    assert c1
    _write('b.txt', b'b1\n')
    c2 = _commit('c2')  # introduces b.txt
    assert c2
    _write('b.txt', b'b2\n')
    _commit('c3')

    # Worktree: delete b.txt -> should rewind to c1 (b.txt absent there).
    os.remove('b.txt')
    target = rewind.find_rewind_target(
        app_context, 'main', ['b.txt'], _accept
    )
    assert target == c1


def test_find_rewind_target_deletion_combined_with_modification(app_context):
    _write('a.txt', b'a1\n')
    c1 = _commit('c1')
    assert c1
    _write('a.txt', b'a2\n')
    _write('b.txt', b'b1\n')
    c2 = _commit('c2')
    assert c2
    _write('a.txt', b'a3\n')
    _write('b.txt', b'b2\n')
    _commit('c3')

    # Worktree: a.txt back to a1, b.txt deleted -> only c1 satisfies both.
    _write('a.txt', b'a1\n')
    os.remove('b.txt')
    target = rewind.find_rewind_target(
        app_context, 'main', ['a.txt', 'b.txt'], _accept
    )
    assert target == c1


def test_unique_rewind_branch_name_no_conflict(app_context):
    name = rewind.unique_rewind_branch_name(app_context, 'feature')
    assert name == 'rewind-feature'


def test_unique_rewind_branch_name_increments_on_conflict(app_context):
    _write('a.txt', b'a1\n')
    _commit('c1')
    helper.run_git('branch', 'rewind-feature')
    name = rewind.unique_rewind_branch_name(app_context, 'feature')
    assert name == 'rewind-1_feature'

    helper.run_git('branch', 'rewind-1_feature')
    name = rewind.unique_rewind_branch_name(app_context, 'feature')
    assert name == 'rewind-2_feature'


def test_unique_rewind_branch_name_handles_dir_prefix_conflict(app_context):
    """If "rewind-feature/x" exists git won't allow "rewind-feature" — skip it."""
    _write('a.txt', b'a1\n')
    _commit('c1')
    helper.run_git('branch', 'rewind-feature/sub')
    name = rewind.unique_rewind_branch_name(app_context, 'feature')
    assert name == 'rewind-1_feature'


@pytest.mark.skipif(
    sys.platform == 'win32', reason='hash-object on submodule paths is platform-sensitive'
)
def test_find_rewind_target_skips_gitlink_modes(app_context, monkeypatch):
    """Synthesize a tree entry with gitlink mode and verify it is treated as miss."""
    _write('a.txt', b'a1\n')
    c1 = _commit('c1')
    assert c1

    _write('a.txt', b'a1\n')

    real_ls_tree = rewind.tree_blob_entries

    def fake_ls_tree(context, oid, paths):
        # Pretend a.txt is a gitlink at the only candidate commit.
        return {'a.txt': ('160000', '0' * 40)}

    monkeypatch.setattr(rewind, 'tree_blob_entries', fake_ls_tree)
    target = rewind.find_rewind_target(
        app_context, 'main', ['a.txt'], _accept
    )
    assert target is None
    # Sanity: real implementation would have matched.
    monkeypatch.setattr(rewind, 'tree_blob_entries', real_ls_tree)
    assert rewind.find_rewind_target(
        app_context, 'main', ['a.txt'], _accept
    ) == c1
