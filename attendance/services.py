import io
import json
import logging

import qrcode
from django.db import transaction
from django.utils import timezone

from .models import (
    AttendanceAttemptLog,
    AttendanceRecord,
    CourseRegistration,
    DeviceBinding,
    RotatingSessionToken,
)
from .utils import (
    get_client_ip,
    is_point_in_circle,
    is_point_in_polygon,
    normalize_fingerprint,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Custom exception
# ─────────────────────────────────────────────────────────────

class AttendanceValidationError(Exception):
    """Raised when any attendance business-rule check fails."""
    pass


# ─────────────────────────────────────────────────────────────
# Rotating token service
# ─────────────────────────────────────────────────────────────

def issue_rotating_token(session, request_base_url: str, lifetime_seconds: int = 45) -> dict:
    """
    Issue a fresh rotating token for the session and return the full payload
    that is sent to both the token API and the QR code generator.

    The payload contains everything a student app needs: the session ID,
    the short-lived token, and the direct attendance URL.
    """
    token_obj = RotatingSessionToken.issue_for_session(
        session, lifetime_seconds=lifetime_seconds
    )
    return {
        "session_id": session.id,
        "token": token_obj.token,
        "course": session.course.code,
        "expires_at": token_obj.expires_at.isoformat(),
        # The path the QR code should deep-link to
        "path": f"{request_base_url.rstrip('/')}/student/mark/{session.id}/",
    }


# ─────────────────────────────────────────────────────────────
# QR code generator
# ─────────────────────────────────────────────────────────────

def generate_qr_png(data: str) -> bytes:
    """
    Render a QR code that encodes `data` and return it as a PNG byte string.
    The QR code links to the student attendance page with the session token.
    """
    qr = qrcode.QRCode(
        version=None,       # auto-size
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────
# Attempt logger
# ─────────────────────────────────────────────────────────────

def log_attempt(
    *,
    student=None,
    session=None,
    latitude=None,
    longitude=None,
    device_fingerprint=None,
    ip_address=None,
    reason: str = "",
    meta: dict = None,
) -> None:
    """
    Persist a rejected attendance attempt for audit / anti-fraud analysis.
    All fields are optional so we can log even partially constructed attempts.
    """
    try:
        AttendanceAttemptLog.objects.create(
            student=student,
            session=session,
            latitude=latitude,
            longitude=longitude,
            device_fingerprint=device_fingerprint,
            ip_address=ip_address,
            reason=reason,
            meta=meta or {},
        )
    except Exception:
        # Logging must never break the main request flow
        logger.exception("Failed to write AttendanceAttemptLog (reason=%s)", reason)


# ─────────────────────────────────────────────────────────────
# Core attendance marking service
# ─────────────────────────────────────────────────────────────

@transaction.atomic
def validate_and_mark_attendance(
    *,
    request,
    student,
    session,
    latitude: float,
    longitude: float,
    accuracy_meters: float,
    raw_device_fingerprint: str,
    submitted_token: str,
    submitted_matric_number: str,
    is_mock_location: bool = False,
) -> AttendanceRecord:
    """
    Run every validation check and, if all pass, write the attendance record.

    Checks (in order):
      1. Role — must be a student account
      2. Identity — submitted matric must match the logged-in account
      3. Profile — student must have a StudentProfile (level info)
      4. Session open — status==active AND within the time window
      5. Token — submitted token must exist, be active, and not expired
      6. Level match — student level must match the course level
      7. Enrolment — student must be registered on the course
      8. Duplicate — attendance must not already be recorded
      9. Mock location — client must not report a mock/spoofed location
     10. Accuracy — GPS accuracy must be within acceptable bounds
     11. Device binding — device fingerprint must belong to this student only
     12. Geofence — student coordinates must be inside the defined area
     13. Final time state — re-check for closed/not_started edge cases

    Raises AttendanceValidationError on any failure.
    Returns the created AttendanceRecord on success.
    """
    ip_address = get_client_ip(request)

    # ── 1. Role check ────────────────────────────────────────
    if student.role != "student":
        raise AttendanceValidationError(
            "Only student accounts can mark attendance."
        )

    # ── 2. Identity check ────────────────────────────────────
    if submitted_matric_number.strip().upper() != student.matric_number.upper():
        log_attempt(
            student=student, session=session, ip_address=ip_address,
            reason="Matric mismatch",
            meta={"submitted": submitted_matric_number, "actual": student.matric_number},
        )
        raise AttendanceValidationError(
            "The matric number you selected does not match your account. "
            "Please select your own matric number."
        )

    # ── 3. Student profile ───────────────────────────────────
    try:
        profile = student.student_profile
    except Exception:
        raise AttendanceValidationError(
            "Your student profile is incomplete. "
            "Please contact your lecturer to re-upload your record."
        )

    # ── 4. Session open ──────────────────────────────────────
    if not session.is_open():
        state_hint = ""
        now = timezone.now()
        if session.status != "active":
            state_hint = f" (Session status: {session.get_status_display()})"
        elif now < session.start_time:
            state_hint = " (Not started yet)"
        elif now > session.end_time:
            state_hint = " (Session has ended)"
        raise AttendanceValidationError(
            f"This attendance session is not currently open.{state_hint}"
        )

    # ── 5. Token check ───────────────────────────────────────
    token_obj = (
        RotatingSessionToken.objects
        .filter(session=session, token=submitted_token, is_active=True)
        .select_for_update()   # lock the row to prevent double-spend
        .order_by("-created_at")
        .first()
    )
    if not token_obj or not token_obj.is_valid():
        log_attempt(
            student=student, session=session, ip_address=ip_address,
            reason="Invalid or expired token",
            meta={"submitted_token": submitted_token[:10] + "…"},
        )
        raise AttendanceValidationError(
            "The attendance token is invalid or has expired. "
            "The page refreshes the token automatically — please try again immediately."
        )

    # ── 6. Level match ───────────────────────────────────────
    if profile.level != session.course.level:
        log_attempt(
            student=student, session=session, ip_address=ip_address,
            reason="Level mismatch",
            meta={"student_level": profile.level, "course_level": session.course.level},
        )
        raise AttendanceValidationError(
            f"Your level ({profile.level}) does not match this course's level "
            f"({session.course.level}). Are you in the right session?"
        )

    # ── 7. Course enrolment ──────────────────────────────────
    if not CourseRegistration.objects.filter(
        student=student, course=session.course
    ).exists():
        log_attempt(
            student=student, session=session, ip_address=ip_address,
            reason="Not enrolled in course",
        )
        raise AttendanceValidationError(
            f"You are not registered for {session.course.code}. "
            "Contact your lecturer if this is an error."
        )

    # ── 8. Duplicate check ───────────────────────────────────
    if AttendanceRecord.objects.filter(student=student, session=session).exists():
        raise AttendanceValidationError(
            "Your attendance for this session has already been recorded. "
            "You cannot mark twice."
        )

    # ── 9. Mock location ─────────────────────────────────────
    if is_mock_location:
        log_attempt(
            student=student, session=session,
            latitude=latitude, longitude=longitude,
            device_fingerprint=None, ip_address=ip_address,
            reason="Mock location detected",
        )
        raise AttendanceValidationError(
            "A mock or simulated location was detected. "
            "Disable any fake GPS apps and try again."
        )

    # ── 10. GPS accuracy ─────────────────────────────────────
    if accuracy_meters < 0:
        raise AttendanceValidationError("Invalid location accuracy value.")
    if accuracy_meters > 150:
        raise AttendanceValidationError(
            f"Your GPS accuracy is too low ({accuracy_meters:.0f}m). "
            "Move to a more open area, wait a moment, and try again."
        )

    # ── 11. Device binding ───────────────────────────────────
    try:
        fingerprint = normalize_fingerprint(raw_device_fingerprint)
    except ValueError:
        raise AttendanceValidationError("Device fingerprint is missing or invalid.")

    existing_binding = DeviceBinding.objects.filter(student=student).first()

    if existing_binding:
        if existing_binding.fingerprint != fingerprint:
            log_attempt(
                student=student, session=session, ip_address=ip_address,
                device_fingerprint=fingerprint,
                reason="Device fingerprint mismatch (different device)",
            )
            raise AttendanceValidationError(
                "This account is linked to a different device. "
                "You must use the same device you first used to mark attendance."
            )
        # Known device — update last_seen_at
        existing_binding.touch()
    else:
        # First time — check this device isn't already owned by another student
        if DeviceBinding.objects.filter(fingerprint=fingerprint).exclude(
            student=student
        ).exists():
            log_attempt(
                student=student, session=session, ip_address=ip_address,
                device_fingerprint=fingerprint,
                reason="Device already bound to another student",
            )
            raise AttendanceValidationError(
                "This device is already registered to another student account. "
                "Contact your lecturer if you believe this is a mistake."
            )
        DeviceBinding.objects.create(student=student, fingerprint=fingerprint)

    # ── 12. Geofence check ───────────────────────────────────
    inside_geofence = False
    if session.shape_type == "circle":
        inside_geofence = is_point_in_circle(
            latitude, longitude,
            session.center_lat, session.center_lng,
            session.radius_meters,
        )
    elif session.shape_type == "polygon":
        inside_geofence = is_point_in_polygon(
            latitude, longitude, session.polygon_points
        )

    if not inside_geofence:
        log_attempt(
            student=student, session=session,
            latitude=latitude, longitude=longitude,
            device_fingerprint=fingerprint, ip_address=ip_address,
            reason="Outside geofence",
            meta={
                "accuracy_m": accuracy_meters,
                "shape": session.shape_type,
            },
        )
        raise AttendanceValidationError(
            "You are not within the allowed attendance area. "
            "Ensure you are physically in the classroom and your GPS is on."
        )

    # ── 13. Final time-state ─────────────────────────────────
    state = session.attendance_state_for_now()
    if state == "closed":
        raise AttendanceValidationError("Attendance has just closed. Your mark was not recorded.")
    if state == "not_started":
        raise AttendanceValidationError("Attendance has not started yet.")

    final_status = "late" if state == "late" else "present"

    # ── Consume the token (prevent re-use) ───────────────────
    token_obj.is_active = False
    token_obj.save(update_fields=["is_active"])

    # ── Write the record ─────────────────────────────────────
    record = AttendanceRecord.objects.create(
        student=student,
        session=session,
        status=final_status,
        latitude=latitude,
        longitude=longitude,
        accuracy_meters=accuracy_meters,
        device_fingerprint=fingerprint,
        ip_address=ip_address,
        is_mock_location=False,
        validation_notes={
            "validated_at": timezone.now().isoformat(),
            "state": state,
            "course": session.course.code,
            "course_level": session.course.level,
            "student_level": profile.level,
            "accuracy_m": accuracy_meters,
            "shape_type": session.shape_type,
        },
    )

    logger.info(
        "Attendance marked: student=%s session=%s status=%s ip=%s",
        student.matric_number, session.id, final_status, ip_address,
    )
    return record


# ─────────────────────────────────────────────────────────────
# Dashboard stats helper
# ─────────────────────────────────────────────────────────────

def get_dashboard_stats(lecturer) -> dict:
    """
    Return aggregate statistics for a lecturer's dashboard in a single
    function call, minimising repeated queries in the view.
    """
    from .models import Course, AttendanceSession, AttendanceRecord  # local imports

    courses = Course.objects.filter(lecturer=lecturer)
    course_ids = list(courses.values_list("id", flat=True))

    sessions = AttendanceSession.objects.filter(course_id__in=course_ids).select_related("course")
    session_summaries = []
    total_marked = 0
    active_sessions = 0

    for s in sessions:
        count = s.records.count()
        total_marked += count
        if s.status == "active":
            active_sessions += 1
        session_summaries.append({"session": s, "count": count})

    return {
        "courses": courses.order_by("code"),
        "session_summaries": session_summaries,
        "total_courses": courses.count(),
        "total_sessions": sessions.count(),
        "active_sessions": active_sessions,
        "total_marked": total_marked,
    }