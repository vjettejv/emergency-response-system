"""Small configurable Nominatim adapter; no contact data or background scans."""
import hashlib
import json
import math
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from rest_framework.exceptions import APIException, Throttled


class GeocodingUnavailable(APIException):
    status_code = 503
    default_detail = "Không tìm được địa chỉ lúc này. Bạn vẫn có thể chọn vị trí trên bản đồ."


def readable_address(item):
    address = item.get("address") or {}
    street = " ".join(str(address.get(key, "")) for key in ("house_number", "road")).strip()
    locality = next((address[key] for key in ("quarter", "suburb", "neighbourhood", "city_district", "village") if address.get(key)), "")
    city = next((address[key] for key in ("city", "town", "municipality", "state", "province") if address.get(key)), "")
    parts = list(dict.fromkeys(str(value) for value in (street or item.get("name"), locality, city) if value))
    return (", ".join(parts) or str(item.get("display_name", "")))[:500]


def lookup(operation, parameters):
    provider = settings.GEOCODER_BASE_URL.rstrip("/")
    parsed = urlsplit(provider)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GeocodingUnavailable()
    parameters = {"format": "jsonv2", "addressdetails": 1, "accept-language": "vi", **parameters}
    key = "geocoding:" + hashlib.sha256((provider + operation + urlencode(parameters)).encode()).hexdigest()
    try:
        saved = cache.get(key)
        if saved is not None:
            return saved
        # Shared Redis in production; one upstream request/second for the whole app.
        if not cache.add("geocoding:upstream_slot", True, timeout=1):
            raise Throttled(wait=1)
        request = Request(provider + "/" + operation + "?" + urlencode(parameters),
                          headers={"User-Agent": settings.GEOCODER_USER_AGENT, "Accept": "application/json"})
        with urlopen(request, timeout=5) as response:
            raw = response.read(200001)
        if len(raw) > 200000:
            raise ValueError
        data = json.loads(raw)
        items = data if operation == "search" else [data]
        if not isinstance(items, list):
            raise ValueError
        results = []
        for item in items[:5]:
            if not isinstance(item, dict) or item.get("error"):
                continue
            latitude, longitude = float(item["lat"]), float(item["lon"])
            if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise ValueError
            results.append({"latitude": latitude, "longitude": longitude, "address": readable_address(item)})
        cache.set(key, results, timeout=settings.GEOCODER_CACHE_SECONDS)
        return results
    except Throttled:
        raise
    except Exception:
        # Never log provider URLs, query/location values or raw exception responses.
        raise GeocodingUnavailable() from None


def search(query):
    return lookup("search", {"q": query, "limit": 5, "countrycodes": "vn"})


def reverse(latitude, longitude):
    results = lookup("reverse", {"lat": f"{latitude:.5f}", "lon": f"{longitude:.5f}", "zoom": 18})
    return {"address": results[0]["address"] if results else ""}
