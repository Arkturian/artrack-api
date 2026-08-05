"""
Track Structure Report Generator - Shared logic for script and API endpoint
"""
import requests
import math


def load_waypoint_summary(base_url, track_id, headers):
    """Load report counters without mutating or re-snapping track content."""
    response = requests.get(
        f"{base_url}/tracks/{track_id}/waypoints/detail",
        headers=headers,
        params={"fields": "slim", "limit": 200000, "offset": 0},
    )
    response.raise_for_status()
    waypoints = response.json()
    if not isinstance(waypoints, list):
        raise ValueError("Waypoint detail endpoint returned an unexpected payload.")

    report_waypoints = [
        waypoint
        for waypoint in waypoints
        if waypoint.get("waypoint_type") != "gps_track"
    ]
    skipped = sum(bool(waypoint.get("segment_role")) for waypoint in report_waypoints)
    return {"total": len(report_waypoints), "skipped": skipped}


def closest_point_on_polyline(poly, lat, lon):
    """Calculate closest point on polyline to given coordinate"""
    if len(poly) < 2:
        return (float('inf'), 0.0)

    # Convert to meters
    ref_lat = poly[0][0]
    meters_per_deg_lat = 111320.0
    meters_per_deg_lon = 111320.0 * math.cos(math.radians(ref_lat))

    def to_meters(lat_a, lon_a):
        return (
            (lat_a - poly[0][0]) * meters_per_deg_lat,
            (lon_a - poly[0][1]) * meters_per_deg_lon
        )

    px, py = to_meters(lat, lon)
    candidates = []
    total_along = 0.0

    # Check orthogonal projections
    for i in range(len(poly) - 1):
        ax, ay = to_meters(poly[i][0], poly[i][1])
        bx, by = to_meters(poly[i + 1][0], poly[i + 1][1])

        abx = bx - ax
        aby = by - ay
        apx = px - ax
        apy = py - ay

        ab_len_sq = abx * abx + aby * aby

        if ab_len_sq >= 0.01:
            t = (apx * abx + apy * aby) / ab_len_sq

            if 0 <= t <= 1:
                qx = ax + t * abx
                qy = ay + t * aby
                pqx = px - qx
                pqy = py - qy
                dist = math.sqrt(pqx * pqx + pqy * pqy)
                proj_along = total_along + t * math.sqrt(ab_len_sq)
                candidates.append((dist, proj_along))

        total_along += math.sqrt(ab_len_sq)

    # Check direct distances to GPS points
    total_along = 0.0
    for i, (point_lat, point_lon) in enumerate(poly):
        vx, vy = to_meters(point_lat, point_lon)
        dx = px - vx
        dy = py - vy
        dist = math.sqrt(dx * dx + dy * dy)
        candidates.append((dist, total_along))

        if i < len(poly) - 1:
            next_lat, next_lon = poly[i + 1]
            nx, ny = to_meters(next_lat, next_lon)
            seg_len = math.sqrt((nx - vx)**2 + (ny - vy)**2)
            total_along += seg_len

    if not candidates:
        return (float('inf'), 0.0)

    min_dist, best_along = min(candidates, key=lambda x: x[0])
    return (min_dist, best_along)


def generate_track_report(track_id, show_descriptions=False, api_key="Inetpass1", base_url="https://api.arkturian.com/artrack"):
    """
    Generate complete track structure report.

    Args:
        track_id: Track ID
        show_descriptions: Include descriptions for track, routes, segments, POIs
        api_key: API key for authentication
        base_url: Base URL for API

    Returns:
        str: Complete report as text
    """
    headers = {"X-API-KEY": api_key}
    output = []

    output.append("=" * 80)
    output.append(f"TRACK STRUCTURE REPORT - Track ID: {track_id}")
    output.append("=" * 80)

    # Get track info
    track_resp = requests.get(f"{base_url}/tracks/{track_id}", headers=headers)
    track = track_resp.json()

    track_name = track.get('name', 'N/A')
    track_desc = track.get('description', '')

    output.append(f"\n📍 TRACK: {track_name}")
    if show_descriptions and track_desc:
        output.append(f"   {track_desc}")
    output.append("")

    # Get all routes
    routes_resp = requests.get(f"{base_url}/tracks/{track_id}/routes", headers=headers)
    routes = routes_resp.json()

    output.append(f"📍 ROUTES: {len(routes)} route(s)")
    output.append("")

    total_pois_assigned = 0
    poi_route_assignments = {}  # poi_id -> {'poi': poi_data, 'routes': [route_ids]}

    for route in routes:
        route_id = route['id']
        route_name = route['name']

        output.append("-" * 80)
        output.append(f"🛤️  ROUTE {route_id}: {route_name}")
        if show_descriptions:
            route_desc = route.get('description', '')
            if route_desc:
                output.append(f"    {route_desc}")
        output.append("-" * 80)

        # Get GPS points
        gps_resp = requests.get(
            f"{base_url}/tracks/{track_id}/gps-points?route_id={route_id}&limit=200000&offset=0",
            headers=headers
        )
        gps_points = gps_resp.json() if gps_resp.status_code == 200 else []

        output.append(f"  📊 GPS DATA:")
        output.append(f"     GPS Points: {len(gps_points)}")
        if len(gps_points) == 0:
            output.append(f"     ⚠️  No GPS data - Auto-snap will NOT work!")
        output.append("")

        # Get route overview
        overview_resp = requests.get(f"{base_url}/tracks/{track_id}/routes/{route_id}/overview", headers=headers)
        overview = overview_resp.json()

        # Segments
        segments = overview.get('segments', [])
        segment_list = []
        polyline = [(p['latitude'], p['longitude']) for p in gps_points]

        for seg in segments:
            along_meters = 0
            length_meters = 0

            if len(polyline) >= 2:
                start_wp = seg['start_waypoint']
                end_wp = seg['end_waypoint']

                dist, along_meters = closest_point_on_polyline(
                    polyline,
                    start_wp['latitude'],
                    start_wp['longitude']
                )
                dist_end, along_end = closest_point_on_polyline(
                    polyline,
                    end_wp['latitude'],
                    end_wp['longitude']
                )
                length_meters = along_end - along_meters

            segment_list.append({
                'name': seg['name'],
                'along_meters': along_meters,
                'length_meters': length_meters,
                'description': seg.get('description', '')
            })

        segment_list.sort(key=lambda x: x['along_meters'])

        output.append(f"  📐 SEGMENTS: {len(segments)}")
        for seg in segment_list:
            output.append(f"     ({seg['along_meters']:.0f}m) {seg['name']} [Länge: {seg['length_meters']:.0f}m]")
            if show_descriptions and seg['description']:
                desc = seg['description']
                if len(desc) > 100:
                    desc = desc[:100] + "..."
                output.append(f"        → {desc}")
        output.append("")

        # POIs
        pois = overview.get('waypoints', [])
        total_pois_assigned += len(pois)

        poi_list = []
        for poi in pois:
            # Track for overlapping section
            poi_id = poi['id']
            if poi_id not in poi_route_assignments:
                poi_route_assignments[poi_id] = {'poi': poi, 'routes': []}
            poi_route_assignments[poi_id]['routes'].append(route_id)

            # Calculate snap position
            along_meters = 0
            if len(polyline) >= 2:
                dist, along_meters = closest_point_on_polyline(
                    polyline,
                    poi['latitude'],
                    poi['longitude']
                )

            meta = poi.get('metadata_json', {})
            title = meta.get('title', 'N/A')
            if len(title) > 60:
                title = title[:60] + "..."

            user_desc = poi.get('user_description', '')

            poi_list.append({
                'id': poi['id'],
                'title': title,
                'user_description': user_desc,
                'along_meters': along_meters
            })

        poi_list.sort(key=lambda x: x['along_meters'])

        output.append(f"  📍 POIs ASSIGNED TO THIS ROUTE: {len(pois)}")
        for poi in poi_list:
            output.append(f"     ({poi['along_meters']:.0f}m) POI {poi['id']}: {poi['title']}")
            if show_descriptions and poi['user_description']:
                desc = poi['user_description']
                if len(desc) > 100:
                    desc = desc[:100] + "..."
                output.append(f"        → {desc}")
        output.append("")

    # All waypoints summary
    output.append("-" * 80)
    output.append("🔍 ALL WAYPOINTS IN TRACK")
    output.append("-" * 80)

    waypoint_summary = load_waypoint_summary(base_url, track_id, headers)

    output.append(f"  Total waypoints: {waypoint_summary['total']}")
    output.append(f"  Segment markers (skipped): {waypoint_summary['skipped']}")
    output.append(f"  POIs: {waypoint_summary['total'] - waypoint_summary['skipped']}")
    output.append(f"  POIs assigned to routes: {total_pois_assigned}")
    output.append(
        "  POIs UNASSIGNED: "
        f"{waypoint_summary['total'] - waypoint_summary['skipped'] - total_pois_assigned}"
    )
    output.append("")

    # Overlapping sections
    output.append("-" * 80)
    output.append("🔄 ÜBERLAPPENDE ABSCHNITTE")
    output.append("-" * 80)
    output.append("")

    overlapping_pois = {poi_id: data for poi_id, data in poi_route_assignments.items() if len(data['routes']) > 1}

    if overlapping_pois:
        sections = {}
        for poi_id, data in overlapping_pois.items():
            route_ids_key = tuple(sorted(data['routes']))
            route_names_key = tuple([r['name'] for r in routes if r['id'] in route_ids_key])

            if route_ids_key not in sections:
                sections[route_ids_key] = {
                    'route_names': route_names_key,
                    'route_ids': route_ids_key,
                    'pois': []
                }

            meta = data['poi'].get('metadata_json', {})
            title = meta.get('title', 'N/A')
            sections[route_ids_key]['pois'].append({
                'id': poi_id,
                'title': title,
                'latitude': data['poi']['latitude'],
                'longitude': data['poi']['longitude']
            })

        for idx, (route_ids, section_data) in enumerate(sections.items(), 1):
            first_route_id = section_data['route_ids'][0]
            gps_resp = requests.get(
                f"{base_url}/tracks/{track_id}/gps-points?route_id={first_route_id}&limit=200000",
                headers=headers
            )
            gps_points = gps_resp.json() if gps_resp.status_code == 200 else []
            polyline = [(p['latitude'], p['longitude']) for p in gps_points]

            for poi in section_data['pois']:
                if len(polyline) >= 2:
                    dist, along_meters = closest_point_on_polyline(
                        polyline,
                        poi['latitude'],
                        poi['longitude']
                    )
                    poi['along_meters'] = along_meters
                else:
                    poi['along_meters'] = 0

            section_data['pois'].sort(key=lambda x: x['along_meters'])

            output.append(f"  Überlappender Abschnitt {idx} ({' + '.join(section_data['route_names'])}):")
            for poi in section_data['pois']:
                output.append(f"    ({poi['along_meters']:.0f}m) POI {poi['id']}: {poi['title']}")
        output.append("")
    else:
        output.append(f"  ✅ Keine überlappenden Abschnitte")
        output.append("")

    # Problems section
    output.append("=" * 80)
    output.append("⚠️  PROBLEME")
    output.append("=" * 80)
    output.append("")

    issues = []

    for route in routes:
        route_id = route['id']
        gps_resp = requests.get(
            f"{base_url}/tracks/{track_id}/gps-points?route_id={route_id}&limit=200000&offset=0",
            headers=headers
        )
        gps_points = gps_resp.json() if gps_resp.status_code == 200 else []

        if len(gps_points) == 0:
            issues.append(f"Route {route_id} ({route['name']}): NO GPS DATA")

    poi_total = waypoint_summary['total'] - waypoint_summary['skipped']
    if total_pois_assigned < poi_total:
        unassigned = poi_total - total_pois_assigned
        issues.append(f"{unassigned} POIs sind NICHT zugewiesen (zeigen auf ALLEN Routes in iOS)")

    if issues:
        for issue in issues:
            output.append(f"  ❌ {issue}")
    else:
        output.append(f"  ✅ Keine Probleme gefunden!")

    output.append("")
    output.append("=" * 80)

    return "\n".join(output)
