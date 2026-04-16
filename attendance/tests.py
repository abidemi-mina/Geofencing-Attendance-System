"""
AttendTrack test suite
======================
Covers the core business logic of the attendance system:

  - Model validation (AttendanceSession, Course, User)
  - Service layer: validate_and_mark_attendance
  - Utility functions: haversine, geofence, fingerprint, throttle
  - Views: login, dashboard, upload, session CRUD, student mark flow
  - Security: token expiry, device binding, duplicate prevention, geofence rejection

Run with:
    python manage.py test attendance
"""

import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    AttendanceAttemptLog,
    AttendanceRecord,
    AttendanceSession,
    Course,
    CourseRegistration,
    DeviceBinding,
    RequestThrottleLog,
    RotatingSessionToken,
    StudentProfile,
    User,
)
from .services import AttendanceValidationError, validate_and_mark_attendance
from .utils import (
    haversine_distance_m,
    is_point_in_circle,
    is_point_in_polygon,
    normalize_fingerprint,
    throttle_request,
)


# ─────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────

PASSWORD = "TestPass123!"

def make_admin(**kwargs):
    defaults = dict(
        matric_number="ADMIN/00/001",
        full_name="Dr. Test Lecturer",
        role="admin",
        password=make_password(PASSWORD),
    )
    defaults.update(kwargs)
    return User.objects.create(**defaults)


def make_student(matric="STU/20/001", level="300", **kwargs):
    u = User.objects.create(
        matric_number=matric,
        full_name="Test Student",
        role="student",
        password=make_password(PASSWORD),
        **kwargs,
    )
    StudentProfile.objects.create(user=u, level=level)
    return u


def make_course(lecturer, code="CSC 301", level="300"):
    return Course.objects.create(
        code=code, title="Test Course", lecturer=lecturer, level=level
    )


def make_session(course, admin, status="active", offset_minutes=0, duration_minutes=60):
    now = timezone.now()
    start = now - timedelta(minutes=30) + timedelta(minutes=offset_minutes)
    end   = start + timedelta(minutes=duration_minutes)
    return AttendanceSession.objects.create(
        title="Test Session",
        course=course,
        admin=admin,
        shape_type="circle",
        center_lat=7.4471,
        center_lng=3.8967,
        radius_meters=200,
        start_time=start,
        end_time=end,
        status=status,
    )


def make_token(session, expired=False):
    lifetime = -60 if expired else 45
    return RotatingSessionToken.objects.create(
        session=session,
        token="test-token-abc123",
        expires_at=timezone.now() + timedelta(seconds=lifetime),
        is_active=True,
    )


RAW_FP = "Mozilla/5.0|en-US|1920|1080|0|Linux"
FP_HASH = hashlib.sha256(RAW_FP.encode()).hexdigest()

BASE_PAYLOAD = {
    "matric_number":      "STU/20/001",
    "latitude":           7.4471,
    "longitude":          3.8967,
    "accuracy_meters":    15,
    "device_fingerprint": RAW_FP,
    "token":              "test-token-abc123",
    "is_mock_location":   False,
}


class FakeRequest:
    """Minimal request stand-in for service tests."""
    META = {"REMOTE_ADDR": "127.0.0.1"}


# ─────────────────────────────────────────────────────────────
# Utility tests
# ─────────────────────────────────────────────────────────────

class HaversineTests(TestCase):
    def test_same_point_is_zero(self):
        self.assertAlmostEqual(haversine_distance_m(0, 0, 0, 0), 0, places=2)

    def test_known_distance(self):
        # Lagos to Ibadan ≈ 128 km
        dist = haversine_distance_m(6.5244, 3.3792, 7.3775, 3.9470)
        self.assertAlmostEqual(dist / 1000, 128, delta=5)

    def test_in_circle_true(self):
        self.assertTrue(is_point_in_circle(7.4471, 3.8967, 7.4471, 3.8967, 100))

    def test_in_circle_false(self):
        # Move 1 km north — should be outside 100m radius
        self.assertFalse(is_point_in_circle(7.4561, 3.8967, 7.4471, 3.8967, 100))

    def test_polygon_inside(self):
        polygon = [
            {"lat": 7.44, "lng": 3.89},
            {"lat": 7.45, "lng": 3.89},
            {"lat": 7.45, "lng": 3.90},
            {"lat": 7.44, "lng": 3.90},
        ]
        self.assertTrue(is_point_in_polygon(7.445, 3.895, polygon))

    def test_polygon_outside(self):
        polygon = [
            {"lat": 7.44, "lng": 3.89},
            {"lat": 7.45, "lng": 3.89},
            {"lat": 7.45, "lng": 3.90},
            {"lat": 7.44, "lng": 3.90},
        ]
        self.assertFalse(is_point_in_polygon(7.50, 3.95, polygon))


class FingerprintTests(TestCase):
    def test_produces_64_char_hex(self):
        result = normalize_fingerprint(RAW_FP)
        self.assertEqual(len(result), 64)
        self.assertEqual(result, FP_HASH)

    def test_strips_whitespace(self):
        self.assertEqual(
            normalize_fingerprint("  abc  "),
            normalize_fingerprint("abc"),
        )

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            normalize_fingerprint("")
        with self.assertRaises(ValueError):
            normalize_fingerprint("   ")


# ─────────────────────────────────────────────────────────────
# Model tests
# ─────────────────────────────────────────────────────────────

class UserModelTests(TestCase):
    def test_matric_uppercased_on_save(self):
        u = make_admin(matric_number="admin/99/001")
        self.assertEqual(u.matric_number, "ADMIN/99/001")

    def test_is_admin_property(self):
        admin = make_admin()
        self.assertTrue(admin.is_admin)
        self.assertFalse(admin.is_student)

    def test_is_student_property(self):
        student = make_student()
        self.assertTrue(student.is_student)
        self.assertFalse(student.is_admin)


class CourseModelTests(TestCase):
    def setUp(self):
        self.admin = make_admin()

    def test_code_uppercased_on_save(self):
        c = Course.objects.create(
            code="csc 301", title="Test", lecturer=self.admin, level="300"
        )
        self.assertEqual(c.code, "CSC 301")

    def test_total_students_property(self):
        c = make_course(self.admin)
        s = make_student()
        CourseRegistration.objects.create(student=s, course=c)
        self.assertEqual(c.total_students, 1)


class AttendanceSessionModelTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.course = make_course(self.admin)

    def test_is_open_when_active_and_in_window(self):
        s = make_session(self.course, self.admin, status="active")
        self.assertTrue(s.is_open())

    def test_is_not_open_when_draft(self):
        s = make_session(self.course, self.admin, status="draft")
        self.assertFalse(s.is_open())

    def test_is_not_open_when_past_end(self):
        s = make_session(self.course, self.admin, status="active", offset_minutes=90, duration_minutes=1)
        # This session ended 29 minutes ago
        self.assertFalse(s.is_open())

    def test_attendance_state_present(self):
        s = make_session(self.course, self.admin)
        self.assertEqual(s.attendance_state_for_now(), "present")

    def test_attendance_state_late(self):
        now = timezone.now()
        s = AttendanceSession.objects.create(
            title="Late Test",
            course=self.course, admin=self.admin,
            shape_type="circle",
            center_lat=0, center_lng=0, radius_meters=100,
            start_time=now - timedelta(minutes=60),
            end_time=now + timedelta(minutes=30),
            late_after=now - timedelta(minutes=30),
            status="active",
        )
        self.assertEqual(s.attendance_state_for_now(), "late")

    def test_clean_rejects_end_before_start(self):
        from django.core.exceptions import ValidationError
        now = timezone.now()
        s = AttendanceSession(
            title="Bad",
            course=self.course, admin=self.admin,
            shape_type="circle",
            center_lat=0, center_lng=0, radius_meters=50,
            start_time=now + timedelta(hours=2),
            end_time=now + timedelta(hours=1),
            status="draft",
        )
        with self.assertRaises(ValidationError):
            s.full_clean()

    def test_clean_rejects_circle_without_radius(self):
        from django.core.exceptions import ValidationError
        now = timezone.now()
        s = AttendanceSession(
            title="No Radius",
            course=self.course, admin=self.admin,
            shape_type="circle",
            center_lat=7.0, center_lng=3.0, radius_meters=None,
            start_time=now, end_time=now + timedelta(hours=1),
            status="draft",
        )
        with self.assertRaises(ValidationError):
            s.full_clean()


class RotatingTokenTests(TestCase):
    def setUp(self):
        self.admin   = make_admin()
        self.course  = make_course(self.admin)
        self.session = make_session(self.course, self.admin)

    def test_is_valid_when_fresh(self):
        t = RotatingSessionToken.issue_for_session(self.session)
        self.assertTrue(t.is_valid())

    def test_is_not_valid_when_expired(self):
        t = make_token(self.session, expired=True)
        self.assertFalse(t.is_valid())

    def test_issue_deactivates_previous(self):
        t1 = RotatingSessionToken.issue_for_session(self.session)
        t2 = RotatingSessionToken.issue_for_session(self.session)
        t1.refresh_from_db()
        self.assertFalse(t1.is_active)
        self.assertTrue(t2.is_active)


# ─────────────────────────────────────────────────────────────
# Service tests
# ─────────────────────────────────────────────────────────────

class ValidateAndMarkAttendanceTests(TestCase):
    def setUp(self):
        self.admin   = make_admin()
        self.course  = make_course(self.admin, level="300")
        self.student = make_student(level="300")
        CourseRegistration.objects.create(student=self.student, course=self.course)
        self.session = make_session(self.course, self.admin, status="active")
        self.token   = make_token(self.session)
        self.req     = FakeRequest()

    def _mark(self, **overrides):
        payload = {**BASE_PAYLOAD, **overrides}
        return validate_and_mark_attendance(
            request=self.req,
            student=self.student,
            session=self.session,
            latitude=payload["latitude"],
            longitude=payload["longitude"],
            accuracy_meters=payload["accuracy_meters"],
            raw_device_fingerprint=payload["device_fingerprint"],
            submitted_token=payload["token"],
            submitted_matric_number=payload["matric_number"],
            is_mock_location=payload["is_mock_location"],
        )

    def test_successful_mark(self):
        record = self._mark()
        self.assertEqual(record.status, "present")
        self.assertEqual(record.student, self.student)

    def test_duplicate_rejected(self):
        self._mark()
        with self.assertRaises(AttendanceValidationError) as ctx:
            self._mark()
        self.assertIn("already been marked", str(ctx.exception))

    def test_wrong_matric_rejected(self):
        with self.assertRaises(AttendanceValidationError) as ctx:
            self._mark(matric_number="STU/99/999")
        self.assertIn("matric number", str(ctx.exception).lower())

    def test_expired_token_rejected(self):
        self.token.expires_at = timezone.now() - timedelta(minutes=1)
        self.token.save()
        with self.assertRaises(AttendanceValidationError) as ctx:
            self._mark()
        self.assertIn("token", str(ctx.exception).lower())

    def test_outside_geofence_rejected(self):
        # Move student 10km away
        with self.assertRaises(AttendanceValidationError) as ctx:
            self._mark(latitude=7.53, longitude=4.0)
        self.assertIn("area", str(ctx.exception).lower())
        self.assertEqual(AttendanceAttemptLog.objects.filter(reason="Outside geofence").count(), 1)

    def test_mock_location_rejected(self):
        with self.assertRaises(AttendanceValidationError) as ctx:
            self._mark(is_mock_location=True)
        self.assertIn("mock", str(ctx.exception).lower())

    def test_poor_accuracy_rejected(self):
        with self.assertRaises(AttendanceValidationError) as ctx:
            self._mark(accuracy_meters=200)
        self.assertIn("accuracy", str(ctx.exception).lower())

    def test_wrong_level_rejected(self):
        self.student.student_profile.level = "200"
        self.student.student_profile.save()
        with self.assertRaises(AttendanceValidationError) as ctx:
            self._mark()
        self.assertIn("level", str(ctx.exception).lower())

    def test_not_enrolled_rejected(self):
        CourseRegistration.objects.filter(student=self.student).delete()
        with self.assertRaises(AttendanceValidationError) as ctx:
            self._mark()
        self.assertIn("registered", str(ctx.exception).lower())

    def test_device_binding_created_on_first_mark(self):
        self._mark()
        self.assertTrue(DeviceBinding.objects.filter(student=self.student).exists())

    def test_different_device_rejected_after_binding(self):
        self._mark()
        # Re-issue a token (first one was consumed)
        RotatingSessionToken.objects.create(
            session=self.session, token="second-token",
            expires_at=timezone.now() + timedelta(seconds=45), is_active=True,
        )
        AttendanceRecord.objects.filter(student=self.student).delete()  # allow second attempt
        with self.assertRaises(AttendanceValidationError) as ctx:
            self._mark(device_fingerprint="different|device|fingerprint|xyz|000|Win")
        self.assertIn("device", str(ctx.exception).lower())

    def test_session_not_open_rejected(self):
        self.session.status = "draft"
        self.session.save()
        with self.assertRaises(AttendanceValidationError) as ctx:
            self._mark()
        self.assertIn("not currently open", str(ctx.exception))

    def test_token_consumed_after_successful_mark(self):
        self._mark()
        self.token.refresh_from_db()
        self.assertFalse(self.token.is_active)


# ─────────────────────────────────────────────────────────────
# View tests
# ─────────────────────────────────────────────────────────────

class LoginViewTests(TestCase):
    def setUp(self):
        self.client  = Client()
        self.admin   = make_admin()
        self.student = make_student()

    def test_admin_redirected_to_dashboard(self):
        resp = self.client.post(reverse("login"), {
            "username": self.admin.matric_number,
            "password": PASSWORD,
        })
        self.assertRedirects(resp, reverse("dashboard"))

    def test_student_redirected_to_mark_home(self):
        resp = self.client.post(reverse("login"), {
            "username": self.student.matric_number,
            "password": PASSWORD,
        })
        self.assertRedirects(resp, reverse("student-mark-home"))

    def test_bad_credentials_stay_on_login(self):
        resp = self.client.post(reverse("login"), {
            "username": "FAKE/00/000",
            "password": "wrongpass",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Incorrect matric number or password")


class DashboardViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin  = make_admin()
        self.client.force_login(self.admin)

    def test_dashboard_loads(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)

    def test_student_cannot_access_dashboard(self):
        student = make_student()
        self.client.force_login(student)
        resp = self.client.get(reverse("dashboard"))
        self.assertNotEqual(resp.status_code, 200)


class CreateSessionViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = make_admin()
        make_course(self.admin)
        self.client.force_login(self.admin)

    def test_create_session_page_renders_session_fields(self):
        resp = self.client.get(reverse("create-session"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Create Attendance Session")
        self.assertContains(resp, 'name="title"')
        self.assertContains(resp, 'name="course"')
        self.assertContains(resp, 'name="shape_type"')


class UploadStudentsViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin  = make_admin()
        self.course = make_course(self.admin)
        self.client.force_login(self.admin)

    def _upload(self, content):
        from io import BytesIO
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("students.csv", content.encode(), content_type="text/csv")
        return self.client.post(reverse("upload-students"), {"file": f})

    def test_valid_csv_creates_student(self):
        csv = f"full_name,matric_number,level,course_code\nJane Doe,CSC/20/001,300,{self.course.code}"
        resp = self._upload(csv)
        self.assertRedirects(resp, reverse("dashboard"))
        self.assertTrue(User.objects.filter(matric_number="CSC/20/001").exists())

    def test_missing_column_shows_error(self):
        csv = "full_name,matric_number,level\nJane,CSC/20/001,300"
        resp = self._upload(csv)
        self.assertRedirects(resp, reverse("upload-students"))

    def test_unknown_course_code_skipped(self):
        csv = "full_name,matric_number,level,course_code\nJane,CSC/20/001,300,XXX999"
        self._upload(csv)
        self.assertFalse(User.objects.filter(matric_number="CSC/20/001").exists())

    def test_header_order_and_style_are_flexible(self):
        csv = (
            f"Course Code,Level,Full Name,Matric No\n"
            f"{self.course.code},300,Jane Doe,CSC/20/777"
        )
        resp = self._upload(csv)
        self.assertRedirects(resp, reverse("dashboard"))
        self.assertTrue(User.objects.filter(matric_number="CSC/20/777").exists())

    def test_semicolon_delimited_csv_is_supported(self):
        csv = (
            f"full_name;matric_number;level;course_code\n"
            f"Jane Doe;CSC/20/888;300;{self.course.code}"
        )
        resp = self._upload(csv)
        self.assertRedirects(resp, reverse("dashboard"))
        self.assertTrue(User.objects.filter(matric_number="CSC/20/888").exists())


class SessionTokenAPITests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = make_admin()
        self.course = make_course(self.admin)
        self.student = make_student(level="300")
        CourseRegistration.objects.create(student=self.student, course=self.course)
        self.session = make_session(self.course, self.admin, status="active")

    def test_student_can_fetch_token_for_enrolled_course(self):
        self.client.force_login(self.student)
        resp = self.client.get(reverse("session-token-api", kwargs={"session_id": self.session.id}))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("token", data)

    def test_unenrolled_student_cannot_fetch_token(self):
        outsider = make_student(matric="STU/20/999", level="300")
        self.client.force_login(outsider)
        resp = self.client.get(reverse("session-token-api", kwargs={"session_id": self.session.id}))
        self.assertEqual(resp.status_code, 403)

    def test_unenrolled_student_redirected_from_mark_page(self):
        outsider = make_student(matric="STU/20/998", level="300")
        self.client.force_login(outsider)
        resp = self.client.get(reverse("student-mark-page", kwargs={"session_id": self.session.id}))
        self.assertRedirects(resp, reverse("student-mark-home"))


class MarkAttendanceAPITests(TestCase):
    def setUp(self):
        self.client  = Client()
        self.admin   = make_admin()
        self.course  = make_course(self.admin, level="300")
        self.student = make_student(level="300")
        CourseRegistration.objects.create(student=self.student, course=self.course)
        self.session = make_session(self.course, self.admin, status="active")
        make_token(self.session)
        self.client.force_login(self.student)
        self.url = reverse("mark-attendance-api", kwargs={"session_id": self.session.id})

    def _post(self, data=None):
        payload = {**BASE_PAYLOAD, **(data or {})}
        return self.client.post(
            self.url,
            json.dumps(payload),
            content_type="application/json",
        )

    def test_successful_mark_returns_200(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])

    def test_outside_geofence_returns_400(self):
        resp = self._post({"latitude": 51.5074, "longitude": -0.1278})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])

    def test_invalid_json_returns_400(self):
        resp = self.client.post(self.url, "not-json", content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_admin_cannot_use_mark_api(self):
        self.client.force_login(self.admin)
        resp = self._post()
        self.assertNotEqual(resp.status_code, 200)

    def test_toggle_session_requires_post(self):
        self.client.force_login(self.admin)
        url = reverse("toggle-session", kwargs={"session_id": self.session.id})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 405)   # Method Not Allowed

    def test_student_mark_home_shows_only_enrolled_sessions(self):
        # Create a session for a different course the student is NOT enrolled in
        other_course = make_course(self.admin, code="MTH 101", level="100")
        other_session = make_session(other_course, self.admin, status="active")
        self.client.force_login(self.student)
        resp = self.client.get(reverse("student-mark-home"))
        self.assertEqual(resp.status_code, 200)
        context_sessions = list(resp.context["active_sessions"])
        session_ids = [s.id for s in context_sessions]
        self.assertIn(self.session.id, session_ids)
        self.assertNotIn(other_session.id, session_ids)