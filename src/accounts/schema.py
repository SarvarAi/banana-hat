"""Describe bearer-token authentication in the OpenAPI document."""

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class BearerTokenScheme(OpenApiAuthenticationExtension):
    target_class = "accounts.authentication.BearerTokenAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "description": "An API token from your account page: `Authorization: Bearer bh_...`. "
                           "Tokens are stored hashed, expire, and can be revoked.",
        }
