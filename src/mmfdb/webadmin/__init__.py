"""Standalone HTTP and browser administration surface for MMFDB.

The web application deliberately depends on the same backend service handlers
as the embedded Qt administrator.  It has no ChiSurf or web-framework
dependency and can therefore run from the standalone MMFDB wheel.
"""

from .app import MAX_REQUEST_BYTES, WebAdminApp, create_app, serve

__all__ = ["MAX_REQUEST_BYTES", "WebAdminApp", "create_app", "serve"]
