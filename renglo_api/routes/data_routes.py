#app_data.py
from flask import Blueprint,request,redirect,url_for, jsonify, current_app, session, render_template, make_response
from renglo.auth.login_required import login_required
from renglo.data.data_controller import DataController
from flask_cognito import cognito_auth_required, current_user, current_cognito_jwt

import time,json,csv
import io
import urllib.parse
import boto3
from decimal import Decimal


app_data = Blueprint('app_data', __name__, template_folder='templates',url_prefix='/_data')

# Controller - initialized when the blueprint is registered.
# No AuthController here anymore: the only thing that needed one was resolving a
# caller's identity to authorize a specific ring, and that rule now lives with
# the resource that owns it.
DAC = None

def _get_renglo_config():
    return getattr(current_app, "renglo_config", None) or current_app.config


# ── ring policy ──────────────────────────────────────────────────────────────
#
# Some rings are served by a product API mounted alongside this one instead of by
# the generic CRUD below. Those rings' authorization, redaction and field rules
# live over there, so reaching them through here would bypass all of it — the
# generic routes have to refuse them.
#
# Which rings, and how much to refuse, is deployment configuration. This module
# does not know what any of them contains, which is the point: it used to.
#
#     DATA_API_RING_POLICY = "ring_a:deny,ring_b:deny_write"
#
#   deny        not reachable here at all
#   deny_write  reads still work, POST/PUT/DELETE do not
#
# Absent or empty means every ring is served normally — what a deployment with no
# product API in front of it gets.

_POLICY_MODES = ("deny", "deny_write")
_policy_cache = {}


def _ring_policy():
    """Parse DATA_API_RING_POLICY into {ring: mode}, memoized on the raw string."""
    raw = str(_get_renglo_config().get("DATA_API_RING_POLICY") or "").strip()
    cached = _policy_cache.get(raw)
    if cached is not None:
        return cached

    policy = {}
    for entry in raw.split(","):
        ring, _, mode = entry.partition(":")
        ring, mode = ring.strip(), mode.strip().lower()
        # Skip anything malformed instead of raising: a typo in an env var must
        # not take the whole data API down on the first request.
        if ring and mode in _POLICY_MODES:
            policy[ring] = mode

    _policy_cache[raw] = policy
    return policy


def _policy_block(ring, *, write):
    """403 if the policy reserves this ring for the product API, else None."""
    mode = _ring_policy().get(ring)
    if mode == "deny" or (write and mode == "deny_write"):
        return jsonify({
            "success": False,
            "message": f"Ring '{ring}' is not served by the generic data API",
        }), 403
    return None


@app_data.record_once
def on_load(state):
    """Initialize the controller with config when blueprint is registered."""
    global DAC
    config = state.app.renglo_config
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
    blocked = _policy_block(ring, write=False)
    if blocked:
        return blocked

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
                return jsonify(document), 200
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
                return jsonify(refreshed), 200
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
        return jsonify(response), 200
    


@app_data.route('/<string:portfolio>/_all/<string:ring>', methods=['POST'])
@cognito_auth_required
def route_a_all_post(portfolio,ring):
    blocked = _policy_block(ring, write=True)
    if blocked:
        return blocked

    payload = request.get_json()
    response, status = DAC.post_a_b(portfolio,'_all',ring,payload)
    # Drop the snapshot instead of rebuilding it: refresh_s3_cache pages the whole
    # ring out of DynamoDB, so writes would scale with tenant size. The GET above
    # regenerates it on the next uncached read.
    DAC.invalidate_s3_cache(portfolio, '_all', ring, None)
    return response, status
    

@app_data.route('/<string:portfolio>/<string:org>/<string:ring>', methods=['POST'])
@cognito_auth_required
def route_a_b_post(portfolio,org,ring):
    blocked = _policy_block(ring, write=True)
    if blocked:
        return blocked

    payload = request.get_json()
    response, status = DAC.post_a_b(portfolio,org,ring,payload)
    DAC.invalidate_s3_cache(portfolio, org, ring, None)
    return response, status



@app_data.route('/<string:portfolio>/<string:org>/<string:ring>/_query', methods=['POST'])
@cognito_auth_required
def route_a_b_query(portfolio, org, ring):
    blocked = _policy_block(ring, write=False)
    if blocked:
        return blocked

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
    return response, 200



@app_data.route('/<string:portfolio>/<string:org>/<string:ring>/<string:idx>/', methods=['GET'])
@cognito_auth_required
def route_a_b_c_get_with_slash(portfolio,org,ring,idx):
    return route_a_b_c_get(portfolio,org,ring,idx)
    
@app_data.route('/<string:portfolio>/<string:org>/<string:ring>/<string:idx>', methods=['GET'])
@cognito_auth_required
def route_a_b_c_get(portfolio,org,ring,idx):
    blocked = _policy_block(ring, write=False)
    if blocked:
        return blocked

    return DAC.get_a_b_c(portfolio,org,ring,idx)

    
    
@app_data.route('/<string:portfolio>/<string:org>/<string:ring>/<string:idx>', methods=['PUT'])
@cognito_auth_required
def route_a_b_c_put(portfolio,org,ring,idx):
    blocked = _policy_block(ring, write=True)
    if blocked:
        return blocked

    payload = request.get_json()
    response, status = DAC.put_a_b_c(portfolio,org,ring,idx,payload)
    DAC.invalidate_s3_cache(portfolio, org, ring, None)
    return response, status


@app_data.route('/<string:portfolio>/<string:org>/<string:ring>/<string:idx>', methods=['DELETE'])
@cognito_auth_required
def route_a_b_c_delete(portfolio,org,ring,idx):
    blocked = _policy_block(ring, write=True)
    if blocked:
        return blocked

    response, status = DAC.delete_a_b_c(portfolio,org,ring,idx)
    DAC.invalidate_s3_cache(portfolio, org, ring, None)
    return response, status




    
