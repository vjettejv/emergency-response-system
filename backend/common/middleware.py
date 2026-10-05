from django.conf import settings
from django.http import HttpResponse
from django.utils.cache import patch_vary_headers


class RestrictedCorsMiddleware:
    """Optional exact-origin CORS for token REST clients; no credential cookies."""

    methods = {"GET", "POST", "PATCH", "DELETE", "HEAD", "OPTIONS"}
    headers = {"authorization", "content-type", "x-csrftoken"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = request.headers.get("Origin")
        api = request.path.startswith("/api/v1/")
        allowed = api and origin in settings.CORS_ALLOWED_ORIGINS
        preflight = api and request.method == "OPTIONS" and "Access-Control-Request-Method" in request.headers
        if preflight:
            method = request.headers.get("Access-Control-Request-Method")
            requested = {item.strip().lower() for item in request.headers.get("Access-Control-Request-Headers", "").split(",") if item.strip()}
            if not allowed or method not in self.methods or not requested <= self.headers:
                return HttpResponse(status=403)
            response = HttpResponse(status=204)
            response["Access-Control-Allow-Methods"] = ", ".join(sorted(self.methods))
            response["Access-Control-Allow-Headers"] = ", ".join(sorted(self.headers))
            response["Access-Control-Max-Age"] = "300"
        else:
            response = self.get_response(request)
        if api:
            patch_vary_headers(response, ["Origin"])
        if allowed:
            response["Access-Control-Allow-Origin"] = origin
        return response
