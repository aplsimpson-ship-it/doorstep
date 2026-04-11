from http.server import BaseHTTPRequestHandler
import json
import urllib.request
import urllib.error
import urllib.parse
import math
import os
import csv
import io
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Ofsted CSV URL (updated monthly by Ofsted) ──────────────────────────────
OFSTED_CSV_URL = (
    "https://assets.publishing.service.gov.uk/media/"
    "698b20be95285e721cd7127d/"
    "Management_information_-_state-funded_schools_-_"
    "latest_inspections_as_at_31_Jan_2026.csv"
)

# Module-level cache so we only download once per Vercel instance lifetime
_ofsted_cache = None

def load_ofsted_ratings():
    """Download and cache the Ofsted ratings CSV. Returns dict of URN -> rating."""
    global _ofsted_cache
    if _ofsted_cache is not None:
        return _ofsted_cache
    try:
        req = urllib.request.Request(
            OFSTED_CSV_URL,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            content = r.read().decode("utf-8", errors="replace")
        ratings = {}
        reader = csv.DictReader(io.StringIO(content))
        for row in reader:
            # Find URN and overall effectiveness columns
            urn = (row.get("URN") or row.get("urn") or "").strip()
            # Column name varies slightly across releases
            rating = (
                row.get("Overall effectiveness") or
                row.get("Overall Effectiveness") or
                row.get("overall_effectiveness") or ""
            ).strip()
            if urn and rating:
                ratings[urn] = rating
        _ofsted_cache = ratings
        return ratings
    except Exception as e:
        _ofsted_cache = {}
        return {}

def ofsted_code_to_label(code):
    """Convert numeric Ofsted code to human-readable label."""
    mapping = {
        "1": "Outstanding",
        "2": "Good",
        "3": "Requires improvement",
        "4": "Inadequate",
        "9": "Not yet inspected",
    }
    return mapping.get(str(code).strip(), str(code).strip() or "Not rated")

# ── Helpers ──────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def fetch_json(url, timeout=8, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

# ── Geocoding ─────────────────────────────────────────────────────────────────

def get_lat_lng(postcode):
    pc = postcode.replace(" ", "")
    try:
        data = fetch_json(f"https://api.postcodes.io/postcodes/{pc}", timeout=5)
        return data["result"]["latitude"], data["result"]["longitude"]
    except:
        return None, None

# ── Schools ───────────────────────────────────────────────────────────────────

def get_schools(lat, lng):
    """
    Fetch primary schools from GIAS within 1.5 miles,
    then look up Ofsted ratings from the cached CSV.
    Returns (nearest_2, nearest_2_outstanding).
    """
    ofsted = load_ofsted_ratings()
    try:
        url = (
            f"https://get-information-schools.service.gov.uk/api/v1/schools"
            f"?lat={lat}&lon={lng}&radiusInMiles=1.5"
            f"&phase=primary&includeOffline=false"
        )
        data = fetch_json(url, timeout=10)
        schools = []
        for s in data.get("results", [])[:40]:
            slat = s.get("lat")
            slng = s.get("lon")
            if not slat or not slng:
                continue
            dist = round(haversine(lat, lng, slat, slng), 2)
            urn  = str(s.get("urn") or "")
            # Look up rating from Ofsted CSV first, fall back to GIAS field
            raw_rating = ofsted.get(urn, "")
            if raw_rating:
                rating = ofsted_code_to_label(raw_rating)
            else:
                rating = s.get("ofstedRating") or "Not yet rated"
            schools.append({
                "name":           s.get("name", "Unknown"),
                "ofsted":         rating,
                "distance_miles": dist,
                "urn":            urn,
                "postcode":       (s.get("address") or {}).get("postcode", ""),
                "religious":      s.get("religiousCharacter", "None") not in ("", "None", "Does not apply"),
            })
        schools.sort(key=lambda x: x["distance_miles"])
        nearest     = schools[:2]
        outstanding = [s for s in schools if "outstanding" in s["ofsted"].lower()][:2]
        return nearest, outstanding
    except Exception as e:
        return [], []

# ── Crime ─────────────────────────────────────────────────────────────────────

def get_crime(lat, lng):
    try:
        url = (
            f"https://data.police.uk/api/crimes-street/all-crime"
            f"?lat={lat}&lng={lng}&date=2024-11"
        )
        crimes = fetch_json(url, timeout=10)
        cats = {}
        for c in crimes:
            cat = c.get("category", "other")
            cats[cat] = cats.get(cat, 0) + 1
        return {
            "total":    len(crimes),
            "burglary": cats.get("burglary", 0),
            "vehicle":  cats.get("vehicle-crime", 0),
            "asb":      cats.get("anti-social-behaviour", 0),
            "violence": cats.get("violent-crime", 0),
        }
    except:
        return None

# ── Flood risk ────────────────────────────────────────────────────────────────

def get_flood_risk(lat, lng):
    try:
        url = (
            f"https://environment.data.gov.uk/flood-monitoring/id/floodAreas"
            f"?lat={lat}&long={lng}&dist=0.5"
        )
        data  = fetch_json(url, timeout=8)
        items = data.get("items", [])
        if not items:
            return "Very low"
        levels = [str(i.get("floodWatchingLevel", "")).lower() for i in items]
        if any("severe" in l for l in levels):
            return "High"
        if any("warning" in l for l in levels):
            return "Medium"
        return "Low"
    except:
        return "Very low"

# ── Transport ─────────────────────────────────────────────────────────────────

def get_transport(lat, lng):
    try:
        url = (
            f"https://api.tfl.gov.uk/StopPoint"
            f"?lat={lat}&lon={lng}"
            f"&stopTypes=NaptanMetroStation,NaptanRailStation"
            f"&radius=1200"
            f"&modes=tube,overground,elizabeth-line,national-rail"
        )
        data  = fetch_json(url, timeout=8)
        stops = data.get("stopPoints", [])
        if not stops:
            return None
        # Sort by distance
        stops.sort(key=lambda s: s.get("distance", 9999))
        nearest  = stops[0]
        dist_m   = nearest.get("distance", 0)
        dist_mi  = round(dist_m * 0.000621371, 2)
        walk_min = max(1, round(dist_m / 80))  # ~80m/min walking pace
        lines = []
        for mode in nearest.get("lineModeGroups", []):
            lines.extend(mode.get("lineIdentifier", []))
        # Capitalise line names nicely
        nice_lines = [l.replace("-", " ").title() for l in lines[:4]]
        return {
            "name":           nearest.get("commonName", "Unknown"),
            "distance_miles": dist_mi,
            "walk_mins":      walk_min,
            "lines":          nice_lines,
        }
    except Exception as e:
        return None

# ── Claude ────────────────────────────────────────────────────────────────────

def call_claude(prompt, api_key):
    payload = json.dumps({
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 2000,
        "messages":   [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read())
            return data["content"][0]["text"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        raise Exception(f"Claude HTTP {e.code}: {body}")

# ── Prompt builder ────────────────────────────────────────────────────────────

def build_prompt(address, postcode, price, tenure, beds, weights,
                 nearest_schools, outstanding_schools,
                 crime, flood_risk, transport):

    def school_str(schools):
        if not schools:
            return "No data retrieved"
        parts = []
        for s in schools:
            faith = " (faith school — selective admissions)" if s.get("religious") else ""
            parts.append(f"{s['name']} — {s['ofsted']}, {s['distance_miles']}mi{faith}")
        return "; ".join(parts)

    crime_str = (
        f"Total crimes Nov 2024: {crime['total']} "
        f"(Burglary: {crime['burglary']}, "
        f"Vehicle: {crime['vehicle']}, "
        f"ASB: {crime['asb']}, "
        f"Violence: {crime['violence']})"
        if crime else "No crime data retrieved"
    )

    transport_str = (
        f"{transport['name']}, {transport['distance_miles']}mi "
        f"({transport['walk_mins']} min walk). "
        f"Lines: {', '.join(transport['lines'])}"
        if transport else "No transport data retrieved"
    )

    wstr = ", ".join(f"{k}:{v}%" for k, v in weights.items()) if weights else \
           "schools:30%, transport:20%, crime:20%, amenities:15%, environment:10%, financials:5%"

    return f"""You are a London property research assistant. A young family with a 2.5-year-old (planning 10+ years) wants to evaluate this property. Schools are their top priority.

IMPORTANT: Use the REAL DATA provided below exactly as given for schools, crime, flood risk and transport. Do not substitute your own knowledge for these figures.

Property: {address}
Postcode: {postcode}
Price: {price or 'not provided'}
Tenure: {tenure or 'not provided'}
Bedrooms: {beds or 'not provided'}

=== REAL VERIFIED DATA ===
Two nearest primary schools: {school_str(nearest_schools)}
Two nearest Outstanding primaries: {school_str(outstanding_schools) if outstanding_schools else 'None found within 1.5 miles'}
Crime (Nov 2024, within 1 mile): {crime_str}
Flood risk: {flood_risk}
Transport: {transport_str}

Scoring weights: {wstr}

Return ONLY a JSON object. No markdown, no explanation, just the JSON:
{{
  "address": "full formatted address",
  "area": "neighbourhood, borough",
  "postcode": "{postcode}",
  "overallScore": <integer 0-100 reflecting weighted scores>,
  "summary": "3 honest sentences for a family with a toddler staying 10+ years",
  "keyFacts": {{
    "nearestTube": "<station name> · <distance>mi · <walk_mins> min walk",
    "council": "borough name",
    "schoolsNearby": "brief summary using real school names and ratings above",
    "floodRisk": "{flood_risk}"
  }},
  "categories": [
    {{
      "id": "schools",
      "name": "Primary schools",
      "score": <1-5>,
      "headline": "one line referencing real school names",
      "details": "Use exact school names, ratings and distances from real data. Mention if Outstanding schools are faith schools with selective admissions.",
      "tags": [{{"label": "School Name — Rating", "type": "good|warn|bad|neutral"}}]
    }},
    {{
      "id": "transport",
      "name": "Transport",
      "score": <1-5>,
      "headline": "one line with station name and walk time",
      "details": "Use exact transport data. Add context about lines and typical journey times to central London.",
      "tags": [{{"label": "...", "type": "good|warn|bad|neutral"}}]
    }},
    {{
      "id": "crime",
      "name": "Crime & safety",
      "score": <1-5>,
      "headline": "one line with total crime figure",
      "details": "Use exact crime figures. Context: inner London average is roughly 100-150 total crimes/month within 1 mile.",
      "tags": [{{"label": "...", "type": "good|warn|bad|neutral"}}]
    }},
    {{
      "id": "amenities",
      "name": "Local amenities",
      "score": <1-5>,
      "headline": "one line",
      "details": "Use your knowledge of this specific area: supermarkets, parks, GP, restaurants, high street.",
      "tags": [{{"label": "...", "type": "good|warn|bad|neutral"}}]
    }},
    {{
      "id": "environment",
      "name": "Environment",
      "score": <1-5>,
      "headline": "one line",
      "details": "Use exact flood risk above. Add your knowledge of ULEZ status, nearby roads, air quality.",
      "tags": [{{"label": "...", "type": "good|warn|bad|neutral"}}]
    }},
    {{
      "id": "financials",
      "name": "Financials & value",
      "score": <1-5>,
      "headline": "one line",
      "details": "Council tax band estimate, price vs local average, leasehold issues if relevant, value assessment.",
      "tags": [{{"label": "...", "type": "good|warn|bad|neutral"}}]
    }}
  ],
  "greenFlags": ["specific point 1", "specific point 2", "specific point 3"],
  "watchPoints": ["specific concern 1", "specific concern 2", "specific concern 3"]
}}"""

# ── Handler ───────────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress default request logging

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            self._respond({"error": "ANTHROPIC_API_KEY not set"}, 500)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
        except Exception:
            self._respond({"error": "Invalid request body"}, 400)
            return

        postcode = body.get("postcode", "").strip().upper()
        address  = body.get("address", "").strip()
        price    = body.get("price", "")
        tenure   = body.get("tenure", "")
        beds     = body.get("beds", "")
        weights  = body.get("weights", {})

        if not postcode:
            self._respond({"error": "Postcode is required"}, 400)
            return

        lat, lng = get_lat_lng(postcode)
        if not lat:
            self._respond({"error": f"Could not geocode postcode: {postcode}"}, 400)
            return

        # Fetch all external data in parallel
        nearest_schools, outstanding_schools = [], []
        crime      = None
        flood_risk = "Very low"
        transport  = None

        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {
                ex.submit(get_schools,   lat, lng): "schools",
                ex.submit(get_crime,     lat, lng): "crime",
                ex.submit(get_flood_risk,lat, lng): "flood",
                ex.submit(get_transport, lat, lng): "transport",
            }
            for future in as_completed(futures, timeout=18):
                key = futures[future]
                try:
                    result = future.result()
                    if key == "schools":
                        nearest_schools, outstanding_schools = result
                    elif key == "crime":
                        crime = result
                    elif key == "flood":
                        flood_risk = result
                    elif key == "transport":
                        transport = result
                except Exception:
                    pass

        prompt = build_prompt(
            address, postcode, price, tenure, beds, weights,
            nearest_schools, outstanding_schools,
            crime, flood_risk, transport,
        )

        try:
            raw   = call_claude(prompt, api_key)
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            sc    = json.loads(clean)
            # Always attach real data so frontend can render school rows directly
            sc["nearestSchools"]     = nearest_schools
            sc["outstandingSchools"] = outstanding_schools
            sc["_realData"] = {
                "crime":      crime,
                "flood_risk": flood_risk,
                "transport":  transport,
            }
            self._respond(sc)
        except json.JSONDecodeError as e:
            self._respond({"error": f"JSON parse error: {e}"}, 500)
        except Exception as e:
            self._respond({"error": str(e)}, 500)

    def _respond(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
