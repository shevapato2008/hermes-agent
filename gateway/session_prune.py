"""``session.prune``: the owner runs `hermes sessions prune` for its own store.

The CLI computes the filters and shows the preview from a read-only view; the delete itself is
this owner's write, through the same ``prune_sessions`` / ``retire_prunable`` path housekeeping
uses (busy sessions and the logical owner of a retained reset child are skipped). A CLI writing
the store under a live gateway would be a second writer, so it never does.
"""
import asyncio
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError

_SCALARS = (type(None), bool, int, float, str)


def _filters(raw):
    from hermes_state_maintenance import _PRUNE_FILTER_NAMES
    if (not isinstance(raw, dict) or set(raw) - (_PRUNE_FILTER_NAMES | {'older_than_days'})
            or not all(isinstance(value, _SCALARS) for value in raw.values())):
        raise RuntimeStoreError('invalid_params')
    return dict(raw)


def _ids(raw):
    if (not isinstance(raw, list) or len(raw) > 500
            or not all(isinstance(sid, str) and sid for sid in raw)):
        raise RuntimeStoreError('invalid_params')
    return list(raw)


async def prune(connection, ref, params):
    authority, actor = connection.authority, connection.actor
    # Store-wide deletion across every principal's rows: the local operator only (a ticket from
    # this host's private control socket), never a remote or browser grant.
    if not connection.native_owner or 'session:operator' not in actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    if actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    has_filters = params.get('filters') is not None
    never_active = params.get('never_active_days')
    empty = params.get('empty')
    has_ids = params.get('ids') is not None
    if (empty not in (None, True)
            or sum((has_filters, never_active is not None, empty is True, has_ids)) != 1
            or (never_active is not None and (isinstance(never_active, bool)
                                              or not isinstance(never_active, (int, float))
                                              or never_active < 0))):
        raise RuntimeStoreError('invalid_params')
    filters = _filters(params['filters']) if has_filters else None
    ids = _ids(params['ids']) if has_ids else None
    authority._require_admission_open()
    from gateway.session_runtime_workers import track_mutation
    task = track_mutation(authority, _apply(authority, filters, never_active, empty=bool(empty), ids=ids))
    return await asyncio.shield(task)


async def _apply(authority, filters=None, never_active=None, *, empty=False, ids=None):
    from gateway.session_mutations import retire_live_sessions
    # Resolve from the profile authority's own db_path so any served profile unlinks its own transcripts.
    sessions_dir = Path(authority.db.db_path).parent / 'sessions'
    deleted_ids = []
    if empty:
        deleted = await asyncio.to_thread(
            authority.db.delete_empty_sessions, sessions_dir=sessions_dir, deleted_ids=deleted_ids)
        result = {'deleted': deleted}
    elif ids is not None:
        skipped_active = []
        deleted = await asyncio.to_thread(
            authority.db.delete_sessions, ids, sessions_dir=sessions_dir,
            exclude_active_write_guards=True, skipped_ids=skipped_active,
            include_compression_chain=True, deleted_ids=deleted_ids)
        result = {'deleted': deleted, 'skipped_active': skipped_active}
    elif filters is None:
        deleted, routing, skipped = await asyncio.to_thread(
            authority.db.prune_never_active_keyed_sessions, older_than_days=float(never_active),
            sessions_dir=sessions_dir, deleted_ids=deleted_ids)
        result = {'deleted': deleted, 'routing_deleted': routing, 'skipped': skipped}
    else:
        deleted = await asyncio.to_thread(
            authority.db.prune_sessions, sessions_dir=sessions_dir, exclude_active_write_guards=True,
            deleted_ids=deleted_ids, **filters)
        result = {'deleted': deleted}
    retire_live_sessions(authority, deleted_ids)
    return result
