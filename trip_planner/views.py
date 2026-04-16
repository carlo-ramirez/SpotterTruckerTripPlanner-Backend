from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import requests
import math
from datetime import datetime, timedelta

class TripPlannerView(APIView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.geolocator = Nominatim(user_agent="spotter_trip_planner", timeout=10)
        self.geocode_service = RateLimiter(self.geolocator.geocode, min_delay_seconds=1.5)

    def get_route(self, start, end):
        try:
            url = f"http://router.project-osrm.org/route/v1/driving/{start.longitude},{start.latitude};{end.longitude},{end.latitude}?overview=full&geometries=geojson"
            response = requests.get(url, timeout=10)
            res = response.json()
            if res.get('code') != 'Ok':
                return None, res.get('code')
            return res['routes'][0], 'Ok'
        except Exception as e:
            return None, str(e)

    def geocode_locations(self, locations):
        results = {}
        for key, loc_str in locations.items():
            loc = self.geocode_service(loc_str)
            if not loc:
                return None, f"Could not find location: {loc_str}"
            results[key] = loc
        return results, None

    def apply_hos_rules(self, raw_events, initial_cycle_used):
        # 1. Add fueling logic (every 1000 miles)
        events_with_fuel = []
        miles_since_fuel = 0
        for event in raw_events:
            if event['type'] == 'DRIVING':
                remaining_distance = event['distance']
                while miles_since_fuel + remaining_distance >= 1000:
                    can_go = 1000 - miles_since_fuel
                    ratio = can_go / event['distance']
                    events_with_fuel.append({
                        "type": "DRIVING",
                        "duration": event['duration'] * ratio,
                        "distance": can_go,
                        "desc": event['desc']
                    })
                    events_with_fuel.append({
                        "type": "ON_DUTY_NOT_DRIVING",
                        "duration": 0.5,
                        "distance": 0,
                        "desc": "Fueling"
                    })
                    remaining_distance -= can_go
                    miles_since_fuel = 0
                if remaining_distance > 0:
                    events_with_fuel.append({
                        "type": "DRIVING",
                        "duration": (remaining_distance / event['distance']) * event['duration'],
                        "distance": remaining_distance,
                        "desc": event['desc']
                    })
                    miles_since_fuel += remaining_distance
            else:
                events_with_fuel.append(event)

        # 2. Add 30-minute rest break after 8 hours of driving
        events_with_rest = []
        driving_since_break = 0
        for event in events_with_fuel:
            if event['type'] == 'DRIVING':
                remaining_duration = event['duration']
                while driving_since_break + remaining_duration >= 8:
                    can_drive = 8 - driving_since_break
                    ratio = can_drive / event['duration']
                    events_with_rest.append({
                        "type": "DRIVING",
                        "duration": can_drive,
                        "distance": event['distance'] * ratio,
                        "desc": event['desc']
                    })
                    events_with_rest.append({
                        "type": "OFF_DUTY",
                        "duration": 0.5,
                        "distance": 0,
                        "desc": "30-minute Rest Break"
                    })
                    remaining_duration -= can_drive
                    driving_since_break = 0
                if remaining_duration > 0:
                    events_with_rest.append({
                        "type": "DRIVING",
                        "duration": remaining_duration,
                        "distance": (remaining_duration / event['duration']) * event['distance'] if event['duration'] > 0 else 0,
                        "desc": event['desc']
                    })
                    driving_since_break += remaining_duration
            else:
                events_with_rest.append(event)
        
        return self.simulate_daily_logs(events_with_rest, initial_cycle_used)

    def simulate_daily_logs(self, events, initial_cycle_used):
        daily_logs = []
        current_day_events = []
        state = {
            'time_in_day': 0,
            'day_count': 0,
            'day_driving': 0,
            'day_duty': 0,
            'cycle_duty': initial_cycle_used
        }

        def finish_day():
            if state['time_in_day'] < 24:
                current_day_events.append({
                    "type": "OFF_DUTY",
                    "duration": 24 - state['time_in_day'],
                    "distance": 0,
                    "desc": "End of Day Rest"
                })
            
            daily_logs.append({
                "day": state['day_count'] + 1,
                "events": list(current_day_events),
                "total_miles": sum(e.get('distance', 0) for e in current_day_events),
                "total_driving": sum(e['duration'] for e in current_day_events if e['type'] == 'DRIVING'),
                "total_on_duty": sum(e['duration'] for e in current_day_events if e['type'] in ['DRIVING', 'ON_DUTY_NOT_DRIVING'])
            })
            state['day_count'] += 1
            state['time_in_day'] = 0
            state['day_driving'] = 0
            state['day_duty'] = 0
            current_day_events.clear()

        def add_to_timeline(event_type, duration, distance, desc):
            remaining = duration
            while remaining > 0:
                time_left = 24 - state['time_in_day']
                if time_left <= 0:
                    finish_day()
                    continue
                
                chunk = min(remaining, time_left)
                
                if event_type in ['DRIVING', 'ON_DUTY_NOT_DRIVING']:
                    # HOS Checks
                    if state['cycle_duty'] >= 70:
                        add_to_timeline("OFF_DUTY", 34, 0, "34-hour Cycle Restart")
                        state['cycle_duty'] = 0
                        continue

                    limit = 11 - state['day_driving'] if event_type == 'DRIVING' else 14 - state['day_duty']
                    limit = min(limit, 14 - state['day_duty'], 70 - state['cycle_duty'])

                    if limit <= 0:
                        add_to_timeline("SLEEPER", 10, 0, "Mandatory 10-hour Rest")
                        continue
                    
                    chunk = min(chunk, limit)

                current_day_events.append({
                    "type": event_type,
                    "duration": chunk,
                    "distance": (chunk / duration) * distance if duration > 0 else 0,
                    "desc": desc
                })

                if event_type == 'DRIVING':
                    state['day_driving'] += chunk
                if event_type in ['DRIVING', 'ON_DUTY_NOT_DRIVING']:
                    state['day_duty'] += chunk
                    state['cycle_duty'] += chunk
                
                state['time_in_day'] += chunk
                remaining -= chunk
                if state['time_in_day'] >= 24:
                    finish_day()

        for event in events:
            add_to_timeline(event['type'], event['duration'], event['distance'], event['desc'])
        
        if current_day_events or state['time_in_day'] > 0:
            finish_day()
            
        return daily_logs, state['day_count']

    def post(self, request):
        try:
            # 1. Geocoding
            locations, error = self.geocode_locations({
                'current': request.data.get('current_location'),
                'pickup': request.data.get('pickup_location'),
                'dropoff': request.data.get('dropoff_location')
            })
            if error:
                return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)

            # 2. Routing
            route_p, status_p = self.get_route(locations['current'], locations['pickup'])
            route_d, status_d = self.get_route(locations['pickup'], locations['dropoff'])

            if not route_p or not route_d:
                return Response({"error": f"Routing failed: {status_p if not route_p else status_d}"}, status=status.HTTP_400_BAD_REQUEST)

            # 3. Preparation
            cycle_used = float(request.data.get('cycle_used', 0))
            raw_events = [
                {"type": "DRIVING", "duration": route_p['duration'] / 3600, "distance": route_p['distance'] * 0.000621371, "desc": "To Pickup"},
                {"type": "ON_DUTY_NOT_DRIVING", "duration": 1, "distance": 0, "desc": "Pickup/Loading"},
                {"type": "DRIVING", "duration": route_d['duration'] / 3600, "distance": route_d['distance'] * 0.000621371, "desc": "To Dropoff"},
                {"type": "ON_DUTY_NOT_DRIVING", "duration": 1, "distance": 0, "desc": "Dropoff/Unloading"}
            ]

            # 4. HOS Simulation
            daily_logs, day_count = self.apply_hos_rules(raw_events, cycle_used)

            return Response({
                "summary": {
                    "total_distance_miles": (route_p['distance'] + route_d['distance']) * 0.000621371,
                    "total_driving_hours": (route_p['duration'] + route_d['duration']) / 3600,
                    "estimated_days": day_count
                },
                "route": {
                    "to_pickup": route_p['geometry'],
                    "to_dropoff": route_d['geometry']
                },
                "daily_logs": daily_logs
            })

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
