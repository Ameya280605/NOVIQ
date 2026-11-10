import os
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import ANY, Mock, patch

os.environ["DATABASE_URL"] = "sqlite://"

import boto3
from botocore.exceptions import ClientError

from backend.app.services.s3_storage import (
    ObjectMetadataMismatchError,
    S3Settings,
    S3Storage,
    StorageConfigurationError,
    StorageServiceError,
    generate_object_key,
)


class S3StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = S3Settings(
            region="us-east-1",
            bucket="private-documents-test",
            presigned_url_ttl_seconds=240,
        )
        self.client = Mock()
        self.client.generate_presigned_url.return_value = (
            "https://s3.example.test/signed"
        )
        self.storage = S3Storage(self.settings, self.client)

    def test_server_generated_keys_are_opaque_and_validated(self) -> None:
        key = generate_object_key()
        self.assertRegex(key, r"^documents/[0-9a-f]{32}$")
        self.assertNotIn("tenant", key)
        self.assertNotIn("filename", key)

        with self.assertRaises(ValueError):
            self.storage.create_download_url("../../public/object")
        self.client.generate_presigned_url.assert_not_called()

    def test_upload_presign_is_short_lived_private_and_encrypted(self) -> None:
        result = self.storage.create_upload_url("application/pdf")

        self.assertRegex(result["object_key"], r"^documents/[0-9a-f]{32}$")
        self.assertEqual(result["url"], "https://s3.example.test/signed")
        self.assertEqual(result["headers"]["Content-Type"], "application/pdf")
        self.assertEqual(
            result["headers"]["x-amz-server-side-encryption"], "AES256"
        )
        self.client.generate_presigned_url.assert_called_once()
        operation, kwargs = self.client.generate_presigned_url.call_args
        self.assertEqual(operation, ("put_object",))
        self.assertEqual(kwargs["ExpiresIn"], 240)
        self.assertEqual(kwargs["Params"]["Bucket"], self.settings.bucket)
        self.assertEqual(kwargs["Params"]["ContentType"], "application/pdf")
        self.assertEqual(kwargs["Params"]["ServerSideEncryption"], "AES256")
        self.assertNotIn("ACL", kwargs["Params"])

    def test_upload_presign_uses_optional_kms_encryption(self) -> None:
        storage = S3Storage(
            S3Settings(
                region="us-east-1",
                bucket="private-documents-test",
                kms_key_id="alias/noviq-documents",
                presigned_url_ttl_seconds=120,
            ),
            self.client,
        )
        result = storage.create_upload_url("text/plain")
        params = self.client.generate_presigned_url.call_args.kwargs["Params"]

        self.assertEqual(params["ServerSideEncryption"], "aws:kms")
        self.assertEqual(params["SSEKMSKeyId"], "alias/noviq-documents")
        self.assertEqual(
            result["headers"]["x-amz-server-side-encryption-aws-kms-key-id"],
            "alias/noviq-documents",
        )

    def test_download_presign_uses_short_expiration_and_private_key(self) -> None:
        url = self.storage.create_download_url("documents/" + "a" * 32)

        self.assertEqual(url, "https://s3.example.test/signed")
        operation, kwargs = self.client.generate_presigned_url.call_args
        self.assertEqual(operation, ("get_object",))
        self.assertEqual(kwargs["ExpiresIn"], 240)
        self.assertEqual(
            kwargs["Params"]["Key"], "documents/" + "a" * 32
        )

    def test_object_metadata_returns_only_expected_fields(self) -> None:
        self.client.head_object.return_value = {
            "ContentLength": 91,
            "ContentType": "application/pdf",
            "ETag": '"etag"',
            "ChecksumSHA256": "checksum",
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": "kms-key",
            "Metadata": {"internal": "ignored"},
        }

        metadata = self.storage.get_object_metadata(
            "documents/" + "b" * 32
        )

        self.assertEqual(metadata.content_length, 91)
        self.assertEqual(metadata.content_type, "application/pdf")
        self.assertEqual(metadata.etag, '"etag"')
        self.assertEqual(metadata.checksum_sha256, "checksum")
        self.assertEqual(metadata.server_side_encryption, "aws:kms")
        self.assertEqual(metadata.kms_key_id, "kms-key")
        self.assertFalse(hasattr(metadata, "Metadata"))
        self.client.head_object.assert_called_once_with(
            Bucket=self.settings.bucket,
            Key="documents/" + "b" * 32,
        )

    def test_uploaded_object_metadata_is_checked_against_expected_values(self) -> None:
        self.client.head_object.return_value = {
            "ContentLength": 91,
            "ContentType": "application/pdf",
        }
        metadata = self.storage.verify_uploaded_object(
            "documents/" + "1" * 32, "application/pdf", 91
        )
        self.assertEqual(metadata.content_length, 91)

        with self.assertRaises(ObjectMetadataMismatchError):
            self.storage.verify_uploaded_object(
                "documents/" + "1" * 32, "text/plain", 91
            )
        with self.assertRaises(ObjectMetadataMismatchError):
            self.storage.verify_uploaded_object(
                "documents/" + "1" * 32, "application/pdf", 100
            )

    def test_delete_uses_configured_bucket_and_validated_key(self) -> None:
        key = "documents/" + "c" * 32
        self.storage.delete_object(key)
        self.client.delete_object.assert_called_once_with(
            Bucket=self.settings.bucket, Key=key
        )

    def test_s3_error_marks_only_transient_failures_retryable(self) -> None:
        transient = ClientError(
            {
                "Error": {"Code": "SlowDown", "Message": "retry"},
                "ResponseMetadata": {"HTTPStatusCode": 503},
            },
            "DeleteObject",
        )
        self.client.delete_object.side_effect = transient
        with self.assertRaises(StorageServiceError) as transient_error:
            self.storage.delete_object("documents/" + "1" * 32)
        self.assertTrue(transient_error.exception.retryable)

        permanent = ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "denied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "DeleteObject",
        )
        self.client.delete_object.side_effect = permanent
        with self.assertRaises(StorageServiceError) as permanent_error:
            self.storage.delete_object("documents/" + "1" * 32)
        self.assertFalse(permanent_error.exception.retryable)

    def test_missing_configuration_fails_before_client_creation(self) -> None:
        storage = S3Storage(S3Settings(region=None, bucket=None))
        with patch("backend.app.services.s3_storage.boto3.client") as client:
            with self.assertRaises(StorageConfigurationError):
                storage.create_download_url("documents/" + "d" * 32)
        client.assert_not_called()

    def test_environment_configuration_and_ttl_validation(self) -> None:
        env = {
            "AWS_REGION": "eu-west-1",
            "S3_BUCKET_NAME": "documents",
            "AWS_S3_ENDPOINT_URL": "http://localhost:9000",
            "AWS_S3_KMS_KEY_ID": "kms-key",
            "S3_PRESIGNED_URL_TTL_SECONDS": "30",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = S3Settings.from_env()
        self.assertEqual(settings.region, "eu-west-1")
        self.assertEqual(settings.bucket, "documents")
        self.assertEqual(settings.endpoint_url, "http://localhost:9000")
        self.assertEqual(settings.kms_key_id, "kms-key")
        self.assertEqual(settings.presigned_url_ttl_seconds, 30)

        for invalid_ttl in ("not-a-number", "0", "901"):
            with self.subTest(ttl=invalid_ttl):
                with patch.dict(
                    os.environ,
                    {"S3_PRESIGNED_URL_TTL_SECONDS": invalid_ttl},
                    clear=True,
                ):
                    with self.assertRaises(StorageConfigurationError):
                        S3Settings.from_env()

    def test_valid_environment_configuration_enables_document_storage(self) -> None:
        env = {
            "AWS_REGION": "eu-west-1",
            "S3_BUCKET_NAME": "private-documents",
            "AWS_ACCESS_KEY_ID": "test-access-key",
            "AWS_SECRET_ACCESS_KEY": "test-secret-key",
        }
        with patch.dict(os.environ, env, clear=True):
            storage = S3Storage(S3Settings.from_env())
            with patch(
                "backend.app.services.s3_storage.boto3.client",
                return_value=self.client,
            ) as client_factory:
                storage.create_upload_url("application/pdf")

        client_factory.assert_called_once_with(
            "s3",
            region_name="eu-west-1",
            endpoint_url="https://s3.eu-west-1.amazonaws.com",
            config=ANY,
        )
        self.assertEqual(
            client_factory.call_args.kwargs["config"].signature_version,
            "s3v4",
        )
        self.assertEqual(
            self.client.generate_presigned_url.call_args.kwargs["Params"][
                "Bucket"
            ],
            "private-documents",
        )
        self.assertNotIn(
            "test-secret-key",
            repr(client_factory.call_args) + repr(storage._settings),
        )

    def test_missing_bucket_environment_returns_safe_configuration_error(
        self,
    ) -> None:
        env = {
            "AWS_REGION": "eu-west-1",
            "AWS_ACCESS_KEY_ID": "test-access-key",
            "AWS_SECRET_ACCESS_KEY": "test-secret-key",
        }
        with patch.dict(os.environ, env, clear=True):
            storage = S3Storage(S3Settings.from_env())
            with patch(
                "backend.app.services.s3_storage.boto3.client"
            ) as client_factory:
                with self.assertRaises(StorageConfigurationError) as error:
                    storage.create_upload_url("application/pdf")

        self.assertEqual(
            str(error.exception), "AWS region and S3 bucket must be configured"
        )
        self.assertNotIn("test-secret-key", str(error.exception))
        client_factory.assert_not_called()

    def test_client_uses_standard_credential_chain_and_optional_endpoint(self) -> None:
        storage = S3Storage(
            S3Settings(
                region="eu-west-1",
                bucket="documents",
                endpoint_url="http://localhost:9000",
            )
        )
        with patch(
            "backend.app.services.s3_storage.boto3.client",
            return_value=self.client,
        ) as client_factory:
            storage.create_download_url("documents/" + "e" * 32)

        client_factory.assert_called_once_with(
            "s3",
            region_name="eu-west-1",
            endpoint_url="http://localhost:9000",
            config=ANY,
        )
        self.assertEqual(
            client_factory.call_args.kwargs["config"].signature_version,
            "s3v4",
        )
        self.assertNotIn("aws_access_key_id", client_factory.call_args.kwargs)
        self.assertNotIn("aws_secret_access_key", client_factory.call_args.kwargs)

    def test_presigned_upload_uses_mumbai_regional_endpoint_and_sigv4(self) -> None:
        settings = S3Settings(
            region="ap-south-1",
            bucket="noviq-documents-ameya-2026",
        )

        def create_test_client(service_name: str, **kwargs):
            session = boto3.session.Session(
                aws_access_key_id="test-access-key",
                aws_secret_access_key="test-secret-key",
            )
            return session.client(service_name, **kwargs)

        storage = S3Storage(settings)
        with patch(
            "backend.app.services.s3_storage.boto3.client",
            side_effect=create_test_client,
        ) as client_factory:
            result = storage.create_upload_url("application/pdf")

        parsed_url = urlparse(result["url"])
        query = parse_qs(parsed_url.query)
        self.assertIn("s3.ap-south-1.amazonaws.com", parsed_url.netloc)
        self.assertEqual(query["X-Amz-Algorithm"], ["AWS4-HMAC-SHA256"])
        self.assertIn(
            "/ap-south-1/s3/aws4_request",
            query["X-Amz-Credential"][0],
        )
        client_factory.assert_called_once()
        self.assertEqual(
            client_factory.call_args.kwargs["region_name"], "ap-south-1"
        )
        self.assertEqual(
            client_factory.call_args.kwargs["endpoint_url"],
            "https://s3.ap-south-1.amazonaws.com",
        )

    def test_aws_errors_are_wrapped_without_exposing_provider_details(self) -> None:
        provider_error = ClientError(
            {
                "Error": {
                    "Code": "AccessDenied",
                    "Message": "sensitive provider detail",
                }
            },
            "HeadObject",
        )
        self.client.head_object.side_effect = provider_error
        with self.assertRaises(StorageServiceError) as error:
            self.storage.get_object_metadata("documents/" + "f" * 32)
        self.assertEqual(str(error.exception), "Object storage operation failed")
        self.assertNotIn("sensitive provider detail", str(error.exception))

        self.client.delete_object.side_effect = provider_error
        with self.assertRaises(StorageServiceError):
            self.storage.delete_object("documents/" + "f" * 32)

        self.client.generate_presigned_url.side_effect = provider_error
        with self.assertRaises(StorageServiceError):
            self.storage.create_upload_url("application/pdf")

    def test_upload_rejects_empty_content_type(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.create_upload_url(" ")
        self.client.generate_presigned_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
