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
import hashlib
from dataclasses import dataclass
from dataclasses import field
from enum import Enum

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

TRAILER_KEY = 'Agent-Session-Id'
"""Commit trailer the agent hooks write on every commit"""

TRAILER_SEP = '\x02'
"""Separates several trailer values on one commit; distinct from FIELD_SEP"""

TRAILER_FORMAT = (
    'format:%H'
    + FIELD_SEP
    + '%(trailers:key='
    + TRAILER_KEY
    + ',valueonly,separator=%x02)'
)
"""git-log format for the trailer-only second pass.

``separator=`` is not optional: without it the trailers atom appends a newline
after the value, which would run into the next record.
"""

TRAILER_VERSION_KEY = 'trailers-key'
"""cola.version feature key: %(trailers:key=...) needs git 2.22"""


class Mark(Enum):
    """How a commit relates to one agent session.

    The three states are the point of the whole feature: they turn the
    "is HEAD still where I left it" invariant into something visible.
    """

    OWN = 0
    """In base..tip and the trailer names this session"""

    FOREIGN = 1
    """In base..tip but the trailer names someone else, or is absent.

    Somebody committed in the middle of the session.
    """

    STRAY = 2
    """The trailer names this session but the commit is outside base..tip.

    A rewind, a reset, or a cherry-pick moved the commit out from under the
    tip ref.
    """


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

    Sorted newest first: a session's tip always points at a commit at least
    as new as its base, so ``-creatordate`` over the flat ref list orders the
    sessions by tip.  Callers that cap how many sessions they classify take
    the first N.
    """
    status, out, _ = context.git.for_each_ref(
        prefix, format=REF_FORMAT, sort='-creatordate', _readonly=True
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


def parse_trailers(lines) -> dict[str, list[str]]:
    """Build ``{oid: [session_id, ...]}`` from the trailer-only log pass.

    Each line is ``<oid><FIELD_SEP><value>[<TRAILER_SEP><value>...]``.
    Commits without the trailer are left out entirely.

    >>> lines = [
    ...     'aaa\x01b47c8939',
    ...     'bbb\x01',
    ...     'ccc\x0123ecce3c\x02b47c8939',
    ... ]
    >>> result = parse_trailers(lines)
    >>> sorted(result)
    ['aaa', 'ccc']
    >>> result['ccc']
    ['23ecce3c', 'b47c8939']
    """
    trailers: dict[str, list[str]] = {}
    for line in lines:
        oid, sep, value = line.partition(FIELD_SEP)
        if not sep or not oid:
            continue
        session_ids = [part.strip() for part in value.split(TRAILER_SEP)]
        session_ids = [part for part in session_ids if part]
        if session_ids:
            trailers[oid] = session_ids
    return trailers


def ancestors(commits_by_oid, oid: str | None) -> set[str]:
    """Object IDs reachable from ``oid`` along parent edges, ``oid`` included.

    Walks the in-memory graph the DAG already read, so no ``git rev-list``
    process is needed.  Parents outside the visible history simply stop the
    walk: a truncated history yields a truncated ancestor set, which is the
    right answer for what is on screen.

    >>> from collections import namedtuple
    >>> Node = namedtuple('Node', 'oid parents')
    >>> a = Node('a', [])
    >>> b = Node('b', [a])
    >>> c = Node('c', [b])
    >>> commits = {node.oid: node for node in (a, b, c)}
    >>> sorted(ancestors(commits, 'c'))
    ['a', 'b', 'c']
    >>> sorted(ancestors(commits, 'a'))
    ['a']
    >>> sorted(ancestors(commits, None))
    []
    """
    seen: set[str] = set()
    if not oid or oid not in commits_by_oid:
        return seen
    stack = [oid]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        commit = commits_by_oid.get(current)
        if commit is None:
            continue
        for parent in commit.parents:
            if parent.oid not in seen:
                stack.append(parent.oid)
    return seen


def range_members(
    commits_by_oid, base_oid: str | None, tip_oid: str | None, cache=None
) -> set[str]:
    """``base..tip`` -- reachable from tip, not reachable from base.

    The subtraction matters: a merge can pull in commits that are older than
    base, and "stop the walk when base is reached" would get those wrong.
    ``cache`` is an optional ``{oid: ancestor set}`` dict shared across
    sessions, which pays off because back-to-back sessions share endpoints.

    >>> from collections import namedtuple
    >>> Node = namedtuple('Node', 'oid parents')
    >>> a = Node('a', [])
    >>> b = Node('b', [a])
    >>> c = Node('c', [b])
    >>> commits = {node.oid: node for node in (a, b, c)}
    >>> sorted(range_members(commits, 'a', 'c'))
    ['b', 'c']
    >>> sorted(range_members(commits, 'c', 'c'))
    []
    >>> sorted(range_members(commits, None, 'b'))
    ['a', 'b']
    """

    def reachable(oid):
        if cache is None:
            return ancestors(commits_by_oid, oid)
        if oid not in cache:
            cache[oid] = ancestors(commits_by_oid, oid)
        return cache[oid]

    members = set(reachable(tip_oid))
    if base_oid:
        members -= reachable(base_oid)
    return members


@dataclass(frozen=True)
class SessionThread:
    """One session's commits, each tagged with how it relates to the session.

    ``base_oid`` is carried alongside the marks because the ribbon has to
    reach down to it: base is the session's anchor but is *not* a member of
    ``base..tip``, so it never appears in ``marks``.
    """

    session_id: str
    marks: dict[str, Mark] = field(default_factory=dict)
    base_oid: str | None = None
    tip_oid: str | None = None

    def oids(self, mark: Mark) -> list[str]:
        """Object IDs carrying the given mark"""
        return [oid for oid, value in self.marks.items() if value == mark]

    def counts(self) -> dict[Mark, int]:
        """How many commits carry each mark.

        >>> thread = SessionThread('s', {'a': Mark.OWN, 'b': Mark.FOREIGN})
        >>> counts = thread.counts()
        >>> counts[Mark.OWN], counts[Mark.FOREIGN], counts[Mark.STRAY]
        (1, 1, 0)
        """
        result = {mark: 0 for mark in Mark}
        for mark in self.marks.values():
            result[mark] += 1
        return result


def build_thread(session, commits_by_oid, trailers, cache=None) -> SessionThread:
    """Classify every visible commit that belongs to ``session``.

    ``trailers`` maps an object ID to the session ids its
    ``Agent-Session-Id`` trailers name; it is empty when git is too old to
    report trailers, in which case everything in range is reported as OWN
    because there is no evidence to say otherwise.

    Two degenerate ref states get deliberate answers:

    - **no tip ref** -- the session recorded nothing, so any commit carrying
      its trailer is STRAY: the ref that should anchor it is missing.
    - **no base ref** -- there is nothing to subtract, so the range would be
      the whole history.  Fall back to trailer-only membership; FOREIGN
      cannot be detected without a base.
    """
    session_id = session.session_id
    marks: dict[str, Mark] = {}

    tagged = {
        oid
        for oid, session_ids in trailers.items()
        if session_id in session_ids and oid in commits_by_oid
    }
    have_trailers = bool(trailers)

    if not session.tip_oid:
        for oid in tagged:
            marks[oid] = Mark.STRAY
        return SessionThread(
            session_id=session_id,
            marks=marks,
            base_oid=session.base_oid,
            tip_oid=session.tip_oid,
        )

    if not session.base_oid:
        for oid in tagged & ancestors(commits_by_oid, session.tip_oid):
            marks[oid] = Mark.OWN
        for oid in tagged - set(marks):
            marks[oid] = Mark.STRAY
        return SessionThread(
            session_id=session_id,
            marks=marks,
            base_oid=session.base_oid,
            tip_oid=session.tip_oid,
        )

    members = range_members(
        commits_by_oid, session.base_oid, session.tip_oid, cache=cache
    )
    for oid in members:
        if not have_trailers or oid in tagged:
            marks[oid] = Mark.OWN
        else:
            marks[oid] = Mark.FOREIGN
    for oid in tagged - members:
        marks[oid] = Mark.STRAY

    return SessionThread(
            session_id=session_id,
            marks=marks,
            base_oid=session.base_oid,
            tip_oid=session.tip_oid,
        )


def thread_segments(thread, commits_by_oid):
    """Parent edges that make up one session's band.

    Returns ``(child_oid, parent_oid, mark)`` for every parent edge whose
    two ends are both part of the session, so a band drawn from these
    follows the real history instead of a straight base-to-tip line -- it
    stays truthful across a merge.

    The base commit counts as an end even though it is not a member of
    ``base..tip``: it is what the band has to reach down to. A segment takes
    the mark of its *child*, the newer of the two, so a stray commit hanging
    off the tip draws its own edge dashed.

    >>> from collections import namedtuple
    >>> Node = namedtuple('Node', 'oid parents')
    >>> a = Node('a', [])
    >>> b = Node('b', [a])
    >>> c = Node('c', [b])
    >>> commits = {node.oid: node for node in (a, b, c)}
    >>> thread = SessionThread(
    ...     's', {'b': Mark.OWN, 'c': Mark.OWN}, base_oid='a', tip_oid='c'
    ... )
    >>> sorted(thread_segments(thread, commits))
    [('b', 'a', <Mark.OWN: 0>), ('c', 'b', <Mark.OWN: 0>)]

    Without a base ref the band simply stops at the oldest member:

    >>> thread = SessionThread('s', {'c': Mark.OWN}, tip_oid='c')
    >>> thread_segments(thread, commits)
    []
    """
    marks = thread.marks
    if not marks:
        return []
    covered = set(marks)
    if thread.base_oid:
        covered.add(thread.base_oid)

    segments = []
    for oid, mark in marks.items():
        commit = commits_by_oid.get(oid)
        if commit is None:
            continue
        for parent in commit.parents:
            if parent.oid in covered and parent.oid in commits_by_oid:
                segments.append((oid, parent.oid, mark))
    return segments


def session_hue(session_id: str) -> int:
    """A stable 0-359 hue for a session id.

    Derived from the id rather than assigned in encounter order, so a session
    keeps the same ribbon color across restarts and regardless of which other
    sessions happen to be visible.  Kept in the model, as a plain integer, so
    this file stays free of Qt.

    >>> session_hue('b47c8939-8ae6-4c1b-b9b2-f89c387b3e77')
    212
    >>> session_hue('a') == session_hue('a')
    True
    >>> 0 <= session_hue('') < 360
    True
    """
    digest = hashlib.sha256(session_id.encode('utf-8')).digest()
    return (digest[0] << 8 | digest[1]) * 360 // 65536


DEFAULT_LIMIT = 10
"""How many sessions get classified by default; see build_threads()"""


def build_threads(
    sessions, commits_by_oid, trailers, limit: int = DEFAULT_LIMIT
) -> dict[str, SessionThread]:
    """Run build_thread() for the newest ``limit`` sessions.

    The cap is not cosmetic.  Each session needs the ancestor sets of its two
    endpoints, and the shared cache that keeps this fast holds one set of
    object IDs per endpoint: measured at 9352 commits, 51 sessions cost 88ms
    and roughly 8MB of cached sets, and both grow with
    ``sessions x commits``.  A repository with hundreds of sessions and a
    long history would pay seconds and hundreds of megabytes for threads the
    user cannot see anyway.

    ``sessions`` is expected newest-first, as load_agent_sessions() returns
    it.  ``limit <= 0`` means no cap.

    >>> sessions = {'a': AgentSession('a'), 'b': AgentSession('b')}
    >>> sorted(build_threads(sessions, {}, {}, limit=1))
    ['a']
    >>> sorted(build_threads(sessions, {}, {}, limit=0))
    ['a', 'b']
    """
    selected = list(sessions.items())
    if limit > 0:
        selected = selected[:limit]
    cache: dict[str, set[str]] = {}
    return {
        session_id: build_thread(session, commits_by_oid, trailers, cache=cache)
        for session_id, session in selected
    }
