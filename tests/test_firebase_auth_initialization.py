import sys
import types
import unittest
from unittest.mock import patch

from artrack import auth


class FirebaseAdminInitializationTests(unittest.TestCase):
    def test_initializes_with_configured_project_without_adc(self):
        firebase_admin = types.ModuleType("firebase_admin")
        google_auth_credentials = types.ModuleType("google.auth.credentials")
        initialize_calls = []

        class AnonymousCredentials:
            pass

        def get_app():
            raise ValueError("default app missing")

        def initialize_app(credential, *, options):
            initialize_calls.append((credential, options))

        firebase_admin.get_app = get_app
        firebase_admin.initialize_app = initialize_app
        google_auth_credentials.AnonymousCredentials = AnonymousCredentials

        with patch.dict(
            sys.modules,
            {
                "firebase_admin": firebase_admin,
                "google.auth.credentials": google_auth_credentials,
            },
        ):
            with patch.object(auth.settings, "FIREBASE_PROJECT_ID", "test-project"):
                auth._ensure_firebase_admin_initialized()

        self.assertEqual(len(initialize_calls), 1)
        credential, options = initialize_calls[0]
        self.assertIsInstance(credential, AnonymousCredentials)
        self.assertEqual(options, {"projectId": "test-project"})

    def test_reuses_existing_default_app(self):
        firebase_admin = types.ModuleType("firebase_admin")
        firebase_admin.get_app = lambda: object()
        firebase_admin.initialize_app = lambda **_: self.fail(
            "initialize_app must not run for an existing app"
        )

        with patch.dict(sys.modules, {"firebase_admin": firebase_admin}):
            auth._ensure_firebase_admin_initialized()


if __name__ == "__main__":
    unittest.main()
