from http.server import BaseHTTPRequestHandler
import json
import urllib.request
import urllib.parse
import math

def get_lat_lng(postcode):
    postcode_clean = postcode.replace(' ', '')
    url = f"https://api.postcodes.io/postcodes/{postcode_clean}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
            return data['result']['latitude'], data['result']['longitude']
    except:
        return None, None

def get_schools(lat, lng):
    try:
        url = (f"https://educationandskillsfundingagency.github.io/fsd-app/"
               f"api/schools?lat={lat}&lon={lng}&radius=1609")
        # Use DfE API instead
        url = (f"https://api.gov.uk/education/schools?"
               f"lat={lat}&lng={lng}&radius=1.5")
        raise Exception("Use search fallback")
    except:
        pass

    try:
        url = f"https://get-information-schools.service.gov.uk/api/v1/schools?lat={lat}&lon={lng}&radiusInMiles=1&phase=primary&includeOffline=false"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
            schools = []
            for s in data.get('results', [])[:20]:
                schools.append({
                    'name': s.get('name', ''),
                    'ofsted': s.get('ofstedRating', 'Not rated'),
                    'lat': s.get('lat'),
                    'lng': s.get('lon'),
                    'address': s.get('address', {}).get('postcode', '')
                })
            return schools
    except:
        return []

def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def get_crime(lat, lng):
    try:
        url = f"https://data.police.uk/api/crimes-street/all-crime?lat={lat}&lng={lng}&date=2024-06"
        with urllib.request.urlopen(url, timeout=8) as r:
            crimes = json.loads(r.read())
            total = len(crimes)
            categories = {}
            for c in crimes:
                cat = c.get('category', 'other')
                categories[cat] = categories.get(cat, 0) + 1
            return {
                'total': total,
                'burglary': categories.get('burglary', 0),
                'vehicle': categories.get('vehicle-crime', 0),
                'asb': categories.get('anti-social-behaviour', 0),
                'violence': categories.get('violent-crime', 0)
            }
    except:
        return None

def get_flood_risk(lat, lng):
    try:
        url = f"https://environment.data.gov.uk/flood-monitoring/id/floodAreas?lat={lat}&long={lng}&dist=0.5"
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
            items = data.get('items', [])
            if not items:
                return 'Very low'
            severities = [i.get('floodWatchingLevel', '') for i in items]
            if any('severe' in str(s).lower() for s in severities):
                return 'High'
            if any('warning' in str(s).lower() for s in severities):
                return 'Medium'
            return 'Low'
    except:
        return 'Very low'

def get_tfl(lat, lng):
    try:
        url = f"https://api.tfl.gov.uk/StopPoint?lat={lat}&lon={lng}&stopTypes=NaptanMetroStation,NaptanRailStation&radius=1000&modes=tube,overground,elizabeth-line"
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
            stops = data.get('stopPoints', [])
            if stops:
                nearest = stops[0]
                dist_m = nearest.get('distance', 0)
                dist_miles = round(dist_m * 0.000621371, 2)
                walk_mins = round(dist_m / 80)
                lines = []
                for mode in nearest.get('lineModeGroups', []):
                    lines.extend(mode.get('lineIdentifier', []))
                return {
                    'name': nearest.get('commonName', ''),
                    'distance_miles': dist_miles,
                    'walk_mins': walk_mins,
                    'lines': lines[:3]
                }
    except:
        pass
    return None

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))
        postcode = body.get('postcode', '').strip()

        result = {'postcode': postcode, 'error': None}

        lat, lng = get_lat_lng(postcode)
        if not lat:
            result['error'] = f"Could not find postcode {postcode}"
            self._respond(result)
            return

        result['lat'] = lat
        result['lng'] = lng

        schools = get_schools(lat, lng)
        if schools:
            for s in schools:
                if s['lat'] and s['lng']:
                    s['distance_miles'] = round(haversine(lat, lng, s['lat'], s['lng']), 2)
            schools.sort(key=lambda x: x.get('distance_miles', 99))
            result['nearest_schools'] = schools[:2]
            outstanding = [s for s in schools if 'outstanding' in s['ofsted'].lower()]
            result['outstanding_schools'] = outstanding[:2]
        else:
            result['nearest_schools'] = []
            result['outstanding_schools'] = []

        result['crime'] = get_crime(lat, lng)
        result['flood_risk'] = get_flood_risk(lat, lng)
        result['transport'] = get_tfl(lat, lng)

        self._respond(result)

    def _respond(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)
