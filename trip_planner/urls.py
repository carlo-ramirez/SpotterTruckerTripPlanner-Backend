from django.urls import path
from .views import TripPlannerView

urlpatterns = [
    path('plan-trip/', TripPlannerView.as_view(), name='plan-trip'),
]
