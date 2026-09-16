"""Batch-action tests: recipe CRUD, engine rails, preview/execute gating.
All against the mocked RPC layer - no funded wallet required."""

import hashlib

from tests.conftest import LOCAL, login, unlock


def _b3addr(seed):
    payload = bytes([0x3F]) + hashlib.sha256(seed.encode()).digest()[:20]
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n = int.from_bytes(payload + checksum, 'big')
    B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    s = ''
    while n > 0:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return s


ADDR1 = _b3addr('addr1')
ADDR2 = _b3addr('addr2')
DEST = _b3addr('dest')


def _utxo(txid, amount, conf=5, spendable=True, script=None):
    if script is None:
        script = '76a914' + hashlib.sha256(txid.encode()).hexdigest()[:40] + '88ac'
    return {'txid': txid, 'vout': 0, 'address': ADDR1, 'amount': amount,
            'confirmations': conf, 'spendable': spendable,
            'scriptPubKey': script}


def _recipe():
    return {'name': 'dust sweep', 'filters': {'sources': [ADDR1, ADDR2]},
            'action': {'type': 'consolidate', 'destination': DEST}}


def _post_recipe(client, headers, recipe=None):
    return client.post('/api/batch/recipes', json=recipe or _recipe(),
                       headers=headers)


# ---------------------------------------------------------------------------
# Recipe CRUD
# ---------------------------------------------------------------------------

def test_create_list_get_delete_recipe(client):
    h = login(client)['headers']
    r = _post_recipe(client, h)
    assert r.status_code == 200, r.text
    rid = r.json()['id']

    r = client.get('/api/batch/recipes', headers=h)
    assert r.status_code == 200
    assert any(x['id'] == rid for x in r.json()['recipes'])

    r = client.get(f'/api/batch/recipes/{rid}', headers=h)
    assert r.status_code == 200
    assert r.json()['recipe']['action']['destination'] == DEST

    r = client.delete(f'/api/batch/recipes/{rid}', headers=h)
    assert r.status_code == 200
    r = client.get(f'/api/batch/recipes/{rid}', headers=h)
    assert r.status_code == 404


def test_create_recipe_requires_csrf(client):
    login(client)
    r = client.post('/api/batch/recipes', json=_recipe(), headers=dict(LOCAL))
    assert r.status_code == 403


def test_create_recipe_rejects_invalid(client):
    h = login(client)['headers']
    bad = _recipe()
    bad['action']['destination'] = 'Snotarealaddress'
    assert _post_recipe(client, h, bad).status_code == 422

    bad = _recipe()
    bad['filters']['sources'] = []
    assert _post_recipe(client, h, bad).status_code == 422

    bad = _recipe()
    bad['action']['type'] = 'sendtoaddress'
    assert _post_recipe(client, h, bad).status_code == 422


def test_duplicate_recipe_name_conflict(client):
    h = login(client)['headers']
    assert _post_recipe(client, h).status_code == 200
    assert _post_recipe(client, h).status_code == 409


# ---------------------------------------------------------------------------
# Engine rails: preview selection
# ---------------------------------------------------------------------------

def _seed_utxos(client, utxos):
    client.app.state.app_state.rpc.responses['listunspent'] = utxos


def test_preview_selects_only_p2pkh(client):
    h = login(client)['headers']
    rid = _post_recipe(client, h).json()['id']
    _seed_utxos(client, [
        _utxo('txa', '0.500000000'),
        _utxo('txb', '1.000000000'),
        _utxo('txc', '2.000000000', script='2103deadbeefcafe01ac'),  # carrier
        _utxo('txd', '3.000000000', spendable=False),                 # unspendable
    ])
    r = client.post(f'/api/batch/recipes/{rid}/preview', headers=h)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p['eligible_utxos'] == 2
    assert p['skipped']['script'] == 1
    assert p['skipped']['unspendable'] == 1
    # internal engine fields must never leak to the client
    assert '_chunks' not in p and '_rate' not in p and '_plan_key' not in p
    assert 'confirm_token' in p and len(p['confirm_token']) == 64


def test_preview_min_value_and_sort(client):
    h = login(client)['headers']
    recipe = _recipe()
    recipe['filters']['min_utxo_value'] = '0.400000000'
    rid = _post_recipe(client, h, recipe).json()['id']
    _seed_utxos(client, [
        _utxo('txa', '0.100000000'),
        _utxo('txb', '0.500000000'),
        _utxo('txc', '0.900000000'),
    ])
    p = client.post(f'/api/batch/recipes/{rid}/preview', headers=h).json()
    assert p['eligible_utxos'] == 2
    # total input minus fee rounds down; with 2 inputs and fallback fee
    # the output must be below the 1.4 total input
    total_in = float(p['batches'][0]['total_input'])
    assert 1.3 < total_in < 1.5


# ---------------------------------------------------------------------------
# Execute gating
# ---------------------------------------------------------------------------

def test_execute_requires_wallet_unlock(client):
    h = login(client)['headers']  # logged in, no unlock
    rid = _post_recipe(client, h).json()['id']
    _seed_utxos(client, [_utxo('txa', '0.500000000')])
    r = client.post(f'/api/batch/recipes/{rid}/execute',
                    json={'confirm_token': 'x' * 64}, headers=h)
    assert r.status_code == 423


def test_execute_stale_token_rejected(client):
    h = login(client)['headers']
    unlock(client, h)
    rid = _post_recipe(client, h).json()['id']
    _seed_utxos(client, [_utxo('txa', '0.500000000')])
    p = client.post(f'/api/batch/recipes/{rid}/preview', headers=h).json()
    # UTXO set changes between preview and execute
    _seed_utxos(client, [_utxo('txa', '0.500000000'), _utxo('txb', '0.700000000')])
    r = client.post(f'/api/batch/recipes/{rid}/execute',
                    json={'confirm_token': p['confirm_token']}, headers=h)
    assert r.status_code == 409
    # and nothing was broadcast
    rpc = client.app.state.app_state.rpc
    assert not rpc.called('sendrawtransaction')


def test_execute_happy_path(client):
    h = login(client)['headers']
    unlock(client, h)
    rid = _post_recipe(client, h).json()['id']
    _seed_utxos(client, [_utxo('txa', '0.500000000'), _utxo('txb', '0.700000000')])
    p = client.post(f'/api/batch/recipes/{rid}/preview', headers=h).json()
    r = client.post(f'/api/batch/recipes/{rid}/execute',
                    json={'confirm_token': p['confirm_token']}, headers=h)
    assert r.status_code == 200, r.text
    res = r.json()['results']
    assert len(res) == 1 and res[0]['txid'] == 'deadbeef'
    rpc = client.app.state.app_state.rpc
    assert rpc.called('createrawtransaction')
    assert rpc.called('signrawtransactionwithwallet')
    assert rpc.called('testmempoolaccept')
    assert rpc.called('sendrawtransaction')
    # audit trail exists
    from app import db
    audit = db.audit_list(client.app.state.app_state.settings.db_path)
    assert any(a['action'] == 'batch_execute' for a in audit)
