from copy import deepcopy
from uuid import uuid4

from django.conf import settings
from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class IsolatedRealtimeTestRunner(DiscoverRunner):
    """Prevent transaction tests from publishing into development WS groups."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        layers = deepcopy(settings.CHANNEL_LAYERS)
        layers["default"]["CONFIG"]["prefix"] = f"test.{uuid4().hex}"
        self.channel_settings = override_settings(CHANNEL_LAYERS=layers)
        self.channel_settings.enable()

    def teardown_test_environment(self, **kwargs):
        self.channel_settings.disable()
        super().teardown_test_environment(**kwargs)
