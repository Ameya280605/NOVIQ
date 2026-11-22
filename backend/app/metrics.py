from threading import Lock


BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_lock = Lock()
_requests: dict[tuple[str, str, str], int] = {}
_errors: dict[tuple[str, str, str], int] = {}
_duration: dict[tuple[str, str, str], dict[str, object]] = {}


def reset_metrics() -> None:
    with _lock:
        _requests.clear()
        _errors.clear()
        _duration.clear()


def record_http_request(
    method: str,
    route: str,
    status_code: int,
    duration_seconds: float,
) -> None:
    if route == "/metrics":
        return

    safe_method = method.upper() if method.upper() in {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS",
    } else "OTHER"
    safe_route = route or "unmatched"
    safe_status = str(status_code)
    key = (safe_method, safe_route, safe_status)

    try:
        with _lock:
            _requests[key] = _requests.get(key, 0) + 1
            if status_code >= 400:
                status_class = f"{status_code // 100}xx"
                error_key = (safe_method, safe_route, status_class)
                _errors[error_key] = _errors.get(error_key, 0) + 1

            histogram = _duration.setdefault(
                key,
                {"count": 0, "sum": 0.0, "buckets": {bucket: 0 for bucket in BUCKETS}},
            )
            histogram["count"] = int(histogram["count"]) + 1
            histogram["sum"] = float(histogram["sum"]) + duration_seconds
            buckets = histogram["buckets"]
            for bucket in BUCKETS:
                if duration_seconds <= bucket:
                    buckets[bucket] = int(buckets[bucket]) + 1
    except Exception:
        return


def _escape_label(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _labels(method: str, route: str, extra_name: str, extra_value: str) -> str:
    return (
        f'method="{_escape_label(method)}",'
        f'route="{_escape_label(route)}",'
        f'{extra_name}="{_escape_label(extra_value)}"'
    )


def render_metrics() -> str:
    lines = [
        "# HELP noviq_http_requests_total Total HTTP requests.",
        "# TYPE noviq_http_requests_total counter",
    ]
    with _lock:
        for (method, route, status), count in sorted(_requests.items()):
            lines.append(
                "noviq_http_requests_total"
                f"{{{_labels(method, route, 'status', status)}}} {count}"
            )

        lines.extend([
            "# HELP noviq_http_request_errors_total HTTP error responses.",
            "# TYPE noviq_http_request_errors_total counter",
        ])
        for (method, route, status_class), count in sorted(_errors.items()):
            lines.append(
                "noviq_http_request_errors_total"
                f"{{{_labels(method, route, 'status_class', status_class)}}} {count}"
            )

        lines.extend([
            "# HELP noviq_http_request_duration_seconds HTTP request duration.",
            "# TYPE noviq_http_request_duration_seconds histogram",
        ])
        for (method, route, status), histogram in sorted(_duration.items()):
            label_prefix = _labels(method, route, "status", status)
            buckets = histogram["buckets"]
            for bucket in BUCKETS:
                cumulative = int(buckets[bucket])
                lines.append(
                    "noviq_http_request_duration_seconds_bucket"
                    f'{{{label_prefix},le="{bucket}"}} '
                    f"{cumulative}"
                )
            cumulative_count = int(histogram["count"])
            lines.append(
                "noviq_http_request_duration_seconds_bucket"
                f'{{{label_prefix},le="+Inf"}} {cumulative_count}'
            )
            lines.append(
                "noviq_http_request_duration_seconds_sum"
                f"{{{label_prefix}}} {float(histogram['sum']):.6f}"
            )
            lines.append(
                "noviq_http_request_duration_seconds_count"
                f"{{{label_prefix}}} {cumulative_count}"
            )

    return "\n".join(lines) + "\n"
