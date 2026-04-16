import csv
import hashlib
import logging
import math
from datetime import timedelta

from django.http import HttpResponse
from django.utils import timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Geo helpers
# ─────────────────────────────────────────────────────────────

def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Return the great-circle distance in metres between two (lat, lon) points.
    Uses the haversine formula — accurate for short distances.
    """
    R = 6_371_000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def is_point_in_circle(
    user_lat: float,
    user_lng: float,
    center_lat: float,
    center_lng: float,
    radius_meters: float,
) -> bool:
    """Return True if the user's coordinates fall within the geofence circle."""
    distance = haversine_distance_m(user_lat, user_lng, center_lat, center_lng)
    return distance <= radius_meters


def is_point_in_polygon(user_lat: float, user_lng: float, polygon_points: list) -> bool:
    """
    Ray-casting algorithm to determine if (user_lat, user_lng) is inside
    the polygon defined by polygon_points (list of {lat, lng} dicts).

    This removes the hard dependency on shapely. If shapely IS installed,
    we prefer it for robustness (handles edge/touch cases better).
    """
    try:
        from shapely.geometry import Point, Polygon  # type: ignore
        coords = [(pt["lng"], pt["lat"]) for pt in polygon_points]
        poly = Polygon(coords)
        pt = Point(user_lng, user_lat)
        return poly.contains(pt) or poly.touches(pt)
    except ImportError:
        pass

    # Pure-Python ray-casting fallback
    lats = [pt["lat"] for pt in polygon_points]
    lngs = [pt["lng"] for pt in polygon_points]
    n = len(lats)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = lngs[i], lats[i]
        xj, yj = lngs[j], lats[j]
        if (yi > user_lat) != (yj > user_lat):
            x_intersect = (xj - xi) * (user_lat - yi) / (yj - yi) + xi
            if user_lng < x_intersect:
                inside = not inside
        j = i
    return inside


# ─────────────────────────────────────────────────────────────
# Device fingerprint
# ─────────────────────────────────────────────────────────────

def normalize_fingerprint(raw_fingerprint: str) -> str:
    """
    Hash the raw browser fingerprint into a fixed-length 64-char hex string.
    Protects student privacy and normalises varying whitespace.
    """
    if not raw_fingerprint or not raw_fingerprint.strip():
        raise ValueError("Device fingerprint must not be empty.")
    return hashlib.sha256(raw_fingerprint.strip().encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────
# Client IP
# ─────────────────────────────────────────────────────────────

def get_client_ip(request) -> str:
    """
    Resolve the real client IP, respecting X-Forwarded-For from trusted proxies.
    """
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        # The first address in the list is the originating client IP
        ip = forwarded_for.split(",")[0].strip()
        if ip:
            return ip
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


# ─────────────────────────────────────────────────────────────
# Rate limiting / throttle
# ─────────────────────────────────────────────────────────────

def throttle_request(request, limit: int = 20, minutes: int = 5) -> bool:
    """
    Return True (= blocked) if the IP has exceeded `limit` requests to this
    path within the last `minutes`.

    BUG FIX: The original code always created a log entry, even when blocking.
    That was correct. But it also returned `count >= limit` *before* inserting,
    meaning the very first over-limit request slipped through. We now insert
    first, then check the count (inclusive of the new entry).
    Also cleans up entries older than the window to prevent table bloat.
    """
    from .models import RequestThrottleLog  # local import to avoid circular

    ip = get_client_ip(request)
    path = request.path
    now = timezone.now()
    window_start = now - timedelta(minutes=minutes)

    # Insert the current request first
    RequestThrottleLog.objects.create(ip_address=ip, path=path)

    # Count how many times this IP hit this path in the window (including now)
    count = RequestThrottleLog.objects.filter(
        ip_address=ip,
        path=path,
        created_at__gte=window_start,
    ).count()

    # Periodically prune old records (roughly 1-in-50 chance) to prevent bloat
    if count % 50 == 0:
        RequestThrottleLog.objects.filter(
            created_at__lt=window_start - timedelta(hours=1)
        ).delete()

    return count > limit


# ─────────────────────────────────────────────────────────────
# CSV export
# ─────────────────────────────────────────────────────────────

def export_records_to_csv(filename: str, records) -> HttpResponse:
    """
    Stream an attendance CSV download.
    Includes all records; each row has enough info for offline reconciliation.
    """
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    # UTF-8 BOM so Excel opens it correctly without encoding issues
    response.write("\ufeff")

    writer = csv.writer(response)
    writer.writerow([
        "Student Name",
        "Matric Number",
        "Session",
        "Course",
        "Status",
        "Marked At (UTC)",
        "Latitude",
        "Longitude",
        "Accuracy (m)",
        "IP Address",
    ])

    for rec in records:
        writer.writerow([
            rec.student.full_name,
            rec.student.matric_number,
            rec.session.title,
            rec.session.course.code,
            rec.get_status_display(),
            rec.marked_at.strftime("%Y-%m-%d %H:%M:%S"),
            f"{rec.latitude:.6f}",
            f"{rec.longitude:.6f}",
            f"{rec.accuracy_meters:.1f}" if rec.accuracy_meters is not None else "",
            rec.ip_address or "",
        ])

    return response


def export_full_course_roster_csv(filename: str, course) -> HttpResponse:
    """
    Export ALL registered students for a course with their attendance status
    across every session — useful for generating a complete mark sheet.
    """
    from .models import AttendanceRecord  # local import

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")

    sessions = list(course.sessions.order_by("start_time"))
    registrations = (
        course.registrations
        .select_related("student")
        .order_by("student__full_name")
    )

    writer = csv.writer(response)
    header = ["Student Name", "Matric Number", "Level"]
    for s in sessions:
        header.append(f"{s.title} ({s.start_time.strftime('%d/%m/%Y')})")
    header.append("Total Present")
    header.append("Total Late")
    writer.writerow(header)

    for reg in registrations:
        student = reg.student
        row = [student.full_name, student.matric_number]
        try:
            row.append(student.student_profile.level)
        except Exception:
            row.append("—")

        present_count = 0
        late_count = 0
        for session in sessions:
            try:
                record = AttendanceRecord.objects.get(student=student, session=session)
                row.append(record.get_status_display())
                if record.status == "present":
                    present_count += 1
                elif record.status == "late":
                    late_count += 1
            except AttendanceRecord.DoesNotExist:
                row.append("Absent")

        row.append(present_count)
        row.append(late_count)
        writer.writerow(row)

    return response