import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from routes.auth import get_current_user
from services.store import _load_user_store, _save_user_store

router = APIRouter()

_WS_LIMIT = {
    "free": 1, "starter": 1, "growth": 1, "basic": 1,
    "pro":  1, "elite":   1, "personal": 1, "agency": 10,
}


def _ensure_default_workspace(ustore: dict) -> dict:
    if not ustore.get('workspaces'):
        ws_id = 'ws_' + str(uuid.uuid4())[:8]
        ustore['workspaces'] = [{
            'id':         ws_id,
            'name':       'My Workspace',
            'niche':      ustore.get('automation', {}).get('niche', ''),
            'platforms':  [],
            'created_at': datetime.utcnow().isoformat(),
        }]
        ustore['active_workspace'] = ws_id
    return ustore


@router.get('/api/workspaces')
def list_workspaces(current_user: dict = Depends(get_current_user)):
    uid    = current_user['id']
    ustore = _load_user_store(uid)
    ustore = _ensure_default_workspace(ustore)
    _save_user_store(uid, ustore)
    plan  = ustore.get('billing', {}).get('plan', 'free')
    limit = _WS_LIMIT.get(plan, 1)
    return {
        'workspaces':       ustore['workspaces'],
        'active_workspace': ustore.get('active_workspace', ustore['workspaces'][0]['id']),
        'limit':            limit,
    }


@router.post('/api/workspaces')
async def create_workspace(req: Request, current_user: dict = Depends(get_current_user)):
    body   = await req.json()
    uid    = current_user['id']
    ustore = _load_user_store(uid)
    ustore = _ensure_default_workspace(ustore)
    plan   = ustore.get('billing', {}).get('plan', 'free')
    limit  = _WS_LIMIT.get(plan, 1)
    if len(ustore['workspaces']) >= limit:
        raise HTTPException(status_code=403, detail='workspace_limit_reached')
    ws_id = 'ws_' + str(uuid.uuid4())[:8]
    ws = {
        'id':         ws_id,
        'name':       str(body.get('name', 'New Workspace'))[:60].strip() or 'New Workspace',
        'niche':      str(body.get('niche', ''))[:80],
        'platforms':  body.get('platforms', []),
        'created_at': datetime.utcnow().isoformat(),
    }
    ustore['workspaces'].append(ws)
    _save_user_store(uid, ustore)
    return {'ok': True, 'workspace': ws}


@router.patch('/api/workspaces/{wid}')
async def update_workspace(wid: str, req: Request, current_user: dict = Depends(get_current_user)):
    body   = await req.json()
    uid    = current_user['id']
    ustore = _load_user_store(uid)
    ws     = next((w for w in ustore.get('workspaces', []) if w['id'] == wid), None)
    if not ws:
        raise HTTPException(status_code=404, detail='Workspace not found')
    if 'name' in body:
        ws['name']  = str(body['name'])[:60].strip() or ws['name']
    if 'niche' in body:
        ws['niche'] = str(body['niche'])[:80]
    _save_user_store(uid, ustore)
    return {'ok': True, 'workspace': ws}


@router.delete('/api/workspaces/{wid}')
def delete_workspace(wid: str, current_user: dict = Depends(get_current_user)):
    uid    = current_user['id']
    ustore = _load_user_store(uid)
    wss    = ustore.get('workspaces', [])
    if len(wss) <= 1:
        raise HTTPException(status_code=400, detail='Cannot delete the last workspace')
    ustore['workspaces'] = [w for w in wss if w['id'] != wid]
    if ustore.get('active_workspace') == wid:
        ustore['active_workspace'] = ustore['workspaces'][0]['id']
    _save_user_store(uid, ustore)
    return {'ok': True}


@router.post('/api/workspaces/{wid}/switch')
def switch_workspace_ep(wid: str, current_user: dict = Depends(get_current_user)):
    uid    = current_user['id']
    ustore = _load_user_store(uid)
    if not any(w['id'] == wid for w in ustore.get('workspaces', [])):
        raise HTTPException(status_code=404, detail='Workspace not found')
    ustore['active_workspace'] = wid
    _save_user_store(uid, ustore)
    return {'ok': True, 'active_workspace': wid}
