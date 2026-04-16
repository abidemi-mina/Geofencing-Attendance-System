import csv
import json
import logging

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.hashers import make_password
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from .forms import AttendanceSessionForm, CourseForm
from .models import (
    AttendanceRecord,
    AttendanceSession,
    Course,
    CourseRegistration,
    StudentProfile,
    User,
)
from .services import (
    AttendanceValidationError,
    generate_qr_png,
    get_dashboard_stats,
    issue_rotating_token,
    validate_and_mark_attendance,
)
from .utils import export_full_course_roster_csv, export_records_to_csv, throttle_request

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────

def is_admin_user(user):
    return user.is_authenticated and user.role == "admin"


def is_student_user(user):
    return user.is_authenticated and user.role == "student"


# ─────────────────────────────────────────────────────────────
# Login / logout
# ─────────────────────────────────────────────────────────────

class MatricLoginView(LoginView):
    template_name = "login.html"

    def get_success_url(self):
        user = self.request.user
        if user.role == "admin":
            return reverse_lazy("dashboard")
        return reverse_lazy("student-mark-home")

    def form_invalid(self, form):
        # Provide a clear, human-readable error instead of Django's generic one
        messages.error(
            self.request,
            "Incorrect matric number or password. Please check your credentials and try again."
        )
        return super().form_invalid(form)


def logout_view(request):
    logout(request)
    messages.success(request, "You have been signed out successfully.")
    return redirect("login")


# ─────────────────────────────────────────────────────────────
# Admin: dashboard
# ─────────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin_user, login_url="/login/")
def dashboard(request):
    stats = get_dashboard_stats(request.user)
    return render(request, "dashboard.html", stats)


# ─────────────────────────────────────────────────────────────
# Admin: courses
# ─────────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin_user, login_url="/login/")
def create_course(request):
    if request.method == "POST":
        form = CourseForm(request.POST)
        if form.is_valid():
            course = form.save(commit=False)
            course.lecturer = request.user
            try:
                course.full_clean()
                course.save()
                messages.success(
                    request,
                    f"Course '{course.code} — {course.title}' created successfully. "
                    f"You can now upload students for this course."
                )
                return redirect("dashboard")
            except ValidationError as exc:
                for field, errs in exc.message_dict.items():
                    for err in errs:
                        form.add_error(field if field != "__all__" else None, err)
        # Fall through to re-render with errors
    else:
        form = CourseForm()

    return render(request, "create_course.html", {"form": form})


@login_required
@user_passes_test(is_admin_user, login_url="/login/")
def course_students(request, course_id):
    course = get_object_or_404(Course, id=course_id, lecturer=request.user)
    students = (
        course.registrations
        .select_related("student", "student__student_profile")
        .order_by("student__full_name")
    )
    return render(request, "course_student.html", {
        "course": course,
        "students": students,
    })


@login_required
@user_passes_test(is_admin_user, login_url="/login/")
def course_records(request, course_id):
    course = get_object_or_404(Course, id=course_id, lecturer=request.user)
    records = (
        AttendanceRecord.objects
        .filter(session__course=course)
        .select_related("student", "session", "session__course")
        .order_by("-marked_at")
    )
    # Per-session summary for a quick overview
    session_summaries = []
    seen = set()
    for rec in records:
        if rec.session_id not in seen:
            seen.add(rec.session_id)
            session_summaries.append({
                "session": rec.session,
                "present": records.filter(session=rec.session, status="present").count(),
                "late":    records.filter(session=rec.session, status="late").count(),
            })
    return render(request, "course_records.html", {
        "course": course,
        "records": records,
        "session_summaries": session_summaries,
    })


@login_required
@user_passes_test(is_admin_user, login_url="/login/")
def course_export(request, course_id):
    """Export all attendance records for a course as CSV."""
    course = get_object_or_404(Course, id=course_id, lecturer=request.user)
    records = (
        AttendanceRecord.objects
        .filter(session__course=course)
        .select_related("student", "session")
        .order_by("session__start_time", "student__full_name")
    )
    return export_records_to_csv(
        f"{course.code.lower()}_attendance.csv", records
    )


@login_required
@user_passes_test(is_admin_user, login_url="/login/")
def course_roster_export(request, course_id):
    """
    Export a full roster CSV — one row per student showing their status
    across every session (present / late / absent).
    """
    course = get_object_or_404(Course, id=course_id, lecturer=request.user)
    return export_full_course_roster_csv(
        f"{course.code.lower()}_full_roster.csv", course
    )


# ─────────────────────────────────────────────────────────────
# Admin: student upload
# ─────────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin_user, login_url="/login/")
@transaction.atomic
def upload_students(request):
    if request.method != "POST":
        return render(request, "upload_students.html")

    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        messages.error(request, "No file was selected. Please choose a CSV file to upload.")
        return redirect("upload-students")
    if not uploaded_file.name.lower().endswith(".csv"):
        messages.error(request, "Only .csv files are accepted.")
        return redirect("upload-students")
    if uploaded_file.size > 5 * 1024 * 1024:
        messages.error(request, "File is too large. Maximum size is 5 MB.")
        return redirect("upload-students")

    try:
        text = uploaded_file.read().decode("utf-8-sig")
        lines = text.splitlines()
        if not lines:
            messages.error(request, "The uploaded CSV is empty.")
            return redirect("upload-students")

        # Be flexible with delimiter and header styles so admins can upload
        # files exported from different tools (Excel, Sheets, etc.).
        sample = "\n".join(lines[:5])
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(lines, dialect=dialect)

        def normalize_header(value: str) -> str:
            return "".join(ch for ch in (value or "").strip().lower() if ch.isalnum())

        required_cols = {"full_name", "matric_number", "level", "course_code"}
        aliases = {
            "full_name": {"full_name", "full name", "fullname", "student_name", "student name", "name"},
            "matric_number": {
                "matric_number", "matric number", "matricnumber", "matric_no", "matric no",
                "matric", "registration_number", "registration number", "reg_no", "reg no",
            },
            "level": {"level", "student_level", "student level", "class_level", "class level"},
            "course_code": {"course_code", "course code", "coursecode", "course", "course_id", "course id"},
        }

        alias_lookup = {
            normalize_header(alias): canonical
            for canonical, alias_set in aliases.items()
            for alias in alias_set
        }

        header_map = {}
        for raw_header in (reader.fieldnames or []):
            canonical = alias_lookup.get(normalize_header(raw_header))
            if canonical and canonical not in header_map:
                header_map[canonical] = raw_header

        missing = required_cols - set(header_map.keys())
        if missing:
            found_display = ", ".join(reader.fieldnames or []) or "(none)"
            messages.error(
                request,
                f"CSV is missing required columns: {', '.join(sorted(missing))}. "
                f"Found: {found_display}."
            )
            return redirect("upload-students")

        imported = 0
        skipped  = 0
        skipped_reasons = []

        for i, row in enumerate(reader, start=2):  # row 1 = header
            full_name = (row.get(header_map["full_name"]) or "").strip()
            matric_number = (row.get(header_map["matric_number"]) or "").strip().upper()
            level = (row.get(header_map["level"]) or "").strip()
            course_code = (row.get(header_map["course_code"]) or "").strip().upper()

            if not all([full_name, matric_number, level, course_code]):
                skipped += 1
                skipped_reasons.append(f"Row {i}: missing fields.")
                continue

            # Only allow registration into this lecturer's own courses
            course = Course.objects.filter(
                code=course_code, lecturer=request.user
            ).first()
            if not course:
                skipped += 1
                skipped_reasons.append(
                    f"Row {i}: course code '{course_code}' not found under your account."
                )
                continue

            # Create or update student account
            student, created = User.objects.get_or_create(
                matric_number=matric_number,
                defaults={
                    "full_name": full_name,
                    "role": "student",
                    "password": make_password("ChangeMe123!"),
                },
            )

            # Do not overwrite an admin/lecturer account
            if student.role != "student":
                skipped += 1
                skipped_reasons.append(
                    f"Row {i}: {matric_number} is an admin account — skipped."
                )
                continue

            # Update name if it changed
            if student.full_name != full_name:
                student.full_name = full_name
                student.save(update_fields=["full_name"])

            # Update or create profile (level can change across uploads)
            StudentProfile.objects.update_or_create(
                user=student, defaults={"level": level}
            )

            # Register in the course (idempotent)
            CourseRegistration.objects.get_or_create(student=student, course=course)
            imported += 1

        # Build a clear summary message
        if imported > 0:
            msg = f"Upload successful — {imported} student registration(s) processed."
            if skipped > 0:
                msg += f" {skipped} row(s) were skipped."
            messages.success(request, msg)
        else:
            messages.warning(
                request,
                f"No rows were imported. All {skipped} row(s) were skipped. "
                "Check that your course codes exist and all required columns are filled."
            )

        if skipped_reasons and skipped <= 10:
            for reason in skipped_reasons:
                messages.warning(request, reason)

        return redirect("dashboard")

    except UnicodeDecodeError:
        messages.error(
            request,
            "Could not read the file — please save it as UTF-8 encoded CSV."
        )
        return redirect("upload-students")
    except Exception as exc:
        logger.exception("Unexpected CSV upload error")
        messages.error(
            request,
            "An unexpected error occurred while processing the file. "
            "Please check the format and try again."
        )
        return redirect("upload-students")


# ─────────────────────────────────────────────────────────────
# Admin: sessions
# ─────────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_admin_user, login_url="/login/")
def create_session(request):
    if request.method == "POST":
        form = AttendanceSessionForm(request.POST, lecturer=request.user)
        if form.is_valid():
            session = form.save(commit=False)
            session.admin = request.user

            if session.shape_type == "polygon":
                raw = request.POST.get("polygon_points", "").strip()
                try:
                    session.polygon_points = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    messages.error(request, "Polygon data was corrupted. Please redraw it.")
                    return render(request, "create_session.html", {"form": form})
                # Clear circle-specific fields
                session.center_lat    = None
                session.center_lng    = None
                session.radius_meters = None
            else:
                # Clear polygon field for circle sessions
                session.polygon_points = None

            try:
                session.full_clean()
                session.save()
                messages.success(
                    request,
                    f"Session '{session.title}' created. "
                    "Share the link or QR code with your students."
                )
                return redirect("session-detail", session_id=session.id)
            except ValidationError as exc:
                for field, errs in exc.message_dict.items():
                    for err in errs:
                        form.add_error(field if field != "__all__" else None, err)
    else:
        form = AttendanceSessionForm(lecturer=request.user)

    return render(request, "create_session.html", {"form": form})


@login_required
@user_passes_test(is_admin_user, login_url="/login/")
def session_detail(request, session_id):
    session = get_object_or_404(
        AttendanceSession.objects.select_related("course"),
        id=session_id,
        admin=request.user,
    )
    share_link = request.build_absolute_uri(f"/student/mark/{session.id}/")
    records = (
        session.records
        .select_related("student")
        .order_by("-marked_at")[:20]   # latest 20 for the detail view
    )
    return render(request, "session_detail.html", {
        "session":    session,
        "share_link": share_link,
        "records":    records,
    })


@login_required
@user_passes_test(is_admin_user, login_url="/login/")
@require_POST   # BUG FIX: toggle must be POST-only (was GET, allowing CSRF-free toggling)
def toggle_session_status(request, session_id):
    session = get_object_or_404(
        AttendanceSession, id=session_id, admin=request.user
    )
    old_status = session.status

    if session.status == "draft":
        session.status = "active"
        msg = f"Session '{session.title}' is now active. Students can mark attendance."
    elif session.status == "active":
        session.status = "closed"
        msg = f"Session '{session.title}' has been closed."
    else:
        # Already closed — nothing to do
        messages.info(request, f"Session '{session.title}' is already closed.")
        return redirect("dashboard")

    session.save(update_fields=["status"])
    logger.info(
        "Session %s status changed: %s → %s by %s",
        session.id, old_status, session.status, request.user.matric_number,
    )
    messages.success(request, msg)
    return redirect("dashboard")


@login_required
@user_passes_test(is_admin_user, login_url="/login/")
def session_export(request, session_id):
    session = get_object_or_404(
        AttendanceSession.objects.select_related("course"),
        id=session_id,
        admin=request.user,
    )
    records = (
        session.records
        .select_related("student", "session")
        .order_by("student__full_name")
    )
    filename = f"{session.course.code.lower()}_{session.id}_attendance.csv"
    return export_records_to_csv(filename, records)


# ─────────────────────────────────────────────────────────────
# Token & QR APIs  (admin-facing, for the session detail page)
# ─────────────────────────────────────────────────────────────

@login_required
def session_token_api(request, session_id):
    """
    Return a fresh rotating token as JSON.
    Called every 45 seconds by the session detail page JS.
    """
    # Admin: can fetch token only for their own sessions.
    # Student: can fetch token only if enrolled in the session's course.
    if request.user.role == "admin":
        session = get_object_or_404(
            AttendanceSession.objects.select_related("course"),
            id=session_id,
            admin=request.user,
        )
    elif request.user.role == "student":
        session = get_object_or_404(
            AttendanceSession.objects.select_related("course"),
            id=session_id,
        )
        is_enrolled = CourseRegistration.objects.filter(
            student=request.user,
            course=session.course,
        ).exists()
        if not is_enrolled:
            return JsonResponse(
                {"ok": False, "message": "You are not registered for this course."},
                status=403,
            )
    else:
        return JsonResponse(
            {"ok": False, "message": "Account role not permitted for token access."},
            status=403,
        )

    payload = issue_rotating_token(
        session,
        request.build_absolute_uri("/").rstrip("/"),
        lifetime_seconds=45,
    )
    return JsonResponse(payload)


@login_required
@user_passes_test(is_admin_user, login_url="/login/")
def session_qr(request, session_id):
    """
    Render a rotating QR code PNG for display on the projector / shared screen.
    Encodes a JSON payload (session ID + token + attendance URL).
    """
    session = get_object_or_404(
        AttendanceSession.objects.select_related("course"),
        id=session_id,
        admin=request.user,
    )
    payload = issue_rotating_token(
        session,
        request.build_absolute_uri("/").rstrip("/"),
        lifetime_seconds=45,
    )
    png = generate_qr_png(json.dumps(payload))
    return HttpResponse(png, content_type="image/png")


# ─────────────────────────────────────────────────────────────
# Student: home & mark page
# ─────────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_student_user, login_url="/login/")
def student_mark_home(request):
    """
    Show the student a list of currently active sessions they are enrolled in.
    BUG FIX: original showed ALL active sessions regardless of enrolment.
    Now filters to only show sessions for courses the student is registered in.
    """
    enrolled_course_ids = CourseRegistration.objects.filter(
        student=request.user
    ).values_list("course_id", flat=True)

    active_sessions = (
        AttendanceSession.objects
        .filter(status="active", course_id__in=enrolled_course_ids)
        .select_related("course")
        .order_by("start_time")
    )

    # Also check if student already marked any of these (to show status)
    already_marked = set(
        AttendanceRecord.objects.filter(
            student=request.user,
            session__in=active_sessions,
        ).values_list("session_id", flat=True)
    )

    return render(request, "student_mark.html", {
        "active_sessions": active_sessions,
        "already_marked":  already_marked,
    })


@login_required
@user_passes_test(is_student_user, login_url="/login/")
def student_mark_page(request, session_id):
    """Open a specific session's attendance page for a student."""
    session = get_object_or_404(
        AttendanceSession.objects.select_related("course"),
        id=session_id,
    )

    if not CourseRegistration.objects.filter(
        student=request.user,
        course=session.course,
    ).exists():
        messages.error(request, "You are not registered for this course.")
        return redirect("student-mark-home")

    # Check if already marked — show a clear status instead of allowing re-mark
    already_marked = AttendanceRecord.objects.filter(
        student=request.user, session=session
    ).first()

    return render(request, "student_mark.html", {
        "session":        session,
        "already_marked": already_marked,
    })


# ─────────────────────────────────────────────────────────────
# Student: matric search API
# ─────────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_student_user, login_url="/login/")
@require_GET
def search_students(request):
    """
    Autocomplete endpoint for the matric number input on the mark-attendance form.

    BUG FIX (critical): The original query had `id=request.user.id` which means
    it returned the logged-in student ONLY if their matric matched the query.
    This is the intended security behaviour (students can only select themselves),
    but the filter was structured wrong — it should be:
        filter(id=request.user.id, matric_number__icontains=query)
    which is exactly what we do here, explicitly and clearly.
    """
    query = (request.GET.get("q") or "").strip().upper()
    if len(query) < 2:
        return JsonResponse({"results": []})

    # Students may only look up their own matric — not anyone else's
    matches = User.objects.filter(
        id=request.user.id,
        matric_number__icontains=query,
    ).only("matric_number", "full_name")

    return JsonResponse({
        "results": [
            {"matric": s.matric_number, "name": s.full_name}
            for s in matches
        ]
    })


# ─────────────────────────────────────────────────────────────
# Student: mark attendance API
# ─────────────────────────────────────────────────────────────

@login_required
@user_passes_test(is_student_user, login_url="/login/")
@require_POST
@csrf_protect
def mark_attendance_api(request, session_id):
    """
    POST endpoint that receives the student's location, device fingerprint,
    and live token, then runs the full validation pipeline.
    """
    # Rate-limit: 10 attempts per 5 minutes per IP
    if throttle_request(request, limit=10, minutes=5):
        return JsonResponse(
            {"ok": False, "message": "Too many requests. Please wait a few minutes and try again."},
            status=429,
        )

    session = get_object_or_404(
        AttendanceSession.objects.select_related("course"),
        id=session_id,
    )

    # ── Parse body ───────────────────────────────────────────
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"ok": False, "message": "Request body is not valid JSON."},
            status=400,
        )

    # ── Whitelist fields (reject any unexpected keys) ────────
    allowed_fields = {
        "matric_number", "latitude", "longitude",
        "accuracy_meters", "device_fingerprint", "token", "is_mock_location",
    }
    extra_fields = set(payload.keys()) - allowed_fields
    if extra_fields:
        return JsonResponse(
            {"ok": False, "message": f"Unexpected fields in request: {', '.join(extra_fields)}."},
            status=400,
        )

    # ── Extract and validate individual fields ───────────────
    submitted_matric = str(payload.get("matric_number", "")).strip().upper()
    if not submitted_matric:
        return JsonResponse(
            {"ok": False, "message": "Matric number is required."},
            status=400,
        )

    try:
        latitude  = float(payload["latitude"])
        longitude = float(payload["longitude"])
        accuracy  = float(payload.get("accuracy_meters", 999))
    except (KeyError, TypeError, ValueError):
        return JsonResponse(
            {"ok": False, "message": "Latitude, longitude, and accuracy must be valid numbers."},
            status=400,
        )

    if not (-90 <= latitude <= 90):
        return JsonResponse(
            {"ok": False, "message": "Latitude is out of range (-90 to 90)."},
            status=400,
        )
    if not (-180 <= longitude <= 180):
        return JsonResponse(
            {"ok": False, "message": "Longitude is out of range (-180 to 180)."},
            status=400,
        )

    device_fingerprint = str(payload.get("device_fingerprint", "")).strip()
    token              = str(payload.get("token", "")).strip()
    is_mock            = bool(payload.get("is_mock_location", False))

    if len(device_fingerprint) < 10 or len(device_fingerprint) > 2000:
        return JsonResponse(
            {"ok": False, "message": "Device fingerprint is invalid or missing."},
            status=400,
        )
    if not token:
        return JsonResponse(
            {"ok": False, "message": "Attendance token is required."},
            status=400,
        )

    # ── Delegate to the service layer ────────────────────────
    try:
        record = validate_and_mark_attendance(
            request=request,
            student=request.user,
            session=session,
            latitude=latitude,
            longitude=longitude,
            accuracy_meters=accuracy,
            raw_device_fingerprint=device_fingerprint,
            submitted_token=token,
            submitted_matric_number=submitted_matric,
            is_mock_location=is_mock,
        )
        return JsonResponse({
            "ok":       True,
            "message":  "Attendance marked successfully.",
            "status":   record.get_status_display(),
            "marked_at": record.marked_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
        })

    except AttendanceValidationError as exc:
        return JsonResponse(
            {"ok": False, "message": str(exc)},
            status=400,
        )
    except Exception:
        logger.exception(
            "Unexpected error in mark_attendance_api for session=%s student=%s",
            session_id, request.user.matric_number,
        )
        return JsonResponse(
            {"ok": False, "message": "An unexpected server error occurred. Please try again."},
            status=500,
        )