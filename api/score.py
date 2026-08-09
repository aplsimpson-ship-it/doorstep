from http.server import BaseHTTPRequestHandler
import json
import urllib.request
import urllib.error
import math
import os
import csv
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

# ── Constants ─────────────────────────────────────────────────────────────────

SCHOOLS_CSV_URL = (
    "https://raw.githubusercontent.com/aplsimpson-ship-it/doorstep/main/schools_primary.csv"
)

# ── Caches ────────────────────────────────────────────────────────────────────

_schools_cache = None  # v2

# ── Helpers ───────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def fetch_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
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

def load_schools():
    global _schools_cache
    if _schools_cache is not None:
        return _schools_cache
    try:
        req = urllib.request.Request(SCHOOLS_CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            content = r.read().decode("utf-8", errors="replace")
        schools = []
        reader = csv.DictReader(io.StringIO(content))
        for row in reader:
            try:
                lat = float(row.get("Latitude", "") or 0)
                lng = float(row.get("Longitude", "") or 0)
            except:
                continue
            if not lat or not lng:
                continue
            schools.append({
                "urn":      row.get("URN", "").strip(),
                "name":     row.get("EstablishmentName", "").strip(),
                "lat":      lat,
                "lng":      lng,
                "postcode": row.get("Postcode", "").strip(),
                "ofsted":   row.get("OfstedRating", "Not yet rated").strip(),
                "religious": row.get("ReligiousCharacter", "").strip() == "Yes",
            })
        _schools_cache = schools
        return schools
    except Exception as e:
        _schools_cache = []
        return []

def get_schools(lat, lng):
    schools = load_schools()
    if not schools:
        return [], []

    nearby = []
    for s in schools:
        dist = haversine(lat, lng, s["lat"], s["lng"])
        if dist > 1.5:
            continue
        nearby.append({
            "name":           s["name"],
            "ofsted":         s["ofsted"],
            "distance_miles": round(dist, 2),
            "lat":            s["lat"],
            "lng":            s["lng"],
            "urn":            s["urn"],
            "postcode":       s["postcode"],
            "religious":      s["religious"],
        })

    nearby.sort(key=lambda x: x["distance_miles"])
    candidates  = nearby[:4]
    outstanding = [s for s in nearby if "outstanding" in s["ofsted"].lower()][:2]

    ors_key = os.environ.get("ORS_API_KEY", "")
    if ors_key:
        seen = {id(s) for s in candidates}
        extra = [s for s in outstanding if id(s) not in seen]
        for s in candidates + extra:
            try:
                ors_payload = json.dumps({
                    "coordinates": [[lng, lat], [s["lng"], s["lat"]]]
                }).encode()
                ors_req = urllib.request.Request(
                    "https://api.openrouteservice.org/v2/directions/foot-walking",
                    data=ors_payload,
                    headers={
                        "Authorization": ors_key,
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(ors_req, timeout=8) as r:
                    ors_data = json.loads(r.read())
                segment = ors_data["routes"][0]["segments"][0]
                s["distance_miles"] = round(segment["distance"] * 0.000621371, 2)
            except:
                pass

    candidates.sort(key=lambda x: x["distance_miles"])
    nearest     = candidates[:2]
    outstanding = sorted(outstanding, key=lambda x: x["distance_miles"])[:2]

    for s in nearest + outstanding:
        s.pop("lat", None)
        s.pop("lng", None)

    return nearest, outstanding

# ── Crime ─────────────────────────────────────────────────────────────────────

def get_crime(lat, lng):
    try:
        delta = 0.00725
        poly = (
            f"{lat+delta},{lng}:{lat},{lng+delta}:"
            f"{lat-delta},{lng}:{lat},{lng-delta}"
        )
        for months_back in range(2, 5):
            date = (datetime.utcnow().replace(day=1) - timedelta(days=30*months_back)).strftime("%Y-%m")
            url = (
                f"https://data.police.uk/api/crimes-street/all-crime"
                f"?poly={poly}&date={date}"
            )
            try:
                crimes = fetch_json(url, timeout=10)
                if crimes:
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
                        "date":     date,
                    }
            except:
                continue
        return None
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
        ors_key = os.environ.get("ORS_API_KEY", "")

        def get_nearest_stop(modes, stop_types):
            url = (
                f"https://api.tfl.gov.uk/StopPoint"
                f"?lat={lat}&lon={lng}"
                f"&stopTypes={stop_types}"
                f"&radius=1500"
                f"&modes={modes}"
            )
            data  = fetch_json(url, timeout=8)
            stops = data.get("stopPoints", [])
            if not stops:
                return None
            stops.sort(key=lambda s: s.get("distance", 9999))
            nearest     = stops[0]
            station_lat = nearest.get("lat", 0)
            station_lng = nearest.get("lon", 0)
            lines = []
            for mode in nearest.get("lineModeGroups", []):
                lines.extend(mode.get("lineIdentifier", []))
            nice_lines = [l.replace("-", " ").title() for l in lines[:4]]

            if not ors_key or not station_lat or not station_lng:
                return None

            ors_payload = json.dumps({
                "coordinates": [[lng, lat], [station_lng, station_lat]]
            }).encode()
            ors_req = urllib.request.Request(
                "https://api.openrouteservice.org/v2/directions/foot-walking",
                data=ors_payload,
                headers={
                    "Authorization": ors_key,
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0"
                },
                method="POST"
            )
            with urllib.request.urlopen(ors_req, timeout=8) as r:
                ors_data = json.loads(r.read())
            segment = ors_data["routes"][0]["segments"][0]
            return {
                "name":           nearest.get("commonName", "Unknown"),
                "distance_miles": round(segment["distance"] * 0.000621371, 2),
                "walk_mins":      max(1, round(segment["duration"] / 60)),
                "lines":          nice_lines,
            }

        tube       = get_nearest_stop("tube,elizabeth-line", "NaptanMetroStation")
        overground = get_nearest_stop("overground,national-rail", "NaptanRailStation")

        if not tube and not overground:
            return None

        # Only show overground/rail if it's closer than the tube
        show_overground = (
            overground and (
                not tube or
                overground["distance_miles"] < tube["distance_miles"]
            )
        )

        return {
            "tube":       tube,
            "overground": overground if show_overground else None,
        }
    except:
        return None

# ── Claude ────────────────────────────────────────────────────────────────────

def call_claude(prompt, api_key):
    payload = json.dumps({
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 3000,
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

# ── Prompt ────────────────────────────────────────────────────────────────────

def build_prompt(postcode, listing_url, weights,
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
        f"Total crimes {crime.get('date', 'recent')}: {crime['total']} "
        f"(Burglary: {crime['burglary']}, "
        f"Vehicle: {crime['vehicle']}, "
        f"ASB: {crime['asb']}, "
        f"Violence: {crime['violence']})"
        if crime else "No crime data retrieved"
    )

    def fmt_stop(s):
        return f"{s['name']}, {s['distance_miles']}mi ({s['walk_mins']} min walk), Lines: {', '.join(s['lines'])}"

    if not transport:
        transport_str = "No transport data retrieved"
    else:
        parts = []
        if transport.get("tube"):
            parts.append(f"Nearest tube/Elizabeth line: {fmt_stop(transport['tube'])}")
        if transport.get("overground"):
            parts.append(f"Nearest overground/rail: {fmt_stop(transport['overground'])}")
        transport_str = " | ".join(parts) if parts else "No transport data retrieved"

    wstr = ", ".join(f"{k}:{v}%" for k, v in weights.items()) if weights else \
           "schools:30%, transport:20%, crime:20%, amenities:15%, environment:10%, financials:5%"

    return f"""You are a London property research assistant. A young family with a 2.5-year-old (planning 10+ years) wants to evaluate this property. Schools are their top priority.

IMPORTANT: Use the REAL DATA provided below exactly as given for schools, crime, flood risk and transport. Do not substitute your own knowledge for these figures. Do not make assumptions about price, tenure, bedrooms, chain status, or property condition — only reference these if explicitly provided below.

Postcode: {postcode}
Listing URL: {listing_url or 'not provided'}

=== REAL VERIFIED DATA ===
Two nearest primary schools: {school_str(nearest_schools)}
Two nearest Outstanding primaries: {school_str(outstanding_schools) if outstanding_schools else 'None found within 1.5 miles'}
Crime (within 0.5 miles): {crime_str}
Flood risk: {flood_risk}
Transport: {transport_str}

Scoring weights: {wstr}

Return ONLY a JSON object. No markdown, no explanation, just the JSON:
{{
  "address": "best guess at full address from postcode",
  "area": "neighbourhood, borough",
  "postcode": "{postcode}",
  "overallScore": 0,
  "summary": "3 honest sentences for a family with a toddler staying 10+ years. Do not mention price, tenure or chain status unless provided above.",
  "keyFacts": {{
        "nearestTube": "nearest tube/Elizabeth line station with distance and walk time. If overground/rail is also provided and closer, list that too.",    "council": "borough name",
    "schoolsNearby": "brief summary using real school names and ratings above",
    "floodRisk": "{flood_risk}"
  }},
  "categories": [
    {{
      "id": "schools",
      "name": "Primary schools",
      "score": <use EXACTLY this rubric based on the two nearest primaries:
        5 = both Outstanding,
        4 = one Outstanding and one Good,
        3 = both Good,
        2 = one Good and one Requires Improvement or worse,
        1 = both Requires Improvement or worse or no data>,
      "headline": "one line referencing real school names and ratings",
      "details": "Use exact school names, ratings and distances from real data. List both nearest primaries with their ratings. Mention if any Outstanding schools are faith schools with selective admissions.",
      "tags": [{{"label": "School Name — Rating", "type": "good|warn|bad|neutral"}}]
    }},
{{
      "id": "transport",
      "name": "Transport",
      "score": <use EXACTLY this rubric based on walk time to nearest tube or Elizabeth line station only:
        5 = under 10 min walk,
        4 = 11-15 min walk,
        3 = 16-20 min walk,
        1 = over 20 min walk>,
      "headline": "one line with tube station name and walk time",
      "details": "Use exact transport data. Base score on tube/Elizabeth line walk time only. Add context about lines and typical journey times to central London. If overground/rail is also listed, mention it but note it does not affect the score.",
      "tags": [{{"label": "...", "type": "good|warn|bad|neutral"}}]
    }},
    {{
      "id": "crime",
      "name": "Crime & safety",
      "score": <use EXACTLY this rubric based on total crimes within 0.5 miles:
        5 = under 100 crimes,
        4 = 100-200 crimes,
        3 = 200-300 crimes,
        2 = 300-400 crimes,
        1 = over 400 crimes>,
      "headline": "one line with total crime figure and date",
      "details": "Use exact crime figures and date. Inner London average is 250-350 crimes/month within 0.5 miles. Below 200 is low, 200-350 is average, 350-500 is above average, over 500 is high.",
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
      "details": "Council tax band estimate for this postcode. Do not mention price or tenure unless provided above.",
      "tags": [{{"label": "...", "type": "good|warn|bad|neutral"}}]
    }}
  ],
  "greenFlags": ["specific point 1", "specific point 2", "specific point 3"],
  "watchPoints": ["specific concern 1", "specific concern 2", "specific concern 3"]
}}"""

# ── Handler ───────────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

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

        postcode    = body.get("postcode", "").strip().upper()
        listing_url = body.get("listing_url", "").strip()
        weights     = body.get("weights", {})

        if not postcode:
            self._respond({"error": "Postcode is required"}, 400)
            return

        lat, lng = get_lat_lng(postcode)
        if not lat:
            self._respond({"error": f"Could not geocode postcode: {postcode}"}, 400)
            return

        nearest_schools, outstanding_schools = [], []
        crime      = None
        flood_risk = "Very low"
        transport  = None

        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {
                ex.submit(get_schools,    lat, lng): "schools",
                ex.submit(get_crime,      lat, lng): "crime",
                ex.submit(get_flood_risk, lat, lng): "flood",
                ex.submit(get_transport,  lat, lng): "transport",
            }
            for future in as_completed(futures, timeout=25):
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
            postcode, listing_url, weights,
            nearest_schools, outstanding_schools,
            crime, flood_risk, transport,
        )

        try:
            raw   = call_claude(prompt, api_key)
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            sc    = json.loads(clean)
            # Recalculate overall score precisely in Python
  # Override transport score in Python based on exact rubric
            tube_walk_mins = transport["tube"]["walk_mins"] if transport and transport.get("tube") else 999
            if tube_walk_mins <= 10:
                transport_score = 5
            elif tube_walk_mins <= 15:
                transport_score = 4
            elif tube_walk_mins <= 20:
                transport_score = 3
            else:
                transport_score = 1
            for cat in sc.get("categories", []):
                if cat["id"] == "transport":
                    cat["score"] = transport_score
                    break

            # Override schools score in Python based on exact rubric
            def ofsted_rank(rating):
                r = rating.lower()
                if "outstanding" in r: return 4
                if "good" in r: return 3
                if "requires" in r: return 2
                if "inadequate" in r: return 1
                return 0
            if nearest_schools:
                s1 = ofsted_rank(nearest_schools[0]["ofsted"]) if len(nearest_schools) > 0 else 0
                s2 = ofsted_rank(nearest_schools[1]["ofsted"]) if len(nearest_schools) > 1 else 0
                top = max(s1, s2)
                bot = min(s1, s2)
                if top == 4 and bot == 4:   schools_score = 5
                elif top == 4 and bot == 3: schools_score = 4
                elif top == 3 and bot == 3: schools_score = 3
                elif top == 3 and bot <= 2: schools_score = 2
                else:                        schools_score = 1
            else:
                schools_score = 1
            for cat in sc.get("categories", []):
                if cat["id"] == "schools":
                    cat["score"] = schools_score
                    break

            # Override crime score in Python based on exact rubric
            crime_total = crime["total"] if crime else 999
            if crime_total < 100:   crime_score = 5
            elif crime_total < 200: crime_score = 4
            elif crime_total < 300: crime_score = 3
            elif crime_total < 400: crime_score = 2
            else:                   crime_score = 1
            for cat in sc.get("categories", []):
                if cat["id"] == "crime":
                    cat["score"] = crime_score
                    break

            # Recalculate overall score precisely in Python
            weight_map = {
                "schools":     weights.get("schools", 30),
                "transport":   weights.get("transport", 20),
                "crime":       weights.get("crime", 20),
                "amenities":   weights.get("amenities", 15),
                "environment": weights.get("environment", 10),
                "financials":  weights.get("financials", 5),
            }
            total_weight = sum(weight_map.values())
            weighted_sum = 0
            for cat in sc.get("categories", []):
                cat_id = cat.get("id")
                cat_score = cat.get("score", 0)
                if cat_id in weight_map:
                    weighted_sum += (cat_score / 5) * weight_map[cat_id]
            sc["overallScore"] = round((weighted_sum / total_weight) * 100)
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
