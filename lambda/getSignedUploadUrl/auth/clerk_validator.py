"""
Clerk JWT Token Validation for AWS Lambda

This module provides utilities to validate Clerk JWT tokens in Lambda functions.
It fetches Clerk's public keys (JWKS) and verifies token signatures.
"""

import json
import jwt
import requests
from functools import lru_cache
from typing import Optional, Dict, Any

# Clerk domains - Support both TEST and PRODUCTION
CLERK_PRODUCTION_DOMAIN = "clerk.resumi.cv"
CLERK_TEST_DOMAIN = "advanced-pony-6.accounts.dev"

# CORS headers for all responses
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS"
}


@lru_cache(maxsize=2)
def get_clerk_jwks(domain: str):
    """
    Fetch Clerk's public keys (JWKS) for JWT verification.
    Cached to avoid repeated requests.

    Args:
        domain: Clerk domain (production or test)

    Returns:
        dict: JWKS (JSON Web Key Set) from Clerk
    """
    try:
        jwks_url = f"https://{domain}/.well-known/jwks.json"
        print(f"Fetching JWKS from: {jwks_url}")
        response = requests.get(jwks_url, timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error fetching Clerk JWKS from {domain}: {str(e)}")
        return None


def get_signing_key(token: str) -> Optional[str]:
    """
    Extract the public key from Clerk JWKS that matches the token's key ID.

    Args:
        token: JWT token string

    Returns:
        str: Public key in PEM format, or None if not found
    """
    try:
        # Get the key ID from token header
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get('kid')

        if not kid:
            print("No 'kid' found in token header")
            return None

        # Get JWKS from Clerk
        jwks = get_clerk_jwks()
        if not jwks:
            return None

        # Find the matching key
        for key in jwks.get('keys', []):
            if key.get('kid') == kid:
                # Convert JWK to PEM format
                return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))

        print(f"No matching key found for kid: {kid}")
        return None

    except Exception as e:
        print(f"Error getting signing key: {str(e)}")
        return None


def verify_clerk_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Verify a Clerk JWT token and extract the payload.

    Args:
        token: JWT token string (without 'Bearer ' prefix)

    Returns:
        dict: Token payload containing userId (as 'sub') and other claims
        None: If token is invalid
    """
    try:
        # Get the signing key
        signing_key = get_signing_key(token)
        if not signing_key:
            print("Could not get signing key")
            return None

        # Verify and decode the token
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            options={"verify_exp": True}  # Verify token hasn't expired
        )

        print(f"Token verified successfully for user: {payload.get('sub')}")
        return payload

    except jwt.ExpiredSignatureError:
        print("Token has expired")
        return None
    except jwt.InvalidTokenError as e:
        print(f"Invalid token: {str(e)}")
        return None
    except Exception as e:
        print(f"Error verifying token: {str(e)}")
        return None


def extract_token_from_header(authorization_header: Optional[str]) -> Optional[str]:
    """
    Extract JWT token from Authorization header.

    Args:
        authorization_header: Full Authorization header value (e.g., "Bearer abc123")

    Returns:
        str: Token without 'Bearer ' prefix
        None: If header is missing or malformed
    """
    if not authorization_header:
        return None

    parts = authorization_header.split()
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        print(f"Malformed Authorization header: {authorization_header}")
        return None

    return parts[1]


def get_user_id_from_event(event: Dict[str, Any]) -> Optional[str]:
    """
    Extract and validate user ID from Lambda event.

    This is the main function to call from Lambda handlers.
    It extracts the token from headers, validates it, and returns the user ID.

    Args:
        event: Lambda event dictionary

    Returns:
        str: Clerk user ID (from 'sub' claim in token)
        None: If authentication fails
    """
    try:
        # Get Authorization header
        headers = event.get('headers', {})
        auth_header = headers.get('Authorization') or headers.get('authorization')

        if not auth_header:
            print("No Authorization header found")
            return None

        # Extract token
        token = extract_token_from_header(auth_header)
        if not token:
            print("Could not extract token from header")
            return None

        # Verify token
        payload = verify_clerk_token(token)
        if not payload:
            print("Token verification failed")
            return None

        # Extract user ID (stored in 'sub' claim)
        user_id = payload.get('sub')
        if not user_id:
            print("No 'sub' claim in token payload")
            return None

        return user_id

    except Exception as e:
        print(f"Error getting user ID from event: {str(e)}")
        return None


def create_unauthorized_response(message: str = "Unauthorized") -> Dict[str, Any]:
    """
    Create a standard 401 Unauthorized response.

    Args:
        message: Error message to return

    Returns:
        dict: Lambda response dictionary
    """
    return {
        "statusCode": 401,
        "headers": CORS_HEADERS,
        "body": json.dumps({"error": message})
    }


def create_forbidden_response(message: str = "Forbidden") -> Dict[str, Any]:
    """
    Create a standard 403 Forbidden response.

    Args:
        message: Error message to return

    Returns:
        dict: Lambda response dictionary
    """
    return {
        "statusCode": 403,
        "headers": CORS_HEADERS,
        "body": json.dumps({"error": message})
    }
