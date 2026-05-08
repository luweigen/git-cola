"""UI orchestration for the "Rewind check" feature."""

from __future__ import annotations

import os

from qtpy import QtGui
from qtpy import QtWidgets
from qtpy.QtCore import Qt

from .. import rewind as rewind_logic
from ..git import STDOUT
from ..i18n import N_
from ..interaction import Interaction


def _commit_summary(context, oid: str, max_lines: int = 4) -> str:
    """Return ``"<short-hash> <subject>\\n<rest of up to max_lines>"``.

    The short hash and the commit's subject (message line 1) are joined on
    the same line; any additional message lines (up to ``max_lines`` total)
    follow on their own lines.
    """
    short = context.git.log(
        '-1', '--pretty=%h', oid, _readonly=True
    )[STDOUT].strip() or oid[:12]
    body = context.git.log(
        '-1', '--pretty=%B', oid, _readonly=True
    )[STDOUT]
    lines = body.splitlines()[:max_lines]
    if not lines:
        return short
    first, rest = lines[0], lines[1:]
    head = f'{short} {first}'.rstrip()
    if rest:
        head = head + '\n' + '\n'.join(rest).rstrip()
    return head


def _format_dirty_listing(context, dirty_paths: list[str], limit: int = 20) -> str:
    """Build a human-readable list of dirty paths, marking deletions."""
    worktree = context.git.worktree() or context.git.getcwd()
    lines = []
    for path in dirty_paths[:limit]:
        if worktree and not os.path.lexists(os.path.join(worktree, path)):
            lines.append(f'    D  {path}')
        else:
            lines.append(f'    M  {path}')
    if len(dirty_paths) > limit:
        lines.append(N_('    ... and %d more') % (len(dirty_paths) - limit))
    return '\n'.join(lines)


def run_rewind_check(context, branch: str, dirty_paths: list[str]) -> None:
    """Search ``branch`` history for a commit whose tree matches the working
    tree for ``dirty_paths`` and, on user confirmation, create a backup
    branch and ``git reset --hard`` to that commit.
    """
    if not branch or not dirty_paths:
        return

    def progress(checked: int) -> bool:
        title = N_('Rewind check')
        text = N_('Checked %d commits without finding a match.') % checked
        info = N_('Continue searching?')
        return Interaction.confirm(title, text, info, N_('Continue'))

    QtWidgets.QApplication.setOverrideCursor(QtGui.QCursor(Qt.WaitCursor))
    try:
        target_oid = rewind_logic.find_rewind_target(
            context, branch, dirty_paths, progress
        )
    finally:
        QtWidgets.QApplication.restoreOverrideCursor()

    if not target_oid:
        Interaction.information(
            N_('Rewind check'),
            message=N_('No matching commit was found.'),
        )
        return

    new_branch = rewind_logic.unique_rewind_branch_name(context, branch)
    if not new_branch:
        Interaction.critical(
            N_('Rewind check'),
            message=N_(
                'Could not allocate a backup branch name; please rename '
                'existing rewind-* branches and try again.'
            ),
        )
        return

    summary = _commit_summary(context, target_oid)
    dirty_listing = _format_dirty_listing(context, dirty_paths)
    title = N_('Rewind check')
    question = N_('Rewind %(branch)s to %(summary)s') % {
        'branch': branch,
        'summary': summary,
    }
    info = N_(
        'A backup branch "%(backup)s" will be created at the current HEAD,\n'
        'then "%(branch)s" will be reset --hard to %(oid)s.\n'
        'The dirty files in your worktree match the target commit, so their\n'
        'content is preserved.\n'
        '\n'
        'Dirty files (%(count)d):\n'
        '%(listing)s'
    ) % {
        'backup': new_branch,
        'branch': branch,
        'oid': target_oid[:12],
        'count': len(dirty_paths),
        'listing': dirty_listing,
    }
    if not Interaction.confirm(title, question, info, N_('Rewind')):
        return

    status, _out, err = context.git.branch(new_branch, 'HEAD')
    if status != 0:
        Interaction.critical(
            N_('Rewind check'),
            message=N_('Failed to create backup branch "%s"') % new_branch,
            details=err or '',
        )
        return

    status, _out, err = context.git.reset(target_oid, '--', hard=True)
    if status != 0:
        Interaction.critical(
            N_('Rewind check'),
            message=N_('git reset --hard %s failed') % target_oid,
            details=err or '',
        )
        return

    context.model.update_status()
    Interaction.information(
        N_('Rewind check'),
        message=N_('Reset "%(branch)s" to %(oid)s; backup at "%(backup)s".')
        % {
            'branch': branch,
            'oid': target_oid[:12],
            'backup': new_branch,
        },
    )
