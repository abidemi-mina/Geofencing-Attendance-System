import logging
import secrets
from datetime import timedelta

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Custom user manager
# ─────────────────────────────────────────────────────────────

class UserManager(BaseUserManager):
    def create_user(self, matric_number, full_name, password=None, **extra_fields):
        if not matric_number:
            raise ValueError("Matric number is required.")
        if not full_name:
            raise ValueError("Full name is required.")
        matric_number = matric_number.strip().upper()
        user = self.model(matric_number=matric_number, full_name=full_name.strip(), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, matric_number, full_name, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", "admin")
        if not extra_fields.get("is_staff"):
            raise ValueError("Superuser must have is_staff=True.")
        if not extra_fields.get("is_superuser"):
            raise ValueError("Superuser must have is_superuser=True.")
        return self.create_user(matric_number, full_name, password, **extra_fields)


# ─────────────────────────────────────────────────────────────
# User
# ─────────────────────────────────────────────────────────────

class User(AbstractUser):
    ROLE_CHOICES = (
        ("admin", "Admin / Lecturer"),
        ("student", "Student"),
    )

    # Remove unused AbstractUser fields
    username = None
    first_name = None
    last_name = None

    matric_number = models.CharField(max_length=30, unique=True, db_index=True)
    full_name = models.CharField(max_length=150)
    email = models.EmailField(blank=True, null=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="student", db_index=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    USERNAME_FIELD = "matric_number"
    REQUIRED_FIELDS = ["full_name"]

    # Resolve reverse accessor clashes with the default auth models
    groups = models.ManyToManyField(
        "auth.Group",
        related_name="attendance_users",
        blank=True,
        verbose_name="groups",
    )
    user_permissions = models.ManyToManyField(
        "auth.Permission",
        related_name="attendance_user_permissions",
        blank=True,
        verbose_name="user permissions",
    )

    objects = UserManager()

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["full_name"]

    def save(self, *args, **kwargs):
        if self.matric_number:
            self.matric_number = self.matric_number.strip().upper()
        if self.full_name:
            self.full_name = self.full_name.strip()
        super().save(*args, **kwargs)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_student(self):
        return self.role == "student"

    def __str__(self):
        return f"{self.full_name} ({self.matric_number})"


# ─────────────────────────────────────────────────────────────
# Student profile (level, etc.)
# ─────────────────────────────────────────────────────────────

class StudentProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="student_profile"
    )
    level = models.CharField(max_length=10, db_index=True)

    class Meta:
        verbose_name = "Student Profile"

    def __str__(self):
        return f"{self.user.full_name} — Level {self.level}"


# ─────────────────────────────────────────────────────────────
# Course
# ─────────────────────────────────────────────────────────────

class Course(models.Model):
    code = models.CharField(max_length=20, unique=True, db_index=True)
    title = models.CharField(max_length=150)
    lecturer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="courses_taught",
        limit_choices_to={"role": "admin"},
    )
    level = models.CharField(max_length=10, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]
        verbose_name = "Course"

    def save(self, *args, **kwargs):
        if self.code:
            self.code = self.code.strip().upper()
        if self.title:
            self.title = self.title.strip()
        super().save(*args, **kwargs)

    @property
    def total_students(self):
        return self.registrations.count()

    @property
    def total_sessions(self):
        return self.sessions.count()

    def __str__(self):
        return f"{self.code} — {self.title}"


# ─────────────────────────────────────────────────────────────
# Course registration
# ─────────────────────────────────────────────────────────────

class CourseRegistration(models.Model):
    student = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="course_registrations"
    )
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="registrations"
    )
    registered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("student", "course")
        indexes = [models.Index(fields=["student", "course"])]
        ordering = ["course__code"]
        verbose_name = "Course Registration"

    def __str__(self):
        return f"{self.student.matric_number} → {self.course.code}"


# ─────────────────────────────────────────────────────────────
# Attendance session
# ─────────────────────────────────────────────────────────────

class AttendanceSession(models.Model):
    SHAPE_CHOICES = (
        ("circle", "Circle"),
        ("polygon", "Polygon"),
    )
    STATUS_CHOICES = (
        ("draft", "Draft"),
        ("active", "Active"),
        ("closed", "Closed"),
    )

    title = models.CharField(max_length=150)
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="sessions"
    )
    # "admin" means the lecturer who created it — kept for backward compat
    admin = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="created_sessions"
    )
    shape_type = models.CharField(
        max_length=20, choices=SHAPE_CHOICES, default="circle"
    )
    center_lat = models.FloatField(blank=True, null=True)
    center_lng = models.FloatField(blank=True, null=True)
    radius_meters = models.FloatField(blank=True, null=True)
    polygon_points = models.JSONField(blank=True, null=True)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    late_after = models.DateTimeField(blank=True, null=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="draft", db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Attendance Session"

    # ── Validation ─────────────────────────────────────────

    def clean(self):
        errors = {}

        if self.start_time and self.end_time:
            if self.start_time >= self.end_time:
                errors["end_time"] = "End time must be after start time."
            if self.late_after:
                if not (self.start_time <= self.late_after <= self.end_time):
                    errors["late_after"] = (
                        "Late-after time must fall within the session window."
                    )

        if self.shape_type == "circle":
            if self.center_lat is None or self.center_lng is None:
                errors["center_lat"] = "Circle geofence requires a center coordinate."
            if self.radius_meters is None:
                errors["radius_meters"] = "Circle geofence requires a radius."
            elif self.radius_meters <= 0:
                errors["radius_meters"] = "Radius must be greater than zero."
            # Sanity: latitude / longitude range
            if self.center_lat is not None and not (-90 <= self.center_lat <= 90):
                errors["center_lat"] = "Latitude must be between -90 and 90."
            if self.center_lng is not None and not (-180 <= self.center_lng <= 180):
                errors["center_lng"] = "Longitude must be between -180 and 180."

        elif self.shape_type == "polygon":
            if not self.polygon_points or len(self.polygon_points) < 3:
                errors["polygon_points"] = (
                    "Polygon geofence requires at least 3 coordinate points."
                )
            else:
                for pt in self.polygon_points:
                    if not isinstance(pt, dict) or "lat" not in pt or "lng" not in pt:
                        errors["polygon_points"] = (
                            "Each polygon point must be a dict with 'lat' and 'lng'."
                        )
                        break

        if errors:
            raise ValidationError(errors)

    # ── State helpers ───────────────────────────────────────

    def is_open(self):
        """True only when status==active AND right now is within the window."""
        now = timezone.now()
        return (
            self.status == "active"
            and self.start_time <= now <= self.end_time
        )

    def attendance_state_for_now(self):
        """Return the granular state: not_started | present | late | closed."""
        now = timezone.now()
        if now < self.start_time:
            return "not_started"
        if now > self.end_time:
            return "closed"
        if self.late_after and now > self.late_after:
            return "late"
        return "present"

    @property
    def marked_count(self):
        return self.records.count()

    @property
    def present_count(self):
        return self.records.filter(status="present").count()

    @property
    def late_count(self):
        return self.records.filter(status="late").count()

    def __str__(self):
        return f"{self.title} ({self.course.code})"


# ─────────────────────────────────────────────────────────────
# Rotating session token
# ─────────────────────────────────────────────────────────────

class RotatingSessionToken(models.Model):
    session = models.ForeignKey(
        AttendanceSession,
        on_delete=models.CASCADE,
        related_name="rotating_tokens",
    )
    token = models.CharField(max_length=128, unique=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Rotating Session Token"

    @classmethod
    def issue_for_session(cls, session, lifetime_seconds=45):
        """
        Deactivate any live tokens for this session, then issue a fresh one.
        Uses select_for_update to prevent race conditions.
        """
        now = timezone.now()
        # Deactivate all unexpired tokens for this session atomically
        cls.objects.filter(
            session=session, is_active=True, expires_at__gt=now
        ).update(is_active=False)

        return cls.objects.create(
            session=session,
            token=secrets.token_urlsafe(32),   # 32 bytes → 43-char URL-safe string
            expires_at=now + timedelta(seconds=lifetime_seconds),
            is_active=True,
        )

    def is_valid(self):
        return self.is_active and timezone.now() <= self.expires_at

    def __str__(self):
        return f"Token for {self.session} (active={self.is_active})"


# ─────────────────────────────────────────────────────────────
# Device binding  (one device per student account)
# ─────────────────────────────────────────────────────────────

class DeviceBinding(models.Model):
    student = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="device_binding"
    )
    # SHA-256 hex digest of the raw fingerprint string
    fingerprint = models.CharField(max_length=64, unique=True, db_index=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Device Binding"

    def touch(self):
        """Update last_seen_at without touching other fields."""
        self.save(update_fields=["last_seen_at"])

    def __str__(self):
        return f"{self.student.matric_number} device"


# ─────────────────────────────────────────────────────────────
# Attendance record  (the authoritative mark)
# ─────────────────────────────────────────────────────────────

class AttendanceRecord(models.Model):
    STATUS_CHOICES = (
        ("present", "Present"),
        ("late", "Late"),
    )

    student = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="attendance_records"
    )
    session = models.ForeignKey(
        AttendanceSession, on_delete=models.CASCADE, related_name="records"
    )
    marked_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    latitude = models.FloatField()
    longitude = models.FloatField()
    accuracy_meters = models.FloatField(blank=True, null=True)
    device_fingerprint = models.CharField(max_length=64, db_index=True)  # SHA-256 hex
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    is_mock_location = models.BooleanField(default=False)
    validation_notes = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("student", "session")
        ordering = ["-marked_at"]
        verbose_name = "Attendance Record"

    def __str__(self):
        return f"{self.student.matric_number} — {self.session.title} [{self.status}]"


# ─────────────────────────────────────────────────────────────
# Audit: failed / rejected attempts
# ─────────────────────────────────────────────────────────────

class AttendanceAttemptLog(models.Model):
    student = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="attempt_logs",
    )
    session = models.ForeignKey(
        AttendanceSession, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="attempt_logs",
    )
    attempted_at = models.DateTimeField(auto_now_add=True)
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    device_fingerprint = models.CharField(max_length=64, blank=True, null=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    reason = models.CharField(max_length=255, db_index=True)
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-attempted_at"]
        verbose_name = "Attempt Log"

    def __str__(self):
        matric = self.student.matric_number if self.student else "unknown"
        return f"[{self.reason}] {matric} @ {self.attempted_at:%Y-%m-%d %H:%M}"


# ─────────────────────────────────────────────────────────────
# Throttle log  (per-IP rate limiting)
# ─────────────────────────────────────────────────────────────

class RequestThrottleLog(models.Model):
    ip_address = models.GenericIPAddressField(db_index=True)
    path = models.CharField(max_length=255, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Throttle Log"

    def __str__(self):
        return f"{self.ip_address} → {self.path} @ {self.created_at:%H:%M:%S}"