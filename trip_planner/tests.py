from unittest.mock import patch
from rest_framework.test import APITestCase
from trip_planner.views import TripPlannerView


class TripPlannerValidationTests(APITestCase):
    def test_rejects_cycle_used_over_70(self):
        response = self.client.post('/api/plan-trip/', {
            "current_location": "Dallas, TX",
            "pickup_location": "Austin, TX",
            "dropoff_location": "Houston, TX",
            "cycle_used": 71
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('cycle_used', response.data)

    def test_rejects_blank_locations(self):
        response = self.client.post('/api/plan-trip/', {
            "current_location": "   ",
            "pickup_location": "Austin, TX",
            "dropoff_location": "Houston, TX",
            "cycle_used": 5
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('current_location', response.data)

    @patch('trip_planner.views.TripPlannerView.geocode_locations')
    @patch('trip_planner.views.TripPlannerView.get_route')
    def test_accepts_valid_payload(self, mock_get_route, mock_geocode_locations):
        mock_geocode_locations.return_value = ({
            'current': object(),
            'pickup': object(),
            'dropoff': object(),
        }, None)
        mock_get_route.side_effect = [
            ({
                'duration': 3600,
                'distance': 16093.4,
                'geometry': {'coordinates': [[-96.8, 32.7], [-97.7, 30.2]]}
            }, 'Ok'),
            ({
                'duration': 7200,
                'distance': 32186.8,
                'geometry': {'coordinates': [[-97.7, 30.2], [-95.3, 29.7]]}
            }, 'Ok'),
        ]

        response = self.client.post('/api/plan-trip/', {
            "current_location": "Dallas, TX",
            "pickup_location": "Austin, TX",
            "dropoff_location": "Houston, TX",
            "cycle_used": 5
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertIn('summary', response.data)
        self.assertIn('daily_logs', response.data)

    def test_on_duty_not_driving_can_satisfy_30_min_break(self):
        view = TripPlannerView()
        raw_events = [
            {"type": "DRIVING", "duration": 7.8, "distance": 500, "desc": "Leg 1"},
            {"type": "ON_DUTY_NOT_DRIVING", "duration": 0.5, "distance": 0, "desc": "Fueling"},
            {"type": "DRIVING", "duration": 0.4, "distance": 25, "desc": "Leg 2"},
        ]

        daily_logs, _ = view.apply_hos_rules(raw_events, 0)
        all_events = [event for day in daily_logs for event in day["events"]]
        break_events = [event for event in all_events if event["desc"] == "30-minute Rest Break"]

        self.assertEqual(len(break_events), 0)
