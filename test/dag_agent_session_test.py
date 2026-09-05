"""Tests for reading refs/agent/session/ and labeling DAG commits"""
import pytest

from cola.models import agentsession
from cola.models import dag

from .helper import app_context
from .helper import run_git

# Prevent unused imports lint errors.
assert app_context is not None


SESSION_A = 'b47c8939-8ae6-4c1b-b9b2-f89c387b3e77'
SESSION_B = '23ecce3c-dc37-4f0b-9b3a-0c8a1e2d4f60'


def commit(message):
    """Create a commit touching a file named after the message"""
    run_git('commit', '--allow-empty', '-m', message)
    return run_git('rev-parse', 'HEAD').strip()


def record_session(session_id, base_oid, tip_oid=None):
    """Write the base/tip refs an agent hook would write"""
    prefix = agentsession.SESSION_PREFIX + session_id
    run_git('update-ref', '--create-reflog', prefix + '/base', base_oid)
    if tip_oid:
        run_git('update-ref', '--create-reflog', prefix + '/tip', tip_oid)


def read_commits(context, ref='HEAD', count=1000):
    """Run a RepoReader over the repository and return its commits"""
    params = dag.DAG(ref, count)
    reader = dag.RepoReader(context, params)
    return list(reader.get())


def test_load_agent_sessions_reads_base_and_tip(app_context):
    """for-each-ref finds both sub-refs with a single prefix query"""
    base = commit('one')
    tip = commit('two')
    record_session(SESSION_A, base, tip)

    sessions = agentsession.load_agent_sessions(app_context)

    assert list(sessions) == [SESSION_A]
    session = sessions[SESSION_A]
    assert session.base_oid == base
    assert session.tip_oid == tip
    assert session.updated  # creatordate is populated from the tip ref
    assert not session.is_empty()


def test_load_agent_sessions_without_refs(app_context):
    """A repository with no session refs yields no sessions"""
    commit('one')
    assert agentsession.load_agent_sessions(app_context) == {}


def test_load_agent_sessions_ignores_other_refs(app_context):
    """Branches and tags are not mistaken for session refs"""
    oid = commit('one')
    run_git('tag', 'v1.0')
    run_git('branch', 'topic')
    run_git('update-ref', 'refs/agent/session/stray', oid)

    assert agentsession.load_agent_sessions(app_context) == {}


def test_load_agent_sessions_base_only(app_context):
    """A session that has not committed anything has a base but no tip"""
    base = commit('one')
    record_session(SESSION_A, base)

    session = agentsession.load_agent_sessions(app_context)[SESSION_A]

    assert session.base_oid == base
    assert session.tip_oid is None
    assert session.is_empty()


def test_repo_reader_labels_base_and_tip(app_context):
    """The base and tip commits carry session labels, others do not"""
    base = commit('one')
    middle = commit('two')
    tip = commit('three')
    record_session(SESSION_A, base, tip)
    app_context.model.update_status()

    labels = {c.oid: c.session_labels for c in read_commits(app_context)}

    assert labels[base] == [(agentsession.BASE, SESSION_A)]
    assert labels[tip] == [(agentsession.TIP, SESSION_A)]
    assert labels[middle] == []


def test_repo_reader_labels_back_to_back_sessions(app_context):
    """A commit ending one session and starting the next carries both labels"""
    base = commit('one')
    shared = commit('two')
    tip = commit('three')
    record_session(SESSION_A, base, shared)
    record_session(SESSION_B, shared, tip)
    app_context.model.update_status()

    labels = {c.oid: c.session_labels for c in read_commits(app_context)}

    # Tips sort before bases so the commit reads "tip, base" left to right.
    assert labels[shared] == [
        (agentsession.TIP, SESSION_A),
        (agentsession.BASE, SESSION_B),
    ]


def test_repo_reader_respects_the_disable_flag(app_context):
    """cola.dag.agentsessions=false skips the session refs entirely"""
    base = commit('one')
    tip = commit('two')
    record_session(SESSION_A, base, tip)
    app_context.model.update_status()

    params = dag.DAG('HEAD', 1000)
    params.set_agent_sessions(False)
    reader = dag.RepoReader(app_context, params)
    commits = list(reader.get())

    assert reader.sessions == {}
    assert all(not c.session_labels for c in commits)


def test_repo_reader_unreachable_tip(app_context):
    """A tip that was reset away leaves the visible base labeled on its own"""
    base = commit('one')
    tip = commit('two')
    record_session(SESSION_A, base, tip)
    run_git('reset', '--hard', base)
    app_context.model.update_status()

    commits = read_commits(app_context)
    oids = [c.oid for c in commits]

    assert tip not in oids
    labels = {c.oid: c.session_labels for c in commits}
    assert labels[base] == [(agentsession.BASE, SESSION_A)]


def test_refresh_key_tracks_tip_movement(app_context):
    """Advancing a tip changes the refresh key even when no branch moved"""
    base = commit('one')
    record_session(SESSION_A, base, base)
    before = agentsession.refresh_key(agentsession.load_agent_sessions(app_context))

    tip = commit('two')
    run_git(
        'update-ref', agentsession.SESSION_PREFIX + SESSION_A + '/tip', tip
    )
    after = agentsession.refresh_key(agentsession.load_agent_sessions(app_context))

    assert before != after


@pytest.mark.parametrize(
    'kind,expected',
    [
        # The marker glyph carries "which end", so the word is left out.
        (agentsession.TIP, '▶ b47c8939'),
        (agentsession.BASE, '⚑ b47c8939'),
    ],
)
def test_label_text(kind, expected):
    assert agentsession.label_text(kind, SESSION_A) == expected


@pytest.mark.parametrize(
    'kind,expected',
    [
        (agentsession.TIP, 'refs/agent/session/' + SESSION_A + '/tip'),
        (agentsession.BASE, 'refs/agent/session/' + SESSION_A + '/base'),
    ],
)
def test_session_ref_round_trips(kind, expected):
    """The ref the menu copies parses back to the same session and end"""
    refname = agentsession.session_ref(SESSION_A, kind)
    assert refname == expected
    assert agentsession.parse_session_ref(refname) == (SESSION_A, kind)


def commit_for(session_id, message):
    """Commit with the Agent-Session-Id trailer an agent hook would add"""
    run_git(
        'commit',
        '--allow-empty',
        '-m',
        message,
        '--trailer',
        'Agent-Session-Id: ' + session_id,
    )
    return run_git('rev-parse', 'HEAD').strip()


def threads_for(context, ref='HEAD', count=1000):
    """Read the repository and return the per-session classification"""
    params = dag.DAG(ref, count)
    reader = dag.RepoReader(context, params)
    list(reader.get())
    return reader.threads


def test_range_is_computed_without_git_rev_list(app_context):
    """base..tip comes from the in-memory parent graph, endpoints included"""
    base = commit('one')
    middle = commit('two')
    tip = commit('three')
    app_context.model.update_status()
    commits = {c.oid: c for c in read_commits(app_context)}

    members = agentsession.range_members(commits, base, tip)

    assert members == {middle, tip}


def test_range_excludes_merged_in_ancestors_of_base(app_context):
    """A merge can pull in commits older than base; those are not in range"""
    root = commit('root')
    run_git('checkout', '-q', '-b', 'side')
    side = commit('side work')
    run_git('checkout', '-q', 'main')
    base = commit('base')
    run_git('merge', '-q', '--no-ff', '-m', 'merge side', 'side')
    tip = run_git('rev-parse', 'HEAD').strip()
    app_context.model.update_status()
    commits = {c.oid: c for c in read_commits(app_context, ref='--all')}

    members = agentsession.range_members(commits, base, tip)

    # The side branch is reachable from tip but not from base, so it counts.
    assert side in members
    assert tip in members
    # base and its ancestors do not.
    assert base not in members
    assert root not in members


def test_own_and_foreign(app_context):
    """A commit by somebody else inside base..tip is FOREIGN, not OWN"""
    base = commit('base')
    mine_a = commit_for(SESSION_A, 'agent work')
    theirs = commit('someone else')
    mine_b = commit_for(SESSION_A, 'more agent work')
    record_session(SESSION_A, base, mine_b)
    app_context.model.update_status()

    marks = threads_for(app_context)[SESSION_A].marks

    assert marks[mine_a] == agentsession.Mark.OWN
    assert marks[mine_b] == agentsession.Mark.OWN
    assert marks[theirs] == agentsession.Mark.FOREIGN
    assert base not in marks  # base itself is not part of base..tip


def test_stray_when_the_tip_ref_falls_behind(app_context):
    """A trailer-tagged commit outside base..tip is STRAY

    This is the ``HEAD != tip`` anomaly the UserPromptSubmit hook warns
    about, made visible: the commit exists and says which session made it,
    but the ref that should anchor it does not reach it.
    """
    base = commit('base')
    anchored = commit_for(SESSION_A, 'anchored')
    record_session(SESSION_A, base, anchored)
    orphaned = commit_for(SESSION_A, 'tip ref never advanced to here')
    app_context.model.update_status()

    marks = threads_for(app_context)[SESSION_A].marks

    assert marks[anchored] == agentsession.Mark.OWN
    assert marks[orphaned] == agentsession.Mark.STRAY


def test_without_trailers_everything_in_range_is_own(app_context):
    """git < 2.22 reports no trailers: the range still works, FOREIGN cannot"""
    base = commit('base')
    mine = commit_for(SESSION_A, 'agent work')
    theirs = commit('someone else')
    record_session(SESSION_A, base, theirs)
    app_context.model.update_status()
    commits = {c.oid: c for c in read_commits(app_context)}
    session = agentsession.load_agent_sessions(app_context)[SESSION_A]

    thread = agentsession.build_thread(session, commits, {})

    assert thread.marks == {
        mine: agentsession.Mark.OWN,
        theirs: agentsession.Mark.OWN,
    }


def test_counts(app_context):
    """SessionThread.counts() summarises the three states"""
    base = commit('base')
    commit_for(SESSION_A, 'mine')
    commit('theirs')
    tip = commit_for(SESSION_A, 'mine again')
    record_session(SESSION_A, base, tip)
    app_context.model.update_status()

    counts = threads_for(app_context)[SESSION_A].counts()

    assert counts[agentsession.Mark.OWN] == 2
    assert counts[agentsession.Mark.FOREIGN] == 1
    assert counts[agentsession.Mark.STRAY] == 0


def test_thread_without_tip_ref(app_context):
    """No tip ref means the anchor is missing: tagged commits are STRAY"""
    base = commit('base')
    tagged = commit_for(SESSION_A, 'agent work')
    record_session(SESSION_A, base)
    app_context.model.update_status()

    marks = threads_for(app_context)[SESSION_A].marks

    assert marks == {tagged: agentsession.Mark.STRAY}


def test_thread_without_base_ref(app_context):
    """No base ref means no range to subtract: fall back to the trailer"""
    commit('root')
    tagged = commit_for(SESSION_A, 'agent work')
    commit('someone else')
    tip = commit_for(SESSION_A, 'more agent work')
    run_git(
        'update-ref', agentsession.SESSION_PREFIX + SESSION_A + '/tip', tip
    )
    app_context.model.update_status()

    marks = threads_for(app_context)[SESSION_A].marks

    # Only trailer-tagged commits; FOREIGN cannot be told without a base.
    assert marks == {
        tagged: agentsession.Mark.OWN,
        tip: agentsession.Mark.OWN,
    }


def test_no_sessions_skips_the_trailer_pass(app_context):
    """Without session refs the second git-log pass never runs"""
    commit('one')
    app_context.model.update_status()

    params = dag.DAG('HEAD', 1000)
    reader = dag.RepoReader(app_context, params)
    list(reader.get())

    assert reader.threads == {}
    assert reader._read_trailers(['HEAD']) == {}


def test_session_limit_classifies_only_the_newest(app_context):
    """The classification cap keeps ancestor-set memory bounded"""
    base = commit('base')
    older_tip = commit_for(SESSION_A, 'older session')
    newer_tip = commit_for(SESSION_B, 'newer session')
    record_session(SESSION_A, base, older_tip)
    record_session(SESSION_B, older_tip, newer_tip)
    app_context.model.update_status()

    params = dag.DAG('HEAD', 1000)
    params.set_agent_session_limit(1)
    reader = dag.RepoReader(app_context, params)
    list(reader.get())

    # Both sessions still get their base/tip labels; only the newest is
    # classified.
    assert sorted(reader.sessions) == sorted([SESSION_A, SESSION_B])
    assert list(reader.threads) == [SESSION_B]


def segments_for(context, session_id, ref='HEAD'):
    """Return the (child, parent, mark) edges making up a session's band"""
    params = dag.DAG(ref, 1000)
    reader = dag.RepoReader(context, params)
    commits = {c.oid: c for c in reader.get()}
    return agentsession.thread_segments(reader.threads[session_id], commits)


def test_ribbon_reaches_down_to_base(app_context):
    """base anchors the band even though it is not a member of base..tip"""
    base = commit('base')
    first = commit_for(SESSION_A, 'first')
    tip = commit_for(SESSION_A, 'second')
    record_session(SESSION_A, base, tip)
    app_context.model.update_status()

    segments = segments_for(app_context, SESSION_A)

    assert sorted(segments) == sorted([
        (first, base, agentsession.Mark.OWN),
        (tip, first, agentsession.Mark.OWN),
    ])


def test_ribbon_segment_takes_the_mark_of_its_child(app_context):
    """The edge into a foreign commit is the one that pinches"""
    base = commit('base')
    mine = commit_for(SESSION_A, 'mine')
    theirs = commit('theirs')
    tip = commit_for(SESSION_A, 'mine again')
    record_session(SESSION_A, base, tip)
    app_context.model.update_status()

    marks = dict(
        ((child, parent), mark)
        for child, parent, mark in segments_for(app_context, SESSION_A)
    )

    assert marks[(mine, base)] == agentsession.Mark.OWN
    assert marks[(theirs, mine)] == agentsession.Mark.FOREIGN
    assert marks[(tip, theirs)] == agentsession.Mark.OWN


def test_ribbon_follows_a_merge(app_context):
    """The band follows real parent edges, both sides of a merge included"""
    base = commit('base')
    run_git('checkout', '-q', '-b', 'side')
    side = commit_for(SESSION_A, 'side work')
    run_git('checkout', '-q', 'main')
    trunk = commit_for(SESSION_A, 'trunk work')
    run_git('merge', '-q', '--no-ff', '-m', 'merge side', 'side')
    tip = run_git('rev-parse', 'HEAD').strip()
    record_session(SESSION_A, base, tip)
    app_context.model.update_status()

    edges = {(child, parent) for child, parent, _ in
             segments_for(app_context, SESSION_A, ref='--all')}

    # Both parents of the merge are inside the session, so both are drawn.
    assert (tip, trunk) in edges
    assert (tip, side) in edges
    assert (trunk, base) in edges


def test_branch_name_matches_the_old_branch_layout(app_context):
    """The 'rename' stand-in reproduces agent/{id}.{files} from the tip"""
    from cola.widgets import dag as dagwidget

    commit('base')
    run_git('commit', '--allow-empty', '-m', 'seed')
    with open('notes.md', 'w') as f:
        f.write('note\n')
    with open('_private.txt', 'w') as f:
        f.write('skipped\n')
    run_git('add', 'notes.md', '_private.txt')
    tip = commit_for(SESSION_A, 'agent work')

    basenames = dagwidget._branch_tip_basenames(app_context, tip)

    # Paths starting with "_" are left out by _branch_tip_basenames().
    assert basenames == ['notes.md']
    assert agentsession.branch_name(SESSION_A, basenames) == (
        'agent/' + SESSION_A + '.notes.md'
    )


def test_prune_removes_both_refs(app_context):
    """Pruning drops the refs; the commits and their trailers survive"""
    base = commit('base')
    tip = commit_for(SESSION_A, 'agent work')
    record_session(SESSION_A, base, tip)
    assert SESSION_A in agentsession.load_agent_sessions(app_context)

    for kind in (agentsession.BASE, agentsession.TIP):
        run_git('update-ref', '-d', agentsession.session_ref(SESSION_A, kind))

    assert agentsession.load_agent_sessions(app_context) == {}
    # The commit is untouched, so the session is still identifiable and the
    # refs can be rebuilt from the trailer.
    trailer = run_git(
        'log', '-1', '--format=%(trailers:key=Agent-Session-Id,valueonly)', tip
    )
    assert SESSION_A in trailer


def test_is_recent_filters_by_tip_date(app_context):
    """The panel's 'Recent only' box uses the tip commit date"""
    import datetime

    base = commit('base')
    tip = commit('tip')
    record_session(SESSION_A, base, tip)
    session = agentsession.load_agent_sessions(app_context)[SESSION_A]

    updated = agentsession.parse_updated(session)
    assert updated is not None
    long_after = updated + datetime.timedelta(days=90)

    assert agentsession.is_recent(session, days=30, now=updated)
    assert not agentsession.is_recent(session, days=30, now=long_after)
    assert agentsession.is_recent(session, days=0, now=long_after)


OLD_COAUTHOR = 'claude code/claude-opus-5/%s <noreply@anthropic.com>'


def commit_old_format(session_id, message):
    """Commit with the pre-2026.8.31 three-part Co-authored-by trailer"""
    run_git(
        'commit',
        '--allow-empty',
        '-m',
        message,
        '--trailer',
        'Co-authored-by: ' + OLD_COAUTHOR % session_id,
    )
    return run_git('rev-parse', 'HEAD').strip()


def test_agent_notation_narrows_to_one_session(app_context):
    """agent:<id> in the revision arguments expands to base..tip"""
    commit('root')
    base = commit('base')
    commit_for(SESSION_A, 'first')
    tip = commit_for(SESSION_A, 'second')
    record_session(SESSION_A, base, tip)
    commit('later, unrelated')
    app_context.model.update_status()

    commits = read_commits(app_context, ref='agent:' + SESSION_A[:8])

    assert [c.summary for c in commits] == ['first', 'second']


def test_agent_notation_drops_unusable_ids(app_context):
    """An id that matches nothing must not make git fail on everything"""
    commit('root')
    app_context.model.update_status()
    sessions = {}

    assert agentsession.expand_ref_args(['agent:nope'], sessions) == []
    assert agentsession.expand_ref_args(['main'], sessions) == ['main']


def test_cli_agent_session_flag_replaces_the_revision_args():
    """--agent-session <id> means look at that session and nothing else"""
    from cola import dag as dagmain

    args = dagmain.parse_args(argv=['--agent-session', 'abc123'])
    params = dag.DAG('main --', 100)
    params.set_arguments(args)

    assert params.ref == 'agent:abc123'


def test_cli_no_agent_sessions_flag():
    """--no-agent-sessions turns the whole feature off"""
    from cola import dag as dagmain

    args = dagmain.parse_args(argv=['--no-agent-sessions'])
    params = dag.DAG('main --', 100)
    params.set_arguments(args)

    assert params.agent_sessions is False


def test_rebuild_recreates_refs_from_new_trailers(app_context):
    """Refs can be rebuilt after a prune, from the trailers alone"""
    from cola.widgets import dag as dagwidget

    base = commit('base')
    commit_for(SESSION_A, 'first')
    tip = commit_for(SESSION_A, 'second')
    app_context.model.update_status()
    assert agentsession.load_agent_sessions(app_context) == {}

    count = dagwidget.rebuild_session_refs(app_context)

    assert count == 1
    session = agentsession.load_agent_sessions(app_context)[SESSION_A]
    assert session.base_oid == base
    assert session.tip_oid == tip


def test_rebuild_reads_the_old_coauthor_format(app_context):
    """Repositories predating Agent-Session-Id rebuild without a rewrite"""
    from cola.widgets import dag as dagwidget

    base = commit('base')
    commit_old_format(SESSION_B, 'old style one')
    tip = commit_old_format(SESSION_B, 'old style two')
    app_context.model.update_status()

    count = dagwidget.rebuild_session_refs(app_context)

    assert count == 1
    session = agentsession.load_agent_sessions(app_context)[SESSION_B]
    assert session.base_oid == base
    assert session.tip_oid == tip


def test_rebuild_leaves_existing_refs_alone(app_context):
    """Live refs know things the trailers do not, so they are not overwritten"""
    from cola.widgets import dag as dagwidget

    base = commit('base')
    first = commit_for(SESSION_A, 'first')
    commit_for(SESSION_A, 'second')
    # A hook-written tip that deliberately lags the newest tagged commit.
    record_session(SESSION_A, base, first)

    count = dagwidget.rebuild_session_refs(app_context)

    assert count == 0
    session = agentsession.load_agent_sessions(app_context)[SESSION_A]
    assert session.tip_oid == first


def test_range_does_not_leak_an_offscreen_base(app_context):
    """With base off screen the range must not pick it up as FOREIGN

    ``agent:<id>`` narrows the view to ``base..tip``, which puts base one
    commit past the edge. ancestors(base) is then empty, so anything
    ancestors(tip) reported outside the visible set could not be subtracted.
    """
    base = commit('base')
    first = commit_for(SESSION_A, 'first')
    tip = commit_for(SESSION_A, 'second')
    record_session(SESSION_A, base, tip)
    app_context.model.update_status()

    params = dag.DAG('agent:' + SESSION_A, 1000)
    reader = dag.RepoReader(app_context, params)
    oids = [c.oid for c in reader.get()]

    assert base not in oids
    marks = reader.threads[SESSION_A].marks
    assert marks == {
        first: agentsession.Mark.OWN,
        tip: agentsession.Mark.OWN,
    }
