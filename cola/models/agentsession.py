"""Read the ``refs/agent/session/`` refs written by the agent hooks.

Each agent session records two refs::

    refs/agent/session/{session_id}/base    session start, written once
    refs/agent/session/{session_id}/tip     advanced after every commit

``git log --decorate`` does not decorate ``refs/agent/*`` -- only the well-known
namespaces (``refs/heads``, ``refs/remotes``, ``refs/tags``, ``refs/stash``,
``HEAD``) are decorated by default.  Passing ``--decorate-refs=refs/agent/*``
would work but that option is a *whitelist* which replaces the defaults, so it
also drops the ``HEAD -> `` arrow, and it requires git 2.13 while git-cola
supports git 2.2.  We therefore read these refs ourselves with
``git for-each-ref`` and attach labels to commits by object ID.
"""
from __future__ import annotations
from dataclasses import dataclass

SESSION_PREFIX = 'refs/agent/session/'
"""Default namespace holding the per-session base/tip refs"""

BASE = 'base'
TIP = 'tip'

SHORT_LEN = 8
"""Number of session-id characters shown in a label"""

FIELD_SEP = '\x01'
"""for-each-ref field separator; cannot appear in a ref name or a date"""

REF_FORMAT = FIELD_SEP.join(
    ('%(objectname)', '%(refname)', '%(creatordate:iso-strict)')
)
"""for-each-ref format producing the fields parse_for_each_ref() expects"""


def parse_session_ref(refname: str, prefix: str = SESSION_PREFIX):
    """Split a session ref into its ``(session_id, kind)`` parts.

    ``kind`` is either "base" or "tip".  Returns ``None`` when the ref is not a
    session ref.  The session id may itself contain "/" so only the last path
    component is taken as the kind.

    >>> parse_session_ref('refs/agent/session/abc123/tip')
    ('abc123', 'tip')
    >>> parse_session_ref('refs/agent/session/abc123/base')
    ('abc123', 'base')
    >>> parse_session_ref('refs/agent/session/team/abc123/tip')
    ('team/abc123', 'tip')
    >>> parse_session_ref('refs/agent/session/abc123') is None
    True
    >>> parse_session_ref('refs/agent/session/abc123/head') is None
    True
    >>> parse_session_ref('refs/heads/main') is None
    True
    """
    if not refname.startswith(prefix):
        return None
    tail = refname[len(prefix) :]
    session_id, sep, kind = tail.rpartition('/')
    if not sep or not session_id or kind not in (BASE, TIP):
        return None
    return (session_id, kind)


@dataclass(frozen=True)
class AgentSession:
    """One agent session: where it started and where it currently ends.

    ``base_oid`` or ``tip_oid`` can be ``None``.  A session that only ever ran
    the Stop hook has no base ref, and a session that has not committed
    anything yet has no tip ref.
    """

    session_id: str
    base_oid: str | None = None
    tip_oid: str | None = None
    updated: str = ''

    @property
    def short(self) -> str:
        """The abbreviated session id used in labels.

        >>> AgentSession('b47c8939-8ae6-4c1b-b9b2-f89c387b3e77').short
        'b47c8939'
        """
        return self.session_id[:SHORT_LEN]

    def is_empty(self) -> bool:
        """True when the session recorded no commits at all.

        >>> AgentSession('x', base_oid='a', tip_oid='a').is_empty()
        True
        >>> AgentSession('x', base_oid='a', tip_oid='b').is_empty()
        False
        >>> AgentSession('x', base_oid='a').is_empty()
        True
        """
        return not self.tip_oid or self.tip_oid == self.base_oid


def parse_for_each_ref(lines, prefix: str = SESSION_PREFIX) -> dict[str, AgentSession]:
    """Build sessions from ``for-each-ref`` output.

    Each line is ``<objectname><SEP><refname><SEP><creatordate>``.  The
    session's ``updated`` timestamp is taken from the tip ref, which is the one
    the hooks advance after every commit.

    >>> lines = [
    ...     'aaa\\x01refs/agent/session/s1/base\\x012026-09-05T10:00:00+03:00',
    ...     'bbb\\x01refs/agent/session/s1/tip\\x012026-09-05T12:00:00+03:00',
    ...     'ccc\\x01refs/heads/main\\x012026-09-05T12:00:00+03:00',
    ... ]
    >>> sessions = parse_for_each_ref(lines)
    >>> sorted(sessions)
    ['s1']
    >>> sessions['s1'].base_oid, sessions['s1'].tip_oid
    ('aaa', 'bbb')
    >>> sessions['s1'].updated
    '2026-09-05T12:00:00+03:00'
    """
    base_oids: dict[str, str] = {}
    tip_oids: dict[str, str] = {}
    updated: dict[str, str] = {}
    order: list[str] = []

    for line in lines:
        if not line:
            continue
        fields = line.split(FIELD_SEP)
        if len(fields) < 2:
            continue
        oid = fields[0]
        refname = fields[1]
        date = fields[2] if len(fields) > 2 else ''
        parsed = parse_session_ref(refname, prefix=prefix)
        if parsed is None or not oid:
            continue
        session_id, kind = parsed
        if session_id not in updated:
            order.append(session_id)
            updated[session_id] = ''
        if kind == BASE:
            base_oids[session_id] = oid
        else:
            tip_oids[session_id] = oid
            updated[session_id] = date

    sessions = {}
    for session_id in order:
        sessions[session_id] = AgentSession(
            session_id=session_id,
            base_oid=base_oids.get(session_id),
            tip_oid=tip_oids.get(session_id),
            updated=updated[session_id],
        )
    return sessions


def load_agent_sessions(
    context, prefix: str = SESSION_PREFIX
) -> dict[str, AgentSession]:
    """Return ``{session_id: AgentSession}`` for every session ref in the repo.

    A single ``for-each-ref`` reads both the base and the tip refs.  The
    pattern must be the *prefix* -- ``refs/agent/session/*`` matches nothing
    because ``for-each-ref`` wildcards do not cross "/".
    """
    status, out, _ = context.git.for_each_ref(
        prefix, format=REF_FORMAT, _readonly=True
    )
    if status != 0:
        return {}
    return parse_for_each_ref(out.splitlines(), prefix=prefix)


def labels_by_oid(sessions) -> dict[str, list[tuple[str, str]]]:
    """Map object IDs to the ``(kind, session_id)`` labels anchored there.

    A single commit can carry several labels: the base of one session is very
    often the tip of the previous one.  Tips sort before bases so that a commit
    that ends one session and starts another reads "tip, base" left to right.

    >>> sessions = {
    ...     's1': AgentSession('s1', base_oid='aaa', tip_oid='bbb'),
    ...     's2': AgentSession('s2', base_oid='bbb', tip_oid='ccc'),
    ... }
    >>> labels = labels_by_oid(sessions)
    >>> labels['bbb']
    [('tip', 's1'), ('base', 's2')]
    >>> labels['aaa']
    [('base', 's1')]
    """
    labels: dict[str, list[tuple[str, str]]] = {}
    for session in sessions.values():
        if session.tip_oid:
            labels.setdefault(session.tip_oid, []).append((TIP, session.session_id))
        if session.base_oid:
            labels.setdefault(session.base_oid, []).append((BASE, session.session_id))
    for entries in labels.values():
        entries.sort(key=lambda entry: (entry[0] != TIP, entry[1]))
    return labels


def label_text(kind: str, session_id: str) -> str:
    """Format the text drawn inside a base/tip label box.

    The marker glyph already says which end this is, so the word "tip" /
    "base" is left out: it doubles the label width for nothing.

    >>> label_text('tip', 'b47c8939-8ae6-4c1b')
    '▶ b47c8939'
    >>> label_text('base', 'b47c8939-8ae6-4c1b')
    '⚑ b47c8939'
    """
    marker = '▶' if kind == TIP else '⚑'
    return f'{marker} {session_id[:SHORT_LEN]}'


def session_ref(session_id: str, kind: str, prefix: str = SESSION_PREFIX) -> str:
    """Return the full ref name for one end of a session.

    >>> session_ref('abc123', 'tip')
    'refs/agent/session/abc123/tip'
    >>> session_ref('abc123', 'base')
    'refs/agent/session/abc123/base'
    """
    return f'{prefix}{session_id}/{kind}'


def refresh_key(sessions) -> frozenset:
    """A hashable summary used to detect session-ref changes between refreshes.

    ``GitDAG.display()`` compares branch/tag sets to decide whether a redraw is
    needed.  Session tips move after every agent commit without any branch
    changing, so the tips have to take part in that comparison.

    >>> a = refresh_key({'s': AgentSession('s', base_oid='x', tip_oid='y')})
    >>> b = refresh_key({'s': AgentSession('s', base_oid='x', tip_oid='z')})
    >>> a == b
    False
    """
    return frozenset(
        (session.session_id, session.base_oid, session.tip_oid)
        for session in sessions.values()
    )
