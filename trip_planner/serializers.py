from rest_framework import serializers


class TripPlanRequestSerializer(serializers.Serializer):
    current_location = serializers.CharField(max_length=255)
    pickup_location = serializers.CharField(max_length=255)
    dropoff_location = serializers.CharField(max_length=255)
    cycle_used = serializers.FloatField(min_value=0, max_value=70, required=False, default=0)

    def validate(self, attrs):
        for field in ("current_location", "pickup_location", "dropoff_location"):
            attrs[field] = attrs[field].strip()
            if not attrs[field]:
                raise serializers.ValidationError({field: "This field cannot be blank."})
        return attrs
