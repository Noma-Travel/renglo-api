"""
Configuration management for Renglo API.

This module provides utilities for loading and managing configuration
that can be injected into renglo controllers.
"""

import os
import sys


def load_env_config(config_path=None):
    """
    Load environment configuration from env_config.py and/or environment variables.
    Returns a dictionary of configuration values.
    
    This allows renglo-lib (which is framework-agnostic) to receive config
    via constructor injection rather than importing it directly.
    
    Args:
        config_path (str): Optional path to config module. If not provided,
                          looks in current working directory.
    
    Priority:
        1. Load from env_config.py file (if available)
        2. Override with environment variables (for Lambda/Docker)
        3. Environment variables take precedence
    """
    config = {}
    
    # Try to load from specified path or current directory
    try:
        if config_path:
            # Load from specific path (for custom deployments)
            import importlib.util
            spec = importlib.util.spec_from_file_location("env_config", config_path)
            env_config = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(env_config)
        else:
            # Try to import from current working directory
            # This is where the application's env_config.py should be
            import env_config
        
        # Extract all uppercase variables (convention for config constants)
        for key in dir(env_config):
            if key.isupper() and not key.startswith('_'):
                config[key] = getattr(env_config, key)
                
    except ImportError:
        print("Info: Using environment variables", file=sys.stderr)
    except Exception as e:
        print(f"Warning: Error loading env_config.py: {e}", file=sys.stderr)
    
    # Load from environment variables (takes precedence over file)
    # This is critical for Lambda/Docker deployments
    env_var_keys = [
        'WL_NAME', 'BASE_URL', 'FE_BASE_URL', 'DOC_BASE_URL',
        'APP_FE_BASE_URL', 'API_GATEWAY_ARN', 'ROLE_ARN', 'SYS_ENV',
        'DYNAMODB_ENTITY_TABLE', 'DYNAMODB_BLUEPRINT_TABLE', 
        'DYNAMODB_RINGDATA_TABLE', 'DYNAMODB_REL_TABLE', 'DYNAMODB_CHAT_TABLE',
        'DYNAMODB_SESSION_TABLE',
        'CSRF_SESSION_KEY', 'SECRET_KEY',
        'COGNITO_REGION', 'COGNITO_USERPOOL_ID', 'COGNITO_APP_CLIENT_ID',
        'COGNITO_CHECK_TOKEN_EXPIRATION',
        'PREVIEW_LAYER', 'S3_BUCKET_NAME',
        'OPENAI_API_KEY', 'AVIATION_STACK_API_KEY', 'RESEND_API_KEY', 'WEBSOCKET_CONNECTIONS', 'ALLOW_DEV_ORIGINS',
        'CORS_ALLOWED_ORIGINS',
        'AGENT_API_OUTPUT', 'AGENT_API_HANDLER', 'EXTERNAL_HANDLERS',
        'OPENSEARCH_ENDPOINT', 'OPENSEARCH_INDEX', 'OPENSEARCH_REFRESH',
        # Microsoft Outlook OAuth / token encryption
        'NEXT_PUBLIC_MS_TENANT_ID', 'NEXT_PUBLIC_MS_CLIENT_ID',
        'NEXT_PUBLIC_MS_CLIENT_SECRET', 'NEXT_PUBLIC_MS_REDIRECT_URI',
        'TOKENS_KMS_KEY_ID',
        'INVITE_FROM', 'SES_INVITE_SENDER',
        'AGENT_LANGUAGE', 'NOMA_ROLE_ENFORCEMENT', 'NOMA_ROLE_ENFORCEMENT_ORGS',
        'NOMA_E2E_SEED_ALLOWED', 'SYSTEM_ADMIN_EMAILS', 'SYSTEM_ADMIN_USER_IDS',
        'SLACK_API_ERRORS_WEBHOOK_URL', 'SLACK_BOOKINGS_WEBHOOK_URL', 'SLACK_ACTIVE_TRAVELS_WEBHOOK_URL',
        'CVC_BASE_URL', 'CVC_CLIENT_ID', 'CVC_CLIENT_SECRET', 'CVC_USERNAME', 'CVC_PASSWORD',
        'ASAAS_API_KEY', 'ASAAS_BASE_URL',
    ]
    
    for key in env_var_keys:
        if key in os.environ:
            config[key] = os.environ[key]
    
    return config


def get_config_for_flask(app):
    """
    Get configuration dict suitable for injecting into renglo controllers
    when running in Flask context.
    
    Args:
        app: Flask application instance
        
    Returns:
        dict: Configuration dictionary
    """
    # In Flask, we can merge Flask config with env_config
    config = load_env_config()
    
    # Flask config takes precedence
    config.update(app.config)
    
    return config


def get_config_for_lambda():
    """
    Get configuration dict suitable for injecting into renglo controllers
    when running in AWS Lambda context.
    
    For Lambda, configuration typically comes from:
    1. Environment variables
    2. env_config.py if deployed with the function
    
    Returns:
        dict: Configuration dictionary
    """
    config = load_env_config()
    
    # Override with environment variables if set
    # This allows Lambda environment variables to override file-based config
    env_var_keys = [
        'DYNAMODB_ENTITY_TABLE', 'DYNAMODB_BLUEPRINT_TABLE', 
        'DYNAMODB_RINGDATA_TABLE', 'DYNAMODB_REL_TABLE', 'DYNAMODB_CHAT_TABLE',
        'DYNAMODB_SESSION_TABLE',
        'COGNITO_REGION', 'COGNITO_USERPOOL_ID', 'COGNITO_APP_CLIENT_ID',
        'OPENAI_API_KEY', 'WEBSOCKET_CONNECTIONS', 'BASE_URL',
        'FE_BASE_URL', 'APP_FE_BASE_URL', 'DOC_BASE_URL', 'ALLOW_DEV_ORIGINS',
        'CORS_ALLOWED_ORIGINS',
        'NEXT_PUBLIC_MS_TENANT_ID', 'NEXT_PUBLIC_MS_CLIENT_ID',
        'NEXT_PUBLIC_MS_CLIENT_SECRET', 'NEXT_PUBLIC_MS_REDIRECT_URI',
        'TOKENS_KMS_KEY_ID',
        'AGENT_API_OUTPUT', 'AGENT_API_HANDLER', 'S3_BUCKET_NAME'
    ]
    
    for key in env_var_keys:
        if key in os.environ:
            config[key] = os.environ[key]
    
    return config

