from base64 import b64encode as b64enc
from hashlib import sha256
from uuid import uuid4
from os.path import join, dirname
from os import getenv
from fastapi import FastAPI, HTTPException
from fastapi.requests import Request
from fastapi.encoders import jsonable_encoder
import json
from datetime import datetime
from dateutil.relativedelta import relativedelta
from calendar import timegm
from jose import jws, jwk, jwt
from jose.constants import ALGORITHMS
from starlette.responses import StreamingResponse, JSONResponse
import dataset
from Crypto.PublicKey import RSA
from Crypto.PublicKey.RSA import RsaKey


def load_file(filename) -> bytes:
    with open(filename, 'rb') as file:
        content = file.read()
    return content


def load_key(filename) -> RsaKey:
    return RSA.import_key(extern_key=load_file(filename), passphrase=None)


# todo: initialize certificate (or should be done by user, and passed through "volumes"?)

app, db = FastAPI(), dataset.connect(str(getenv('DATABASE', 'sqlite:///db.sqlite')))

TOKEN_EXPIRE_DELTA = relativedelta(hours=1)  # days=1
LEASE_EXPIRE_DELTA = relativedelta(days=int(getenv('LEASE_EXPIRE_DAYS', 90)))

DLS_URL = str(getenv('DLS_URL', 'localhost'))
DLS_PORT = int(getenv('DLS_PORT', '443'))
SITE_KEY_XID = getenv('SITE_KEY_XID', '00000000-0000-0000-0000-000000000000')
INSTANCE_KEY_RSA = load_key(join(dirname(__file__), 'cert/instance.private.pem'))
INSTANCE_KEY_PUB = load_key(join(dirname(__file__), 'cert/instance.public.pem'))

jwt_encode_key = jwk.construct(INSTANCE_KEY_RSA.export_key().decode('utf-8'), algorithm=ALGORITHMS.RS256)
jwt_decode_key = jwk.construct(INSTANCE_KEY_PUB.export_key().decode('utf-8'), algorithm=ALGORITHMS.RS512)


def get_token(request: Request) -> dict:
    authorization_header = request.headers['authorization']
    token = authorization_header.split(' ')[1]
    return jwt.decode(token=token, key=jwt_decode_key, algorithms='RS256', options={'verify_aud': False})


@app.get('/')
async def index():
    return JSONResponse({'hello': 'world'})


@app.get('/status')
async def status(request: Request):
    return JSONResponse({'status': 'up'})


@app.get('/-/origins')
async def _origins(request: Request):
    response = list(map(lambda x: jsonable_encoder(x), db['origin'].all()))
    return JSONResponse(response)


@app.get('/-/leases')
async def _leases(request: Request):
    response = list(map(lambda x: jsonable_encoder(x), db['lease'].all()))
    return JSONResponse(response)


# venv/lib/python3.9/site-packages/nls_core_service_instance/service_instance_token_manager.py
@app.get('/client-token')
async def client_token():
    cur_time = datetime.utcnow()
    exp_time = cur_time + relativedelta(years=12)

    service_instance_public_key_configuration = {
        "service_instance_public_key_me": {
            "mod": hex(INSTANCE_KEY_PUB.public_key().n)[2:],
            "exp": INSTANCE_KEY_PUB.public_key().e,
        },
        "service_instance_public_key_pem": INSTANCE_KEY_PUB.export_key().decode('utf-8'),
        "key_retention_mode": "LATEST_ONLY"
    }

    payload = {
        "jti": str(uuid4()),
        "iss": "NLS Service Instance",
        "aud": "NLS Licensed Client",
        "iat": timegm(cur_time.timetuple()),
        "nbf": timegm(cur_time.timetuple()),
        "exp": timegm(exp_time.timetuple()),
        "update_mode": "ABSOLUTE",
        "scope_ref_list": [str(uuid4())],
        "fulfillment_class_ref_list": [],
        "service_instance_configuration": {
            "nls_service_instance_ref": "00000000-0000-0000-0000-000000000000",
            "svc_port_set_list": [
                {
                    "idx": 0,
                    "d_name": "DLS",
                    "svc_port_map": [{"service": "auth", "port": DLS_PORT}, {"service": "lease", "port": DLS_PORT}]
                }
            ],
            "node_url_list": [{"idx": 0, "url": DLS_URL, "url_qr": DLS_URL, "svc_port_set_idx": 0}]
        },
        "service_instance_public_key_configuration": service_instance_public_key_configuration,
    }

    content = jws.sign(payload, key=jwt_encode_key, headers=None, algorithm='RS256')

    response = StreamingResponse(iter([content]), media_type="text/plain")
    filename = f'client_configuration_token_{datetime.now().strftime("%d-%m-%y-%H-%M-%S")}'
    response.headers["Content-Disposition"] = f'attachment; filename={filename}'

    return response


# venv/lib/python3.9/site-packages/nls_services_auth/test/test_origins_controller.py
# {"candidate_origin_ref":"00112233-4455-6677-8899-aabbccddeeff","environment":{"fingerprint":{"mac_address_list":["ff:ff:ff:ff:ff:ff"]},"hostname":"my-hostname","ip_address_list":["192.168.178.123","fe80::","fe80::1%enp6s18"],"guest_driver_version":"510.85.02","os_platform":"Debian GNU/Linux 11 (bullseye) 11","os_version":"11 (bullseye)"},"registration_pending":false,"update_pending":false}
@app.post('/auth/v1/origin')
async def auth_origin(request: Request):
    j = json.loads((await request.body()).decode('utf-8'))

    origin_ref = j['candidate_origin_ref']
    print(f'> [  origin  ]: {origin_ref}: {j}')

    data = dict(
        origin_ref=origin_ref,
        hostname=j['environment']['hostname'],
        guest_driver_version=j['environment']['guest_driver_version'],
        os_platform=j['environment']['os_platform'], os_version=j['environment']['os_version'],
    )

    db['origin'].upsert(data, ['origin_ref'])

    cur_time = datetime.utcnow()
    response = {
        "origin_ref": origin_ref,
        "environment": j['environment'],
        "svc_port_set_list": None,
        "node_url_list": None,
        "node_query_order": None,
        "prompts": None,
        "sync_timestamp": cur_time.isoformat()
    }

    return JSONResponse(response)


# venv/lib/python3.9/site-packages/nls_services_auth/test/test_auth_controller.py
# venv/lib/python3.9/site-packages/nls_core_auth/auth.py - CodeResponse
# {"code_challenge":"...","origin_ref":"00112233-4455-6677-8899-aabbccddeeff"}
@app.post('/auth/v1/code')
async def auth_code(request: Request):
    j = json.loads((await request.body()).decode('utf-8'))

    origin_ref = j['origin_ref']
    print(f'> [   code   ]: {origin_ref}: {j}')

    cur_time = datetime.utcnow()
    delta = relativedelta(minutes=15)
    expires = cur_time + delta

    payload = {
        'iat': timegm(cur_time.timetuple()),
        'exp': timegm(expires.timetuple()),
        'challenge': j['code_challenge'],
        'origin_ref': j['code_challenge'],
        'key_ref': SITE_KEY_XID,
        'kid': SITE_KEY_XID
    }

    auth_code = jws.sign(payload, key=jwt_encode_key, headers={'kid': payload.get('kid')}, algorithm='RS256')

    db['auth'].delete(origin_ref=origin_ref, expires={'<=': cur_time - delta})
    db['auth'].insert(dict(origin_ref=origin_ref, code_challenge=j['code_challenge'], expires=expires))

    response = {
        "auth_code": auth_code,
        "sync_timestamp": cur_time.isoformat(),
        "prompts": None
    }

    return JSONResponse(response)


# venv/lib/python3.9/site-packages/nls_services_auth/test/test_auth_controller.py
# venv/lib/python3.9/site-packages/nls_core_auth/auth.py - TokenResponse
# {"auth_code":"...","code_verifier":"..."}
@app.post('/auth/v1/token')
async def auth_token(request: Request):
    j = json.loads((await request.body()).decode('utf-8'))
    payload = jwt.decode(token=j['auth_code'], key=jwt_decode_key)

    code_challenge = payload['origin_ref']

    origin_ref = db['auth'].find_one(code_challenge=code_challenge)['origin_ref']
    print(f'> [   auth   ]: {origin_ref} ({code_challenge}): {j}')

    # validate the code challenge
    if payload['challenge'] != b64enc(sha256(j['code_verifier'].encode('utf-8')).digest()).rstrip(b'=').decode('utf-8'):
        raise HTTPException(status_code=401, detail='expected challenge did not match verifier')

    cur_time = datetime.utcnow()
    access_expires_on = cur_time + TOKEN_EXPIRE_DELTA

    new_payload = {
        'iat': timegm(cur_time.timetuple()),
        'nbf': timegm(cur_time.timetuple()),
        'iss': 'https://cls.nvidia.org',
        'aud': 'https://cls.nvidia.org',
        'exp': timegm(access_expires_on.timetuple()),
        'origin_ref': payload['origin_ref'],
        'key_ref': SITE_KEY_XID,
        'kid': SITE_KEY_XID,
    }

    auth_token = jwt.encode(new_payload, key=jwt_encode_key, headers={'kid': payload.get('kid')}, algorithm='RS256')

    response = {
        "expires": access_expires_on.isoformat(),
        "auth_token": auth_token,
        "sync_timestamp": cur_time.isoformat(),
    }

    return JSONResponse(response)


# {'fulfillment_context': {'fulfillment_class_ref_list': []}, 'lease_proposal_list': [{'license_type_qualifiers': {'count': 1}, 'product': {'name': 'NVIDIA RTX Virtual Workstation'}}], 'proposal_evaluation_mode': 'ALL_OF', 'scope_ref_list': ['00112233-4455-6677-8899-aabbccddeeff']}
@app.post('/leasing/v1/lessor')
async def leasing_lessor(request: Request):
    j, token = json.loads((await request.body()).decode('utf-8')), get_token(request)

    code_challenge = token['origin_ref']
    scope_ref_list = j['scope_ref_list']

    origin_ref = db['auth'].find_one(code_challenge=code_challenge)['origin_ref']

    print(f'> [  create  ]: {origin_ref} ({code_challenge}): create leases for scope_ref_list {scope_ref_list}')

    cur_time = datetime.utcnow()
    lease_result_list = []
    for scope_ref in scope_ref_list:
        expires = cur_time + LEASE_EXPIRE_DELTA
        lease_result_list.append({
            "ordinal": 0,
            # https://docs.nvidia.com/license-system/latest/nvidia-license-system-user-guide/index.html
            "lease": {
                "ref": scope_ref,
                "created": cur_time.isoformat(),
                "expires": expires.isoformat(),
                # The percentage of the lease period that must elapse before a licensed client can renew a license
                "recommended_lease_renewal": 0.15,
                "offline_lease": "true",
                "license_type": "CONCURRENT_COUNTED_SINGLE"
            }
        })

        data = dict(origin_ref=origin_ref, lease_ref=scope_ref, lease_created=cur_time, lease_expires=expires)
        db['lease'].insert_ignore(data, ['origin_ref', 'lease_ref'])  # todo: handle update

    response = {
        "lease_result_list": lease_result_list,
        "result_code": "SUCCESS",
        "sync_timestamp": cur_time.isoformat(),
        "prompts": None
    }

    return JSONResponse(response)


# venv/lib/python3.9/site-packages/nls_services_lease/test/test_lease_multi_controller.py
# venv/lib/python3.9/site-packages/nls_dal_service_instance_dls/schema/service_instance/V1_0_21__product_mapping.sql
@app.get('/leasing/v1/lessor/leases')
async def leasing_lessor_lease(request: Request):
    token = get_token(request)

    code_challenge = token['origin_ref']

    origin_ref = db['auth'].find_one(code_challenge=code_challenge)['origin_ref']
    active_lease_list = list(map(lambda x: x['lease_ref'], db['lease'].find(origin_ref=origin_ref)))
    print(f'> [  leases  ]: {origin_ref} ({code_challenge}): found {len(active_lease_list)} active leases')

    cur_time = datetime.utcnow()
    response = {
        "active_lease_list": active_lease_list,
        "sync_timestamp": cur_time.isoformat(),
        "prompts": None
    }

    return JSONResponse(response)


# venv/lib/python3.9/site-packages/nls_core_lease/lease_single.py
@app.put('/leasing/v1/lease/{lease_ref}')
async def leasing_lease_renew(request: Request, lease_ref: str):
    token = get_token(request)

    code_challenge = token['origin_ref']

    origin_ref = db['auth'].find_one(code_challenge=code_challenge)['origin_ref']
    print(f'> [  renew   ]: {origin_ref} ({code_challenge}): renew {lease_ref}')

    if db['lease'].count(origin_ref=origin_ref, lease_ref=lease_ref) == 0:
        raise HTTPException(status_code=404, detail='requested lease not available')

    cur_time = datetime.utcnow()
    expires = cur_time + LEASE_EXPIRE_DELTA
    response = {
        "lease_ref": lease_ref,
        "expires": expires.isoformat(),
        "recommended_lease_renewal": 0.16,
        "offline_lease": True,
        "prompts": None,
        "sync_timestamp": cur_time.isoformat(),
    }

    data = dict(origin_ref=origin_ref, lease_ref=lease_ref, lease_expires=expires, lease_last_update=cur_time)
    db['lease'].update(data, ['origin_ref', 'lease_ref'])

    return JSONResponse(response)


@app.delete('/leasing/v1/lessor/leases')
async def leasing_lessor_lease_remove(request: Request):
    token = get_token(request)

    code_challenge = token['origin_ref']

    origin_ref = db['auth'].find_one(code_challenge=code_challenge)['origin_ref']
    released_lease_list = list(map(lambda x: x['lease_ref'], db['lease'].find(origin_ref=origin_ref)))
    deletions = db['lease'].delete(origin_ref=origin_ref)
    print(f'> [  remove  ]: {origin_ref} ({code_challenge}): removed {deletions} leases')

    cur_time = datetime.utcnow()
    response = {
        "released_lease_list": released_lease_list,
        "release_failure_list": None,
        "sync_timestamp": cur_time.isoformat(),
        "prompts": None
    }
    return JSONResponse(response)


if __name__ == '__main__':
    import uvicorn

    ###
    #
    # Running `python app/main.py` assumes that the user created a keypair, e.g. with openssl.
    #
    # openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout app/cert/webserver.key -out app/cert/webserver.crt
    #
    ###

    print(f'> Starting dev-server ...')

    ssl_keyfile = join(dirname(__file__), 'cert/webserver.key')
    ssl_certfile = join(dirname(__file__), 'cert/webserver.crt')

    uvicorn.run('main:app', host='0.0.0.0', port=443, ssl_keyfile=ssl_keyfile, ssl_certfile=ssl_certfile, reload=True)