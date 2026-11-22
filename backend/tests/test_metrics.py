import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "metrics-test-signing-key-123456"

from fastapi import APIRouter
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.metrics import record_http_request, reset_metrics, render_metrics


metrics_router = APIRouter()


@metrics_router.get("/_test/metrics-error")
def metrics_error():
    raise RuntimeError("forced metrics error")


@metrics_router.get("/_test/metrics/{item_id}")
def metrics_parameterized(item_id: int):
    return {"item_id": item_id}


app.include_router(metrics_router)


def test_metrics_endpoint_reports_http_requests():
    reset_metrics()
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    metrics_response = client.get("/metrics")
    assert metrics_response.status_code == 200
    assert (
        metrics_response.headers["content-type"]
        == "text/plain; version=0.0.4; charset=utf-8"
    )
    body = metrics_response.text
    assert "noviq_http_requests_total" in body
    assert 'route="/"' in body
    assert 'method="GET"' in body
    assert 'status="200"' in body
    assert "noviq_http_request_duration_seconds" in body
    assert 'route="/metrics"' not in body


def test_metrics_monitor_error_responses():
    reset_metrics()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/_test/metrics-error")

    assert response.status_code == 500
    body = client.get("/metrics").text
    assert "noviq_http_request_errors_total" in body
    assert 'status_class="5xx"' in body
    assert 'route="/_test/metrics-error"' in body


def test_histogram_bucket_counts_are_cumulative_and_have_inf():
    reset_metrics()

    record_http_request("GET", "/test", 200, 0.01)
    record_http_request("GET", "/test", 200, 0.4)
    record_http_request("GET", "/test", 200, 3.0)
    body = render_metrics()

    assert 'noviq_http_request_duration_seconds_bucket{method="GET",route="/test",status="200",le="0.01"} 1' in body
    assert 'noviq_http_request_duration_seconds_bucket{method="GET",route="/test",status="200",le="0.5"} 2' in body
    assert 'noviq_http_request_duration_seconds_bucket{method="GET",route="/test",status="200",le="10.0"} 3' in body
    assert 'noviq_http_request_duration_seconds_bucket{method="GET",route="/test",status="200",le="+Inf"} 3' in body
    assert 'noviq_http_request_duration_seconds_count{method="GET",route="/test",status="200"} 3' in body


def test_unhandled_500_increments_requests_and_errors():
    reset_metrics()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/_test/metrics-error")

    assert response.status_code == 500
    body = client.get("/metrics").text
    assert 'noviq_http_requests_total{method="GET",route="/_test/metrics-error",status="500"} 1' in body
    assert 'noviq_http_request_errors_total{method="GET",route="/_test/metrics-error",status_class="5xx"} 1' in body


def test_metrics_route_is_excluded_and_route_templates_prevent_dynamic_ids():
    reset_metrics()
    client = TestClient(app)

    client.get("/metrics")
    client.get("/metrics")
    body = client.get("/metrics").text

    assert 'route="/metrics"' not in body
    assert 'route="/projects/{project_id}"' not in body

    record_http_request("GET", "/projects/{project_id}", 200, 0.1)
    body = render_metrics()
    assert 'route="/projects/{project_id}"' in body
    assert "/projects/123" not in body


def test_parameterized_route_records_template_not_actual_url():
    reset_metrics()
    client = TestClient(app)

    response = client.get("/_test/metrics/123")

    assert response.status_code == 200
    assert response.json() == {"item_id": 123}
    body = client.get("/metrics").text
    assert 'route="/_test/metrics/{item_id}"' in body
    assert "/_test/metrics/123" not in body
