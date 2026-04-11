from http.server import BaseHTTPRequestHandler
import json
import urllib.request
import urllib.parse
import math
import os

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
        with urllib.request.urlopen(req, timeout=8) as r:
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
    except Exception as e:
        return [], []

def get_crime(lat, lng):
    try:
        url = f"https://data.police.uk/api/crimes-street/all-crime?lat={lat}&lng={lng}&date=2024-06"
        with urllib.request.urlopen(url, timeout=10) as r:
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

def call_claude(prompt, api_key):
    url = "https://api.anthropic.com/v1/messages"
    payload = json.dumps({
        "model": "claude-sonnet-4-5",
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
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
        return data['content'][0]['text']

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

        nearest_schools, outstanding_schools = get_schools(lat, lng)
        crime      = get_crime(lat, lng)
        flood_risk = get_flood_risk(lat, lng)
        transport  = get_tfl(lat, lng)

        nearest_str     = '; '.join([f"{s['name']} ({s['ofsted']}, {s['distance_miles']}mi)" for s in nearest_schools]) or 'No data'
        outstanding_str = '; '.join([f"{s['name']} ({s['distance_miles']}mi)" for s in outstanding_schools]) or 'None within 1.5 miles'
        crime_str       = f"Total: {crime['total']}, Burglary: {crime['burglary']}, Vehicle: {crime['vehicle']}, ASB: {crime['asb']}, Violence: {crime['violence']}" if crime else 'No data'
        transport_str   = f"{transport['name']}, {transport['distance_miles']}mi ({transport['walk_mins']} min walk), Lines: {', '.join(transport['lines'])}" if transport else 'No data'
        wstr            = ', '.join([f"{k}:{v}%" for k,v in weights.items()]) if weights else 'schools:30%, transport:20%, crime:20%, amenities:15%, environment:10%, financials:5%'

        prompt = f"""You are a London property research assistant helping a young family (2.5-year-old, planning 10+ years). Schools are their top priority.

Use ONLY the real verified data below for schools, crime, flood risk and transport. Do not substitute your own estimates for these figures.

Property: {address}
Postcode: {postcode}
{f'Price: {price}' if price else ''}
{f'Tenure: {tenure}' if tenure else ''}
{f'Bedrooms: {beds}' if beds else ''}

REAL DATA:
- Two nearest primaries: {nearest_str}
- Two nearest Outstanding primaries: {outstanding_str}
- Crime (Jun 2024, within 1 mile): {crime_str}
- Flood risk: {flood_risk}
- Transport: {transport_str}

Scoring weights: {wstr}

Return ONLY valid JSON, no markdown, no preamble:
{{
  "address": "formatted address",
  "area": "neighbourhood, borough",
  "postcode": "{postcode}",
  "overallScore": <0-100>,
  "summary": "3 sentence honest family-focused assessment",
  "keyFacts": {{
    "nearestTube": "use exact transport data above",
    "council": "borough name",
    "schoolsNearby": "summary using exact school data above",
    "floodRisk": "{flood_risk}"
  }},
  "categories": [
    {{
      "id": "schools",
      "name": "Primary schools",
      "score": <1-5>,
      "headline": "one line using real school names",
      "details": "Use the exact school names, ratings and distances provided. List both nearest primaries and both nearest Outstanding schools. Note if any are faith schools.",
      "tags": [{{"label":"...","type":"good|warn|bad|neutral"}}]
    }},
    {{"id":"transport","name":"Transport","score":<1-5>,"headline":"one line","details":"Use exact transport data. Add your knowledge of lines and journey times.","tags":[{{"label":"...","type":"good|warn|bad|neutral"}}]}},
    {{"id":"crime","name":"Crime & safety","score":<1-5>,"headline":"one line","details":"Use exact crime figures. Inner London average is roughly 80-120 crimes/month within 1 mile.","tags":[{{"label":"...","type":"good|warn|bad|neutral"}}]}},
    {{"id":"amenities","name":"Local amenities","score":<1-5>,"headline":"one line","details":"Use your knowledge of this area for amenities.","tags":[{{"label":"...","type":"good|warn|bad|neutral"}}]}},
    {{"id":"environment","name":"Environment","score":<1-5>,"headline":"one line","details":"Use exact flood risk. Use your knowledge for ULEZ and noise.","tags":[{{"label":"...","type":"good|warn|bad|neutral"}}]}},
    {{"id":"financials","name":"Financials & value","score":<1-5>,"headline":"one line","details":"Use your knowledge for council tax, price comparisons and value.","tags":[{{"label":"...","type":"good|warn|bad|neutral"}}]}}
  ],
  "nearestSchools": {json.dumps(nearest_schools)},
  "outstandingSchools": {json.dumps(outstanding_schools)},
  "greenFlags": ["point 1","point 2","point 3"],
  "watchPoints": ["concern 1","concern 2","concern 3"]
}}"""

        try:
            raw = call_claude(prompt, api_key)
            sc  = json.loads(raw.replace('```json','').replace('```','').strip())
            sc['_realData'] = {
                'nearest_schools': nearest_schools,
                'outstanding_schools': outstanding_schools,
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
