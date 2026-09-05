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
