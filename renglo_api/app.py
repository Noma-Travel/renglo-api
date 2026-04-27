"""
Renglo API - Flask application factory
"""

from flask import Flask, jsonify, request, session, g
from flask_caching import Cache
from flask_cors import CORS
from flask_cognito import CognitoAuth, cognito_auth_required
import logging
import time
import os
import sys
from renglo_api.config import load_env_config


def _is_aws_lambda_runtime() -> bool:
    """True inside the Lambda execution environment (Zappa, SAM, etc.)."""
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return True
    if (os.environ.get("AWS_EXECUTION_ENV") or "").startswith("AWS_Lambda"):
        return True
    return False


def _collect_allowed_cors_origins(app_config: dict) -> set[str]:
    """
    Build the set of browser origins that may call the API. Uses FE_BASE_URL,
    APP_FE_BASE_URL, and a comma-separated CORS_ALLOWED_ORIGINS (e.g. from zappa).
    """
    s: set[str] = set()
    for k in ("FE_BASE_URL", "APP_FE_BASE_URL"):
        v = (app_config.get(k) or os.environ.get(k) or "").strip()
        if v:
            s.add(v.rstrip("/"))
    extra = (app_config.get("CORS_ALLOWED_ORIGINS") or os.environ.get("CORS_ALLOWED_ORIGINS") or "").strip()
    if extra:
        for p in extra.split(","):
            p = p.strip().rstrip("/")
            if p:
                s.add(p)
    return s


def _install_cors_wsgi_middleware(app: Flask, allowed: frozenset[str]) -> None:
    """
    Wrap the WSGI app so every response (including automatic OPTIONS, 4xx, 5xx) gets
    Access-Control-* headers. API Gateway (AWS_PROXY) and CloudFront will forward them.
    Relying only on @app.after_request is brittle with some WSGI/Zappa response paths.
    """
    if not allowed:
        return

    parent = app.wsgi_app

    def wsgi_with_cors(environ, start_response):
        def _drop_inner_cors(name) -> bool:
            if isinstance(name, str) and name.lower().startswith("access-control-"):
                return False
            if isinstance(name, (bytes, bytearray)):
                n = name.lower()
                if n.startswith(b"access-control-allow-") or n.startswith(
                    b"access-control-expose-"
                ):
                    return False
            return True

        def start_response_cors(status, response_headers, exc_info=None):
            # Error responses pass exc_info; still inject CORS so the browser
            # does not report a misleading CORS failure on 5xx/exception paths.
            try:
                origin = (
                    environ.get("HTTP_ORIGIN", "").strip()
                    or (environ.get("Origin") or "").strip()
                )
            except Exception:
                origin = ""
            h = list(response_headers)
            # Drop any CORS from inner stack (e.g. flask-cors) so we emit one consistent policy
            h = [(k, v) for (k, v) in h if _drop_inner_cors(k)]
            if origin and origin in allowed:
                h.append(("Access-Control-Allow-Origin", origin))
            h.append(
                (
                    "Access-Control-Allow-Methods",
                    "GET, POST, PUT, DELETE, PATCH, OPTIONS",
                )
            )
            h.append(
                (
                    "Access-Control-Allow-Headers",
                    "Content-Type, Authorization, Accept, X-Api-Key, X-Amz-Date, X-Amz-Security-Token, X-Org-Id, X-Portfolio-Id, X-Requested-With, Cache-Control, Pragma, Expires, Origin",
                )
            )
            h.append(("Access-Control-Expose-Headers", "*"))
            return start_response(status, h, exc_info)

        return parent(environ, start_response_cors)

    app.wsgi_app = wsgi_with_cors


def create_app(config=None, config_path=None):
    """
    Factory function to create and configure the Flask app.
    Can be imported and run from anywhere.
    
    Args:
        config (dict): Configuration dictionary to use. If provided, takes precedence.
        config_path (str): Path to env_config.py file. If not provided, looks in current directory.
    
    Usage:
        # Option 1: Pass config dict directly (recommended for production)
        app = create_app(config={'DYNAMODB_CHAT_TABLE': 'prod_chat', ...})
        
        # Option 2: Load from env_config.py in current directory
        app = create_app()
        
        # Option 3: Load from specific path
        app = create_app(config_path='/path/to/env_config.py')
    """
    # static_url_path must not be '/': a root catch-all <path:filename> would match
    # every path (e.g. /_docs/...), miss the file, 404, and the global 404 handler would run.
    app = Flask(__name__, 
                static_folder='../static/dist',
                static_url_path='/_st')
    
    # Load environment-specific config if not provided directly
    if config is None:
        env_config = load_env_config(config_path)
        app.config.update(env_config)
    else:
        # Use provided config directly
        app.config.update(config)
    
    # Make config available for controller instantiation
    app.renglo_config = dict(app.config)
    
    # Setup cache
    cache = Cache(app)
    app.cache = cache
    
    # Set up logging
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('zappa').setLevel(logging.WARNING)
    app.logger.info(f'Python Version: {sys.version}')
    
    # Lambda: prefer runtime env; some cold paths only set AWS_EXECUTION_ENV
    app.config['IS_LAMBDA'] = _is_aws_lambda_runtime()
    
    # Setup CORS based on environment
    if app.config['IS_LAMBDA']:
        app.logger.info('RUNNING ON LAMBDA ENVIRONMENT')
        app.logger.info('BASE_URL:' + str(app.config.get('BASE_URL', 'NOT SET')))
        app.logger.info('FE_BASE_URL:' + str(app.config.get('FE_BASE_URL', 'NOT SET')))
        
        allowed_set = _collect_allowed_cors_origins(app.config)
        origins = list(allowed_set)
        if not origins:
            app.logger.error(
                'CORS: no FE_BASE_URL / APP_FE_BASE_URL / CORS_ALLOWED_ORIGINS — browser calls will fail CORS. '
                'Set these in zappa environment_variables (or env_config) and redeploy.'
            )
        else:
            app.logger.info('CORS: APP_FE_BASE_URL: %s', app.config.get('APP_FE_BASE_URL', 'NOT SET'))
        
        # Add development origins only if explicitly enabled
        if app.config.get('ALLOW_DEV_ORIGINS', False):
            app.logger.warning('DEVELOPMENT ORIGINS ENABLED - NOT RECOMMENDED FOR PRODUCTION')
            for o in (
                "http://127.0.0.1:5173",
                "http://127.0.0.1:5174",
                "http://127.0.0.1:3000",
            ):
                origins.append(o)
                allowed_set.add(o)
        
        app.logger.info('CORS Origins configured: %s', origins)
        # WSGI middleware handles all CORS for Lambda. Do not also use flask_cors.CORS: it
        # duplicates Access-Control-* and API clients/Gateway may keep the narrower header only.
        if allowed_set:
            _install_cors_wsgi_middleware(app, frozenset(allowed_set))
        # API Gateway + stage (e.g. /noma_prod/...) must be stripped or Flask routes
        # registered at /_schd, /_data, etc. will 404. See renglo_api.apigw_stage_middleware.
        pfx = (app.config.get("URL_PREFIX") or os.environ.get("URL_PREFIX") or "").strip().strip(
            "/"
        )
        if pfx:
            from renglo_api.apigw_stage_middleware import strip_url_prefix

            app.wsgi_app = strip_url_prefix(app.wsgi_app, url_prefix=pfx)
            app.logger.info("APIGW: URL_PREFIX strip active for /%s (PATH_INFO for Flask routes)", pfx)
    else:
        app.logger.info('RUNNING ON LOCAL ENVIRONMENT')
        CORS(app, resources={r"/*": {
            "origins": [
                "http://127.0.0.1:5173",
                "http://127.0.0.1:5174",
                "http://127.0.0.1:3000",
                "http://localhost:3000",
            ]
        }})
    
    # Initialize CognitoAuth
    cognito = CognitoAuth(app)
    
    # Register blueprints (routes)
    from renglo_api.routes.auth_routes import app_auth
    from renglo_api.routes.data_routes import app_data
    from renglo_api.routes.search_routes import app_search
    from renglo_api.routes.blueprint_routes import app_blueprint
    from renglo_api.routes.docs_routes import app_docs
    from renglo_api.routes.schd_routes import app_schd
    from renglo_api.routes.chat_routes import app_chat
    from renglo_api.routes.state_routes import app_state
    from renglo_api.routes.session_routes import app_session

    app.register_blueprint(app_data)
    app.register_blueprint(app_search)
    app.register_blueprint(app_blueprint)
    app.register_blueprint(app_auth)
    app.register_blueprint(app_docs)
    app.register_blueprint(app_schd)
    app.register_blueprint(app_chat)
    app.register_blueprint(app_state)
    app.register_blueprint(app_session)
    # Backward-compat aliases: support routes both with and without "_" prefixes.
    # This prevents frontend/backend drift (e.g. /data vs /_data) from causing
    # API Gateway 404 preflight failures that surface as CORS errors in browsers.
    def _register_alias(blueprint, alias_prefix):
        app.register_blueprint(
            blueprint,
            url_prefix=alias_prefix,
            name=f'{blueprint.name}_alias_{alias_prefix.strip("/").replace("/", "_")}'
        )

    _register_alias(app_data, '/data')
    _register_alias(app_auth, '/auth')
    _register_alias(app_search, '/search')
    _register_alias(app_blueprint, '/blueprint')
    _register_alias(app_docs, '/docs')
    _register_alias(app_schd, '/schd')
    _register_alias(app_chat, '/chat')
    _register_alias(app_state, '/state')
    _register_alias(app_session, '/session')
    
    # Template Filters
    @app.template_filter()
    def diablify(string):
        return '666' + str(string)
    
    @app.template_filter()
    def nonone(val):
        if not val is None:
            return val
        else:
            return ''
    
    @app.template_filter()
    def is_list(val):
        return isinstance(val, list)
    
    # Unmatched route / true 404 — do not return 301 (bad for APIs and browser caching).
    @app.errorhandler(404)
    def not_found(error):
        renglo_fe_url = app.config.get('FE_BASE_URL', '')
        return jsonify({'error': f'Not found. FE: {renglo_fe_url}'}), 404
    
    # Basic routes
    @app.route('/')
    def index():
        app.logger.info('Hitting the root')
        try:
            return app.send_static_file('index.html')
        except:
            return jsonify({'message': 'Renglo API is running', 'version': '1.0.0'}), 200
    
    @app.route('/time')
    @cognito_auth_required
    def get_current_time():
        return {'time': time.time()}
    
    @app.route('/timex')
    def get_current_timex():
        session['current_user'] = '7e5fb15bb'
        return {'time': time.time()}
    
    @app.route('/ping')
    def ping():
        app.logger.info("Ping!: %s", time.time())
        return {
            'pong': True,
            'time': time.time(),
        }
    
    @app.route('/message', methods=['POST'])
    def real_time_message():
        app.logger.info("WEBSOCKET MESSAGE!: %s", time.time())
        payload = request.get_json()
        app.logger.info(payload)
        return {
            'ws': True,
            'time': time.time(),
            'input': payload,
        }
    
    return app


def run(host='0.0.0.0', port=5000, debug=True):
    """
    Convenience function to run the app for local development.
    """
    app = create_app()
    app.run(host=host, port=port, debug=debug)


# For Zappa deployment - create app instance at module level
app = create_app()
