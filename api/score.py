from http.server import BaseHTTPRequestHandler
import json
import urllib.request
import urllib.error
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

def get_lat_lng(postcode):
    postcode_clean = postcode.replace(' ', '')
    url = f"https://api.postcodes.io/postcodes/{postcode_clean}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
            return data['result']['latitude'], data['result']['longitude']
    except:
        return None, None

def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def get_schools(lat, lng):
    try:
        url = f"https://get-information-schools.service.gov.uk/api/v1/schools?lat={lat}&lon={lng}&radiusInMiles=1.5&phase=primary&includeOffline=false"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read())
            schools = []
            for s in data.get('results', [])[:30]:
                slat = s.get('lat')
                slng = s.get('lon')
                dist = round(haversine(lat, lng, slat, slng), 2) if slat and slng else 99
                schools.append({
                    'name': s.get('name', ''),
                    'ofsted': s.get('ofstedRating', 'Not rated'),
                    'distance_miles': dist,
                    'address': s.get('address', {}).get('postcode', '')
                })
            schools.sort(key=lambda x: x['distance_miles'])
            nearest = schools[:2]
            outstanding = [s for s in schools if 'outstanding' in s['ofsted'].lower()][:2]
            return nearest, outstanding
    except:
        return [], []

def get_crime(lat, lng):
    try:
        url = f"https://data.police.uk/api/crimes-street/all-crime?lat={lat}&lng={lng}&date=2024-06"
        with urllib.request.urlopen(url, timeout=6) as r:
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
        with urllib.request.urlopen(url, timeout=6) as r:
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
        with urllib.request.urlopen(url, timeout=6) as r:
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

def call_claude(prompt, api_key):
    url = "https://api.anthropic.com/v1/messages"
    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01'
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read())
            return data['content'][0]['text']
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        raise Exception(f"Claude HTTP {e.code}: {error_body}")

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_POST(self):
        api_key = os.environ.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            self._respond({'error': 'API key not configured'}, 500)
            return

        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))
        postcode = body.get('postcode', '').strip()
        address  = body.get('address', '').strip()
        price    = body.get('price', '')
        tenure   = body.get('tenure', '')
        beds     = body.get('beds', '')
        weights  = body.get('weights', {})

        lat, lng = get_lat_lng(postcode)
        if not lat:
            self._respond({'error': f'Could not find postcode {postcode}'}, 400)
            return

        # Fetch all external APIs in parallel
        nearest_schools, outstanding_schools = [], []
        crime      = None
        flood_risk = 'Very low'
        transport  = None

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(get_schools, lat, lng): 'schools',
                executor.submit(get_crime, lat, lng): 'crime',
                executor.submit(get_flood_risk, lat, lng): 'flood',
                executor.submit(get_tfl, lat, lng): 'tfl',
            }
            for future in as_completed(futures, timeout=10):
                key = futures[future]
                try:
                    result = future.result()
                    if key == 'schools':
                        nearest_schools, outstanding_schools = result
                    elif key == 'crime':
                        crime = result
                    elif key == 'flood':
                        flood_risk = result
                    elif key == 'tfl':
                        transport = result
                except:
                    pass

        nearest_str     = '; '.join([f"{s['name']} ({s['ofsted']}, {s['distance_miles']}mi)" for s in nearest_schools]) or 'No data available'
        outstanding_str = '; '.join([f"{s['name']} ({s['distance_miles']}mi)" for s in outstanding_schools]) or 'None found within 1.5 miles'
        crime_str       = f"Total: {crime['total']}, Burglary: {crime['burglary']}, Vehicle: {crime['vehicle']}, ASB: {crime['asb']}, Violence: {crime['violence']}" if crime else 'No data available'
        transport_str   = f"{transport['name']}, {transport['distance_miles']}mi ({transport['walk_mins']} min walk), Lines: {', '.join(transport['lines'])}" if transport else 'No data available'
        wstr            = ', '.join([f"{k}:{v}%" for k,v in weights.items()]) if weights else 'schools:30%, transport:20%, crime:20%, amenities:15%, environment:10%, financials:5%'

        prompt = f"""You are a London property research assistant helping a young family (2.5-year-old, planning 10+ years). Schools are their top priority.

Use ONLY the real verified data below for schools, crime, flood risk and transport.

Property: {address}
Postcode: {postcode}
Price: {price if price else 'not provided'}
Tenure: {tenure if tenure else 'not provided'}
Bedrooms: {beds if beds else 'not provided'}

REAL DATA:
- Two nearest primaries: {nearest_str}
- Two nearest Outstanding primaries: {outstanding_str}
- Crime Jun 2024 within 1 mile: {crime_str}
- Flood risk: {flood_risk}
- Transport: {transport_str}

Scoring weights: {wstr}

Return ONLY valid JSON with no markdown or preamble. Use double curly braces for the JSON structure. The JSON must have these exact keys: address, area, postcode, overallScore, summary, keyFacts (with nearestTube, council, schoolsNearby, floodRisk), categories (array of 6 objects each with id, name, score, headline, details, tags), nearestSchools, outstandingSchools, greenFlags, watchPoints."""

        try:
            raw = call_claude(prompt, api_key)
            clean = raw.strip()
            if clean.startswith('```'):
                clean = clean.split('```')[1]
                if clean.startswith('json'):
                    clean = clean[4:]
            sc = json.loads(clean.strip())
            sc['nearestSchools'] = nearest_schools
            sc['outstandingSchools'] = outstanding_schools
            sc['_realData'] = {
                'crime': crime,
                'flood_risk': flood_risk,
                'transport': transport
            }
            self._respond(sc)
        except Exception as e:
            self._respond({'error': f'Claude error: {str(e)}'}, 500)

    def _respond(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)
