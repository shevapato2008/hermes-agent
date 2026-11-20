"""Legacy bulk maintenance must exclude the real gateway owner reservation."""
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import hermes_state_runtime as rt
from gateway.runtime_ownership import ProfileOwnership
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_cli import gateway_client, gateway_runtime
from hermes_cli.gateway_client import rpc_error
from hermes_cli.web_routers.sessions import manage_router
from hermes_state import SessionDB


@pytest.mark.asyncio
async def test_maintenance_routes_refuse_live_owner_without_changing_rows(tmp_path, monkeypatch):
    monkeypatch.setattr('hermes_state._default_db_path', lambda: tmp_path / 'state.db')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    app = FastAPI()
    app.include_router(manage_router)
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('ended-empty', source='cli')
        db.end_session('ended-empty', 'complete')
        before = db.get_session('ended-empty')
        owner = ProfileOwnership()
        owner.reserve([tmp_path])
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                for method, path, body in [('POST', 'bulk-delete', {'ids': ['ended-empty']}),
                        ('DELETE', 'empty', None), ('POST', 'owner-backfill', {}),
                        ('POST', 'prune', {'older_than_days': 1})]:
                    response = await client.request(method, '/api/sessions/' + path, json=body)
                    assert response.status_code == 409, (path, response.text)
                    assert 'maintenance' in response.json()['detail'].lower()
                    assert db.get_session('ended-empty') == before
        finally:
            owner.close()


@pytest.mark.asyncio
async def test_maintenance_routes_forward_to_live_gateway_owner(tmp_path, monkeypatch):
    monkeypatch.setattr('hermes_state._default_db_path', lambda: tmp_path / 'state.db')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    app = FastAPI()
    app.include_router(manage_router)
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        retired = []
        runner = SimpleNamespace(
            _draining=False,
            session_store=SimpleNamespace(retire_runtime_sessions=retired.extend),
        )
        authority = SessionAuthority(
            runner, profile_id=str(tmp_path), instance_id='owner', db=db, epoch=epoch)

        db.create_session('ended-empty', source='cli')
        db.end_session('ended-empty', 'complete')

        db.create_session('to-bulk-delete', source='cli')
        db.append_message('to-bulk-delete', 'user', 'hello')
        authority.sessions['to-bulk-delete'] = LiveSession(None, 'route-bulk')

        db.create_session('to-prune', source='cli')
        db.append_message('to-prune', 'user', 'old')
        aged = time.time() - 10 * 86400
        db._execute_write(lambda conn: (
            conn.execute('UPDATE sessions SET started_at=?, last_activity_at=?, ended_at=? WHERE id=?',
                         (aged, aged, aged, 'to-prune')),
            conn.execute('UPDATE messages SET timestamp=? WHERE session_id=?', (aged, 'to-prune')),
        ))

        @asynccontextmanager
        async def owner_socket(*_args, **_kwargs):
            identity = {
                'user_id': 'local-operator', 'provider': 'local',
                'profile_id': authority.profile_id, 'instance_id': authority.instance_id,
                'native_bootstrap': True,
                'capabilities': ['session:create', 'session:read', 'session:submit', 'session:control'],
            }
            connection = AuthorityConnection(authority, object(), identity, operator=True)

            async def rpc(method, _timeout=None, **params):
                frame = await connection.dispatch({'id': 1, 'method': method, 'params': params})
                if 'error' in frame:
                    raise rpc_error(frame['error'])
                return frame['result']

            try:
                yield SimpleNamespace(rpc=rpc)
            finally:
                await connection.close()

        monkeypatch.setattr(
            gateway_runtime, 'discover_gateway_endpoint',
            lambda home, **_: gateway_runtime.GatewayDiscovery(
                'ready', endpoint=SimpleNamespace(control_home=str(tmp_path))))
        monkeypatch.setattr(gateway_client, 'connect_gateway', owner_socket)

        owner = ProfileOwnership()
        owner.reserve([tmp_path])
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                empty_res = await client.delete('/api/sessions/empty')
                assert empty_res.status_code == 200, empty_res.text
                assert empty_res.json() == {'ok': True, 'deleted': 1}
                assert db.get_session('ended-empty') is None

                bulk_res = await client.post('/api/sessions/bulk-delete', json={'ids': ['to-bulk-delete']})
                assert bulk_res.status_code == 200, bulk_res.text
                assert bulk_res.json() == {'ok': True, 'deleted': 1, 'skipped_active': []}
                assert db.get_session('to-bulk-delete') is None
                assert 'to-bulk-delete' not in authority.sessions
                assert 'to-bulk-delete' in retired

                prune_res = await client.post('/api/sessions/prune', json={'older_than_days': 1})
                assert prune_res.status_code == 200, prune_res.text
                assert prune_res.json() == {'ok': True, 'removed': 1, 'skipped_open': 0}
                assert db.get_session('to-prune') is None
        finally:
            owner.close()


@pytest.mark.asyncio
async def test_exclusive_offline_maintenance_preserves_bulk_behavior(tmp_path, monkeypatch):
    monkeypatch.setattr('hermes_state._default_db_path', lambda: tmp_path / 'state.db')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    app = FastAPI()
    app.include_router(manage_router)
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('parent', source='cli')
        db.create_session('child', source='cli', parent_session_id='parent')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            result = await client.post('/api/sessions/bulk-delete', json={'ids': ['parent', 'absent']})
            assert result.status_code == 200, result.text
            assert result.json() == {'ok': True, 'deleted': 1, 'skipped_active': []}
            assert db.get_session('parent') is None
            assert db.get_session('child')['parent_session_id'] is None
