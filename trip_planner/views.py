from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import requests
import math
from datetime import datetime, timedelta

class TripPlannerView(APIView):
    def get_route(self, start, end):
        try:
            url = f"http://router.project-osrm.org/route/v1/driving/{start.longitude},{start.latitude};{end.longitude},{end.latitude}?overview=full&geometries=geojson"
            response = requests.get(url, timeout=10)
            res = response.json()
            if res.get('code') != 'Ok':
                print(f"OSRM Error: {res.get('code')} for coordinates {start.latitude},{start.longitude} to {end.latitude},{end.longitude}")
                return None, res.get('code')
            return res['routes'][0], 'Ok'
        except Exception as e:
            print(f"Routing Exception: {str(e)}")
            return None, str(e)

    def post(self, request):
        current_loc_str = request.data.get('current_location')
        pickup_loc_str = request.data.get('pickup_location')
        dropoff_loc_str = request.data.get('dropoff_location')
        cycle_used = float(request.data.get('cycle_used', 0))

        # Increased timeout to 10s to avoid ReadTimeoutError
        geolocator = Nominatim(user_agent="spotter_trip_planner", timeout=10)
        # Added RateLimiter to handle many requests gracefully
        geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.5)

        try:
            current_loc = geocode(current_loc_str)
            pickup_loc = geocode(pickup_loc_str)
            dropoff_loc = geocode(dropoff_loc_str)

            if not current_loc:
                return Response({"error": f"Could not find location: {current_loc_str}"}, status=status.HTTP_400_BAD_REQUEST)
            if not pickup_loc:
                return Response({"error": f"Could not find location: {pickup_loc_str}"}, status=status.HTTP_400_BAD_REQUEST)
            if not dropoff_loc:
                return Response({"error": f"Could not find location: {dropoff_loc_str}"}, status=status.HTTP_400_BAD_REQUEST)

            route_to_pickup, pickup_status = self.get_route(current_loc, pickup_loc)
            route_to_dropoff, dropoff_status = self.get_route(pickup_loc, dropoff_loc)

            if not route_to_pickup:
                error_msg = "Could not calculate route to pickup."
                if pickup_status == 'NoRoute':
                    error_msg += " No land-based driving route found (might involve sea crossing)."
                return Response({"error": error_msg}, status=status.HTTP_400_BAD_REQUEST)
                
            if not route_to_dropoff:
                error_msg = "Could not calculate route to destination."
                if dropoff_status == 'NoRoute':
                    error_msg += " No land-based driving route found (might involve sea crossing)."
                return Response({"error": error_msg}, status=status.HTTP_400_BAD_REQUEST)

            total_distance_meters = route_to_pickup['distance'] + route_to_dropoff['distance']
            total_duration_seconds = route_to_pickup['duration'] + route_to_dropoff['duration']

            # HOS Calculations
            # Assumptions:
            # - Property-carrying driver, 70hrs/8days
            # - 11-hour driving limit
            # - 14-hour on-duty window
            # - 30-minute rest break after 8 hours of driving
            # - 10 consecutive hours off duty
            # - Fueling every 1,000 miles
            # - 1 hour for pickup and drop-off

            logs = []
            current_time = datetime.now()
            remaining_cycle = 70 - cycle_used
            
            total_miles = total_distance_meters * 0.000621371
            total_driving_hours = total_duration_seconds / 3600
            
            # Simplified simulation
            # 1 hour for pickup
            # 1 hour for dropoff
            # Fueling every 1000 miles (approx 0.5 hour)
            
            current_day_driving = 0
            current_day_duty = 0
            total_miles_covered = 0
            
            # Start at current location
            # Drive to pickup
            # Load (1 hour)
            # Drive to dropoff
            # Unload (1 hour)
            
            events = [
                {"type": "DRIVING", "duration": route_to_pickup['duration'] / 3600, "distance": route_to_pickup['distance'] * 0.000621371, "desc": "To Pickup"},
                {"type": "ON_DUTY_NOT_DRIVING", "duration": 1, "distance": 0, "desc": "Pickup/Loading"},
                {"type": "DRIVING", "duration": route_to_dropoff['duration'] / 3600, "distance": route_to_dropoff['distance'] * 0.000621371, "desc": "To Dropoff"},
                {"type": "ON_DUTY_NOT_DRIVING", "duration": 1, "distance": 0, "desc": "Dropoff/Unloading"}
            ]
            
            # 1. Add fueling logic
            events_with_fuel = []
            miles_since_fuel = 0
            for event in events:
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
            
            events = events_with_rest

            # 3. Simulate day-by-day with 10-hour rest and 24-hour timeline
            daily_logs = []
            current_day_events = []
            current_time_in_day = 0 # 0 to 24 hours
            day_count = 0
            
            # Tracking HOS limits
            current_day_driving = 0
            current_day_duty = 0
            total_cycle_duty = cycle_used
            
            def finish_day():
                nonlocal current_time_in_day, current_day_events, day_count, current_day_driving, current_day_duty
                # Fill the rest of the 24 hours with OFF_DUTY if needed
                if current_time_in_day < 24:
                    current_day_events.append({
                        "type": "OFF_DUTY",
                        "duration": 24 - current_time_in_day,
                        "distance": 0,
                        "desc": "End of Day Rest"
                    })
                
                daily_logs.append({
                    "day": day_count + 1,
                    "events": current_day_events,
                    "total_miles": sum(e.get('distance', 0) for e in current_day_events),
                    "total_driving": sum(e['duration'] for e in current_day_events if e['type'] == 'DRIVING'),
                    "total_on_duty": sum(e['duration'] for e in current_day_events if e['type'] in ['DRIVING', 'ON_DUTY_NOT_DRIVING'])
                })
                day_count += 1
                current_day_events = []
                current_time_in_day = 0
                current_day_driving = 0
                current_day_duty = 0

            def add_event_to_timeline(type, duration, distance, desc):
                nonlocal current_time_in_day, current_day_events, current_day_driving, current_day_duty, total_cycle_duty
                remaining_event_duration = duration
                
                while remaining_event_duration > 0:
                    time_left_in_day = 24 - current_time_in_day
                    if time_left_in_day <= 0:
                        finish_day()
                        continue
                        
                    duration_to_add = min(remaining_event_duration, time_left_in_day)
                    
                    # If it's a driving/duty event, we must also check daily HOS limits AND cycle limits
                    if type in ['DRIVING', 'ON_DUTY_NOT_DRIVING']:
                        # 11hr driving limit, 14hr duty limit
                        if type == 'DRIVING':
                            daily_limit_left = min(11 - current_day_driving, 14 - current_day_duty)
                        else:
                            daily_limit_left = 14 - current_day_duty
                        
                        # Cycle limit (70 hours)
                        cycle_limit_left = 70 - total_cycle_duty
                        
                        if cycle_limit_left <= 0:
                            # Must take 34-hour restart
                            add_34_hour_restart()
                            continue
                            
                        if daily_limit_left <= 0:
                            # Must take 10 hour rest break now
                            add_rest_break(10)
                            continue
                            
                        duration_to_add = min(duration_to_add, daily_limit_left, cycle_limit_left)
                    
                    current_day_events.append({
                        "type": type,
                        "duration": duration_to_add,
                        "distance": (duration_to_add / duration) * distance if duration > 0 else 0,
                        "desc": desc
                    })
                    
                    if type == 'DRIVING':
                        current_day_driving += duration_to_add
                    if type in ['DRIVING', 'ON_DUTY_NOT_DRIVING']:
                        current_day_duty += duration_to_add
                        total_cycle_duty += duration_to_add
                        
                    current_time_in_day += duration_to_add
                    remaining_event_duration -= duration_to_add
                    
                    if current_time_in_day >= 24:
                        finish_day()

            def add_rest_break(duration):
                add_event_to_timeline("SLEEPER", duration, 0, "Mandatory 10-hour Rest")
            
            def add_34_hour_restart():
                nonlocal total_cycle_duty
                add_event_to_timeline("OFF_DUTY", 34, 0, "34-hour Cycle Restart")
                total_cycle_duty = 0 # Reset cycle duty after 34-hour restart

            # Process all trip events
            for event in events:
                add_event_to_timeline(event['type'], event['duration'], event['distance'], event['desc'])
                
            # Finish the last day
            if current_day_events or current_time_in_day > 0:
                finish_day()

            return Response({
                "summary": {
                    "total_distance_miles": total_miles,
                    "total_driving_hours": total_driving_hours,
                    "estimated_days": day_count
                },
                "route": {
                    "to_pickup": route_to_pickup['geometry'],
                    "to_dropoff": route_to_dropoff['geometry']
                },
                "daily_logs": daily_logs
            })

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
