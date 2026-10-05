import os
from pathlib import Path
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def required_env(name):
    value = os.environ.get(name)
    if not value:
        raise ImproperlyConfigured(f"Environment variable {name} is required.")
    return value


def positive_int_env(name, default):
    try:
        value = int(os.environ.get(name, default))
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a positive integer.") from exc
    if value <= 0:
        raise ImproperlyConfigured(f"{name} must be a positive integer.")
    return value


def bool_env(name, default=False):
    value = os.environ.get(name, str(default)).lower()
    if value not in ("true", "false"):
        raise ImproperlyConfigured(f"{name} must be true or false.")
    return value == "true"


def list_env(name, default=""):
    return [value.strip() for value in os.environ.get(name, default).split(",") if value.strip()]


ENVIRONMENT = os.environ.get("DJANGO_ENV", "development")
if ENVIRONMENT not in ("development", "production"):
    raise ImproperlyConfigured("DJANGO_ENV must be development or production.")
PRODUCTION = ENVIRONMENT == "production"


CLUSTER_RADIUS_METERS = positive_int_env("CLUSTER_RADIUS_METERS", 300)
CLUSTER_TIME_WINDOW_MINUTES = positive_int_env("CLUSTER_TIME_WINDOW_MINUTES", 30)
CLUSTER_MAX_CANDIDATES = positive_int_env("CLUSTER_MAX_CANDIDATES", 50)
DISPATCH_RADIUS_METERS = positive_int_env("DISPATCH_RADIUS_METERS", 50000)
DISPATCH_MAX_SUGGESTIONS = positive_int_env("DISPATCH_MAX_SUGGESTIONS", 20)
GPS_MIN_INTERVAL_SECONDS = positive_int_env("GPS_MIN_INTERVAL_SECONDS", 5)
GPS_MAX_AGE_SECONDS = positive_int_env("GPS_MAX_AGE_SECONDS", 120)
GPS_FUTURE_TOLERANCE_SECONDS = positive_int_env("GPS_FUTURE_TOLERANCE_SECONDS", 30)
WS_AUTH_TIMEOUT_SECONDS = 5
WS_HEARTBEAT_SECONDS = 30
WS_ALLOWED_ORIGINS = list_env("WS_ALLOWED_ORIGINS", "" if PRODUCTION else "http://localhost:8000,http://127.0.0.1:8000")
ASGI_APPLICATION = "config.asgi.application"
TEST_RUNNER = "config.test_runner.IsolatedRealtimeTestRunner"
CHANNEL_LAYERS = {"default": {
    "BACKEND": "channels_redis.core.RedisChannelLayer",
    "CONFIG": {"hosts": [{"address": os.environ.get("REDIS_URL", "redis://redis:6379/0"),
                          "socket_connect_timeout": 1, "socket_timeout": 10}],
               "prefix": os.environ.get("REDIS_CHANNEL_PREFIX", "emergency"),
               "expiry": 60, "group_expiry": 120, "capacity": 100},
}}


SECRET_KEY = required_env("DJANGO_SECRET_KEY")
DEBUG = bool_env("DJANGO_DEBUG")
ALLOWED_HOSTS = list_env("DJANGO_ALLOWED_HOSTS", "" if PRODUCTION else "localhost,127.0.0.1")
HTTPS_ENABLED = bool_env("DJANGO_HTTPS_ENABLED", PRODUCTION)
CSRF_TRUSTED_ORIGINS = list_env("CSRF_TRUSTED_ORIGINS")
CORS_ALLOWED_ORIGINS = list_env("CORS_ALLOWED_ORIGINS")
if PRODUCTION:
    if DEBUG or len(SECRET_KEY) < 50 or "replace-with" in SECRET_KEY:
        raise ImproperlyConfigured("Production requires DEBUG=false and a random secret of at least 50 characters.")
    if not ALLOWED_HOSTS or any("*" in host or host.startswith(".") for host in ALLOWED_HOSTS):
        raise ImproperlyConfigured("Production requires explicit DJANGO_ALLOWED_HOSTS.")
    if not WS_ALLOWED_ORIGINS:
        raise ImproperlyConfigured("Production requires explicit WS_ALLOWED_ORIGINS.")
    for origin in WS_ALLOWED_ORIGINS + CSRF_TRUSTED_ORIGINS + CORS_ALLOWED_ORIGINS:
        parsed = urlparse(origin)
        if (parsed.scheme not in (("https",) if HTTPS_ENABLED else ("http", "https"))
                or not parsed.hostname or "*" in origin or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ImproperlyConfigured("Production origins must be explicit HTTP(S) origins without paths or wildcards.")

INSTALLED_APPS = [
    "daphne",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "rest_framework",
    "rest_framework.authtoken",
    "accounts",
    "incidents",
    "teams",
    "dispatch",
    "realtime",
    "evidence",
    "common",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "common.middleware.RestrictedCorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": os.environ.get("POSTGRES_DB", "emergency"),
        "USER": os.environ.get("POSTGRES_USER", "emergency"),
        "PASSWORD": required_env("POSTGRES_PASSWORD"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {"connect_timeout": 5, "sslmode": os.environ.get("POSTGRES_SSLMODE", "verify-full" if PRODUCTION else "prefer")},
        "CONN_HEALTH_CHECKS": True,
    }
}
if os.environ.get("POSTGRES_SSLROOTCERT"):
    DATABASES["default"]["OPTIONS"]["sslrootcert"] = os.environ["POSTGRES_SSLROOTCERT"]
if PRODUCTION and (DATABASES["default"]["OPTIONS"]["sslmode"] != "verify-full"
                   or not os.environ.get("POSTGRES_SSLROOTCERT")):
    raise ImproperlyConfigured("Production requires POSTGRES_SSLMODE=verify-full and POSTGRES_SSLROOTCERT.")
CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache",
                      "LOCATION": os.environ.get("REDIS_CACHE_URL", "redis://redis:6379/2"),
                      "OPTIONS": {"socket_connect_timeout": 2, "socket_timeout": 2},
                      "KEY_PREFIX": "emergency-auth"}} if PRODUCTION else {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
}
# Optional native-library paths for a Windows installation of GeoDjango.
for library_setting in ("GDAL_LIBRARY_PATH", "GEOS_LIBRARY_PATH"):
    if os.environ.get(library_setting):
        globals()[library_setting] = os.environ[library_setting]

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_THROTTLE_RATES": {"auth": "20/min", "geocoding": "30/min"},
    "NUM_PROXIES": 1 if PRODUCTION else None,
}
LANGUAGE_CODE = "vi"
TIME_ZONE = "Asia/Ho_Chi_Minh"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
LEAFLET_TILE_URL = os.environ.get("LEAFLET_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
GEOCODER_BASE_URL = os.environ.get("GEOCODER_BASE_URL", "https://nominatim.openstreetmap.org")
GEOCODER_USER_AGENT = os.environ.get("GEOCODER_USER_AGENT", "EmergencyResponseSystem/1.0 (+https://vjettejv.id.vn/)")
GEOCODER_CACHE_SECONDS = positive_int_env("GEOCODER_CACHE_SECONDS", 86400)
INCIDENT_LOCATION_DISTANCE_WARNING_METERS = positive_int_env("INCIDENT_LOCATION_DISTANCE_WARNING_METERS", 1000)
CAMERA_VIDEO_MAX_SECONDS = positive_int_env("CAMERA_VIDEO_MAX_SECONDS", 30)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_COOKIE_SECURE = HTTPS_ENABLED or not DEBUG
CSRF_COOKIE_SECURE = HTTPS_ENABLED or not DEBUG
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if PRODUCTION else None
SECURE_SSL_REDIRECT = PRODUCTION and HTTPS_ENABLED
# Readiness carries no operational data and is also used inside the containers.
SECURE_REDIRECT_EXEMPT = [r"^api/health/$"]
SECURE_HSTS_SECONDS = positive_int_env("DJANGO_HSTS_SECONDS", 3600) if PRODUCTION and HTTPS_ENABLED else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# Credentials are resolved by the AWS SDK environment/IAM credential chain.
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "")
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-1")
MEDIA_MAX_BYTES = positive_int_env("MEDIA_MAX_BYTES", 50 * 1024 * 1024)
MEDIA_UPLOAD_TTL_SECONDS = positive_int_env("MEDIA_UPLOAD_TTL_SECONDS", 300)
MEDIA_DOWNLOAD_TTL_SECONDS = positive_int_env("MEDIA_DOWNLOAD_TTL_SECONDS", 300)
MEDIA_CLEANUP_GRACE_SECONDS = positive_int_env("MEDIA_CLEANUP_GRACE_SECONDS", 120)
if max(MEDIA_UPLOAD_TTL_SECONDS, MEDIA_DOWNLOAD_TTL_SECONDS) > 3600:
    raise ImproperlyConfigured("Media URL TTL must not exceed 3600 seconds.")
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/1")
CELERY_TASK_DEFAULT_QUEUE = os.environ.get("CELERY_TASK_DEFAULT_QUEUE", "emergency-background")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_IGNORE_RESULT = True
CELERY_BROKER_CONNECTION_TIMEOUT = 2
CELERY_BROKER_TRANSPORT_OPTIONS = {"socket_connect_timeout": 2, "socket_timeout": 2, "visibility_timeout": 3600}
CELERY_TASK_PUBLISH_RETRY = False
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_CONNECTION_RETRY = True
CELERY_BROKER_CONNECTION_MAX_RETRIES = 10
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_WORKER_LOG_COLOR = False
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = 45
CELERY_TASK_TIME_LIMIT = 60
CELERY_BEAT_SCHEDULE = {"recover-media-cleanup": {"task": "evidence.sweep_media", "schedule": 60.0}}

LOGGING = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {"safe": {"()": "common.logging.SafeFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "safe"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        name: {"handlers": ["console"], "level": "WARNING", "propagate": False}
        for name in ("django.request", "django.server", "daphne", "botocore", "boto3", "urllib3")
    },
}
