import asyncio
import os
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "user-security-test-signing-key-123456"

from pydantic import ValidationError

from backend.app.main import app, lifespan
from backend.app.schemas.users import TeamMemberCreate, UserCreate
from backend.app.utils import jwt
from backend.app.utils.security import hash_password, verify_password


class UserSecurityValidationTests(unittest.TestCase):
    def test_user_input_requires_valid_email_and_password_length(self) -> None:
        with self.assertRaises(ValidationError):
            UserCreate(
                company_name="Example",
                name="User",
                email="not-an-email",
                password="short",
            )

        with self.assertRaises(ValidationError):
            UserCreate(
                company_name="Example",
                name="User",
                email="user@example.test",
                password="short",
            )

    def test_team_member_input_validates_email_and_password_length(self) -> None:
        with self.assertRaises(ValidationError):
            TeamMemberCreate(
                name="Member",
                email="invalid",
                password="secure-password",
            )

        with self.assertRaises(ValidationError):
            TeamMemberCreate(
                name="Member",
                email="member@example.test",
                password="short",
            )

    def test_password_schemas_enforce_72_utf8_byte_limit(self) -> None:
        boundary_password = "é" * 36
        oversized_password = "é" * 37

        for schema in (UserCreate, TeamMemberCreate):
            valid = schema(
                **(
                    {
                        "company_name": "Example",
                        "name": "User",
                    }
                    if schema is UserCreate
                    else {"name": "Member"}
                ),
                email="user@example.test",
                password=boundary_password,
            )
            self.assertEqual(len(valid.password.encode("utf-8")), 72)

            with self.assertRaises(ValidationError):
                schema(
                    **(
                        {
                            "company_name": "Example",
                            "name": "User",
                        }
                        if schema is UserCreate
                        else {"name": "Member"}
                    ),
                    email="user@example.test",
                    password=oversized_password,
                )

    def test_password_hashing_rejects_oversized_utf8_passwords(self) -> None:
        boundary_password = "é" * 36
        oversized_password = "é" * 37
        password_hash = hash_password(boundary_password)

        self.assertTrue(verify_password(boundary_password, password_hash))
        with self.assertRaises(ValueError):
            hash_password(oversized_password)
        self.assertFalse(verify_password(oversized_password, password_hash))

    def test_jwt_secret_validation_rejects_missing_or_short_secret(self) -> None:
        with patch.object(jwt, "SECRET_KEY", None):
            with self.assertRaises(RuntimeError):
                jwt.validate_jwt_secret()

        with patch.object(jwt, "SECRET_KEY", "too-short"):
            with self.assertRaises(RuntimeError):
                jwt.validate_jwt_secret()

    def test_jwt_secret_validation_accepts_strong_secret(self) -> None:
        with patch.object(
            jwt,
            "SECRET_KEY",
            "aB3dE5gH7jK9mP2rT4vX6z!@#-$%^&*()",
        ):
            jwt.validate_jwt_secret()

    def test_application_startup_rejects_invalid_jwt_secret(self) -> None:
        async def run_lifespan() -> None:
            async with lifespan(app):
                pass

        with patch.object(jwt, "SECRET_KEY", "too-short"):
            with self.assertRaises(RuntimeError):
                asyncio.run(run_lifespan())
