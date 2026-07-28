#app_data.py
from flask import Blueprint,request,redirect,url_for, jsonify, current_app, session, render_template, make_response
from renglo.auth.login_required import login_required
from renglo.auth.auth_controller import AuthController
from renglo.data.data_controller import DataController
from flask_cognito import cognito_auth_required, current_user, current_cognito_jwt

import time,json,csv
import io
import urllib.parse
import boto3
from decimal import Decimal


app_data = Blueprint('app_data', __name__, template_folder='templates',url_prefix='/_data')

# Controllers - will be initialized when blueprint is registered
AUC = None
DAC = None

PAYMENT_METHODS_RING = "noma_payment_methods"
NOMA_TRAVELS_RING = "noma_travels"


def _get_renglo_config():
    return getattr(current_app, "renglo_config", None) or current_app.config


def _payment_crypto():
    try:
        from noma.utilities import payment_method_crypto as crypto
        return crypto
    except ImportError:
        return None


def _payment_authz():
    try:
        from noma.utilities import payment_method_authz as pma
        return pma
    except ImportError:
        return None


def _resolve_payment_caller(portfolio, org):
    pma = _payment_authz()
    if not pma:
        return None
    return pma.resolve_payment_caller(DAC, AUC, _get_renglo_config(), portfolio, org)


def _forbid(message="Forbidden"):
    return jsonify({"success": False, "message": message}), 403


def _strip_noma_travels_approval_fields(payload):
    """Drop client-writable purchase-approval fields on generic trip PUTs."""
    try:
        from noma.utilities.purchase_approval import strip_client_approval_fields
        return strip_client_approval_fields(payload)
    except ImportError:
        # Fallback if noma package is unavailable in this process.
        if not isinstance(payload, dict):
            return payload
        protected = {
            "approval_status",
            "approval_snapshot_hash",
            "approval_requested_at",
            "approval_requested_by",
            "approval_previous_status",
            "approval_decided_at",
            "approval_decided_by",
            "approved_by",
            "approved_at",
            "rejection_reason",
        }
        return {k: v for k, v in payload.items() if k not in protected}


def _is_payment_methods_ring(ring: str) -> bool:
    return ring == PAYMENT_METHODS_RING


def _apply_payment_list_authz(portfolio, org, document):
    caller = _resolve_payment_caller(portfolio, org)
    pma = _payment_authz()
    if not caller or not pma:
        return document
    return pma.filter_payment_methods_payload(
        document,
        role=caller.get("role"),
        caller_ids=caller.get("caller_ids"),
        managed_ids=caller.get("managed_ids"),
    )


def _redact_payment_methods_payload(payload):
    crypto = _payment_crypto()
    if not crypto or not isinstance(payload, dict):
        return payload
    return crypto.redact_payment_methods_response(payload, config=_get_renglo_config())


def _redact_payment_method_doc(doc):
    crypto = _payment_crypto()
    if not crypto or not isinstance(doc, dict):
        return doc
    return crypto.redact_payment_method_for_api(doc, config=_get_renglo_config())


def _jsonify_ring_response(ring, document, status=200, portfolio=None, org=None):
    if _is_payment_methods_ring(ring):
        if portfolio and org and isinstance(document, dict) and "items" in document:
            document = _apply_payment_list_authz(portfolio, org, document)
        if isinstance(document, dict) and (
            "items" in document or "item" in document or document.get("success") is not None
        ):
            document = _redact_payment_methods_payload(document)
        else:
            document = _redact_payment_method_doc(document)
    return jsonify(document), status


def _prepare_payment_method_write(payload, *, action: str, existing_doc=None):
    crypto = _payment_crypto()
    if not crypto:
        return payload, None, False

    encrypted, err = crypto.prepare_payment_method_write_payload(
        payload,
        _get_renglo_config(),
        action=action,
        existing_doc=existing_doc,
    )
    if err:
        return payload, err, False
    return encrypted, None, crypto.needs_encrypted_sidecar(encrypted)


def _persist_payment_method_sidecar(portfolio, org, pm_id, encrypted_doc, *, action: str):
    crypto = _payment_crypto()
    if not crypto:
        return False, "Payment crypto module is not available"
    return crypto.persist_payment_method_encrypted_sidecar(
        DAC,
        portfolio,
        org,
        pm_id,
        encrypted_doc,
        action=action,
    )

@app_data.record_once
def on_load(state):
    """Initialize controllers with config when blueprint is registered."""
    global AUC, DAC
    config = state.app.renglo_config
    AUC = AuthController(config=config)
    DAC = DataController(config=config)

# Set the route and accepted methods



@app_data.route('/')
@cognito_auth_required
def index():
   #Nothing to show here
    return jsonify(message='')


#TEST (DELETE)
@app_data.route('/t1')
def t1():

    current_app.logger.info('t1')
    return jsonify(message="t1")
    

@app_data.route('/<string:portfolio>/<string:org>/<string:ring>/', methods=['GET'])
@cognito_auth_required
def route_a_b_get_with_slash(portfolio, org, ring):
    return route_a_b_get(portfolio, org, ring)

@app_data.route('/<string:portfolio>/<string:org>/<string:ring>', methods=['GET'])
@cognito_auth_required
def route_a_b_get(portfolio, org, ring):

    limit = request.args.get('limit', default=987, type=int)  # Retrieve limit, default to 1000
    lastkey = request.args.get('lastkey')  # Retrieve lastkey, default to None
    sort = request.args.get('sort')  # Retrieve sort, default to None
    all = request.args.get('all')
    refresh = request.args.get('refresh')  # Check for refresh parameter
    # When paged=1, list from DynamoDB in pages (first page has no lastkey) instead of the S3 snapshot.
    paged = request.args.get('paged') == '1'

    response = []
    
    if not lastkey and not paged:
        # If you are not using pagination, use the cache
        all = True
    
    if all or refresh:  # Check if 'all' or 'refresh' is present
        s3_client = boto3.client('s3')
        bucket_name = current_app.config['S3_BUCKET_NAME']  
        file_path = f'data/{portfolio}/{org}/{ring}'
        
        try:
            # Check if this document already exists in S3
            s3_client.head_object(Bucket=bucket_name, Key=file_path)
            # If it exists and refresh is set, raise an exception to trigger regeneration
            if refresh:
                current_app.logger.debug('Document exists, but refresh is set. Raising exception to regenerate document.')
                raise Exception("Force regeneration due to refresh flag.")
            else:
                response = s3_client.get_object(Bucket=bucket_name, Key=file_path)
                document = json.loads(response['Body'].read())
                current_app.logger.debug('Document already exists, retrieving from S3')
                return _jsonify_ring_response(ring, document, 200, portfolio=portfolio, org=org)
        except (s3_client.exceptions.ClientError, Exception) as e:
            # If it does not exist or if we raised an exception, call DAC.get_a_b()
            current_app.logger.warning(
                'Falling back to cache refresh for %s due to %s',
                file_path,
                str(e)
            )
            try:
                # Keep return shape consistent with the S3 document path.
                refreshed, _status = DAC.refresh_s3_cache(portfolio, org, ring, sort)
                return _jsonify_ring_response(ring, refreshed, 200, portfolio=portfolio, org=org)
            except Exception as refresh_error:
                current_app.logger.exception(
                    'Failed to refresh cache for %s/%s/%s',
                    portfolio,
                    org,
                    ring
                )
                return jsonify({
                    'success': False,
                    'message': 'Failed to fetch data',
                    'error': str(refresh_error)
                }), 500
        
    else:
        response = DAC.get_a_b(portfolio, org, ring, limit, lastkey, sort)
        return _jsonify_ring_response(ring, response, 200, portfolio=portfolio, org=org)
    


@app_data.route('/<string:portfolio>/_all/<string:ring>', methods=['POST'])
@cognito_auth_required
def route_a_all_post(portfolio,ring):
    
    payload = request.get_json()
    response, status = DAC.post_a_b(portfolio,'_all',ring,payload)
    DAC.refresh_s3_cache(portfolio, '_all', ring, None)
    return response, status
    

@app_data.route('/<string:portfolio>/<string:org>/<string:ring>', methods=['POST'])
@cognito_auth_required
def route_a_b_post(portfolio,org,ring):
    
    payload = request.get_json()
    encrypted_doc = payload
    sidecar_needed = False

    if _is_payment_methods_ring(ring):
        caller = _resolve_payment_caller(portfolio, org)
        pma = _payment_authz()
        if caller and pma:
            normalized, authz_err = pma.prepare_payment_method_authz_write(
                payload,
                role=caller.get("role"),
                caller_ids=caller.get("caller_ids"),
                managed_ids=caller.get("managed_ids"),
                user_id=caller.get("user_id"),
                action="post",
            )
            if authz_err:
                status = 403 if "Forbidden" in authz_err else 400
                return jsonify({"success": False, "message": authz_err}), status
            payload = normalized
        encrypted_doc, encrypt_err, sidecar_needed = _prepare_payment_method_write(
            payload,
            action="post",
        )
        if encrypt_err:
            return jsonify({
                'success': False,
                'message': 'Payment method encryption failed',
                'error': encrypt_err,
            }), 400

    response, status = DAC.post_a_b(portfolio,org,ring,encrypted_doc)

    if (
        _is_payment_methods_ring(ring)
        and status == 200
        and isinstance(response, dict)
        and response.get('success')
    ):
        if sidecar_needed:
            item = response.get('item') or {}
            pm_id = str(item.get('_id') or '').strip()
            if pm_id:
                ok, sidecar_err = _persist_payment_method_sidecar(
                    portfolio,
                    org,
                    pm_id,
                    encrypted_doc,
                    action='post',
                )
                if not ok:
                    return jsonify({
                        'success': False,
                        'message': 'Payment method saved but encrypted sidecar persist failed',
                        'error': sidecar_err,
                    }), 500
        response = _redact_payment_methods_payload(response)

    DAC.refresh_s3_cache(portfolio, org, ring, None)
    return response, status



@app_data.route('/<string:portfolio>/<string:org>/<string:ring>/_query', methods=['POST'])
@cognito_auth_required
def route_a_b_query(portfolio, org, ring):
    
    limit = request.args.get('limit', default=999, type=int)  # Retrieve limit, default to 1000
    lastkey = request.args.get('lastkey')  # Retrieve lastkey, default to None
    sort = request.args.get('sort')  # Retrieve sort, default to None
    payload = request.get_json()
    
    '''
    Payload sample 1
    The value is any string at the end of the index string portfolio:org:ring:<any_string>
    {
        'operator':'begins_with',
        'value':'123453:active',
        'filter':{
            'operator':'greater_than',
            'field':'launch_time'
            'value':'17234432453'
        },
        'sort':'desc'
    }
    
    Payload sample 2
    This is a special case where the index is a timestamp  portfolio:org:ring:<timestamp> 
    In the background it is a 'begins_with' with an empty sufix
    Value is always empty. 
    Returns a list of items ordered chronologically. 
    The filter is optional but recommended to shorten the response size
    {
        'operator':'chrono',
        'filter':{
            'operator':'greater_than',
            'value':'17234432453'
        },
        'sort':'desc'
    }
    '''
       
    query = {
        'portfolio':portfolio,
        'org':org,
        'ring':ring,
        'operator':payload.get('operator', None),
        'value':payload.get('value', None),
        'filter':payload.get('filter',{}),
        'limit':limit,
        'lastkey':lastkey,
        'sort': payload.get('sort', sort)
    }
       
    response = DAC.get_a_b_query(query)
    if _is_payment_methods_ring(ring) and isinstance(response, dict):
        response = _apply_payment_list_authz(portfolio, org, response)
        response = _redact_payment_methods_payload(response)
    return response, 200



@app_data.route('/<string:portfolio>/<string:org>/<string:ring>/<string:idx>/', methods=['GET'])
@cognito_auth_required
def route_a_b_c_get_with_slash(portfolio,org,ring,idx):
    return route_a_b_c_get(portfolio,org,ring,idx)
    
@app_data.route('/<string:portfolio>/<string:org>/<string:ring>/<string:idx>', methods=['GET'])
@cognito_auth_required
def route_a_b_c_get(portfolio,org,ring,idx):

    doc = DAC.get_a_b_c(portfolio,org,ring,idx)
    if _is_payment_methods_ring(ring):
        caller = _resolve_payment_caller(portfolio, org)
        pma = _payment_authz()
        if caller and pma:
            allowed, err = pma.gate_payment_method_doc(
                doc,
                role=caller.get("role"),
                caller_ids=caller.get("caller_ids"),
                managed_ids=caller.get("managed_ids"),
                mutate=False,
            )
            if not allowed:
                return _forbid(err or "Forbidden")
        return _jsonify_ring_response(ring, doc, 200, portfolio=portfolio, org=org)
    return doc

    
    
@app_data.route('/<string:portfolio>/<string:org>/<string:ring>/<string:idx>', methods=['PUT'])
@cognito_auth_required
def route_a_b_c_put(portfolio,org,ring,idx):
    
    payload = request.get_json()
    encrypted_doc = payload
    sidecar_needed = False

    if ring == NOMA_TRAVELS_RING:
        # Travelers must not forge approval_status / snapshot via generic PUT.
        # Server handlers (request/approve_purchase) call DAC.put_a_b_c directly.
        encrypted_doc = _strip_noma_travels_approval_fields(encrypted_doc)

    if _is_payment_methods_ring(ring):
        existing_doc = DAC.get_a_b_c(portfolio, org, ring, idx)
        if (
            not isinstance(existing_doc, dict)
            or existing_doc.get('error')
            or existing_doc.get('success') is False
        ):
            existing_doc = None
        caller = _resolve_payment_caller(portfolio, org)
        pma = _payment_authz()
        if caller and pma:
            normalized, authz_err = pma.prepare_payment_method_authz_write(
                payload,
                role=caller.get("role"),
                caller_ids=caller.get("caller_ids"),
                managed_ids=caller.get("managed_ids"),
                user_id=caller.get("user_id"),
                existing_doc=existing_doc,
                action="put",
            )
            if authz_err:
                status = 403 if "Forbidden" in authz_err else 400
                return jsonify({"success": False, "message": authz_err}), status
            payload = normalized
        encrypted_doc, encrypt_err, sidecar_needed = _prepare_payment_method_write(
            payload,
            action="put",
            existing_doc=existing_doc,
        )
        if encrypt_err:
            return jsonify({
                'success': False,
                'message': 'Payment method encryption failed',
                'error': encrypt_err,
            }), 400

    response, status = DAC.put_a_b_c(portfolio,org,ring,idx,encrypted_doc)

    if (
        _is_payment_methods_ring(ring)
        and status == 200
        and isinstance(response, dict)
        and response.get('success')
    ):
        if sidecar_needed:
            ok, sidecar_err = _persist_payment_method_sidecar(
                portfolio,
                org,
                idx,
                encrypted_doc,
                action='put',
            )
            if not ok:
                return jsonify({
                    'success': False,
                    'message': 'Payment method updated but encrypted sidecar persist failed',
                    'error': sidecar_err,
                }), 500
        response = _redact_payment_methods_payload(response)

    DAC.refresh_s3_cache(portfolio, org, ring, None)
    return response, status

    
@app_data.route('/<string:portfolio>/<string:org>/<string:ring>/<string:idx>', methods=['DELETE'])
@cognito_auth_required
def route_a_b_c_delete(portfolio,org,ring,idx):

    if _is_payment_methods_ring(ring):
        existing_doc = DAC.get_a_b_c(portfolio, org, ring, idx)
        caller = _resolve_payment_caller(portfolio, org)
        pma = _payment_authz()
        if caller and pma:
            allowed, err = pma.gate_payment_method_doc(
                existing_doc,
                role=caller.get("role"),
                caller_ids=caller.get("caller_ids"),
                managed_ids=caller.get("managed_ids"),
                mutate=True,
            )
            if not allowed:
                return _forbid(err or "Forbidden")

    response, status = DAC.delete_a_b_c(portfolio,org,ring,idx)
    DAC.refresh_s3_cache(portfolio, org, ring, None)
    return response, status




    
