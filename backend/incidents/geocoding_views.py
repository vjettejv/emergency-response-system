from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.permissions import IsCitizen
from . import geocoding
from .serializers import CoordinateField, StrictSerializer


class AddressSearchSerializer(StrictSerializer):
    query = serializers.CharField(min_length=3, max_length=200)


class ReverseAddressSerializer(StrictSerializer):
    latitude = CoordinateField(min_value=-90, max_value=90)
    longitude = CoordinateField(min_value=-180, max_value=180)


class GeocodingView(APIView):
    permission_classes = [IsCitizen]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "geocoding"

    def post(self, request, operation):
        serializer = (AddressSearchSerializer if operation == "search" else ReverseAddressSerializer)(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = geocoding.search(serializer.validated_data["query"]) if operation == "search" else geocoding.reverse(**serializer.validated_data)
        return Response(result, headers={"Cache-Control": "no-store"})
